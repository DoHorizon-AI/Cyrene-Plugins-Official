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
from datetime import UTC, datetime, timedelta
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
_PLAN_LIST_SELECT = "id,title,owner,createdDateTime"
_TODO_TASK_SELECT = (
    "id,title,status,dueDateTime,completedDateTime,importance,createdDateTime,lastModifiedDateTime"
)
_CALENDAR_SELECT = "id,name,owner,canEdit,hexColor"
_MAIL_FOLDER_SELECT = (
    "id,displayName,parentFolderId,childFolderCount,totalItemCount,unreadItemCount,isHidden"
)
_MAIL_MESSAGE_SELECT = (
    "id,subject,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,isRead,"
    "importance,parentFolderId,isDraft"
)
_EVENT_SELECT = "id,subject,start,end,isAllDay,showAs,location,webLink,transactionId,attendees"
_DRIVE_SELECT = "id,name,driveType,webUrl,createdDateTime,lastModifiedDateTime,owner"
_DRIVE_ITEM_SELECT = (
    "id,name,size,eTag,createdDateTime,lastModifiedDateTime,webUrl,file,folder,"
    "parentReference,deleted,sharepointIds"
)
_PAGE_PARAMETER_KEYS = {"$skiptoken", "skiptoken", "$skip", "skip"}


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

    def list_plans(self, *, page_size: int = 50, page_cursor: str | None = None) -> dict[str, Any]:
        """Discover only plan metadata visible to the signed-in user."""

        return self._list_metadata(
            f"{GRAPH_ROOT}/me/planner/plans",
            kind="planner_plans",
            target={},
            select=_PLAN_LIST_SELECT,
            page_size=page_size,
            page_cursor=page_cursor,
            projection=_plan_list_projection,
        )

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

    def list_todo_lists(
        self, *, page_size: int = 50, page_cursor: str | None = None
    ) -> dict[str, Any]:
        """Read one complete server-sized page of the signed-in user's To Do lists.

        Microsoft Graph chooses this endpoint's page size because its reliable request is a
        plain GET; the caller's validated page_size remains bound to continuation cursors but
        does not change the upstream page length. Pages are capped at MAX_PAGE_SIZE and are
        never sliced, so no item is skipped when following @odata.nextLink. 中文：本端点沿用
        Graph 服务端分页；保留完整有界页，不按调用方 page_size 截断。
        """

        _page_size(page_size)
        path = f"{GRAPH_ROOT}/me/todo/lists"
        continuation = self._decode_cursor(
            page_cursor,
            kind="todo_lists",
            target={},
            page_size=page_size,
        )
        query: dict[str, str] = {}
        if continuation is not None:
            parameter = _decode_page_parameter(continuation)
            query[parameter["name"]] = parameter["value"]
        request_path = f"{path}?{urlencode(query)}" if query else path
        value = _json_object(self._api._request("GET", request_path, self._token))
        rows = _collection(value, "value", page_size=MAX_PAGE_SIZE)
        items = [_todo_list_projection(row) for row in rows]
        if "@odata.deltaLink" in value:
            raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
        next_link = value.get("@odata.nextLink")
        next_cursor = None
        if next_link is not None:
            page_parameter = _safe_page_parameter(
                next_link,
                expected_path=path,
                expected_top=None,
                expected_select=None,
                fixed_query={},
            )
            next_cursor = self._encode_cursor(
                kind="todo_lists",
                target={},
                page_size=page_size,
                token=json.dumps(page_parameter, sort_keys=True, separators=(",", ":")),
            )
        return {
            "items": items,
            "page_cursor": page_cursor,
            "next_cursor": next_cursor,
            "complete": next_cursor is None,
            "truncated": False,
        }

    def list_todo_tasks(
        self,
        task_list_id: str,
        *,
        page_size: int = 50,
        page_cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read one bounded page of To Do task metadata from one selected list."""

        _graph_opaque_id(task_list_id, "task_list_id")
        return self._list_metadata(
            f"{GRAPH_ROOT}/me/todo/lists/{quote(task_list_id, safe='')}/tasks",
            kind="todo_tasks",
            target={"task_list_id": task_list_id},
            select=_TODO_TASK_SELECT,
            page_size=page_size,
            page_cursor=page_cursor,
            projection=lambda row: _todo_task_projection(row, task_list_id),
        )

    def get_todo_task(self, task_list_id: str, task_id: str) -> dict[str, Any]:
        """Read one task and prove that it belongs to the selected To Do list."""

        raw = self._get_todo_task_raw(task_list_id, task_id)
        return _todo_task_projection(raw, task_list_id)

    def create_todo_task(
        self,
        task_list_id: str,
        title: str,
        *,
        due_date_time: str | None = None,
        importance: str | None = None,
    ) -> dict[str, Any]:
        """Create one task once and return UNKNOWN when the response is ambiguous."""

        _graph_opaque_id(task_list_id, "task_list_id")
        safe_title = _task_title(title)
        body: dict[str, Any] = {"title": safe_title}
        if due_date_time is not None:
            body["dueDateTime"] = _graph_date_time(due_date_time)
        if importance is not None:
            body["importance"] = _todo_importance(importance)
        try:
            response = self._api._request(
                "POST",
                f"{GRAPH_ROOT}/me/todo/lists/{quote(task_list_id, safe='')}/tasks",
                self._token,
                body=body,
                write=True,
            )
            if response.status != 201:
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True)
            created = _json_object(response, uncertain=True)
            task_id = created.get("id")
            if not isinstance(task_id, str) or created.get("title") != safe_title:
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True)
            raw = self._get_todo_task_raw(task_list_id, task_id)
            if not _todo_task_changes_match(raw, body):
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True)
            task = _todo_task_projection(raw, task_list_id)
            return {"status": "CONFIRMED", "task": task, "reconciliation": "not_required"}
        except GraphApiFailure as error:
            if not error.uncertain:
                raise
            return {
                "status": "UNKNOWN",
                "task": None,
                "task_list_id": task_list_id,
                "reconciliation": "create_response_ambiguous",
            }

    def update_todo_task(
        self,
        task_list_id: str,
        task_id: str,
        if_match: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Patch selected task fields with If-Match and verify the exact readback."""

        raw_before = self._get_todo_task_raw(task_list_id, task_id)
        etag = _etag(if_match)
        if raw_before.get("@odata.etag") != etag:
            raise GraphApiFailure("CONCURRENCY_CONFLICT")
        graph_changes = _todo_task_changes(changes)
        task_path = (
            f"{GRAPH_ROOT}/me/todo/lists/{quote(task_list_id, safe='')}"
            f"/tasks/{quote(task_id, safe='')}"
        )
        try:
            self._api._request(
                "PATCH",
                task_path,
                self._token,
                body=graph_changes,
                headers={"If-Match": etag},
                write=True,
            )
        except GraphApiFailure as error:
            if error.code == "CONCURRENCY_CONFLICT" or not error.uncertain:
                raise
            try:
                raw_after = self._get_todo_task_raw(task_list_id, task_id)
            except GraphApiFailure:
                return _unknown_resource_write(
                    "todo_task", task_list_id, task_id, "update_readback_unavailable"
                )
            if _todo_task_changes_match(raw_after, graph_changes):
                return {
                    "status": "RECONCILED",
                    "task": _todo_task_projection(raw_after, task_list_id),
                    "reconciliation": "readback_match",
                }
            return _unknown_resource_write(
                "todo_task", task_list_id, task_id, "update_readback_unverified"
            )
        try:
            raw_after = self._get_todo_task_raw(task_list_id, task_id)
        except GraphApiFailure:
            return _unknown_resource_write(
                "todo_task", task_list_id, task_id, "update_readback_unavailable"
            )
        if not _todo_task_changes_match(raw_after, graph_changes):
            return _unknown_resource_write(
                "todo_task", task_list_id, task_id, "update_readback_unverified"
            )
        return {
            "status": "CONFIRMED",
            "task": _todo_task_projection(raw_after, task_list_id),
            "reconciliation": "readback_match",
        }

    def delete_todo_task(self, task_list_id: str, task_id: str, if_match: str) -> dict[str, Any]:
        """Delete one selected task with ETag protection and deletion readback."""

        before = self._get_todo_task_raw(task_list_id, task_id)
        etag = _etag(if_match)
        if before.get("@odata.etag") != etag:
            raise GraphApiFailure("CONCURRENCY_CONFLICT")
        uncertain = False
        task_path = (
            f"{GRAPH_ROOT}/me/todo/lists/{quote(task_list_id, safe='')}"
            f"/tasks/{quote(task_id, safe='')}"
        )
        try:
            self._api._request(
                "DELETE",
                task_path,
                self._token,
                headers={"If-Match": etag},
                write=True,
            )
        except GraphApiFailure as error:
            if error.code == "CONCURRENCY_CONFLICT":
                raise
            if not error.uncertain:
                raise
            uncertain = True
        try:
            self._get_todo_task_raw(task_list_id, task_id)
        except GraphApiFailure as error:
            if error.code == "GRAPH_RESOURCE_NOT_FOUND":
                return {
                    "status": "RECONCILED" if uncertain else "CONFIRMED",
                    "task_list_id": task_list_id,
                    "task_id": task_id,
                    "reconciliation": "deleted_readback",
                }
            return _unknown_resource_write(
                "todo_task", task_list_id, task_id, "delete_readback_unavailable"
            )
        return _unknown_resource_write(
            "todo_task", task_list_id, task_id, "delete_readback_still_exists"
        )

    def list_calendars(
        self, *, page_size: int = 50, page_cursor: str | None = None
    ) -> dict[str, Any]:
        """Read one bounded page of the signed-in user's calendar metadata."""

        return self._list_metadata(
            f"{GRAPH_ROOT}/me/calendars",
            kind="calendars",
            target={},
            select=_CALENDAR_SELECT,
            page_size=page_size,
            page_cursor=page_cursor,
            projection=_calendar_projection,
        )

    def list_calendar_events(
        self,
        calendar_id: str,
        start_date_time: str,
        end_date_time: str,
        *,
        page_size: int = 50,
        page_cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read a bounded calendar-view page and expand recurring instances."""

        _graph_opaque_id(calendar_id, "calendar_id")
        start = _utc_datetime_with_offset(start_date_time, "start_date_time")
        end = _utc_datetime_with_offset(end_date_time, "end_date_time")
        if datetime.fromisoformat(end.replace("Z", "+00:00")) <= datetime.fromisoformat(
            start.replace("Z", "+00:00")
        ):
            raise GraphApiFailure("INVALID_ARGUMENTS")
        path = f"{GRAPH_ROOT}/me/calendars/{quote(calendar_id, safe='')}/calendarView"
        fixed = {"startDateTime": start, "endDateTime": end}
        return self._list_metadata(
            path,
            kind="calendar_events",
            target={"calendar_id": calendar_id, "start": start, "end": end},
            select=_EVENT_SELECT,
            page_size=page_size,
            page_cursor=page_cursor,
            projection=_event_projection,
            fixed_query=fixed,
            headers={"Prefer": 'outlook.timezone="UTC"'},
        )

    def get_calendar_event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        """Read one event projection without body, attachments, or attendees."""

        _graph_opaque_id(calendar_id, "calendar_id")
        _graph_opaque_id(event_id, "event_id")
        path = (
            f"{GRAPH_ROOT}/me/calendars/{quote(calendar_id, safe='')}"
            f"/events/{quote(event_id, safe='')}"
        )
        raw = _json_object(
            self._api._request(
                "GET",
                f"{path}?{urlencode({'$select': _EVENT_SELECT})}",
                self._token,
                headers={"Prefer": 'outlook.timezone="UTC"'},
            )
        )
        if raw.get("id") != event_id:
            raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
        return _event_projection(raw)

    def get_calendar_freebusy(
        self,
        schedules: list[str],
        start_date_time: str,
        end_date_time: str,
        interval_minutes: int,
    ) -> dict[str, Any]:
        """Read bounded availability only for the selected schedules and range."""

        safe_schedules = _schedule_ids(schedules)
        if not 1 <= len(safe_schedules) <= 20:
            raise GraphApiFailure("INVALID_ARGUMENTS")
        start = _utc_datetime_with_offset(start_date_time, "start_date_time")
        end = _utc_datetime_with_offset(end_date_time, "end_date_time")
        start_value = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_value = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if (
            end_value <= start_value
            or end_value - start_value > timedelta(days=31)
            or isinstance(interval_minutes, bool)
            or not isinstance(interval_minutes, int)
            or interval_minutes not in {5, 10, 15, 30, 60}
        ):
            raise GraphApiFailure("INVALID_ARGUMENTS")
        body = {
            "schedules": safe_schedules,
            "startTime": _graph_date_time(start),
            "endTime": _graph_date_time(end),
            "availabilityViewInterval": interval_minutes,
        }
        raw = _json_object(
            self._api._request(
                "POST",
                f"{GRAPH_ROOT}/me/calendar/getSchedule",
                self._token,
                body=body,
            )
        )
        values = _collection(raw, "value", page_size=20)
        result = []
        for value in values:
            schedule_id = _bounded_text(value.get("scheduleId"), 320)
            availability = value.get("availabilityView")
            items = value.get("scheduleItems", [])
            if (
                not isinstance(schedule_id, str)
                or schedule_id.casefold() not in set(safe_schedules)
                or not isinstance(availability, str)
                or len(availability) > 10_000
                or not isinstance(items, list)
                or len(items) > 1000
            ):
                raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
            slots = []
            for item in items:
                if not isinstance(item, Mapping):
                    raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
                status = item.get("status")
                if not isinstance(status, str) or status not in {
                    "free",
                    "tentative",
                    "busy",
                    "oof",
                    "workingElsewhere",
                    "unknown",
                }:
                    raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
                slots.append(
                    {
                        "status": status,
                        "start": _graph_datetime_projection(item.get("start")),
                        "end": _graph_datetime_projection(item.get("end")),
                    }
                )
            result.append(
                {
                    "schedule_id": schedule_id,
                    "availability_view": availability,
                    "interval_minutes": interval_minutes,
                    "schedule_items": slots,
                }
            )
        return {
            "schedules": result,
            "start_date_time": start,
            "end_date_time": end,
            "complete": True,
            "truncated": False,
        }

    def create_calendar_event(
        self,
        calendar_id: str,
        subject: str,
        start_date_time: str,
        end_date_time: str,
        transaction_id: str,
        *,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Create one recipient-free event with Graph transaction reconciliation."""

        _graph_opaque_id(calendar_id, "calendar_id")
        safe_subject = _task_title(subject)
        transaction = _uuid_canonical(transaction_id)
        start = _utc_datetime_with_offset(start_date_time, "start_date_time")
        end = _utc_datetime_with_offset(end_date_time, "end_date_time")
        if datetime.fromisoformat(end.replace("Z", "+00:00")) <= datetime.fromisoformat(
            start.replace("Z", "+00:00")
        ):
            raise GraphApiFailure("INVALID_ARGUMENTS")
        body: dict[str, Any] = {
            "subject": safe_subject,
            "start": _graph_date_time(start),
            "end": _graph_date_time(end),
            "transactionId": transaction,
            "attendees": [],
        }
        if location is not None:
            body["location"] = {"displayName": _bounded_input_text(location, 255)}
        path = f"{GRAPH_ROOT}/me/calendars/{quote(calendar_id, safe='')}/events"
        try:
            response = self._api._request("POST", path, self._token, body=body, write=True)
            if response.status != 201:
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True)
            created = _json_object(response, uncertain=True)
            created_id = created.get("id")
            if (
                not isinstance(created_id, str)
                or created.get("transactionId") != transaction
                or created.get("subject") != safe_subject
            ):
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True)
            raw = self._get_calendar_event_raw(calendar_id, created_id)
            if (
                raw.get("transactionId") != transaction
                or raw.get("subject") != safe_subject
                or raw.get("attendees") != []
            ):
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True)
            return {
                "status": "CONFIRMED",
                "event": _event_projection(raw),
                "reconciliation": "not_required",
            }
        except GraphApiFailure as error:
            if not error.uncertain:
                raise
            matches = self._find_event_by_transaction(path, transaction, safe_subject)
            if len(matches) == 1:
                return {
                    "status": "RECONCILED",
                    "event": _event_projection(matches[0]),
                    "reconciliation": "transaction_id_readback",
                }
            return {
                "status": "UNKNOWN",
                "event": None,
                "transaction_id": transaction,
                "reconciliation": "transaction_id_readback_ambiguous"
                if matches
                else "transaction_id_readback_unavailable_or_empty",
            }

    def update_calendar_event(
        self,
        calendar_id: str,
        event_id: str,
        if_match: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Update recipient-free event fields with ETag concurrency and readback."""

        before = self._get_calendar_event_raw(calendar_id, event_id)
        etag = _etag(if_match)
        if before.get("@odata.etag") != etag:
            raise GraphApiFailure("CONCURRENCY_CONFLICT")
        if before.get("attendees"):
            raise GraphApiFailure("EVENT_HAS_ATTENDEES")
        graph_changes = _event_changes(changes)
        path = (
            f"{GRAPH_ROOT}/me/calendars/{quote(calendar_id, safe='')}"
            f"/events/{quote(event_id, safe='')}"
        )
        uncertain = False
        try:
            self._api._request(
                "PATCH",
                path,
                self._token,
                body=graph_changes,
                headers={"If-Match": etag},
                write=True,
            )
        except GraphApiFailure as error:
            if error.code == "CONCURRENCY_CONFLICT":
                raise
            if not error.uncertain:
                raise
            uncertain = True
        try:
            after = self._get_calendar_event_raw(calendar_id, event_id)
        except GraphApiFailure:
            return _unknown_resource_write(
                "calendar_event", calendar_id, event_id, "update_readback_unavailable"
            )
        if not _event_changes_match(after, graph_changes):
            return _unknown_resource_write(
                "calendar_event", calendar_id, event_id, "update_readback_unverified"
            )
        return {
            "status": "RECONCILED" if uncertain else "CONFIRMED",
            "event": _event_projection(after),
            "reconciliation": "readback_match",
        }

    def delete_calendar_event(
        self, calendar_id: str, event_id: str, if_match: str
    ) -> dict[str, Any]:
        """Delete a recipient-free event and confirm deletion by exact ID readback."""

        before = self._get_calendar_event_raw(calendar_id, event_id)
        etag = _etag(if_match)
        if before.get("@odata.etag") != etag:
            raise GraphApiFailure("CONCURRENCY_CONFLICT")
        if before.get("attendees"):
            raise GraphApiFailure("EVENT_HAS_ATTENDEES")
        path = (
            f"{GRAPH_ROOT}/me/calendars/{quote(calendar_id, safe='')}"
            f"/events/{quote(event_id, safe='')}"
        )
        uncertain = False
        try:
            self._api._request("DELETE", path, self._token, headers={"If-Match": etag}, write=True)
        except GraphApiFailure as error:
            if error.code == "CONCURRENCY_CONFLICT":
                raise
            if not error.uncertain:
                raise
            uncertain = True
        try:
            self._get_calendar_event_raw(calendar_id, event_id)
        except GraphApiFailure as error:
            if error.code == "GRAPH_RESOURCE_NOT_FOUND":
                return {
                    "status": "RECONCILED" if uncertain else "CONFIRMED",
                    "calendar_id": calendar_id,
                    "event_id": event_id,
                    "reconciliation": "deleted_readback",
                }
            return _unknown_resource_write(
                "calendar_event", calendar_id, event_id, "delete_readback_unavailable"
            )
        return _unknown_resource_write(
            "calendar_event", calendar_id, event_id, "delete_readback_still_exists"
        )

    def list_mail_folders(
        self, *, page_size: int = 50, page_cursor: str | None = None
    ) -> dict[str, Any]:
        """Read one bounded page of mail-folder metadata for the signed-in user."""

        return self._list_metadata(
            f"{GRAPH_ROOT}/me/mailFolders",
            kind="mail_folders",
            target={},
            select=_MAIL_FOLDER_SELECT,
            page_size=page_size,
            page_cursor=page_cursor,
            projection=_mail_folder_projection,
        )

    def list_mail_messages(
        self,
        folder_id: str,
        *,
        page_size: int = 50,
        page_cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read metadata-only messages from one exact folder route."""

        folder = _graph_opaque_id(folder_id, "folder_id")
        path = f"{GRAPH_ROOT}/me/mailFolders/{quote(folder, safe='')}/messages"
        return self._list_metadata(
            path,
            kind="mail_messages",
            target={"folder_id": folder},
            select=_MAIL_MESSAGE_SELECT,
            page_size=page_size,
            page_cursor=page_cursor,
            projection=lambda row: _mail_message_projection(row, expected_folder_id=folder),
        )

    def get_mail_message(self, folder_id: str, message_id: str) -> dict[str, Any]:
        """Read one metadata-only message and verify its folder membership."""

        folder = _graph_opaque_id(folder_id, "folder_id")
        message = _graph_opaque_id(message_id, "message_id")
        path = (
            f"{GRAPH_ROOT}/me/mailFolders/{quote(folder, safe='')}/messages/"
            f"{quote(message, safe='')}?{urlencode({'$select': _MAIL_MESSAGE_SELECT})}"
        )
        raw = _json_object(self._api._request("GET", path, self._token))
        return _mail_message_projection(
            raw,
            expected_folder_id=folder,
            expected_message_id=message,
        )

    def list_mail_drafts(
        self, *, page_size: int = 50, page_cursor: str | None = None
    ) -> dict[str, Any]:
        """Read draft metadata only from the current user's fixed Drafts folder."""

        path = f"{GRAPH_ROOT}/me/mailFolders/drafts/messages"
        return self._list_metadata(
            path,
            kind="mail_drafts",
            target={"folder_id": "drafts"},
            select=_MAIL_MESSAGE_SELECT,
            page_size=page_size,
            page_cursor=page_cursor,
            projection=lambda row: _mail_message_projection(row, require_draft=True),
        )

    def create_mail_draft(
        self,
        subject: str,
        body_text: str,
        *,
        to_recipients: list[str] | None = None,
        cc_recipients: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create one plain-text draft in the signed-in user's Drafts folder once."""

        payload: dict[str, Any] = {
            "subject": _mail_plain_text(subject, "subject", 255),
            "body": {"contentType": "text", "content": _mail_body_text(body_text)},
        }
        if to_recipients is not None:
            payload["toRecipients"] = _mail_graph_recipients(to_recipients)
        if cc_recipients is not None:
            payload["ccRecipients"] = _mail_graph_recipients(cc_recipients)
        path = f"{GRAPH_ROOT}/me/mailFolders/drafts/messages"
        try:
            response = self._api._request("POST", path, self._token, body=payload, write=True)
            if response.status != 201:
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True)
            created = _json_object(response, uncertain=True)
            try:
                projected = _mail_message_projection(created, require_draft=True)
            except GraphApiFailure:
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True) from None
            return {"status": "CONFIRMED", "message": projected}
        except GraphApiFailure as error:
            if not error.uncertain:
                raise
            return {
                "status": "UNKNOWN",
                "message": None,
                "reconciliation": "draft_create_response_uncertain",
            }

    def update_mail_draft(
        self,
        message_id: str,
        if_match: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Update a verified Drafts-folder message with its current ETag once."""

        message = _graph_opaque_id(message_id, "message_id")
        before = self._get_mail_draft_raw(message)
        etag = _etag(if_match)
        if before.get("@odata.etag") != etag:
            raise GraphApiFailure("CONCURRENCY_CONFLICT")
        payload = _mail_draft_changes(changes)
        path = f"{GRAPH_ROOT}/me/messages/{quote(message, safe='')}"
        try:
            response = self._api._request(
                "PATCH",
                path,
                self._token,
                body=payload,
                headers={"If-Match": etag},
                write=True,
            )
            if response.status != 200:
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True)
            updated = _json_object(response, uncertain=True)
            try:
                projected = _mail_message_projection(
                    updated,
                    expected_message_id=message,
                    require_draft=True,
                )
            except GraphApiFailure:
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True) from None
            return {"status": "CONFIRMED", "message": projected}
        except GraphApiFailure as error:
            if not error.uncertain:
                raise
            return {
                "status": "UNKNOWN",
                "message_id": message,
                "reconciliation": "draft_update_response_uncertain",
            }

    def delete_mail_draft(self, message_id: str, if_match: str) -> dict[str, Any]:
        """Delete one verified Drafts-folder message with its current ETag once."""

        message = _graph_opaque_id(message_id, "message_id")
        before = self._get_mail_draft_raw(message)
        etag = _etag(if_match)
        if before.get("@odata.etag") != etag:
            raise GraphApiFailure("CONCURRENCY_CONFLICT")
        path = f"{GRAPH_ROOT}/me/messages/{quote(message, safe='')}"
        try:
            response = self._api._request(
                "DELETE",
                path,
                self._token,
                headers={"If-Match": etag},
                write=True,
            )
            if response.status != 204:
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True)
            return {"status": "CONFIRMED", "message_id": message}
        except GraphApiFailure as error:
            if not error.uncertain:
                raise
            return {
                "status": "UNKNOWN",
                "message_id": message,
                "reconciliation": "draft_delete_response_uncertain",
            }

    def send_mail_draft(self, message_id: str) -> dict[str, Any]:
        """Send only a preflight-verified existing draft; acceptance is not delivery."""

        message = _graph_opaque_id(message_id, "message_id")
        self._get_mail_draft_raw(message)
        path = f"{GRAPH_ROOT}/me/messages/{quote(message, safe='')}/send"
        try:
            response = self._api._request("POST", path, self._token, write=True)
            if response.status != 202:
                raise GraphApiFailure("GRAPH_OUTCOME_UNKNOWN", uncertain=True)
            return {"status": "ACCEPTED", "message_id": message}
        except GraphApiFailure as error:
            if not error.uncertain:
                raise
            return {
                "status": "UNKNOWN",
                "message_id": message,
                "reconciliation": "send_response_uncertain",
            }

    def _get_mail_draft_raw(self, message_id: str) -> dict[str, Any]:
        """Read one message through the fixed Drafts route and require isDraft."""

        message = _graph_opaque_id(message_id, "message_id")
        path = (
            f"{GRAPH_ROOT}/me/mailFolders/drafts/messages/{quote(message, safe='')}?"
            f"{urlencode({'$select': _MAIL_MESSAGE_SELECT})}"
        )
        raw = _json_object(self._api._request("GET", path, self._token))
        projected = _mail_message_projection(
            raw,
            expected_message_id=message,
            require_draft=True,
        )
        if projected["message_id"] != message:
            raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
        return raw

    def list_sharepoint_drives(
        self,
        site_id: str,
        *,
        page_size: int = 50,
        page_cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read document-library metadata from one selected SharePoint site."""

        _site_id(site_id)
        return self._list_metadata(
            f"{GRAPH_ROOT}/sites/{quote(site_id, safe=',')}/drives",
            kind="sharepoint_drives",
            target={"site_id": site_id},
            select=_DRIVE_SELECT,
            page_size=page_size,
            page_cursor=page_cursor,
            projection=lambda row: _drive_projection(row, site_id),
        )

    def list_sharepoint_drive_items(
        self,
        site_id: str,
        drive_id: str,
        *,
        parent_item_id: str | None = None,
        page_size: int = 50,
        page_cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read metadata-only items in one selected document library folder."""

        _site_id(site_id)
        _graph_opaque_id(drive_id, "drive_id")
        if parent_item_id is not None:
            _graph_opaque_id(parent_item_id, "parent_item_id")
        suffix = (
            "/root/children"
            if parent_item_id is None
            else f"/items/{quote(parent_item_id, safe='')}/children"
        )
        path = (
            f"{GRAPH_ROOT}/sites/{quote(site_id, safe=',')}/drives/"
            f"{quote(drive_id, safe='')}{suffix}"
        )
        return self._list_metadata(
            path,
            kind="sharepoint_drive_items",
            target={
                "site_id": site_id,
                "drive_id": drive_id,
                "parent_item_id": parent_item_id or "root",
            },
            select=_DRIVE_ITEM_SELECT,
            page_size=page_size,
            page_cursor=page_cursor,
            projection=lambda row: _drive_item_projection(row, site_id, drive_id),
        )

    def _list_metadata(
        self,
        path: str,
        *,
        kind: str,
        target: Mapping[str, str],
        select: str,
        page_size: int,
        page_cursor: str | None,
        projection: Callable[[Mapping[str, Any]], dict[str, Any]],
        fixed_query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Fetch one fixed-route collection page and sign any validated next token."""

        _page_size(page_size)
        fixed = dict(fixed_query or {})
        continuation = self._decode_cursor(
            page_cursor, kind=kind, target=target, page_size=page_size
        )
        query: dict[str, str | int] = {**fixed, "$select": select, "$top": page_size}
        if continuation is not None:
            parameter = _decode_page_parameter(continuation)
            query[parameter["name"]] = parameter["value"]
        response = self._api._request(
            "GET",
            f"{path}?{urlencode(query)}",
            self._token,
            headers=headers,
        )
        value = _json_object(response)
        rows = _collection(value, "value", page_size=page_size)
        items = [projection(row) for row in rows]
        next_link = value.get("@odata.nextLink")
        if "@odata.deltaLink" in value:
            raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
        next_cursor = None
        if next_link is not None:
            page_parameter = _safe_page_parameter(
                next_link,
                expected_path=path,
                expected_top=page_size,
                expected_select=select,
                fixed_query=fixed,
            )
            next_cursor = self._encode_cursor(
                kind=kind,
                target=target,
                page_size=page_size,
                token=json.dumps(page_parameter, sort_keys=True, separators=(",", ":")),
            )
        return {
            "items": items,
            "page_cursor": page_cursor,
            "next_cursor": next_cursor,
            "complete": next_cursor is None,
            "truncated": False,
        }

    def _get_todo_task_raw(self, task_list_id: str, task_id: str) -> dict[str, Any]:
        """Read and identity-check one task under its exact list route."""

        _graph_opaque_id(task_list_id, "task_list_id")
        _graph_opaque_id(task_id, "task_id")
        path = (
            f"{GRAPH_ROOT}/me/todo/lists/{quote(task_list_id, safe='')}"
            f"/tasks/{quote(task_id, safe='')}"
        )
        raw = _json_object(
            self._api._request(
                "GET", f"{path}?{urlencode({'$select': _TODO_TASK_SELECT})}", self._token
            )
        )
        if raw.get("id") != task_id:
            raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
        return raw

    def _get_calendar_event_raw(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        """Read and identity-check an event under one exact selected calendar."""

        _graph_opaque_id(calendar_id, "calendar_id")
        _graph_opaque_id(event_id, "event_id")
        path = (
            f"{GRAPH_ROOT}/me/calendars/{quote(calendar_id, safe='')}"
            f"/events/{quote(event_id, safe='')}"
        )
        raw = _json_object(
            self._api._request(
                "GET",
                f"{path}?{urlencode({'$select': _EVENT_SELECT})}",
                self._token,
                headers={"Prefer": 'outlook.timezone="UTC"'},
            )
        )
        if raw.get("id") != event_id:
            raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
        return raw

    def _find_event_by_transaction(
        self, path: str, transaction_id: str, subject: str
    ) -> list[dict[str, Any]]:
        """Read at most two exact transaction-ID matches after an uncertain create."""

        query = urlencode(
            {
                "$filter": f"transactionId eq '{transaction_id}'",
                "$select": _EVENT_SELECT,
                "$top": 2,
            }
        )
        try:
            value = _json_object(
                self._api._request(
                    "GET",
                    f"{path}?{query}",
                    self._token,
                    headers={"Prefer": 'outlook.timezone="UTC"'},
                )
            )
            matches = _collection(value, "value", page_size=2)
            return [
                item
                for item in matches
                if item.get("transactionId") == transaction_id and item.get("subject") == subject
            ]
        except GraphApiFailure:
            return []

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


def _plan_list_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project one user's Planner selection candidate without tasks or group data."""

    plan_id = value.get("id")
    if not isinstance(plan_id, str) or not _PLANNER_ID.fullmatch(plan_id):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "plan_id": plan_id,
        "title": _bounded_text(value.get("title"), 255),
        "owner_id": _bounded_text(value.get("owner"), 128),
        "created_date_time": _bounded_text(value.get("createdDateTime"), 64),
        "etag": _bounded_text(value.get("@odata.etag"), 1024),
    }


def _mail_folder_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project folder identity and counts without custom or message content."""

    folder_id = _graph_opaque_id(value.get("id"), "folder_id")
    hidden = value.get("isHidden")
    if hidden is not None and not isinstance(hidden, bool):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "folder_id": folder_id,
        "display_name": _bounded_text(value.get("displayName"), 255),
        "parent_folder_id": _bounded_text(value.get("parentFolderId"), 512),
        "child_folder_count": _bounded_int(value.get("childFolderCount"), 0, 100_000),
        "total_item_count": _bounded_int(value.get("totalItemCount"), 0, 2_147_483_647),
        "unread_item_count": _bounded_int(value.get("unreadItemCount"), 0, 2_147_483_647),
        "is_hidden": hidden,
    }


def _mail_message_projection(
    value: Mapping[str, Any],
    *,
    expected_folder_id: str | None = None,
    expected_message_id: str | None = None,
    require_draft: bool = False,
) -> dict[str, Any]:
    """Retain only selected metadata; body, preview, and attachments never escape."""

    message_id = _graph_opaque_id(value.get("id"), "message_id")
    folder_id = _graph_opaque_id(value.get("parentFolderId"), "folder_id")
    is_draft = value.get("isDraft")
    is_read = value.get("isRead")
    if not isinstance(is_draft, bool) or (is_read is not None and not isinstance(is_read, bool)):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    if expected_message_id is not None and message_id != expected_message_id:
        raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
    if expected_folder_id is not None and folder_id != expected_folder_id:
        raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
    if require_draft and not is_draft:
        raise GraphApiFailure("MESSAGE_NOT_DRAFT")
    importance = value.get("importance")
    if importance is not None and importance not in {"low", "normal", "high"}:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "message_id": message_id,
        "folder_id": folder_id,
        "subject": _bounded_text(value.get("subject"), 255),
        "from": _mail_recipient_projection(value.get("from")),
        "to_recipients": _mail_recipients_projection(value.get("toRecipients")),
        "cc_recipients": _mail_recipients_projection(value.get("ccRecipients")),
        "received_date_time": _bounded_text(value.get("receivedDateTime"), 64),
        "sent_date_time": _bounded_text(value.get("sentDateTime"), 64),
        "is_read": is_read,
        "importance": importance,
        "is_draft": is_draft,
        "etag": _bounded_text(value.get("@odata.etag"), 1024),
    }


def _mail_recipient_projection(value: Any) -> dict[str, str | None] | None:
    """Project one sender or recipient without exposing extra Graph fields."""

    if value is None:
        return None
    if not isinstance(value, Mapping) or not isinstance(value.get("emailAddress"), Mapping):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    address = value["emailAddress"].get("address")
    name = value["emailAddress"].get("name")
    if address is not None and (
        not isinstance(address, str)
        or len(address) > 320
        or any(ord(char) < 32 for char in address)
    ):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "name": _bounded_text(name, 255),
        "address": _bounded_text(address, 320),
    }


def _mail_recipients_projection(value: Any) -> list[dict[str, str | None]]:
    """Project at most fifty address/name pairs from a Graph recipient array."""

    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 50:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    recipients = [_mail_recipient_projection(item) for item in value]
    if any(item is None for item in recipients):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return [item for item in recipients if item is not None]


def _mail_plain_text(value: Any, field: str, maximum: int) -> str:
    """Validate bounded nonempty single-line draft text."""

    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return value


def _mail_body_text(value: Any) -> str:
    """Validate bounded plain text while allowing normal line breaks and tabs."""

    if (
        not isinstance(value, str)
        or len(value) > 65_536
        or any(ord(char) < 32 and char not in "\r\n\t" for char in value)
    ):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return value


def _mail_graph_recipients(value: Any) -> list[dict[str, dict[str, str]]]:
    """Convert bounded plain addresses to Graph recipient objects."""

    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise GraphApiFailure("INVALID_ARGUMENTS")
    if any(
        not isinstance(address, str)
        or not 3 <= len(address) <= 320
        or address.count("@") != 1
        or re.fullmatch(r"[^@\s<>]+@[^@\s<>]+", address) is None
        or any(ord(char) < 32 for char in address)
        for address in value
    ) or len({address.casefold() for address in value}) != len(value):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return [{"emailAddress": {"address": address}} for address in value]


def _mail_draft_changes(value: Mapping[str, Any]) -> dict[str, Any]:
    """Convert only approved draft edits into Graph message properties."""

    if not isinstance(value, Mapping) or not value or set(value) - {
        "subject",
        "body_text",
        "to_recipients",
        "cc_recipients",
    }:
        raise GraphApiFailure("INVALID_ARGUMENTS")
    result: dict[str, Any] = {}
    if "subject" in value:
        result["subject"] = _mail_plain_text(value["subject"], "subject", 255)
    if "body_text" in value:
        result["body"] = {"contentType": "text", "content": _mail_body_text(value["body_text"])}
    if "to_recipients" in value:
        result["toRecipients"] = _mail_graph_recipients(value["to_recipients"])
    if "cc_recipients" in value:
        result["ccRecipients"] = _mail_graph_recipients(value["cc_recipients"])
    return result


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


def _safe_page_parameter(
    link: Any,
    *,
    expected_path: str,
    expected_top: int | None,
    expected_select: str | None,
    fixed_query: Mapping[str, str],
) -> dict[str, str]:
    """Extract a paging parameter from an exact Graph origin/path and query."""

    if not isinstance(link, str) or len(link) > 12_000:
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    try:
        parsed = urlsplit(link)
        port = parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
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
    allowed = _PAGE_PARAMETER_KEYS | set(fixed_query)
    if expected_top is not None:
        allowed.add("$top")
    if expected_select is not None:
        allowed.add("$select")
    if set(query) - allowed or any(len(values) != 1 for values in query.values()):
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    names = _PAGE_PARAMETER_KEYS.intersection(query)
    if len(names) != 1:
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    if expected_top is not None and query.get("$top", [str(expected_top)])[0] != str(expected_top):
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    if (
        expected_select is not None
        and query.get("$select", [expected_select])[0] != expected_select
    ):
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    for key, expected in fixed_query.items():
        if key in query and query[key][0] != expected:
            raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    name = next(iter(names))
    value = query[name][0]
    if not value or len(value) > 8_192 or any(char in value for char in "\r\n\x00"):
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    return {"name": name, "value": value}


def _decode_page_parameter(value: str) -> dict[str, str]:
    """Validate the signed cursor's typed Graph paging parameter."""

    try:
        parsed = _parse_json_object(value.encode("utf-8"))
    except (ValueError, UnicodeError):
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR") from None
    if (
        set(parsed) != {"name", "value"}
        or parsed.get("name") not in _PAGE_PARAMETER_KEYS
        or not isinstance(parsed.get("value"), str)
        or not 1 <= len(parsed["value"]) <= 8_192
        or any(char in parsed["value"] for char in "\r\n\x00")
    ):
        raise GraphApiFailure("INVALID_PROVIDER_CURSOR")
    return {"name": parsed["name"], "value": parsed["value"]}


def _graph_opaque_id(value: Any, field: str) -> str:
    """Validate an opaque Graph resource ID without URL or query syntax."""

    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or re.fullmatch(r"[A-Za-z0-9._!~+=-]{1,512}", value) is None
    ):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return value


def _uuid_canonical(value: Any) -> str:
    """Require one canonical lowercase UUID for idempotency identifiers."""

    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise GraphApiFailure("INVALID_ARGUMENTS")
    try:
        normalized = str(uuid.UUID(value))
    except ValueError:
        raise GraphApiFailure("INVALID_ARGUMENTS") from None
    if normalized != value:
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return normalized


def _utc_datetime_with_offset(value: Any, field: str) -> str:
    """Normalize an explicit-offset ISO timestamp to a UTC RFC3339 string."""

    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise GraphApiFailure("INVALID_ARGUMENTS")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise GraphApiFailure("INVALID_ARGUMENTS") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _graph_date_time(value: str) -> dict[str, str]:
    """Encode a normalized instant in Graph's explicit UTC date-time shape."""

    normalized = _utc_datetime_with_offset(value, "date_time")
    return {"dateTime": normalized.removesuffix("Z"), "timeZone": "UTC"}


def _graph_datetime_projection(value: Any) -> str:
    """Return Graph date-time metadata in UTC without exposing event content."""

    if not isinstance(value, Mapping):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    date_time = value.get("dateTime")
    time_zone = value.get("timeZone")
    if not isinstance(date_time, str) or not isinstance(time_zone, str):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    if time_zone.casefold() != "utc":
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    try:
        parsed = datetime.fromisoformat(date_time.replace("Z", "+00:00"))
    except ValueError:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _todo_importance(value: Any) -> str:
    """Validate the To Do importance enum accepted by Microsoft Graph."""

    if value not in {"low", "normal", "high"}:
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return value


def _todo_list_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project only identity and display metadata for one task list."""

    list_id = value.get("id")
    if not isinstance(list_id, str) or not 1 <= len(list_id) <= 512:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    well_known = value.get("wellknownListName")
    if well_known is not None and not isinstance(well_known, str):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "task_list_id": list_id,
        "display_name": _bounded_text(value.get("displayName"), 255),
        "well_known_list_name": _bounded_text(well_known, 80),
        "etag": _bounded_text(value.get("@odata.etag"), 1024),
    }


def _todo_task_projection(value: Mapping[str, Any], task_list_id: str) -> dict[str, Any]:
    """Project To Do task status and dates without notes or checklist content."""

    task_id = value.get("id")
    status = value.get("status")
    importance = value.get("importance")
    valid_status = {"notStarted", "inProgress", "completed", "waitingOnOthers", "deferred"}
    if (
        not isinstance(task_id, str)
        or not 1 <= len(task_id) <= 512
        or status not in valid_status
        or importance not in {"low", "normal", "high"}
    ):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "task_id": task_id,
        "task_list_id": task_list_id,
        "title": _bounded_text(value.get("title"), 255),
        "status": status,
        "due_date_time": _todo_datetime_projection(value.get("dueDateTime")),
        "completed_date_time": _todo_datetime_projection(value.get("completedDateTime")),
        "importance": importance,
        "etag": _bounded_text(value.get("@odata.etag"), 1024),
    }


def _todo_datetime_projection(value: Any) -> dict[str, str] | None:
    """Retain To Do's date and timezone pair without inventing conversions."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    date_time = value.get("dateTime")
    time_zone = value.get("timeZone")
    if (
        not isinstance(date_time, str)
        or len(date_time) > 64
        or not isinstance(time_zone, str)
        or len(time_zone) > 128
    ):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return {"date_time": date_time, "time_zone": time_zone}


def _calendar_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project calendar name and identifier without owner contact details."""

    calendar_id = value.get("id")
    if not isinstance(calendar_id, str) or not 1 <= len(calendar_id) <= 512:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    owner = value.get("owner")
    owner_name = owner.get("name") if isinstance(owner, Mapping) else None
    return {
        "calendar_id": calendar_id,
        "name": _bounded_text(value.get("name"), 255),
        "owner_name": _bounded_text(owner_name, 255),
        "can_edit": value.get("canEdit") if isinstance(value.get("canEdit"), bool) else None,
    }


def _event_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project meeting metadata and ETag without body, attendee identities, or files."""

    event_id = value.get("id")
    attendees = value.get("attendees")
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 512:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    if not isinstance(attendees, list) or len(attendees) > 500:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    location = value.get("location")
    if location is not None and not isinstance(location, Mapping):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "event_id": event_id,
        "subject": _bounded_text(value.get("subject"), 255),
        "start_date_time": _graph_datetime_projection(value.get("start")),
        "end_date_time": _graph_datetime_projection(value.get("end")),
        "is_all_day": value.get("isAllDay") if isinstance(value.get("isAllDay"), bool) else None,
        "show_as": _bounded_text(value.get("showAs"), 40),
        "location": _bounded_text(location.get("displayName"), 255)
        if isinstance(location, Mapping)
        else None,
        "web_url": _bounded_text(value.get("webLink"), 2048),
        "transaction_id": _bounded_text(value.get("transactionId"), 64),
        "etag": _bounded_text(value.get("@odata.etag"), 1024),
        "has_attendees": bool(attendees),
    }


def _drive_projection(value: Mapping[str, Any], site_id: str) -> dict[str, Any]:
    """Project document-library metadata and the original Microsoft link."""

    drive_id = value.get("id")
    if not isinstance(drive_id, str) or not 1 <= len(drive_id) <= 512:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "site_id": site_id,
        "drive_id": drive_id,
        "name": _bounded_text(value.get("name"), 255),
        "drive_type": _bounded_text(value.get("driveType"), 40),
        "web_url": _bounded_text(value.get("webUrl"), 2048),
        "created_date_time": _bounded_text(value.get("createdDateTime"), 64),
        "last_modified_date_time": _bounded_text(value.get("lastModifiedDateTime"), 64),
    }


def _drive_item_projection(value: Mapping[str, Any], site_id: str, drive_id: str) -> dict[str, Any]:
    """Return selected drive-item metadata without a download/content endpoint."""

    item_id = value.get("id")
    parent = value.get("parentReference")
    if not isinstance(item_id, str) or not 1 <= len(item_id) <= 512:
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    if not isinstance(parent, Mapping) or parent.get("driveId") != drive_id:
        raise GraphApiFailure("RESOURCE_IDENTITY_MISMATCH")
    file_info = value.get("file")
    folder_info = value.get("folder")
    deleted = value.get("deleted")
    if (
        (file_info is not None and not isinstance(file_info, Mapping))
        or (folder_info is not None and not isinstance(folder_info, Mapping))
        or (deleted is not None and not isinstance(deleted, Mapping))
    ):
        raise GraphApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "site_id": site_id,
        "drive_id": drive_id,
        "item_id": item_id,
        "name": _bounded_text(value.get("name"), 255),
        "size": _bounded_int(value.get("size"), 0, 2**63 - 1),
        "etag": _bounded_text(value.get("eTag"), 1024),
        "created_date_time": _bounded_text(value.get("createdDateTime"), 64),
        "last_modified_date_time": _bounded_text(value.get("lastModifiedDateTime"), 64),
        "content_type": _bounded_text(file_info.get("mimeType"), 128)
        if isinstance(file_info, Mapping)
        else None,
        "is_folder": isinstance(folder_info, Mapping),
        "is_deleted": isinstance(deleted, Mapping),
        "web_url": _bounded_text(value.get("webUrl"), 2048),
        "parent_item_id": _bounded_text(parent.get("id"), 512),
    }


def _todo_task_changes(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a narrow To Do update and map names to Graph resource fields."""

    allowed = {"title", "due_date_time", "status", "importance"}
    if not value or set(value) - allowed:
        raise GraphApiFailure("INVALID_ARGUMENTS")
    result: dict[str, Any] = {}
    if "title" in value:
        result["title"] = _task_title(value["title"])
    if "due_date_time" in value:
        result["dueDateTime"] = _graph_date_time(value["due_date_time"])
    if "status" in value:
        status = value["status"]
        if status not in {"notStarted", "inProgress", "completed", "waitingOnOthers", "deferred"}:
            raise GraphApiFailure("INVALID_ARGUMENTS")
        result["status"] = status
    if "importance" in value:
        result["importance"] = _todo_importance(value["importance"])
    return result


def _todo_task_changes_match(raw: Mapping[str, Any], changes: Mapping[str, Any]) -> bool:
    """Compare only requested task fields after an ambiguous write."""

    for key, expected in changes.items():
        actual = raw.get(key)
        if key == "dueDateTime":
            if not _graph_todo_datetime_equal(actual, expected):
                return False
        elif actual != expected:
            return False
    return True


def _graph_todo_datetime_equal(actual: Any, expected: Any) -> bool:
    """Compare Graph dateTimeTimeZone values as exact UTC instants."""

    if not isinstance(actual, Mapping) or not isinstance(expected, Mapping):
        return actual is None and expected is None
    try:
        actual_time = datetime.fromisoformat(str(actual["dateTime"]).replace("Z", "+00:00"))
        expected_time = datetime.fromisoformat(str(expected["dateTime"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    actual_zone = actual.get("timeZone")
    expected_zone = expected.get("timeZone")
    if actual_zone != expected_zone:
        return False
    if actual_time.tzinfo is None:
        actual_time = actual_time.replace(tzinfo=UTC)
    if expected_time.tzinfo is None:
        expected_time = expected_time.replace(tzinfo=UTC)
    return actual_time.astimezone(UTC) == expected_time.astimezone(UTC)


def _event_changes(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate event updates while excluding attendees, body, and recurrence."""

    allowed = {"subject", "start_date_time", "end_date_time", "location"}
    if not value or set(value) - allowed:
        raise GraphApiFailure("INVALID_ARGUMENTS")
    result: dict[str, Any] = {}
    if "subject" in value:
        result["subject"] = _task_title(value["subject"])
    for field in ("start_date_time", "end_date_time"):
        if field in value:
            result["start" if field == "start_date_time" else "end"] = _graph_date_time(
                value[field]
            )
    if "location" in value:
        result["location"] = {"displayName": _bounded_input_text(value["location"], 255)}
    return result


def _event_changes_match(raw: Mapping[str, Any], changes: Mapping[str, Any]) -> bool:
    """Verify requested event values by readback, without exposing other fields."""

    for key, expected in changes.items():
        actual = raw.get(key)
        if key in {"start", "end"}:
            if not _graph_todo_datetime_equal(actual, expected):
                return False
        elif key == "location":
            if not isinstance(actual, Mapping) or actual.get("displayName") != expected.get(
                "displayName"
            ):
                return False
        elif actual != expected:
            return False
    return True


def _unknown_resource_write(
    resource_type: str, target_id: str, resource_id: str, reason: str
) -> dict[str, Any]:
    """Report an unverified mutation without claiming success or replaying it."""

    return {
        "status": "UNKNOWN",
        "resource_type": resource_type,
        "target_id": target_id,
        "resource_id": resource_id,
        "reconciliation": reason,
    }


def _schedule_ids(value: Any) -> list[str]:
    """Validate exact free/busy schedule IDs and normalize their comparison case."""

    if not isinstance(value, list) or any(
        not isinstance(item, str)
        or not 3 <= len(item) <= 320
        or re.fullmatch(r"[^\s@]+@[^\s@]+", item) is None
        for item in value
    ):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    normalized = [item.casefold() for item in value]
    if len(normalized) != len(set(normalized)):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return normalized


def _bounded_input_text(value: Any, maximum: int) -> str:
    """Require bounded, non-control text supplied as user-facing metadata."""

    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 for char in value)
    ):
        raise GraphApiFailure("INVALID_ARGUMENTS")
    return value.strip()


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
