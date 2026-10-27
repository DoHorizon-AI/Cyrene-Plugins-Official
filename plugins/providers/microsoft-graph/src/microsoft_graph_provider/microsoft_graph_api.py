"""
┌────────────────────────────────────────────────────────────────────┐
│  Microsoft Graph v1.0 fixed-route transport                         │
│  固定 Microsoft Graph v1.0 路由、边界与委托身份校验                   │
└────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, urlencode, urlsplit

GRAPH_HOST = "graph.microsoft.com"
GRAPH_ROOT = "/v1.0"
MAX_RESPONSE_BYTES = 1_048_576
MAX_CURSOR_CHARS = 12_000
MAX_PAGE_SIZE = 100
MAX_RECONCILE_PAGES = 5
MAX_RECONCILE_TASKS = MAX_PAGE_SIZE * MAX_RECONCILE_PAGES
MAX_CREATE_POSSIBLE_MATCHES = 10
_PLANNER_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_SITE_ID = re.compile(r"[A-Za-z0-9.-]{1,255},[0-9a-fA-F-]{36},[0-9a-fA-F-]{36}\Z")
_SP_DELTA_SELECT = (
    "id,name,eTag,createdDateTime,lastModifiedDateTime,contentType,deleted,parentReference"
)


class GraphApiFailure(RuntimeError):
    """Sanitized Graph failure with retry and ambiguous-write classification."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
        uncertain: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.uncertain = uncertain


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One bounded HTTP response from the fixed Graph host."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    """Transport seam used by the HTTPS implementation and synthetic tests."""

    def request(
        self,
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        """Send one request to an already fixed host and relative path."""


class _HttpsTransport:
    """Use certificate-verified HTTPS without following redirects."""

    def request(
        self,
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        if host != GRAPH_HOST:
            raise GraphApiFailure("GRAPH_HOST_REJECTED")
        connection = http.client.HTTPSConnection(host, port=443, timeout=timeout_seconds)
        try:
            connection.request(method, path, body=body, headers=dict(headers))
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            return HttpResponse(
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                response_body,
            )
        finally:
            connection.close()


class MicrosoftGraphApi:
    """Create one verified, profile-bound Graph session per provider invocation."""

    def __init__(
        self,
        *,
        cursor_key: bytes,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        transport: Transport | Callable[..., HttpResponse] | None = None,
    ) -> None:
        if len(cursor_key) != 32:
            raise ValueError("cursor signing key must contain 32 bytes")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds is outside the supported bound")
        if not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("max_response_bytes is outside the supported bound")
        self._cursor_key = cursor_key
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._transport = transport or _HttpsTransport()

    def open_session(
        self,
        access_token: str,
        external_subject: str,
        *,
        binding_id: str,
        profile_ref: str,
    ) -> MicrosoftGraphSession:
        """Verify `/me` with this token before exposing any resource operation."""

        token = _bearer_token(access_token)
        tenant_id, object_id = _external_subject(external_subject)
        if not binding_id or len(binding_id) > 256 or not _UUID.fullmatch(profile_ref):
            raise GraphApiFailure("PROFILE_BINDING_INVALID")
        response = self._request(
            "GET",
            f"{GRAPH_ROOT}/me?{urlencode({'$select': 'id'})}",
            token,
        )
        me = _json_object(response)
        returned_id = me.get("id")
        try:
            canonical_id = str(uuid.UUID(returned_id)).lower()
        except (ValueError, TypeError, AttributeError):
            raise GraphApiFailure("GRAPH_IDENTITY_INVALID") from None
        if canonical_id != object_id:
            raise GraphApiFailure("GRAPH_ACTOR_MISMATCH")
        return MicrosoftGraphSession(
            self,
            token,
            tenant_id=tenant_id,
            object_id=object_id,
            binding_id=binding_id,
            profile_ref=profile_ref,
        )

    def _request(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        write: bool = False,
    ) -> HttpResponse:
        """Send one request to graph.microsoft.com/v1.0 without redirect following."""

        if not path.startswith(f"{GRAPH_ROOT}/") or "\\" in path or "\r" in path or "\n" in path:
            raise GraphApiFailure("GRAPH_ROUTE_REJECTED")
        token = _bearer_token(access_token)
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Cyrene-Microsoft-Graph-Provider/0.1",
        }
        payload: bytes | None = None
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            try:
                payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            except (TypeError, ValueError):
                raise GraphApiFailure("INVALID_REQUEST_BODY") from None
        for key, value in (headers or {}).items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise GraphApiFailure("INVALID_REQUEST_HEADER")
            if key.lower() in {"host", "authorization", "connection", "content-length"}:
                raise GraphApiFailure("INVALID_REQUEST_HEADER")
            if any(char in key + value for char in "\r\n\x00"):
                raise GraphApiFailure("INVALID_REQUEST_HEADER")
            request_headers[key] = value
        try:
            transport = self._transport
            if hasattr(transport, "request"):
                response = transport.request(
                    method,
                    GRAPH_HOST,
                    path,
                    request_headers,
                    payload,
                    self._timeout_seconds,
                )
            else:
                response = transport(
                    method,
                    GRAPH_HOST,
                    path,
                    request_headers,
                    payload,
                    self._timeout_seconds,
                )
        except (OSError, TimeoutError, http.client.HTTPException):
            raise GraphApiFailure(
                "GRAPH_OUTCOME_UNKNOWN" if write else "GRAPH_TRANSPORT_UNAVAILABLE",
                retryable=not write,
                uncertain=write,
            ) from None
        if not isinstance(response, HttpResponse):
            try:
                response = HttpResponse(
                    int(response.status),
                    {str(key).lower(): str(value) for key, value in response.headers.items()},
                    bytes(response.body),
                )
            except (AttributeError, TypeError, ValueError):
                raise GraphApiFailure("INVALID_TRANSPORT_RESPONSE", uncertain=write) from None
        if len(response.body) > self._max_response_bytes:
            raise GraphApiFailure("GRAPH_RESPONSE_TOO_LARGE", uncertain=write)
        if 300 <= response.status < 400:
            raise GraphApiFailure("GRAPH_REDIRECT_REJECTED", uncertain=write)
        if not 200 <= response.status < 300:
            raise _status_failure(response, write=write)
        return HttpResponse(
            response.status,
            {str(key).lower(): str(value) for key, value in response.headers.items()},
            response.body,
        )


class MicrosoftGraphSession:
    """A short-lived in-memory session bound to one verified delegated user."""

    def __init__(
        self,
        api: MicrosoftGraphApi,
        access_token: str,
        *,
        tenant_id: str,
        object_id: str,
        binding_id: str,
        profile_ref: str,
    ) -> None:
        self._api = api
        self._token = access_token
        self.tenant_id = tenant_id
        self.object_id = object_id
        self._binding_id = binding_id
        self._profile_ref = profile_ref

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        """Read one exact Planner plan and return only small metadata fields."""

        _planner_id(plan_id, "plan_id")
        path = f"{GRAPH_ROOT}/planner/plans/{quote(plan_id, safe='')}"
        raw = _json_object(self._api._request("GET", path, self._token))
        if raw.get("id") != plan_id:
            raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
        return _plan_projection(raw, expected_id=plan_id)

    def list_tasks(
        self,
        plan_id: str,
        *,
        page_size: int = 50,
        page_cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read one bounded page of tasks in an exact plan."""

        _planner_id(plan_id, "plan_id")
        _page_size(page_size)
        path = self._plan_tasks_path(plan_id)
        continuation = self._decode_cursor(
            page_cursor,
            kind="planner_tasks",
            target={"plan_id": plan_id},
            page_size=page_size,
        )
        query: dict[str, str | int] = {"$top": page_size}
        if continuation is not None:
            query["$skiptoken"] = continuation
        response = self._api._request("GET", f"{path}?{urlencode(query)}", self._token)
        value = _json_object(response)
        rows = _collection(value, "value", page_size=page_size)
        tasks = []
        for row in rows:
            if row.get("planId") != plan_id:
                raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
            tasks.append(_task_projection(row, expected_plan_id=plan_id))
        next_link = value.get("@odata.nextLink")
        next_cursor = None
        if next_link is not None:
            raw_token = _continuation_token(
                next_link,
                expected_path=path,
                allowed_keys={"$skiptoken", "skiptoken", "$top"},
                token_keys={"$skiptoken", "skiptoken"},
                expected_top=page_size,
            )
            next_cursor = self._encode_cursor(
                kind="planner_tasks",
                target={"plan_id": plan_id},
                page_size=page_size,
                token=raw_token,
            )
        elif "@odata.deltaLink" in value:
            raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
        return {
            "tasks": tasks,
            "page_cursor": page_cursor,
            "next_cursor": next_cursor,
            "complete": next_cursor is None,
            "truncated": False,
        }

    def get_task(self, plan_id: str, task_id: str) -> dict[str, Any]:
        """Read one task and prove that it belongs to the requested plan."""

        raw = self._get_task_raw(plan_id, task_id)
        return _task_projection(raw, expected_plan_id=plan_id)

    def create_task(
        self,
        plan_id: str,
        bucket_id: str,
        title: str,
        *,
        due_date_time: str | None = None,
    ) -> dict[str, Any]:
        """Create one task, reconciling ambiguous outcomes through bounded readback."""

        _planner_id(plan_id, "plan_id")
        _planner_id(bucket_id, "bucket_id")
        safe_title = _task_title(title)
        body: dict[str, Any] = {"planId": plan_id, "bucketId": bucket_id, "title": safe_title}
        if due_date_time is not None:
            body["dueDateTime"] = _utc_datetime(due_date_time, "due_date_time")
        self._verify_plan_and_bucket(plan_id, bucket_id)
        started_at = time.time()
        try:
            response = self._api._request(
                "POST",
                f"{GRAPH_ROOT}/planner/tasks",
                self._token,
                body=body,
                write=True,
            )
            if response.status != 201:
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True)
            created = _json_object(response, uncertain=True)
            if (
                created.get("planId") != plan_id
                or created.get("bucketId") != bucket_id
                or created.get("title") != safe_title
            ):
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True)
            try:
                projection = _task_projection(created, expected_plan_id=plan_id)
            except GraphApiFailure:
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True) from None
            return {
                "status": "CONFIRMED",
                "task": projection,
                "reconciliation": "not_required",
            }
        except GraphApiFailure as error:
            if not error.uncertain:
                raise
            return self._reconcile_create(
                plan_id=plan_id,
                bucket_id=bucket_id,
                title=safe_title,
                started_at=started_at,
            )

    def update_task(
        self,
        plan_id: str,
        task_id: str,
        if_match: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply a narrow task patch with the caller's current ETag and verify by readback."""

        safe_changes = _task_changes(changes)
        safe_etag = _etag(if_match)
        before = self._get_task_raw(plan_id, task_id)
        current_etag = before.get("@odata.etag")
        if not isinstance(current_etag, str) or current_etag != safe_etag:
            raise GraphApiFailure("CONCURRENCY_CONFLICT")
        graph_changes = _graph_task_changes(safe_changes)
        try:
            self._api._request(
                "PATCH",
                f"{GRAPH_ROOT}/planner/tasks/{quote(task_id, safe='')}",
                self._token,
                body=graph_changes,
                headers={"If-Match": safe_etag, "Prefer": "return=representation"},
                write=True,
            )
        except GraphApiFailure as error:
            if error.code == "CONCURRENCY_CONFLICT" or not error.uncertain:
                raise
            try:
                after = self._get_task_raw(plan_id, task_id)
            except GraphApiFailure:
                return _unknown_update(plan_id, task_id)
            if _changes_match(after, graph_changes):
                return {
                    "status": "RECONCILED",
                    "task": _task_projection(after, expected_plan_id=plan_id),
                    "reconciliation": "readback_match",
                }
            return _unknown_update(plan_id, task_id)
        try:
            after = self._get_task_raw(plan_id, task_id)
        except GraphApiFailure:
            return _unknown_update(plan_id, task_id)
        if not _changes_match(after, graph_changes):
            return _unknown_update(plan_id, task_id)
        return {
            "status": "CONFIRMED",
            "task": _task_projection(after, expected_plan_id=plan_id),
            "reconciliation": "readback_match",
        }

    def get_site(self, site_id: str) -> dict[str, Any]:
        """Read minimal metadata for one exact SharePoint site."""

        _site_id(site_id)
        path = f"{GRAPH_ROOT}/sites/{quote(site_id, safe=',')}"
        query = urlencode({"$select": "id,displayName,siteCollection"})
        raw = _json_object(self._api._request("GET", f"{path}?{query}", self._token))
        if raw.get("id") != site_id:
            raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
        return {
            "site_id": site_id,
            "display_name": _bounded_text(raw.get("displayName"), 255),
        }

    def get_list(self, site_id: str, list_id: str) -> dict[str, Any]:
        """Read metadata for one list inside the exact selected site."""

        _site_id(site_id)
        _uuid_id(list_id, "list_id")
        path = self._sharepoint_list_path(site_id, list_id)
        query = urlencode(
            {"$select": "id,name,displayName,createdDateTime,lastModifiedDateTime,list"}
        )
        raw = _json_object(self._api._request("GET", f"{path}?{query}", self._token))
        if raw.get("id") != list_id:
            raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
        list_info = raw.get("list")
        template = list_info.get("template") if isinstance(list_info, Mapping) else None
        return {
            "site_id": site_id,
            "list_id": list_id,
            "name": _bounded_text(raw.get("name"), 255),
            "display_name": _bounded_text(raw.get("displayName"), 255),
            "created_date_time": _bounded_text(raw.get("createdDateTime"), 64),
            "last_modified_date_time": _bounded_text(raw.get("lastModifiedDateTime"), 64),
            "template": _bounded_text(template, 80),
        }

    def list_items_delta(
        self,
        site_id: str,
        list_id: str,
        *,
        page_size: int = 50,
        page_cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read one metadata-only SharePoint delta page for a selected site/list."""

        _site_id(site_id)
        _uuid_id(list_id, "list_id")
        _page_size(page_size)
        path = self._sharepoint_delta_path(site_id, list_id)
        continuation = self._decode_cursor(
            page_cursor,
            kind="sharepoint_delta",
            target={"site_id": site_id, "list_id": list_id},
            page_size=page_size,
        )
        query: dict[str, str | int] = {"$select": _SP_DELTA_SELECT, "$top": page_size}
        if continuation is not None:
            query["token"] = continuation
        value = _json_object(self._api._request("GET", f"{path}?{urlencode(query)}", self._token))
        rows = _collection(value, "value", page_size=page_size)
        items = [_sharepoint_item_projection(row) for row in rows]
        next_link = value.get("@odata.nextLink")
        delta_link = value.get("@odata.deltaLink")
        if (next_link is None) == (delta_link is None):
            raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
        link = next_link if next_link is not None else delta_link
        raw_token = _continuation_token(
            link,
            expected_path=path,
            allowed_keys={"token", "$top", "$select"},
            token_keys={"token"},
            expected_top=page_size,
            expected_select=_SP_DELTA_SELECT,
        )
        cursor = self._encode_cursor(
            kind="sharepoint_delta",
            target={"site_id": site_id, "list_id": list_id},
            page_size=page_size,
            token=raw_token,
        )
        complete = delta_link is not None
        return {
            "items": items,
            "site_id": site_id,
            "list_id": list_id,
            "page_cursor": page_cursor,
            "next_cursor": cursor,
            "complete": complete,
            "truncated": False,
        }

    def _get_task_raw(self, plan_id: str, task_id: str) -> dict[str, Any]:
        """Read one raw task for target checks and ETag reconciliation."""

        _planner_id(plan_id, "plan_id")
        _planner_id(task_id, "task_id")
        path = f"{GRAPH_ROOT}/planner/tasks/{quote(task_id, safe='')}"
        raw = _json_object(self._api._request("GET", path, self._token))
        if raw.get("id") != task_id or raw.get("planId") != plan_id:
            raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
        return raw

    def _verify_plan_and_bucket(self, plan_id: str, bucket_id: str) -> None:
        """Check that the supplied bucket belongs to the selected plan before creation."""

        self.get_plan(plan_id)
        path = f"{GRAPH_ROOT}/planner/buckets/{quote(bucket_id, safe='')}"
        raw = _json_object(self._api._request("GET", path, self._token))
        if raw.get("id") != bucket_id or raw.get("planId") != plan_id:
            raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")

    def _reconcile_create(
        self,
        *,
        plan_id: str,
        bucket_id: str,
        title: str,
        started_at: float,
    ) -> dict[str, Any]:
        """Return possible readback matches without claiming an ambiguous create succeeded."""

        cursor: str | None = None
        matches: dict[str, dict[str, Any]] = {}
        count = 0
        try:
            for _ in range(MAX_RECONCILE_PAGES):
                rows, cursor, complete = self._list_task_rows(
                    plan_id,
                    page_size=MAX_PAGE_SIZE,
                    page_cursor=cursor,
                )
                count += len(rows)
                for row in rows:
                    creator = row.get("createdBy")
                    creator_user = creator.get("user") if isinstance(creator, Mapping) else None
                    created_at = _parse_timestamp(row.get("createdDateTime"))
                    if (
                        row.get("planId") == plan_id
                        and row.get("bucketId") == bucket_id
                        and row.get("title") == title
                        and isinstance(creator_user, Mapping)
                        and creator_user.get("id") == self.object_id
                        and created_at is not None
                        and created_at >= started_at - 120
                    ):
                        task_id = row.get("id")
                        if isinstance(task_id, str) and _PLANNER_ID.fullmatch(task_id):
                            matches[task_id] = {
                                "task_id": task_id,
                                "bucket_id": bucket_id,
                                "created_date_time": _bounded_text(row.get("createdDateTime"), 64),
                            }
                if complete:
                    break
                if count >= MAX_RECONCILE_TASKS:
                    return _unknown_create(
                        plan_id,
                        "bounded_readback_incomplete",
                        possible_matches=tuple(matches.values()),
                        match_count=len(matches),
                        possible_matches_complete=False,
                    )
            else:
                return _unknown_create(
                    plan_id,
                    "bounded_readback_incomplete",
                    possible_matches=tuple(matches.values()),
                    match_count=len(matches),
                    possible_matches_complete=False,
                )
        except GraphApiFailure:
            return _unknown_create(
                plan_id,
                "readback_unavailable",
                possible_matches=tuple(matches.values()),
                match_count=len(matches),
                possible_matches_complete=False,
            )
        return _unknown_create(
            plan_id,
            "no_match" if not matches else "ambiguous_remote_create",
            possible_matches=tuple(matches.values()),
            match_count=len(matches),
            possible_matches_complete=True,
        )

    def _list_task_rows(
        self,
        plan_id: str,
        *,
        page_size: int,
        page_cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """Read one raw task page for internal write reconciliation."""

        path = self._plan_tasks_path(plan_id)
        continuation = self._decode_cursor(
            page_cursor,
            kind="planner_tasks",
            target={"plan_id": plan_id},
            page_size=page_size,
        )
        query: dict[str, str | int] = {"$top": page_size}
        if continuation is not None:
            query["$skiptoken"] = continuation
        value = _json_object(self._api._request("GET", f"{path}?{urlencode(query)}", self._token))
        rows = _collection(value, "value", page_size=MAX_PAGE_SIZE)
        if any(row.get("planId") != plan_id for row in rows):
            raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
        next_link = value.get("@odata.nextLink")
        if next_link is None:
            if "@odata.deltaLink" in value:
                raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
            return rows, None, True
        token = _continuation_token(
            next_link,
            expected_path=path,
            allowed_keys={"$skiptoken", "skiptoken", "$top"},
            token_keys={"$skiptoken", "skiptoken"},
            expected_top=page_size,
        )
        next_cursor = self._encode_cursor(
            kind="planner_tasks",
            target={"plan_id": plan_id},
            page_size=page_size,
            token=token,
        )
        return rows, next_cursor, False

    def _plan_tasks_path(self, plan_id: str) -> str:
        return f"{GRAPH_ROOT}/planner/plans/{quote(plan_id, safe='')}/tasks"

    def _sharepoint_list_path(self, site_id: str, list_id: str) -> str:
        return f"{GRAPH_ROOT}/sites/{quote(site_id, safe=',')}/lists/{quote(list_id, safe='')}"

    def _sharepoint_delta_path(self, site_id: str, list_id: str) -> str:
        return f"{self._sharepoint_list_path(site_id, list_id)}/items/delta"

    def _encode_cursor(
        self,
        *,
        kind: str,
        target: Mapping[str, str],
        page_size: int,
        token: str,
    ) -> str:
        """Sign opaque Graph continuation state and bind it to profile and target."""

        if not token or len(token) > 8_192 or any(char in token for char in "\r\n\x00"):
            raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
        value = {
            "v": 1,
            "kind": kind,
            "binding_id": self._binding_id,
            "profile_ref": self._profile_ref,
            "actor_id": self.object_id,
            "target": dict(target),
            "page_size": page_size,
            "token": token,
        }
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self._api._cursor_key, body, hashlib.sha256).digest()
        cursor = "mgc1." + _b64url(body) + "." + _b64url(signature)
        if len(cursor) > MAX_CURSOR_CHARS:
            raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
        return cursor

    def _decode_cursor(
        self,
        value: str | None,
        *,
        kind: str,
        target: Mapping[str, str],
        page_size: int,
    ) -> str | None:
        """Verify a cursor before reusing its opaque Graph token on the fixed route."""

        if value is None:
            return None
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_CURSOR_CHARS:
            raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
        parts = value.split(".")
        if len(parts) != 3 or parts[0] != "mgc1":
            raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
        try:
            body = _b64url_decode(parts[1])
            signature = _b64url_decode(parts[2])
        except ValueError:
            raise GraphApiFailure("INVALID_PROVIDER_CURSOR") from None
        expected = hmac.new(self._api._cursor_key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
        decoded = _parse_json_object(body)
        if set(decoded) != {
            "v",
            "kind",
            "binding_id",
            "profile_ref",
            "actor_id",
            "target",
            "page_size",
            "token",
        }:
            raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
        if (
            decoded.get("v") != 1
            or decoded.get("kind") != kind
            or decoded.get("binding_id") != self._binding_id
            or decoded.get("profile_ref") != self._profile_ref
            or decoded.get("actor_id") != self.object_id
            or decoded.get("target") != dict(target)
            or decoded.get("page_size") != page_size
            or not isinstance(decoded.get("token"), str)
        ):
            raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
        token = decoded["token"]
        if not 1 <= len(token) <= 8_192 or any(char in token for char in "\r\n\x00"):
            raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
        return token


def _status_failure(response: HttpResponse, *, write: bool) -> GraphApiFailure:
    """Map HTTP status to an explicit, sanitized provider error."""

    status = response.status
    retry_after = _retry_after(response.headers)
    if status == 401:
        return GraphApiFailure("GRAPH_AUTHENTICATION_FAILED")
    if status == 403:
        return GraphApiFailure("GRAPH_ACCESS_DENIED")
    if status == 404:
        return GraphApiFailure("GRAPH_RESOURCE_NOT_FOUND")
    if status == 410:
        return GraphApiFailure("GRAPH_DELTA_RESYNC_REQUIRED")
    if status in {409, 412}:
        return GraphApiFailure("CONCURRENCY_CONFLICT")
    if status == 429:
        return GraphApiFailure("GRAPH_RATE_LIMITED", retryable=True, retry_after=retry_after)
    if 500 <= status <= 599:
        return GraphApiFailure(
            "GRAPH_SERVICE_UNAVAILABLE",
            retryable=not write,
            uncertain=write,
        )
    if status == 400:
        return GraphApiFailure("GRAPH_INVALID_REQUEST")
    return GraphApiFailure("GRAPH_REQUEST_REJECTED")


def _json_object(response: HttpResponse, *, uncertain: bool = False) -> dict[str, Any]:
    """Parse bounded JSON and reject duplicate keys or non-object responses."""

    try:
        value = _parse_json_object(response.body)
    except ValueError:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE", uncertain=uncertain) from None
    return value


def _parse_json_object(data: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def _collection(value: Mapping[str, Any], field: str, *, page_size: int) -> list[dict[str, Any]]:
    """Require a bounded collection of object rows."""

    rows = value.get(field)
    if not isinstance(rows, list) or len(rows) > page_size:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    if any(not isinstance(row, Mapping) for row in rows):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return [dict(row) for row in rows]


def _task_projection(value: Mapping[str, Any], *, expected_plan_id: str) -> dict[str, Any]:
    """Retain the bounded Planner fields needed for workflow status and ETags."""

    task_id = value.get("id")
    if not isinstance(task_id, str) or not _PLANNER_ID.fullmatch(task_id):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    if value.get("planId") != expected_plan_id:
        raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
    result: dict[str, Any] = {
        "task_id": task_id,
        "plan_id": expected_plan_id,
        "bucket_id": _bounded_text(value.get("bucketId"), 128),
        "title": _bounded_text(value.get("title"), 255),
        "percent_complete": _bounded_int(value.get("percentComplete"), 0, 100),
        "priority": _bounded_int(value.get("priority"), 0, 10),
        "due_date_time": _bounded_text(value.get("dueDateTime"), 64),
        "start_date_time": _bounded_text(value.get("startDateTime"), 64),
        "created_date_time": _bounded_text(value.get("createdDateTime"), 64),
        "last_modified_date_time": _bounded_text(value.get("lastModifiedDateTime"), 64),
        "etag": _bounded_text(value.get("@odata.etag"), 1024),
    }
    return result


def _plan_projection(value: Mapping[str, Any], *, expected_id: str) -> dict[str, Any]:
    """Return small metadata for one Planner plan."""

    title = value.get("title")
    return {
        "plan_id": expected_id,
        "title": _bounded_text(title, 255),
        "created_date_time": _bounded_text(value.get("createdDateTime"), 64),
        "etag": _bounded_text(value.get("@odata.etag"), 1024),
    }


def _sharepoint_item_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return list-item metadata without column values, file content, or user names."""

    item_id = value.get("id")
    if not isinstance(item_id, str) or not 1 <= len(item_id) <= 128:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    deleted = value.get("deleted")
    if deleted is not None and not isinstance(deleted, Mapping):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    content_type = value.get("contentType")
    if content_type is not None and not isinstance(content_type, Mapping):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    deleted_state = deleted.get("state") if isinstance(deleted, Mapping) else None
    content_type_name = content_type.get("name") if isinstance(content_type, Mapping) else None
    return {
        "item_id": item_id,
        "name": _bounded_text(value.get("name"), 255),
        "etag": _bounded_text(value.get("eTag"), 1024),
        "created_date_time": _bounded_text(value.get("createdDateTime"), 64),
        "last_modified_date_time": _bounded_text(value.get("lastModifiedDateTime"), 64),
        "content_type": _bounded_text(content_type_name, 80),
        "deleted": deleted_state == "deleted",
    }


def _unknown_create(
    plan_id: str,
    reason: str,
    *,
    possible_matches: tuple[Mapping[str, Any], ...] = (),
    match_count: int | None = None,
    possible_matches_complete: bool = False,
) -> dict[str, Any]:
    """Report an ambiguous create without assigning any candidate to the operation."""

    candidates = [dict(value) for value in possible_matches[:MAX_CREATE_POSSIBLE_MATCHES]]
    return {
        "status": "UNKNOWN",
        "task": None,
        "plan_id": plan_id,
        "reconciliation": reason,
        "possible_matches": candidates,
        "possible_match_count": match_count,
        "possible_matches_complete": possible_matches_complete,
        "possible_matches_truncated": match_count is not None
        and match_count > MAX_CREATE_POSSIBLE_MATCHES,
    }


def _unknown_update(plan_id: str, task_id: str) -> dict[str, Any]:
    return {
        "status": "UNKNOWN",
        "task": None,
        "plan_id": plan_id,
        "task_id": task_id,
        "reconciliation": "readback_unverified",
    }


def _changes_match(value: Mapping[str, Any], changes: Mapping[str, Any]) -> bool:
    return all(value.get(key) == expected for key, expected in changes.items())


def _task_changes(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "title",
        "due_date_time",
        "start_date_time",
        "percent_complete",
        "priority",
    }
    if not value or set(value) - allowed:
        raise GraphApiFailure("INVALID_ARGUMENTS")
    result: dict[str, Any] = {}
    if "title" in value:
        result["title"] = _task_title(value["title"])
    for field in ("due_date_time", "start_date_time"):
        if field in value:
            result[field] = _utc_datetime(value[field], field)
    if "percent_complete" in value:
        result["percentComplete"] = _bounded_int(value["percent_complete"], 0, 100)
    if "priority" in value:
        result["priority"] = _bounded_int(value["priority"], 0, 10)
    return result


def _graph_task_changes(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        {"due_date_time": "dueDateTime", "start_date_time": "startDateTime"}.get(key, key): item
        for key, item in value.items()
    }


def _changes_match_projection(raw: Mapping[str, Any], changes: Mapping[str, Any]) -> bool:
    return all(raw.get(key) == expected for key, expected in changes.items())


def _continuation_token(
    link: Any,
    *,
    expected_path: str,
    allowed_keys: set[str],
    token_keys: set[str],
    expected_top: int,
    expected_select: str | None = None,
) -> str:
    """Extract one continuation token after exact origin, path, and query validation."""

    if not isinstance(link, str) or len(link) > 12_000:
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    try:
        parsed = urlsplit(link)
        port = parsed.port
    except ValueError:
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != GRAPH_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path != expected_path
        or parsed.fragment
    ):
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR") from None
    if set(query) - allowed_keys or any(len(values) != 1 for values in query.values()):
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    token_names = token_keys.intersection(query)
    if len(token_names) != 1:
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    token = query[next(iter(token_names))][0]
    if not token or len(token) > 8_192 or any(char in token for char in "\r\n\x00"):
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    raw_top = query.get("$top")
    if raw_top is not None and raw_top[0] != str(expected_top):
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    raw_select = query.get("$select")
    if expected_select is not None and raw_select is not None and raw_select[0] != expected_select:
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    return token


def _bearer_token(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16_384
        or any(char in value for char in "\r\n\x00")
    ):
        raise GraphApiFailure("CREDENTIAL_INVALID")
    return value


def _external_subject(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or value.count(":") != 1:
        raise GraphApiFailure("PROFILE_IDENTITY_INVALID")
    tenant_id, object_id = value.split(":", 1)
    try:
        canonical_tenant = str(uuid.UUID(tenant_id)).lower()
        canonical_object = str(uuid.UUID(object_id)).lower()
    except ValueError:
        raise GraphApiFailure("PROFILE_IDENTITY_INVALID") from None
    if value != f"{canonical_tenant}:{canonical_object}":
        raise GraphApiFailure("PROFILE_IDENTITY_INVALID")
    return canonical_tenant, canonical_object


def _planner_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _PLANNER_ID.fullmatch(value):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return value


def _site_id(value: Any) -> str:
    if not isinstance(value, str) or not _SITE_ID.fullmatch(value):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return value


def _uuid_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return value


def _page_size(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PAGE_SIZE:
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return value


def _task_title(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or not value.strip():
        raise GraphApiFailure("INVALID_ARGUMENTS")
    if any(ord(char) < 32 for char in value):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return value


def _etag(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 1024
        or any(char in value for char in "\r\n\x00")
        or not (value.startswith('W/"') or value.startswith('"'))
        or not value.endswith('"')
    ):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return value


def _utc_datetime(value: Any, field: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise GraphApiFailure("INVALID_ARGUMENTS")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise GraphApiFailure("INVALID_ARGUMENTS") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bounded_int(value: Any, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return value


def _bounded_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    cleaned = "".join(" " if ord(char) < 32 else char for char in value)
    return cleaned[:maximum]


def _parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _retry_after(headers: Mapping[str, str]) -> int | None:
    value = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    if value is None or not value.isdecimal():
        return None
    return min(int(value), 3_600)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
