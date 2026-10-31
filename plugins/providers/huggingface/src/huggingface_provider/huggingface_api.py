"""Fixed-host Hugging Face Hub transport for metadata and small artifacts.

All repo IDs and paths are validated before request construction; auth headers
are never forwarded to storage redirects. 中文：只访问 HF 官方 Hub 与安全下载跳转。
"""

from __future__ import annotations

import base64
import http.client
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

from .settings import ProviderSettings

HUGGINGFACE_HOST = "huggingface.co"
MAX_RESPONSE_BYTES = 1_048_576
ROUTES = {"model": "models", "dataset": "datasets", "space": "spaces"}


class HuggingFaceApiFailure(RuntimeError):
    """Sanitized Hub failure with retry and uncertain-write metadata."""

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
    """One bounded HTTP response from a fixed Hub or trusted storage host."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class HuggingFaceApi:
    """Call fixed Hugging Face endpoints using lease-scoped bearer tokens."""

    def __init__(self, settings: ProviderSettings, *, transport: Any | None = None) -> None:
        self._settings = settings
        self._transport = transport

    def list_repositories(
        self,
        repo_type: str,
        token: str,
        *,
        search: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List public Hub results while suppressing private repository metadata."""

        query: dict[str, str | int] = {"limit": limit}
        if search:
            query["search"] = search
        route = _route(repo_type)
        values = _json_array(self._request("GET", f"/api/{route}?{urlencode(query)}", token))
        results = []
        for item in values:
            repo_id = item.get("id") or item.get("modelId")
            if not isinstance(repo_id, str) or not repo_id:
                raise HuggingFaceApiFailure("INVALID_PROVIDER_RESPONSE")
            if item.get("private") is True:
                continue
            results.append(_repository_projection(item, repo_id=repo_id))
        return {"repositories": results}

    def get_repository(self, repo_type: str, repo_id: str, token: str) -> dict[str, Any]:
        """Read one Hub model, dataset, or Space and confirm its identity."""

        value = _json_object(
            self._request("GET", f"/api/{_route(repo_type)}/{_repo_path(repo_id)}", token)
        )
        returned_id = value.get("id") or value.get("modelId")
        if returned_id != repo_id:
            raise HuggingFaceApiFailure("RESOURCE_IDENTITY_MISMATCH")
        return _repository_projection(value, repo_id=repo_id, include_file_count=True)

    def download_file(
        self,
        repo_id: str,
        revision: str,
        path: str,
        token: str | None,
    ) -> dict[str, Any]:
        """Download one bounded file without forwarding bearer auth to CDN hosts."""

        repo_path = _repo_path(repo_id)
        filename = _path(path)
        revision_path = quote(_revision(revision), safe="")
        endpoint = f"/{repo_path}/resolve/{revision_path}/{filename}"
        response = self._request_file(endpoint, token)
        if len(response.body) > self._settings.max_artifact_bytes:
            raise HuggingFaceApiFailure("ARTIFACT_TOO_LARGE")
        return {
            "repo_id": repo_id,
            "revision": revision,
            "path": path,
            "byte_length": len(response.body),
            "content_base64": base64.b64encode(response.body).decode("ascii"),
            "content_type": _header(response.headers, "content-type") or "application/octet-stream",
        }

    def upload_file(
        self,
        repo_type: str,
        repo_id: str,
        revision: str,
        path: str,
        content: bytes,
        token: str,
    ) -> dict[str, Any]:
        """Upload one small file once and confirm its path exists afterward."""

        if len(content) > self._settings.max_artifact_bytes:
            raise HuggingFaceApiFailure("ARTIFACT_TOO_LARGE")
        route = _route(repo_type)
        encoded_repo = _repo_path(repo_id)
        encoded_revision = quote(_revision(revision), safe="")
        encoded_path = _path(path)
        endpoint = f"/api/{route}/{encoded_repo}/upload/{encoded_revision}/{encoded_path}"
        response = self._request("PUT", endpoint, token, body=content, write=True)
        receipt: dict[str, Any] = {}
        if response.body:
            try:
                value = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise HuggingFaceApiFailure("WRITE_READBACK_UNAVAILABLE", uncertain=True) from None
            if not isinstance(value, dict):
                raise HuggingFaceApiFailure("INVALID_PROVIDER_RESPONSE", uncertain=True)
            commit_url = value.get("commitUrl") or value.get("commit_url")
            if isinstance(commit_url, str):
                receipt["commit_url"] = commit_url
        if not self._file_exists(route, repo_id, revision, path, token):
            raise HuggingFaceApiFailure("WRITE_READBACK_MISMATCH", uncertain=True)
        return {
            "status": "CONFIRMED",
            "repo_type": repo_type,
            "repo_id": repo_id,
            "revision": revision,
            "path": path,
            "byte_length": len(content),
            **receipt,
        }

    def _file_exists(self, route: str, repo_id: str, revision: str, path: str, token: str) -> bool:
        """Read a bounded parent tree and confirm the uploaded path exactly."""

        parent, _, filename = path.rpartition("/")
        base = f"/api/{route}/{_repo_path(repo_id)}/tree/{quote(_revision(revision), safe='')}"
        if parent:
            base += f"/{_path(parent)}"
        try:
            response = self._request("GET", base + "?recursive=false&expand=false&limit=100", token)
            entries = _json_array(response)
        except HuggingFaceApiFailure as error:
            if error.code == "RESOURCE_NOT_FOUND":
                return False
            raise HuggingFaceApiFailure("WRITE_READBACK_UNAVAILABLE", uncertain=True) from None
        for entry in entries:
            candidate = entry.get("path")
            if isinstance(candidate, str) and (
                candidate in (path, filename) or candidate.endswith(f"/{filename}")
            ):
                return True
        return False

    def _request_file(self, path: str, token: str | None) -> HttpResponse:
        """Fetch a Hub resolve URL, retaining auth only on exact Hub-origin hops."""

        current_url = urlunsplit(("https", HUGGINGFACE_HOST, path, "", ""))
        current_host = HUGGINGFACE_HOST
        current_token = token
        response = self._request_raw("GET", HUGGINGFACE_HOST, path, token, None)
        if 200 <= response.status < 300:
            return _bounded(response, self._settings.max_artifact_bytes)
        redirects = 0
        while response.status in {301, 302, 303, 307, 308}:
            if redirects >= 3:
                raise HuggingFaceApiFailure("UPSTREAM_REJECTED")
            location = _header(response.headers, "location")
            if not location:
                raise HuggingFaceApiFailure("INVALID_PROVIDER_RESPONSE")
            if location.strip() != location or any(ord(character) < 32 for character in location):
                raise HuggingFaceApiFailure("INVALID_PROVIDER_RESPONSE")
            try:
                resolved_url = urljoin(current_url, location)
                parsed = urlsplit(resolved_url)
                port = parsed.port
            except ValueError:
                raise HuggingFaceApiFailure("UNTRUSTED_DOWNLOAD_REDIRECT") from None
            host = parsed.hostname or ""
            if (
                parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
                or port not in (None, 443)
                or not _trusted_download_host(host)
            ):
                raise HuggingFaceApiFailure("UNTRUSTED_DOWNLOAD_REDIRECT")
            target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            # Drop auth permanently after a cross-origin hop; CDN URLs authorize themselves.
            redirect_token = (
                current_token
                if current_host == HUGGINGFACE_HOST and host == HUGGINGFACE_HOST
                else None
            )
            response = self._request_raw("GET", host, target, redirect_token, None)
            current_host = host
            current_token = redirect_token
            current_url = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
            )
            redirects += 1
        self._raise_for_status(response, write=False)
        return _bounded(response, self._settings.max_artifact_bytes)

    def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        body: bytes | None = None,
        write: bool = False,
    ) -> HttpResponse:
        """Call the fixed Hub origin once and sanitize all provider failures."""

        response = self._request_raw(method, HUGGINGFACE_HOST, path, token, body)
        self._raise_for_status(response, write=write)
        return _bounded(response, MAX_RESPONSE_BYTES)

    def _request_raw(
        self,
        method: str,
        host: str,
        path: str,
        token: str | None,
        body: bytes | None,
    ) -> HttpResponse:
        """Send one request with an optional private Hub bearer token."""

        headers = {
            "Accept": "application/json, application/octet-stream;q=0.9, */*;q=0.8",
            "User-Agent": "Cyrene-HuggingFace-Provider",
        }
        if token is not None:
            if not token or "\r" in token or "\n" in token:
                raise HuggingFaceApiFailure("AUTHORIZATION_UNAVAILABLE")
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/octet-stream"
        try:
            if self._transport is not None:
                response = self._transport(method, host, path, headers, body)
            else:
                connection = http.client.HTTPSConnection(
                    host, timeout=self._settings.timeout_seconds
                )
                try:
                    connection.request(method, path, body=body, headers=headers)
                    reply = connection.getresponse()
                    response = HttpResponse(
                        reply.status,
                        {name.lower(): value for name, value in reply.getheaders()},
                        reply.read(self._settings.max_artifact_bytes + 1),
                    )
                finally:
                    connection.close()
        except (OSError, TimeoutError, http.client.HTTPException):
            raise HuggingFaceApiFailure(
                (
                    "UPSTREAM_UNCERTAIN"
                    if method in {"POST", "PUT", "PATCH", "DELETE"}
                    else "UPSTREAM_UNAVAILABLE"
                ),
                retryable=method == "GET",
                uncertain=method in {"POST", "PUT", "PATCH", "DELETE"},
            ) from None
        if not isinstance(response, HttpResponse):
            response = HttpResponse(
                int(response.status),
                {str(key).lower(): str(value) for key, value in response.headers.items()},
                bytes(response.body),
            )
        return response

    @staticmethod
    def _raise_for_status(response: HttpResponse, *, write: bool) -> None:
        """Map one non-success response into a closed error code."""

        if 200 <= response.status < 300:
            return
        retry_after = _retry_after(response.headers)
        if response.status == 401:
            raise HuggingFaceApiFailure("AUTHENTICATION_FAILED")
        if response.status == 403:
            raise HuggingFaceApiFailure("AUTHORIZATION_DENIED")
        if response.status == 404:
            raise HuggingFaceApiFailure("RESOURCE_NOT_FOUND")
        if response.status == 429:
            raise HuggingFaceApiFailure(
                "RATE_LIMITED",
                retryable=True,
                retry_after=retry_after,
            )
        if response.status == 503:
            raise HuggingFaceApiFailure(
                "UPSTREAM_UNCERTAIN" if write else "UPSTREAM_UNAVAILABLE",
                retryable=not write,
                retry_after=retry_after,
                uncertain=write,
            )
        if response.status >= 500:
            raise HuggingFaceApiFailure(
                "UPSTREAM_UNCERTAIN" if write else "UPSTREAM_UNAVAILABLE",
                retryable=not write,
                retry_after=retry_after,
                uncertain=write,
            )
        raise HuggingFaceApiFailure("UPSTREAM_REJECTED", uncertain=write)


def _route(repo_type: str) -> str:
    """Resolve an allowed repository type to its fixed Hub API route."""

    try:
        return ROUTES[repo_type]
    except KeyError:
        raise HuggingFaceApiFailure("INVALID_ARGUMENTS") from None


def _repo_path(repo_id: str) -> str:
    """Encode a validated owner/name pair without preserving delimiters."""

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo_id):
        raise HuggingFaceApiFailure("INVALID_ARGUMENTS")
    return "/".join(quote(part, safe="") for part in repo_id.split("/"))


def _revision(value: str) -> str:
    """Validate a branch, tag, or immutable commit selector."""

    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise HuggingFaceApiFailure("INVALID_ARGUMENTS")
    return value


def _path(value: str) -> str:
    """Validate and URL-encode a repository path one segment at a time."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise HuggingFaceApiFailure("INVALID_ARGUMENTS")
    return "/".join(quote(part, safe="") for part in value.split("/"))


def _json_object(response: HttpResponse) -> dict[str, Any]:
    """Decode a bounded JSON object without leaking raw provider response text."""

    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HuggingFaceApiFailure("INVALID_PROVIDER_RESPONSE") from None
    if not isinstance(value, dict):
        raise HuggingFaceApiFailure("INVALID_PROVIDER_RESPONSE")
    return value


def _json_array(response: HttpResponse) -> list[dict[str, Any]]:
    """Decode a list of bounded JSON object records."""

    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HuggingFaceApiFailure("INVALID_PROVIDER_RESPONSE") from None
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise HuggingFaceApiFailure("INVALID_PROVIDER_RESPONSE")
    return value


def _repository_projection(
    value: Mapping[str, Any], *, repo_id: str, include_file_count: bool = False
) -> dict[str, Any]:
    """Return stable repository metadata and an optional Hub-reported sibling count.

    Missing or malformed `siblings` metadata stays unknown (`None`) for `repo.get`;
    it is never reported as an empty repository. Search results keep their existing shape.
    中文：仅在 repo.get 响应中投影文件数；Hub 未提供 siblings 时返回未知，不伪报为 0。
    """

    returned_id = value.get("id") or value.get("modelId")
    if returned_id != repo_id:
        raise HuggingFaceApiFailure("RESOURCE_IDENTITY_MISMATCH")
    result = {
        "repo_id": repo_id,
        "private": value.get("private") if isinstance(value.get("private"), bool) else False,
        "gated": value.get("gated") if isinstance(value.get("gated"), (bool, str)) else False,
        "sha": value.get("sha") if isinstance(value.get("sha"), str) else None,
        "last_modified": (
            value.get("lastModified") if isinstance(value.get("lastModified"), str) else None
        ),
        "downloads": value.get("downloads") if isinstance(value.get("downloads"), int) else None,
        "likes": value.get("likes") if isinstance(value.get("likes"), int) else None,
        "tags": _string_list(value.get("tags")),
        "pipeline_tag": (
            value.get("pipeline_tag") if isinstance(value.get("pipeline_tag"), str) else None
        ),
    }
    if include_file_count:
        siblings = value.get("siblings")
        result["file_count"] = len(siblings) if isinstance(siblings, list) else None
    return result


def _string_list(value: Any) -> list[str]:
    """Project at most one hundred short string tags."""

    if not isinstance(value, list):
        return []
    return [item[:100] for item in value[:100] if isinstance(item, str)]


def _bounded(response: HttpResponse, maximum: int) -> HttpResponse:
    """Reject oversized bodies before parsing or returning them."""

    if len(response.body) > maximum:
        raise HuggingFaceApiFailure("RESPONSE_TOO_LARGE")
    return response


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Read one response header case-insensitively."""

    desired = name.lower()
    for key, value in headers.items():
        if key.lower() == desired:
            return value
    return None


def _retry_after(headers: Mapping[str, str]) -> int | None:
    """Return a bounded integer retry hint."""

    raw = _header(headers, "retry-after")
    if raw is None or not raw.isdecimal() or len(raw) > 6:
        return None
    return min(int(raw), 86_400)


def _trusted_download_host(host: str) -> bool:
    """Accept only Hugging Face owned Hub, CDN, and Xet download hosts."""

    name = host.lower().rstrip(".")
    return (
        name == "huggingface.co"
        or name.endswith(".huggingface.co")
        or name == "hf.co"
        or name.endswith(".hf.co")
    )
