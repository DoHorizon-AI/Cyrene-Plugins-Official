"""Fixed-host GitHub Issues REST transport.

中文:仅访问官方 GitHub Cloud API 的 Issues REST 传输层。
"""

from __future__ import annotations

import http.client
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .settings import ProviderSettings, Repository

GITHUB_HOST = "api.github.com"
GITHUB_API_VERSION = "2022-11-28"
MAX_RESPONSE_BYTES = 1_000_000


class GitHubApiFailure(RuntimeError):
    """A bounded failure safe to map into a public typed result."""

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
    """One bounded response from the official API host."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class GitHubApi:
    """Call fixed repository endpoints with one lease-scoped user token."""

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        transport: Any | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    def get_issue(self, repository: Repository, issue_number: int, token: str) -> dict[str, Any]:
        """Read one issue and verify its stable repository identity."""

        self._verify_configured_repository(repository, token)
        response = self._request("GET", self._issue_path(repository, issue_number), token, None)
        result = _json_object(response)
        self._verify_repository(result, repository)
        return _issue_projection(result, expected_number=issue_number)

    def create_comment(
        self,
        repository: Repository,
        issue_number: int,
        body: str,
        token: str,
    ) -> dict[str, Any]:
        """Create one comment without retrying an ambiguous write."""

        self._verify_configured_repository(repository, token)
        path = f"{self._issue_path(repository, issue_number)}/comments"
        response = self._request("POST", path, token, {"body": body}, write=True)
        result = _json_object(response)
        comment_id = result.get("id")
        returned_body = result.get("body")
        if not isinstance(comment_id, int) or not isinstance(returned_body, str):
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE", uncertain=True)
        return {
            "status": "CONFIRMED",
            "comment_id": str(comment_id),
            "issue_number": issue_number,
            "repository_id": repository.repository_id,
            "body": returned_body,
        }

    def update_state(
        self,
        repository: Repository,
        issue_number: int,
        state: str,
        token: str,
    ) -> dict[str, Any]:
        """Update OPEN/CLOSED state and read back the authoritative issue."""

        self._verify_configured_repository(repository, token)
        github_state = "open" if state == "OPEN" else "closed"
        path = self._issue_path(repository, issue_number)
        response = self._request("PATCH", path, token, {"state": github_state}, write=True)
        updated = _json_object(response)
        self._verify_repository(updated, repository)
        try:
            readback = _json_object(self._request("GET", path, token, None))
        except GitHubApiFailure as error:
            raise GitHubApiFailure("STATE_READBACK_UNAVAILABLE", uncertain=True) from error
        self._verify_repository(readback, repository)
        updated_state = readback.get("state")
        if updated_state != github_state:
            raise GitHubApiFailure("STATE_READBACK_MISMATCH", uncertain=True)
        return {
            "status": "CONFIRMED",
            "issue": _issue_projection(readback, expected_number=issue_number),
        }

    def _verify_configured_repository(self, repository: Repository, token: str) -> None:
        """Correlate stable repository ID and owner/name against GitHub."""

        path = f"/repos/{quote(repository.owner, safe='')}/{quote(repository.name, safe='')}"
        response = self._request("GET", path, token, None)
        value = _json_object(response)
        if (
            str(value.get("id")) != repository.repository_id
            or value.get("full_name") != repository.slug
        ):
            raise GitHubApiFailure("REPOSITORY_IDENTITY_MISMATCH")

    @staticmethod
    def _issue_path(repository: Repository, issue_number: int) -> str:
        """Build an API path only from validated configured repo values."""

        owner = quote(repository.owner, safe="")
        name = quote(repository.name, safe="")
        return f"/repos/{owner}/{name}/issues/{issue_number}"

    def _request(
        self,
        method: str,
        path: str,
        token: str,
        body: Mapping[str, Any] | None,
        *,
        write: bool = False,
    ) -> HttpResponse:
        if not token or "\r" in token or "\n" in token:
            raise GitHubApiFailure("AUTHORIZATION_UNAVAILABLE")
        payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "Cyrene-GitHub-Issues-Provider",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            if self._transport is not None:
                response = self._transport(method, GITHUB_HOST, path, headers, payload)
            else:
                response = self._send(method, path, headers, payload)
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise GitHubApiFailure(
                "UPSTREAM_UNCERTAIN" if write else "UPSTREAM_UNAVAILABLE",
                retryable=not write,
                uncertain=write,
            ) from error
        if not isinstance(response, HttpResponse):
            response = HttpResponse(
                int(response.status),
                {str(k).lower(): str(v) for k, v in response.headers.items()},
                bytes(response.body),
            )
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE", uncertain=write)
        if 200 <= response.status < 300:
            return response
        retry_after = _retry_after(response.headers)
        if response.status == 403 and _rate_limit_exhausted(response.headers):
            raise GitHubApiFailure("RATE_LIMITED", retryable=True, retry_after=retry_after)
        if response.status in {401, 403}:
            raise GitHubApiFailure("AUTHORIZATION_DENIED")
        if response.status == 404:
            raise GitHubApiFailure("RESOURCE_NOT_FOUND")
        if response.status == 429:
            raise GitHubApiFailure("RATE_LIMITED", retryable=True, retry_after=retry_after)
        if response.status >= 500:
            raise GitHubApiFailure(
                "UPSTREAM_UNCERTAIN" if write else "UPSTREAM_UNAVAILABLE",
                retryable=not write,
                retry_after=retry_after,
                uncertain=write,
            )
        raise GitHubApiFailure("UPSTREAM_REJECTED")

    def _send(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        payload: bytes | None,
    ) -> HttpResponse:
        connection = http.client.HTTPSConnection(
            GITHUB_HOST, timeout=self._settings.timeout_seconds
        )
        try:
            connection.request(method, path, body=payload, headers=dict(headers))
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            return HttpResponse(
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                body,
            )
        finally:
            connection.close()

    @staticmethod
    def _verify_repository(issue: Mapping[str, Any], repository: Repository) -> None:
        """Reject repository-ID/name mismatches in official API responses."""

        repo = issue.get("repository")
        if isinstance(repo, Mapping):
            repo_id = repo.get("id")
            full_name = repo.get("full_name")
            if str(repo_id) != repository.repository_id or full_name != repository.slug:
                raise GitHubApiFailure("REPOSITORY_IDENTITY_MISMATCH")
        # Issue GET/PATCH often omit nested repository; the API path was built
        # from configured owner/name and the lease is separately repo-ID bound.


def _json_object(response: HttpResponse) -> dict[str, Any]:
    """Decode one bounded successful JSON object without leaking body text."""

    try:
        decoded = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE") from error
    if not isinstance(decoded, dict):
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    return decoded


def _issue_projection(
    issue: Mapping[str, Any], *, expected_number: int | None = None
) -> dict[str, Any]:
    """Return an allowlisted issue projection rather than raw provider JSON."""

    number = issue.get("number")
    state = issue.get("state")
    issue_id = issue.get("id")
    if not isinstance(number, int) or state not in {"open", "closed"}:
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    if expected_number is not None and number != expected_number:
        raise GitHubApiFailure("ISSUE_IDENTITY_MISMATCH")
    if not isinstance(issue_id, int):
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "issue_id": str(issue_id),
        "number": number,
        "state": state.upper(),
        "title": issue.get("title") if isinstance(issue.get("title"), str) else "",
        "html_url": issue.get("html_url") if isinstance(issue.get("html_url"), str) else "",
    }


def _retry_after(headers: Mapping[str, str]) -> int | None:
    """Return a bounded integer Retry-After hint, when the API supplied one."""

    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None or not re.fullmatch(r"\d{1,6}", raw.strip()):
        return None
    return min(int(raw), 86_400)


def _rate_limit_exhausted(headers: Mapping[str, str]) -> bool:
    """Recognize GitHub's documented 403 rate-limit form without body parsing."""

    remaining = headers.get("x-ratelimit-remaining") or headers.get("X-RateLimit-Remaining")
    return remaining == "0"
