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
from urllib.parse import quote, urlencode

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

    def list_repositories(self, token: str) -> dict[str, Any]:
        """List only binding-allowlisted repositories visible to the installation."""

        response = self._request(
            "GET", "/installation/repositories?per_page=100", token, None
        )
        value = _json_object(response)
        items = value.get("repositories")
        if not isinstance(items, list) or len(items) > 100:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        available: dict[str, Mapping[str, Any]] = {}
        for item in items:
            if isinstance(item, Mapping) and isinstance(item.get("id"), int):
                available[str(item["id"])] = item
        results = []
        for configured in self._settings.repositories:
            item = available.get(configured.repository_id)
            if item is None:
                continue
            self._verify_repository(item, configured)
            results.append(_repository_projection(item, configured))
        return {"repositories": results}

    def get_repository(self, repository: Repository, token: str) -> dict[str, Any]:
        """Read one repository and verify the configured numeric ID and slug."""

        path = self._repository_path(repository)
        value = _json_object(self._request("GET", path, token, None))
        self._verify_repository(value, repository)
        return _repository_projection(value, repository)

    def list_branches(self, repository: Repository, token: str) -> dict[str, Any]:
        """Return bounded branch names and commit SHAs for one allowlisted repo."""

        self._verify_configured_repository(repository, token)
        path = f"{self._repository_path(repository)}/branches?per_page=100"
        value = _json_array(self._request("GET", path, token, None))
        branches = []
        for item in value:
            commit = item.get("commit")
            name = item.get("name")
            sha = commit.get("sha") if isinstance(commit, Mapping) else None
            if not isinstance(name, str) or not isinstance(sha, str):
                raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
            branches.append({"name": name[:255], "commit_sha": sha[:64]})
        return {"branches": branches}

    def list_pull_requests(
        self, repository: Repository, state: str, token: str
    ) -> dict[str, Any]:
        """Read a bounded pull-request page with a fixed state query value."""

        self._verify_configured_repository(repository, token)
        query = urlencode({"state": state.lower(), "per_page": 100})
        path = f"{self._repository_path(repository)}/pulls?{query}"
        values = _json_array(self._request("GET", path, token, None))
        return {"pull_requests": [_pull_request_projection(item) for item in values]}

    def get_pull_request(
        self, repository: Repository, pull_number: int, token: str
    ) -> dict[str, Any]:
        """Read one pull request and project status and commit identifiers."""

        self._verify_configured_repository(repository, token)
        path = f"{self._repository_path(repository)}/pulls/{pull_number}"
        value = _json_object(self._request("GET", path, token, None))
        self._verify_repository(value, repository)
        result = _pull_request_projection(value)
        if result["number"] != pull_number:
            raise GitHubApiFailure("PULL_REQUEST_IDENTITY_MISMATCH")
        return {"pull_request": result}

    def list_workflow_runs(
        self,
        repository: Repository,
        token: str,
        *,
        branch: str | None = None,
        commit_sha: str | None = None,
    ) -> dict[str, Any]:
        """Read bounded Actions runs for an optional fixed branch or commit."""

        self._verify_configured_repository(repository, token)
        query: dict[str, str | int] = {"per_page": 100}
        if branch is not None:
            query["branch"] = branch
        if commit_sha is not None:
            query["head_sha"] = commit_sha
        path = f"{self._repository_path(repository)}/actions/runs?{urlencode(query)}"
        value = _json_object(self._request("GET", path, token, None))
        runs = value.get("workflow_runs")
        if not isinstance(runs, list) or len(runs) > 100:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        return {"workflow_runs": [_workflow_run_projection(item) for item in runs]}

    def get_ci_status(
        self, repository: Repository, commit_sha: str, token: str
    ) -> dict[str, Any]:
        """Combine commit status contexts and check-runs without hiding API errors."""

        self._verify_configured_repository(repository, token)
        quoted_sha = quote(commit_sha, safe="")
        status = _json_object(
            self._request(
                "GET",
                f"{self._repository_path(repository)}/commits/{quoted_sha}/status",
                token,
                None,
            )
        )
        checks = _json_object(
            self._request(
                "GET",
                f"{self._repository_path(repository)}/commits/{quoted_sha}/check-runs?per_page=100",
                token,
                None,
            )
        )
        contexts = status.get("statuses")
        check_runs = checks.get("check_runs")
        if not isinstance(contexts, list) or not isinstance(check_runs, list):
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        if len(contexts) > 100 or len(check_runs) > 100:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        projected_contexts = []
        for item in contexts:
            if not isinstance(item, Mapping) or not isinstance(item.get("context"), str):
                raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
            projected_contexts.append(
                {"context": item["context"][:256], "state": _check_state(item.get("state"))}
            )
        projected_checks = []
        for item in check_runs:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
            projected_checks.append(
                {
                    "name": item["name"][:256],
                    "status": _check_status(item.get("status")),
                    "conclusion": _optional_check_conclusion(item.get("conclusion")),
                }
            )
        state = status.get("state")
        if state not in {"pending", "success", "failure", "error"}:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        return {
            "commit_sha": commit_sha,
            "state": state.upper(),
            "statuses": projected_contexts,
            "check_runs": projected_checks,
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

    @staticmethod
    def _repository_path(repository: Repository) -> str:
        """Build a fixed repository API path from validated binding settings."""

        return f"/repos/{quote(repository.owner, safe='')}/{quote(repository.name, safe='')}"

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
        elif "full_name" in issue and (
            str(issue.get("id")) != repository.repository_id
            or issue.get("full_name") != repository.slug
        ):
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


def _json_array(response: HttpResponse) -> list[dict[str, Any]]:
    """Decode one bounded array response containing only JSON objects."""

    try:
        decoded = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE") from error
    if not isinstance(decoded, list) or any(not isinstance(item, dict) for item in decoded):
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    return decoded


def _repository_projection(
    value: Mapping[str, Any], repository: Repository
) -> dict[str, Any]:
    """Return the stable, minimal repository fields needed by the caller."""

    repository_id = value.get("id")
    full_name = value.get("full_name")
    private = value.get("private")
    if (
        str(repository_id) != repository.repository_id
        or full_name != repository.slug
        or not isinstance(private, bool)
    ):
        raise GitHubApiFailure("REPOSITORY_IDENTITY_MISMATCH")
    return {
        "repository_id": repository.repository_id,
        "repository": repository.slug,
        "private": private,
        "default_branch": value.get("default_branch")
        if isinstance(value.get("default_branch"), str)
        else None,
        "html_url": value.get("html_url") if isinstance(value.get("html_url"), str) else None,
    }


def _pull_request_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project one pull request while excluding arbitrary provider fields."""

    number = value.get("number")
    state = value.get("state")
    title = value.get("title")
    head = value.get("head")
    base = value.get("base")
    head_sha = head.get("sha") if isinstance(head, Mapping) else None
    base_ref = base.get("ref") if isinstance(base, Mapping) else None
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or number < 1
        or state not in {"open", "closed"}
        or not isinstance(title, str)
        or not isinstance(head_sha, str)
        or not isinstance(base_ref, str)
    ):
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "number": number,
        "state": state.upper(),
        "title": title[:500],
        "head_sha": head_sha[:64],
        "base_branch": base_ref[:255],
        "html_url": value.get("html_url") if isinstance(value.get("html_url"), str) else None,
        "draft": value.get("draft") if isinstance(value.get("draft"), bool) else False,
    }


def _workflow_run_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project one Actions run with bounded status and commit metadata."""

    run_id = value.get("id")
    name = value.get("name")
    status = value.get("status")
    conclusion = value.get("conclusion")
    head_sha = value.get("head_sha")
    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or not isinstance(name, str)
        or status
        not in {
            "queued",
            "in_progress",
            "completed",
            "waiting",
            "requested",
            "pending",
            "action_required",
        }
        or conclusion
        not in {
            None,
            "success",
            "failure",
            "neutral",
            "cancelled",
            "skipped",
            "timed_out",
            "action_required",
            "startup_failure",
            "stale",
        }
        or not isinstance(head_sha, str)
    ):
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "run_id": str(run_id),
        "name": name[:256],
        "status": status.upper(),
        "conclusion": conclusion.upper() if isinstance(conclusion, str) else None,
        "head_sha": head_sha[:64],
        "html_url": value.get("html_url") if isinstance(value.get("html_url"), str) else None,
        "updated_at": value.get("updated_at") if isinstance(value.get("updated_at"), str) else None,
    }


def _check_state(value: Any) -> str:
    """Validate one GitHub commit-status state."""

    if value not in {"pending", "success", "failure", "error"}:
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    return str(value).upper()


def _check_status(value: Any) -> str:
    """Validate one GitHub check-run status."""

    if value not in {
        "queued",
        "in_progress",
        "completed",
        "waiting",
        "requested",
        "pending",
        "action_required",
    }:
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    return str(value).upper()


def _optional_check_conclusion(value: Any) -> str | None:
    """Validate a nullable GitHub check conclusion."""

    allowed = {
        None,
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "startup_failure",
        "stale",
    }
    if value not in allowed:
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    return value.upper() if isinstance(value, str) else None


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
