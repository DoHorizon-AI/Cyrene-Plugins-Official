"""Fixed-host GitHub Issues REST transport.

中文:仅访问官方 GitHub Cloud API 的 Issues REST 传输层。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .settings import ProviderSettings, Repository

GITHUB_HOST = "api.github.com"
GITHUB_API_VERSION = "2022-11-28"
MAX_RESPONSE_BYTES = 1_000_000
MAX_ACTIONS_LOG_BYTES = 2_000_000
MAX_ACTIONS_LOG_EXCERPT_BYTES = 20_000
MAX_PULL_REQUEST_PAGE_SIZE = 20
MAX_PULL_REQUEST_PAGE_NUMBER = 1000
MAX_PULL_REQUEST_BATCH_PAGES = 5
_ACTIONS_LOG_HOST = re.compile(r"productionresultssa[0-9]{1,3}\.blob\.core\.windows\.net\Z")
TASK_BRANCH_PREFIX = "cyrene/assistant/"


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

    def get_branch(self, repository: Repository, branch: str, token: str) -> dict[str, Any]:
        """Read back one branch with an explicit absence result."""

        self._verify_configured_repository(repository, token)
        _require_branch_name(branch)
        commit_sha = self._read_ref_sha(repository, branch, token)
        return {
            "branch": branch,
            "exists": commit_sha is not None,
            "commit_sha": commit_sha,
        }

    def get_file(
        self, repository: Repository, branch: str, path: str, token: str
    ) -> dict[str, Any]:
        """Read bounded file metadata and an excerpt for exact write reconciliation."""

        self._verify_configured_repository(repository, token)
        _require_branch_name(branch)
        _require_safe_file_path(path)
        result = self._read_file(repository, branch, path, token)
        if result is None:
            return {
                "branch": branch,
                "path": path,
                "exists": False,
                "blob_sha": None,
                "size_bytes": 0,
                "content_excerpt": "",
                "content_truncated": False,
            }
        blob_sha, content = result
        text = content.decode("utf-8", errors="replace")
        excerpt = text[:16_000]
        return {
            "branch": branch,
            "path": path,
            "exists": True,
            "blob_sha": blob_sha,
            "size_bytes": len(content),
            "content_excerpt": excerpt,
            "content_truncated": len(text) > len(excerpt),
        }

    def create_branch(
        self, repository: Repository, branch: str, from_sha: str, token: str
    ) -> dict[str, Any]:
        """Create a task branch and reconcile the ref after every ambiguous outcome."""

        default_branch = self._verify_configured_repository(repository, token)
        _require_task_branch(branch)
        _reject_default_branch(branch, default_branch)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", from_sha):
            raise GitHubApiFailure("INVALID_ARGUMENTS")
        commit = _json_object(
            self._request(
                "GET",
                f"{self._repository_path(repository)}/commits/{quote(from_sha, safe='')}",
                token,
                None,
            )
        )
        if str(commit.get("sha", "")).lower() != from_sha.lower():
            raise GitHubApiFailure("COMMIT_IDENTITY_MISMATCH")
        current_sha = self._read_ref_sha(repository, branch, token)
        if current_sha is not None:
            if current_sha.lower() == from_sha.lower():
                return _branch_write_result(branch, current_sha, already_exists=True)
            raise GitHubApiFailure("BRANCH_ALREADY_EXISTS")
        path = f"{self._repository_path(repository)}/git/refs"
        try:
            response = self._request(
                "POST",
                path,
                token,
                {"ref": f"refs/heads/{branch}", "sha": from_sha},
                write=True,
            )
            value = _json_object(response)
            object_value = value.get("object")
            returned_ref = value.get("ref")
            returned_sha = object_value.get("sha") if isinstance(object_value, Mapping) else None
            if returned_ref != f"refs/heads/{branch}" or returned_sha != from_sha:
                raise GitHubApiFailure("BRANCH_READBACK_MISMATCH", uncertain=True)
        except GitHubApiFailure as error:
            if not error.uncertain:
                raise
            try:
                reconciled_sha = self._read_ref_sha(repository, branch, token)
            except GitHubApiFailure as readback_error:
                raise GitHubApiFailure(
                    "WRITE_OUTCOME_UNCONFIRMED", uncertain=True
                ) from readback_error
            if reconciled_sha is not None and reconciled_sha.lower() == from_sha.lower():
                return _branch_write_result(branch, reconciled_sha, reconciled=True)
            raise
        try:
            readback_sha = self._read_ref_sha(repository, branch, token)
        except GitHubApiFailure as readback_error:
            raise GitHubApiFailure("WRITE_OUTCOME_UNCONFIRMED", uncertain=True) from readback_error
        if readback_sha is None or readback_sha.lower() != from_sha.lower():
            raise GitHubApiFailure("BRANCH_READBACK_MISMATCH", uncertain=True)
        return _branch_write_result(branch, readback_sha)

    def delete_branch(
        self, repository: Repository, branch: str, expected_sha: str, token: str
    ) -> dict[str, Any]:
        """Delete a task ref after a final SHA check and read back its absence.

        GitHub's ref-delete API has no atomic expected-SHA parameter, so the
        caller must serialize this branch's full lifecycle, including ref
        writes and pull-request opening/closing, during deletion. This host
        lock cannot serialize manual changes made directly in GitHub.
        """

        default_branch = self._verify_configured_repository(repository, token)
        _require_task_branch(branch)
        _reject_default_branch(branch, default_branch)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_sha):
            raise GitHubApiFailure("INVALID_ARGUMENTS")
        current_sha = self._read_ref_sha(repository, branch, token)
        if current_sha is None:
            return {
                "status": "CONFIRMED",
                "branch": branch,
                "deleted": True,
                "already_absent": True,
                "verified": True,
            }
        if current_sha.lower() != expected_sha.lower():
            raise GitHubApiFailure("BRANCH_HEAD_MISMATCH")
        if self._branch_has_open_pull_request(repository, branch, token):
            raise GitHubApiFailure("BRANCH_HAS_OPEN_PULL_REQUEST")
        current_default_branch = self._verify_configured_repository(repository, token)
        _reject_default_branch(branch, current_default_branch)
        final_sha = self._read_ref_sha(repository, branch, token)
        if final_sha is None:
            return {
                "status": "CONFIRMED",
                "branch": branch,
                "deleted": True,
                "already_absent": True,
                "verified": True,
            }
        if final_sha.lower() != expected_sha.lower():
            raise GitHubApiFailure("BRANCH_HEAD_MISMATCH")
        path = f"{self._repository_path(repository)}/git/refs/heads/{quote(branch, safe='')}"
        try:
            self._request("DELETE", path, token, None, write=True)
        except GitHubApiFailure as error:
            if not error.uncertain:
                raise
            try:
                reconciled_sha = self._read_ref_sha(repository, branch, token)
            except GitHubApiFailure as readback_error:
                raise GitHubApiFailure(
                    "WRITE_OUTCOME_UNCONFIRMED", uncertain=True
                ) from readback_error
            if reconciled_sha is None:
                return {
                    "status": "CONFIRMED",
                    "branch": branch,
                    "deleted": True,
                    "already_absent": False,
                    "verified": True,
                }
            raise
        try:
            readback_sha = self._read_ref_sha(repository, branch, token)
        except GitHubApiFailure as readback_error:
            raise GitHubApiFailure("WRITE_OUTCOME_UNCONFIRMED", uncertain=True) from readback_error
        if readback_sha is None:
            return {
                "status": "CONFIRMED",
                "branch": branch,
                "deleted": True,
                "already_absent": False,
                "verified": True,
            }
        raise GitHubApiFailure(
            "BRANCH_DELETE_READBACK_MISMATCH" if readback_sha.lower() != expected_sha.lower()
            else "UPSTREAM_UNCERTAIN",
            uncertain=True,
        )

    def commit_file(
        self,
        repository: Repository,
        branch: str,
        path: str,
        message: str,
        content_base64: str,
        expected_sha: str | None,
        token: str,
    ) -> dict[str, Any]:
        """Create or compare-and-update one bounded source file on a task branch."""

        default_branch = self._verify_configured_repository(repository, token)
        _require_task_branch(branch)
        _reject_default_branch(branch, default_branch)
        _require_safe_file_path(path)
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (ValueError, binascii.Error) as error:
            raise GitHubApiFailure("INVALID_ARGUMENTS") from error
        if len(content) > 500_000:
            raise GitHubApiFailure("INVALID_ARGUMENTS")
        if expected_sha is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", expected_sha):
            raise GitHubApiFailure("INVALID_ARGUMENTS")
        expected_blob_sha = _git_blob_sha(content)
        existing = self._read_file(repository, branch, path, token)
        if existing is not None:
            existing_sha, _ = existing
            if existing_sha.lower() == expected_blob_sha:
                return _file_write_result(branch, path, existing_sha, None, already_applied=True)
            if expected_sha is None:
                raise GitHubApiFailure("FILE_ALREADY_EXISTS")
            if existing_sha.lower() != expected_sha.lower():
                raise GitHubApiFailure("FILE_PRECONDITION_FAILED")
        elif expected_sha is not None:
            raise GitHubApiFailure("FILE_PRECONDITION_FAILED")

        encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
        endpoint = f"{self._repository_path(repository)}/contents/{encoded_path}"
        payload: dict[str, Any] = {
            "message": message,
            "content": content_base64,
            "branch": branch,
        }
        if existing is not None:
            payload["sha"] = existing[0]
        try:
            response = self._request("PUT", endpoint, token, payload, write=True)
            value = _json_object(response)
            returned_content = value.get("content")
            commit = value.get("commit")
            response_blob_sha = (
                returned_content.get("sha") if isinstance(returned_content, Mapping) else None
            )
            commit_sha = commit.get("sha") if isinstance(commit, Mapping) else None
            if (
                not isinstance(response_blob_sha, str)
                or response_blob_sha.lower() != expected_blob_sha
                or not isinstance(commit_sha, str)
                or not re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha)
            ):
                raise GitHubApiFailure("FILE_WRITE_RESPONSE_INVALID", uncertain=True)
        except GitHubApiFailure as error:
            if not error.uncertain:
                raise
            try:
                reconciled = self._read_file(repository, branch, path, token)
            except GitHubApiFailure as readback_error:
                raise GitHubApiFailure(
                    "WRITE_OUTCOME_UNCONFIRMED", uncertain=True
                ) from readback_error
            if reconciled is not None and reconciled[0].lower() == expected_blob_sha:
                return _file_write_result(branch, path, reconciled[0], None, reconciled=True)
            raise
        try:
            readback = self._read_file(repository, branch, path, token)
        except GitHubApiFailure as readback_error:
            raise GitHubApiFailure("WRITE_OUTCOME_UNCONFIRMED", uncertain=True) from readback_error
        if readback is None or readback[0].lower() != expected_blob_sha:
            raise GitHubApiFailure("FILE_READBACK_MISMATCH", uncertain=True)
        return _file_write_result(branch, path, readback[0], commit_sha)

    def create_draft_pull_request(
        self,
        repository: Repository,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        token: str,
    ) -> dict[str, Any]:
        """Create a task-branch draft PR, reconcile duplicates, and read back its state."""

        default_branch = self._verify_configured_repository(repository, token)
        _require_task_branch(head_branch)
        _reject_default_branch(head_branch, default_branch)
        _require_branch_name(base_branch)
        if head_branch == base_branch:
            raise GitHubApiFailure("INVALID_ARGUMENTS")
        head_sha = self._read_ref_sha(repository, head_branch, token)
        if head_sha is None:
            raise GitHubApiFailure("RESOURCE_NOT_FOUND")
        if self._read_ref_sha(repository, base_branch, token) is None:
            raise GitHubApiFailure("RESOURCE_NOT_FOUND")
        existing = self._find_pull_request(repository, head_branch, base_branch, token)
        if existing is not None:
            if existing.get("draft") is not True:
                raise GitHubApiFailure("PULL_REQUEST_ALREADY_OPEN")
            if (
                existing["title"] != title
                or existing["body_sha256"] != _text_sha256(body)
            ):
                raise GitHubApiFailure("PULL_REQUEST_ALREADY_OPEN")
            return _draft_pull_request_result(existing, already_exists=True)

        try:
            response = self._request(
                "POST",
                f"{self._repository_path(repository)}/pulls",
                token,
                {
                    "title": title,
                    "body": body,
                    "head": head_branch,
                    "base": base_branch,
                    "draft": True,
                },
                write=True,
            )
            created = _json_object(response)
            self._verify_pull_request_repositories(created, repository)
            created_projection = _pull_request_projection(created)
            if (
                created_projection.get("head_branch") != head_branch
                or created_projection.get("base_branch") != base_branch
                or created_projection["draft"] is not True
            ):
                raise GitHubApiFailure("PULL_REQUEST_CREATE_MISMATCH", uncertain=True)
            pull_number = created_projection["number"]
        except GitHubApiFailure as error:
            if not error.uncertain:
                raise
            try:
                reconciled = self._find_pull_request(
                    repository, head_branch, base_branch, token
                )
            except GitHubApiFailure as readback_error:
                raise GitHubApiFailure(
                    "WRITE_OUTCOME_UNCONFIRMED", uncertain=True
                ) from readback_error
            if (
                reconciled is not None
                and reconciled.get("draft") is True
                and reconciled["head_sha"].lower() == head_sha.lower()
                and reconciled["title"] == title
                and reconciled["body_sha256"] == _text_sha256(body)
            ):
                return _draft_pull_request_result(reconciled, reconciled=True)
            raise
        try:
            readback = self.get_pull_request(repository, pull_number, token)["pull_request"]
        except GitHubApiFailure as readback_error:
            raise GitHubApiFailure("WRITE_OUTCOME_UNCONFIRMED", uncertain=True) from readback_error
        if (
            readback.get("head_branch") != head_branch
            or readback.get("base_branch") != base_branch
            or readback.get("draft") is not True
            or readback.get("head_sha", "").lower() != head_sha.lower()
            or readback.get("title") != title
            or readback.get("body_sha256") != _text_sha256(body)
        ):
            raise GitHubApiFailure("PULL_REQUEST_READBACK_MISMATCH", uncertain=True)
        return _draft_pull_request_result(readback)

    def list_pull_requests(
        self, repository: Repository, state: str, token: str, *, page_cursor: str | None = None
    ) -> dict[str, Any]:
        """Read a bounded PR batch, or one explicit page for a newer tool catalog."""

        self._verify_configured_repository(repository, token)
        if state not in {"OPEN", "CLOSED", "ALL"}:
            raise GitHubApiFailure("INVALID_ARGUMENTS")
        if (
            page_cursor is not None
            and (
                not isinstance(page_cursor, str)
                or re.fullmatch(r"[1-9][0-9]{0,3}", page_cursor) is None
                or int(page_cursor) > MAX_PULL_REQUEST_PAGE_NUMBER
            )
        ):
            raise GitHubApiFailure("INVALID_ARGUMENTS")
        first_page = int(page_cursor) if page_cursor is not None else 1
        batch_mode = page_cursor is None
        page_count = MAX_PULL_REQUEST_BATCH_PAGES if batch_mode else 1
        pull_requests: list[dict[str, Any]] = []
        pages_read = 0
        next_cursor: str | None = None
        next_cursor_source: str | None = None
        has_more = False
        page_number = first_page

        for _ in range(page_count):
            query = urlencode(
                {
                    "state": state.lower(),
                    "per_page": MAX_PULL_REQUEST_PAGE_SIZE,
                    "page": page_number,
                }
            )
            path = f"{self._repository_path(repository)}/pulls?{query}"
            response = self._request("GET", path, token, None)
            if response.status != 200:
                raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
            values = _json_array(response)
            if len(values) > MAX_PULL_REQUEST_PAGE_SIZE:
                raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
            next_cursor = _next_pull_request_cursor(
                response.headers,
                repository=repository,
                state=state,
                current_page=page_number,
            )
            has_more = next_cursor is not None
            next_cursor_source = "github_link" if has_more else None
            if not has_more and len(values) == MAX_PULL_REQUEST_PAGE_SIZE:
                # A full page without a next link is ambiguous; request one more
                # bounded page instead of claiming the list is complete.
                has_more = True
                next_cursor_source = "full_page_boundary"
                if page_number < MAX_PULL_REQUEST_PAGE_NUMBER:
                    next_cursor = str(page_number + 1)
            if has_more and page_number >= MAX_PULL_REQUEST_PAGE_NUMBER:
                next_cursor = None
            pull_requests.extend(_pull_request_projection(item) for item in values)
            pages_read += 1
            if not has_more:
                next_cursor = None
                break
            if batch_mode:
                if next_cursor is None:
                    break
                page_number = int(next_cursor)

        page_limit_reached = has_more and page_number >= MAX_PULL_REQUEST_PAGE_NUMBER
        result_limit_reached = page_limit_reached or (
            has_more and batch_mode and pages_read >= page_count
        )
        complete = first_page == 1 and not has_more
        return {
            "pull_requests": pull_requests,
            "page_cursor": str(first_page),
            "pages_read": pages_read,
            "complete": complete,
            "truncated": not complete,
            "has_more": has_more,
            "next_cursor": None if page_limit_reached else next_cursor,
            "next_cursor_source": next_cursor_source,
            "result_limit_reached": result_limit_reached,
        }

    def get_pull_request(
        self, repository: Repository, pull_number: int, token: str
    ) -> dict[str, Any]:
        """Read one pull request and project status and commit identifiers."""

        self._verify_configured_repository(repository, token)
        path = f"{self._repository_path(repository)}/pulls/{pull_number}"
        value = _json_object(self._request("GET", path, token, None))
        self._verify_repository(value, repository)
        self._verify_pull_request_repositories(value, repository)
        result = _pull_request_projection(value)
        if result["number"] != pull_number:
            raise GitHubApiFailure("PULL_REQUEST_IDENTITY_MISMATCH")
        return {"pull_request": result}

    def find_draft_pull_request(
        self, repository: Repository, head_branch: str, base_branch: str, token: str
    ) -> dict[str, Any]:
        """Find all open or closed PRs for one exact head/base pair."""

        self._verify_configured_repository(repository, token)
        _require_branch_name(head_branch)
        _require_branch_name(base_branch)
        values = self._find_pull_requests(repository, head_branch, base_branch, token)
        projected = [
            {
                "number": item["number"],
                "url": item.get("html_url"),
                "state": item["state"],
                "is_draft": item["draft"],
                "head_sha": item["head_sha"],
                "base_sha": item.get("base_sha"),
            }
            for item in values
        ]
        return {
            "exists": bool(projected),
            "count": len(projected),
            "duplicate": len(projected) > 1,
            "pull_requests": projected,
        }

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
        total_count = value.get("total_count")
        if (
            not isinstance(runs, list)
            or len(runs) > 100
            or isinstance(total_count, bool)
            or not isinstance(total_count, int)
            or total_count < len(runs)
        ):
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        complete = total_count == len(runs)
        return {
            "workflow_runs": [_workflow_run_projection(item) for item in runs],
            "total_count": total_count,
            "complete": complete,
            "truncated": not complete,
        }

    def search_issues(
        self,
        repository: Repository,
        query_text: str,
        state: str,
        labels: list[str],
        limit: int,
        token: str,
        *,
        updated_since: str | None = None,
        updated_until: str | None = None,
        page_cursor: str = "1",
    ) -> dict[str, Any]:
        """Search only issues in one allowlisted repository with a fixed qualifier."""

        self._verify_configured_repository(repository, token)
        if (
            state not in {"OPEN", "CLOSED", "ALL"}
            or not isinstance(labels, list)
            or len(labels) > 10
            or any(
                not isinstance(label, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,49}", label) is None
                for label in labels
            )
            or len(set(labels)) != len(labels)
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
            or not re.fullmatch(r"[1-9][0-9]{0,3}", page_cursor)
            or int(page_cursor) > (1000 + limit - 1) // limit
            or (
                updated_since is not None
                and not _valid_updated_since(updated_since)
            )
            or (
                updated_until is not None
                and not _valid_updated_since(updated_until)
            )
            or (
                updated_since is not None
                and updated_until is not None
                and _parse_utc_timestamp(updated_since) > _parse_utc_timestamp(updated_until)
            )
            or (
                not isinstance(query_text, str)
                or (
                    query_text
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,127}", query_text) is None
                )
            )
        ):
            raise GitHubApiFailure("INVALID_ARGUMENTS")
        query_parts = [f"repo:{repository.slug}", "is:issue"]
        if state == "OPEN":
            query_parts.append("is:open")
        elif state == "CLOSED":
            query_parts.append("is:closed")
        if query_text:
            query_parts.append(query_text)
        query_parts.extend(f'label:"{label}"' for label in labels)
        if updated_since is not None:
            query_parts.append(f"updated:>={_normalize_utc_timestamp(updated_since)}")
        if updated_until is not None:
            query_parts.append(f"updated:<={_normalize_utc_timestamp(updated_until)}")
        query = " ".join(query_parts)
        path = "/search/issues?" + urlencode(
            {
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": limit,
                "page": page_cursor,
            }
        )
        value = _json_object(self._request("GET", path, token, None))
        items = value.get("items")
        total_count = value.get("total_count")
        incomplete_results = value.get("incomplete_results")
        if (
            not isinstance(items, list)
            or len(items) > limit
            or isinstance(total_count, bool)
            or not isinstance(total_count, int)
            or total_count < len(items)
            or not isinstance(incomplete_results, bool)
        ):
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        issues: list[dict[str, Any]] = []
        expected_repository_url = f"https://api.github.com/repos/{repository.slug}".lower()
        for item in items:
            if not isinstance(item, Mapping) or "pull_request" in item:
                raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
            repository_url = item.get("repository_url")
            if (
                not isinstance(repository_url, str)
                or repository_url.lower() != expected_repository_url
            ):
                raise GitHubApiFailure("REPOSITORY_IDENTITY_MISMATCH")
            issues.append(_issue_search_projection(item))
        page_number = int(page_cursor)
        expected_page_items = min(
            limit, max(0, total_count - (page_number - 1) * limit)
        )
        incomplete_results = incomplete_results or len(issues) < expected_page_items
        result_limit_reached = total_count > 1000
        max_pages = (1000 + limit - 1) // limit
        next_cursor = (
            str(page_number + 1)
            if page_number < max_pages and total_count > page_number * limit
            else None
        )
        complete = (
            page_number == 1
            and next_cursor is None
            and total_count <= limit
            and len(issues) == total_count
            and not result_limit_reached
            and not incomplete_results
        )
        return {
            "issues": issues,
            "total_count": total_count,
            "page_cursor": page_cursor,
            "truncated": not complete,
            "complete": complete,
            "incomplete_results": incomplete_results,
            "result_limit_reached": result_limit_reached,
            "next_cursor": next_cursor,
        }

    def list_workflow_jobs(
        self, repository: Repository, run_id: str, limit: int, token: str
    ) -> dict[str, Any]:
        """Read bounded Actions jobs only after verifying the run belongs to this repo."""

        self._verify_configured_repository(repository, token)
        self._get_workflow_run(repository, run_id, token)
        path = (
            f"{self._repository_path(repository)}/actions/runs/{quote(run_id, safe='')}/jobs?"
            + urlencode({"per_page": limit})
        )
        value = _json_object(self._request("GET", path, token, None))
        jobs = value.get("jobs")
        total_count = value.get("total_count")
        if (
            not isinstance(jobs, list)
            or len(jobs) > limit
            or isinstance(total_count, bool)
            or not isinstance(total_count, int)
            or total_count < len(jobs)
        ):
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        projected = [_job_projection(item, expected_run_id=run_id) for item in jobs]
        complete = total_count == len(jobs)
        return {
            "jobs": projected,
            "total_count": total_count,
            "complete": complete,
            "truncated": not complete,
        }

    def read_job_log_excerpt(
        self, repository: Repository, run_id: str, job_id: str, max_lines: int, token: str
    ) -> dict[str, Any]:
        """Read a bounded tail excerpt through GitHub's short-lived log redirect."""

        self._verify_configured_repository(repository, token)
        self._get_workflow_run(repository, run_id, token)
        job_path = f"{self._repository_path(repository)}/actions/jobs/{quote(job_id, safe='')}"
        job = _json_object(self._request("GET", job_path, token, None))
        projected_job = _job_projection(
            job, expected_run_id=run_id, expected_job_id=job_id
        )
        log_response = self._request(
            "GET",
            f"{job_path}/logs",
            token,
            None,
            accepted_statuses=frozenset({302}),
        )
        location = log_response.headers.get("location") or log_response.headers.get("Location")
        if not isinstance(location, str):
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        download = self._download_actions_log(location)
        decoded = download.decode("utf-8", errors="replace")
        lines = decoded.splitlines()
        excerpt_lines = lines[-max_lines:]
        clipped_lines = any(len(line) > 2_000 for line in excerpt_lines)
        excerpt = "\n".join(_redact_log_line(line[:2_000]) for line in excerpt_lines)
        excerpt_bytes = excerpt.encode("utf-8")
        excerpt_limited = len(excerpt_bytes) > MAX_ACTIONS_LOG_EXCERPT_BYTES
        if excerpt_limited:
            excerpt = excerpt_bytes[-MAX_ACTIONS_LOG_EXCERPT_BYTES:].decode(
                "utf-8", errors="ignore"
            )
        return {
            "job_id": job_id,
            "run_id": run_id,
            "job_name": projected_job["name"],
            "excerpt": excerpt,
            "truncated": (
                len(lines) > max_lines
                or clipped_lines
                or excerpt_limited
                or len(download) >= MAX_ACTIONS_LOG_BYTES
            ),
        }

    def _download_actions_log(self, location: str) -> bytes:
        """Fetch a signed log URL only from GitHub's documented Actions blob hosts."""

        try:
            parsed = urlsplit(location)
            host = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError as error:
            raise GitHubApiFailure("LOG_DOWNLOAD_REDIRECT_REJECTED") from error
        if (
            parsed.scheme != "https"
            or not _ACTIONS_LOG_HOST.fullmatch(host)
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or not parsed.path.startswith("/")
            or parsed.path.startswith("//")
            or any(character in location for character in "\r\n\x00")
            or parsed.fragment
            or len(parsed.path) > 8_192
            or len(parsed.query) > 8_192
        ):
            raise GitHubApiFailure("LOG_DOWNLOAD_REDIRECT_REJECTED")
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        headers = {"Accept": "text/plain", "User-Agent": "Cyrene-GitHub-Issues-Provider"}
        try:
            if self._transport is not None:
                response = self._transport("GET", host, path, headers, None)
            else:
                connection = http.client.HTTPSConnection(
                    host, port=443, timeout=self._settings.timeout_seconds
                )
                try:
                    connection.request("GET", path, headers=headers)
                    upstream = connection.getresponse()
                    body = upstream.read(MAX_ACTIONS_LOG_BYTES + 1)
                    response = HttpResponse(
                        upstream.status,
                        {name.lower(): value for name, value in upstream.getheaders()},
                        body,
                    )
                finally:
                    connection.close()
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise GitHubApiFailure("LOG_DOWNLOAD_UNAVAILABLE", retryable=True) from error
        if not isinstance(response, HttpResponse):
            response = HttpResponse(
                int(response.status),
                {str(k).lower(): str(v) for k, v in response.headers.items()},
                bytes(response.body),
            )
        if response.status != 200 or len(response.body) > MAX_ACTIONS_LOG_BYTES:
            raise GitHubApiFailure("LOG_DOWNLOAD_UNAVAILABLE", retryable=response.status >= 500)
        return response.body

    def rerun_failed_jobs(
        self,
        repository: Repository,
        run_id: str,
        expected_workflow_id: str,
        expected_head_branch: str,
        expected_head_sha: str,
        expected_attempt: int,
        token: str,
    ) -> dict[str, Any]:
        """Rerun one failed task-branch CI attempt and reconcile its new attempt."""

        default_branch = self._verify_configured_repository(repository, token)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_head_sha):
            raise GitHubApiFailure("INVALID_ARGUMENTS")
        _require_task_branch(expected_head_branch)
        allowed_workflow_ids = self._settings.source_workflow_ids.get(
            repository.repository_id, frozenset()
        )
        if not allowed_workflow_ids or expected_workflow_id not in allowed_workflow_ids:
            raise GitHubApiFailure("WORKFLOW_NOT_ALLOWLISTED")
        run = self._get_workflow_run(repository, run_id, token)
        if (
            run["workflow_id"] != expected_workflow_id
            or run["head_branch"] != expected_head_branch
            or run["head_sha"].lower() != expected_head_sha.lower()
            or run["run_attempt"] != expected_attempt
        ):
            raise GitHubApiFailure("RUN_PRECONDITION_FAILED")
        if (
            run["status"] != "COMPLETED"
            or run["conclusion"] not in {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED"}
            or not _is_task_branch(run["head_branch"])
            or _same_branch(run["head_branch"], default_branch)
            or re.search(
                r"deploy|release|publish|production|provision|infrastructure",
                f"{run['name']} {run['workflow_path'] or ''}",
                re.I,
            )
        ):
            raise GitHubApiFailure("RUN_NOT_ELIGIBLE_FOR_AUTOMATED_RERUN")
        path = (
            f"{self._repository_path(repository)}/actions/runs/"
            f"{quote(run_id, safe='')}/rerun-failed-jobs"
        )
        try:
            self._request("POST", path, token, None, write=True)
        except GitHubApiFailure as error:
            if not error.uncertain:
                raise
            try:
                readback = self._get_workflow_run(repository, run_id, token)
            except GitHubApiFailure as readback_error:
                raise GitHubApiFailure(
                    "WRITE_OUTCOME_UNCONFIRMED", uncertain=True
                ) from readback_error
            if (
                readback["workflow_id"] == expected_workflow_id
                and readback["head_branch"] == expected_head_branch
                and readback["head_sha"].lower() == expected_head_sha.lower()
                and (
                    readback["run_attempt"] > expected_attempt
                    or readback["status"] != "COMPLETED"
                )
            ):
                return _rerun_result(readback)
            raise
        try:
            readback = self._get_workflow_run(repository, run_id, token)
        except GitHubApiFailure as readback_error:
            raise GitHubApiFailure("WRITE_OUTCOME_UNCONFIRMED", uncertain=True) from readback_error
        if (
            readback["workflow_id"] != expected_workflow_id
            or readback["head_branch"] != expected_head_branch
            or readback["head_sha"].lower() != expected_head_sha.lower()
            or (
                readback["run_attempt"] <= expected_attempt
                and readback["status"] == "COMPLETED"
            )
        ):
            raise GitHubApiFailure("RERUN_READBACK_UNAVAILABLE", uncertain=True)
        return _rerun_result(readback)

    def _get_workflow_run(
        self, repository: Repository, run_id: str, token: str
    ) -> dict[str, Any]:
        """Read one run and reject an identity that does not match the fixed path."""

        path = f"{self._repository_path(repository)}/actions/runs/{quote(run_id, safe='')}"
        value = _json_object(self._request("GET", path, token, None))
        if isinstance(value.get("id"), bool) or str(value.get("id")) != run_id:
            raise GitHubApiFailure("RUN_IDENTITY_MISMATCH")
        run_repository = value.get("repository")
        if isinstance(run_repository, Mapping):
            self._verify_repository(value, repository)
        return _workflow_run_projection(value)

    def _read_ref_sha(
        self, repository: Repository, branch: str, token: str
    ) -> str | None:
        """Return one branch head or None after an authoritative 404 read."""

        _require_branch_name(branch)
        path = f"{self._repository_path(repository)}/git/ref/heads/{quote(branch, safe='')}"
        try:
            value = _json_object(self._request("GET", path, token, None))
        except GitHubApiFailure as error:
            if error.code == "RESOURCE_NOT_FOUND":
                return None
            raise
        expected_ref = f"refs/heads/{branch}"
        target = value.get("object")
        sha = target.get("sha") if isinstance(target, Mapping) else None
        if value.get("ref") != expected_ref or not isinstance(sha, str) or not re.fullmatch(
            r"[0-9a-fA-F]{40}", sha
        ):
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        return sha

    def _read_file(
        self, repository: Repository, branch: str, path: str, token: str
    ) -> tuple[str, bytes] | None:
        """Read a branch file for compare-and-swap and uncertain-write recovery."""

        _require_branch_name(branch)
        _require_safe_file_path(path)
        encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
        endpoint = (
            f"{self._repository_path(repository)}/contents/{encoded_path}?"
            + urlencode({"ref": branch})
        )
        try:
            value = _json_object(self._request("GET", endpoint, token, None))
        except GitHubApiFailure as error:
            if error.code == "RESOURCE_NOT_FOUND":
                return None
            raise
        file_sha = value.get("sha")
        encoded_content = value.get("content")
        if (
            value.get("path") != path
            or value.get("encoding") != "base64"
            or not isinstance(file_sha, str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}", file_sha)
            or not isinstance(encoded_content, str)
        ):
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        try:
            content = base64.b64decode(encoded_content, validate=False)
        except (ValueError, binascii.Error) as error:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE") from error
        if len(content) > 500_000 or _git_blob_sha(content) != file_sha.lower():
            raise GitHubApiFailure("FILE_IDENTITY_MISMATCH")
        return file_sha, content

    def _find_pull_request(
        self, repository: Repository, head_branch: str, base_branch: str, token: str
    ) -> dict[str, Any] | None:
        """Find one open PR for the exact same-repository head and base branches."""

        values = self._find_pull_requests(
            repository, head_branch, base_branch, token, state="open"
        )
        if len(values) > 1:
            raise GitHubApiFailure("PULL_REQUEST_DUPLICATE")
        return values[0] if values else None

    def _find_pull_requests(
        self,
        repository: Repository,
        head_branch: str,
        base_branch: str,
        token: str,
        *,
        state: str = "all",
    ) -> list[dict[str, Any]]:
        """List exact head/base matches without accepting user-supplied API URLs."""

        query = urlencode(
            {
                "state": state,
                "head": f"{repository.owner}:{head_branch}",
                "base": base_branch,
                "per_page": 10,
            }
        )
        values = _json_array(
            self._request("GET", f"{self._repository_path(repository)}/pulls?{query}", token, None)
        )
        if len(values) > 10:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        matches: list[dict[str, Any]] = []
        for value in values:
            self._verify_pull_request_repositories(value, repository)
            projection = _pull_request_projection(value)
            if (
                projection.get("head_branch") == head_branch
                and projection.get("base_branch") == base_branch
            ):
                matches.append(projection)
        return matches

    def _branch_has_open_pull_request(
        self, repository: Repository, branch: str, token: str
    ) -> bool:
        """Refuse task-branch deletion while an open same-repository PR uses it."""

        query = urlencode(
            {
                "state": "open",
                "head": f"{repository.owner}:{branch}",
                "per_page": 100,
            }
        )
        values = _json_array(
            self._request("GET", f"{self._repository_path(repository)}/pulls?{query}", token, None)
        )
        if len(values) > 100:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        for value in values:
            head = value.get("head")
            base = value.get("base")
            head_repo = head.get("repo") if isinstance(head, Mapping) else None
            base_repo = base.get("repo") if isinstance(base, Mapping) else None
            head_ref = head.get("ref") if isinstance(head, Mapping) else None
            if (
                head_ref == branch
                and isinstance(head_repo, Mapping)
                and isinstance(base_repo, Mapping)
                and str(head_repo.get("id")) == repository.repository_id
                and head_repo.get("full_name") == repository.slug
                and str(base_repo.get("id")) == repository.repository_id
                and base_repo.get("full_name") == repository.slug
                and value.get("state") == "open"
            ):
                return True
        if len(values) == 100:
            raise GitHubApiFailure("OPEN_PULL_REQUEST_SEARCH_TRUNCATED")
        return False

    @staticmethod
    def _verify_pull_request_repositories(
        value: Mapping[str, Any], repository: Repository
    ) -> None:
        """Ensure both PR endpoints still belong to the same configured repository."""

        head = value.get("head")
        base = value.get("base")
        for endpoint in (head, base):
            endpoint_repo = endpoint.get("repo") if isinstance(endpoint, Mapping) else None
            if not isinstance(endpoint_repo, Mapping) or (
                str(endpoint_repo.get("id")) != repository.repository_id
                or endpoint_repo.get("full_name") != repository.slug
            ):
                raise GitHubApiFailure("REPOSITORY_IDENTITY_MISMATCH")

    def get_ci_status(
        self, repository: Repository, commit_sha: str, token: str
    ) -> dict[str, Any]:
        """Combine commit status contexts and check-runs without hiding API errors."""

        self._verify_configured_repository(repository, token)
        quoted_sha = quote(commit_sha, safe="")
        status = _json_object(
            self._request(
                "GET",
                f"{self._repository_path(repository)}/commits/{quoted_sha}/status?per_page=100",
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
        statuses_total_count = status.get("total_count")
        check_runs_total_count = checks.get("total_count")
        if not isinstance(contexts, list) or not isinstance(check_runs, list):
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        if len(contexts) > 100 or len(check_runs) > 100:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        if (
            isinstance(statuses_total_count, bool)
            or not isinstance(statuses_total_count, int)
            or statuses_total_count < len(contexts)
            or isinstance(check_runs_total_count, bool)
            or not isinstance(check_runs_total_count, int)
            or check_runs_total_count < len(check_runs)
        ):
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
        statuses_complete = statuses_total_count == len(contexts)
        check_runs_complete = check_runs_total_count == len(check_runs)
        check_runs_terminal = (
            check_runs_complete
            and bool(projected_checks)
            and all(item["status"] == "COMPLETED" for item in projected_checks)
        )
        complete = statuses_complete and check_runs_complete
        state_note = None
        if state == "pending" and not projected_contexts:
            if not statuses_complete:
                state_note = (
                    "The combined commit-status API reports PENDING and returned no status "
                    "contexts in this incomplete response. Required branch-protection contexts "
                    "were not queried."
                )
            elif check_runs_terminal:
                state_note = (
                    "The combined commit-status API reports PENDING but returned no status "
                    "contexts. All returned check runs are terminal; required branch-protection "
                    "contexts were not queried."
                )
            else:
                state_note = (
                    "The combined commit-status API reports PENDING and returned no status "
                    "contexts. Required branch-protection contexts were not queried."
                )
        return {
            "commit_sha": commit_sha,
            "state": state.upper(),
            "statuses_total_count": statuses_total_count,
            "statuses": projected_contexts,
            "check_runs_total_count": check_runs_total_count,
            "check_runs": projected_checks,
            "status_contexts_present": bool(projected_contexts),
            "status_contexts_complete": statuses_complete,
            "check_runs_complete": check_runs_complete,
            "check_runs_terminal": check_runs_terminal,
            "required_contexts_verified": False,
            "state_note": state_note,
            "complete": complete,
            "truncated": not complete,
        }

    def _verify_configured_repository(
        self, repository: Repository, token: str
    ) -> str | None:
        """Correlate stable repository ID and owner/name against GitHub."""

        path = f"/repos/{quote(repository.owner, safe='')}/{quote(repository.name, safe='')}"
        response = self._request("GET", path, token, None)
        value = _json_object(response)
        if (
            str(value.get("id")) != repository.repository_id
            or value.get("full_name") != repository.slug
        ):
            raise GitHubApiFailure("REPOSITORY_IDENTITY_MISMATCH")
        default_branch = value.get("default_branch")
        if default_branch is not None and not isinstance(default_branch, str):
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        return default_branch

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
        accepted_statuses: frozenset[int] = frozenset(),
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
        if response.status in accepted_statuses:
            return response
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


def _next_pull_request_cursor(
    headers: Mapping[str, str],
    *,
    repository: Repository,
    state: str,
    current_page: int,
) -> str | None:
    """Validate GitHub's fixed-host next link and return only its page number."""

    link = headers.get("link") or headers.get("Link")
    if link is None:
        return None
    if not isinstance(link, str):
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    if not link:
        return None

    next_cursor: str | None = None
    for segment in link.split(","):
        match = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel="?([a-z]+)"?\s*', segment)
        if match is None:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        if match.group(2) != "next":
            continue
        if next_cursor is not None:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        try:
            parsed = urlsplit(match.group(1))
            parsed_port = parsed.port
            query = parse_qs(parsed.query, strict_parsing=True)
        except ValueError as error:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE") from error
        expected_paths = {
            f"{GitHubApi._repository_path(repository)}/pulls",
            f"/repositories/{quote(repository.repository_id, safe='')}/pulls",
        }
        page_values = query.get("page", [])
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.lower() != GITHUB_HOST
            or parsed_port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in expected_paths
            or set(query) != {"page", "per_page", "state"}
            or query.get("per_page") != [str(MAX_PULL_REQUEST_PAGE_SIZE)]
            or query.get("state") != [state.lower()]
            or len(page_values) != 1
            or re.fullmatch(r"[1-9][0-9]{0,3}", page_values[0]) is None
            or int(page_values[0]) != current_page + 1
        ):
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        next_cursor = page_values[0]
    return next_cursor


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
    body = value.get("body")
    head = value.get("head")
    base = value.get("base")
    head_sha = head.get("sha") if isinstance(head, Mapping) else None
    head_ref = head.get("ref") if isinstance(head, Mapping) else None
    base_ref = base.get("ref") if isinstance(base, Mapping) else None
    base_sha = base.get("sha") if isinstance(base, Mapping) else None
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
    merged = value.get("merged")
    merged_at_present = "merged_at" in value
    merged_at = value.get("merged_at")
    merge_commit_sha = value.get("merge_commit_sha")
    if merged is not None and not isinstance(merged, bool):
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    if merged_at is not None and (
        not isinstance(merged_at, str) or not _valid_updated_since(merged_at)
    ):
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    if merge_commit_sha is not None and (
        not isinstance(merge_commit_sha, str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", merge_commit_sha) is None
    ):
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")

    projection: dict[str, Any] = {
        "number": number,
        "state": state.upper(),
        "title": title[:500],
        "body_sha256": _text_sha256(body if isinstance(body, str) else ""),
        "head_sha": head_sha[:64],
        "head_branch": head_ref[:255] if isinstance(head_ref, str) else None,
        "base_branch": base_ref[:255],
        "base_sha": base_sha[:64] if isinstance(base_sha, str) else None,
        "html_url": value.get("html_url") if isinstance(value.get("html_url"), str) else None,
        "draft": value.get("draft") if isinstance(value.get("draft"), bool) else False,
        "merge_status": _pull_request_merge_status(
            state=state,
            merged=merged,
            merged_at=merged_at,
            merged_at_present=merged_at_present,
        ),
    }
    # Do not invent false/null facts when a list or detail response omits a field.
    if isinstance(merged, bool):
        projection["merged"] = merged
    if isinstance(merged_at, str):
        projection["merged_at"] = merged_at
    if isinstance(merge_commit_sha, str):
        projection["merge_commit_sha"] = merge_commit_sha.lower()
    return projection


def _pull_request_merge_status(
    *,
    state: str,
    merged: bool | None,
    merged_at: str | None,
    merged_at_present: bool,
) -> str:
    """Summarize merge facts without treating a test SHA as proof of merging.

    GitHub can return a test merge commit before a pull request is merged. Prefer
    the explicit ``merged`` boolean; use an actual ``merged_at`` value only when
    that boolean is absent. A closed response with neither fact remains unknown.

    中文：汇总合并状态时不把测试 merge SHA 当成已合并证明。优先使用上游 boolean；
    缺少该值时才使用 merged_at。closed 响应两者都缺失时保留 UNKNOWN。
    """

    if state == "open":
        if merged is True or merged_at is not None:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        return "OPEN"
    if merged is True:
        if merged_at_present and merged_at is None:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        return "MERGED"
    if merged is False:
        if merged_at is not None:
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        return "CLOSED_UNMERGED"
    if merged_at is not None:
        return "MERGED"
    if merged_at_present:
        return "CLOSED_UNMERGED"
    return "UNKNOWN"


def _workflow_run_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project one Actions run with bounded status and commit metadata."""

    run_id = value.get("id")
    workflow_id = value.get("workflow_id")
    name = value.get("name")
    status = value.get("status")
    conclusion = value.get("conclusion")
    head_sha = value.get("head_sha")
    head_branch = value.get("head_branch")
    run_attempt = value.get("run_attempt")
    workflow_path = value.get("path")
    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or isinstance(workflow_id, bool)
        or not isinstance(workflow_id, int)
        or workflow_id < 1
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
        or not isinstance(head_branch, str)
        or isinstance(run_attempt, bool)
        or not isinstance(run_attempt, int)
        or run_attempt < 1
        or (workflow_path is not None and not isinstance(workflow_path, str))
    ):
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    return {
        "run_id": str(run_id),
        "workflow_id": str(workflow_id),
        "name": name[:256],
        "status": status.upper(),
        "conclusion": conclusion.upper() if isinstance(conclusion, str) else None,
        "head_sha": head_sha[:64],
        "head_branch": head_branch[:255],
        "run_attempt": run_attempt,
        "workflow_path": workflow_path[:512] if isinstance(workflow_path, str) else None,
        "html_url": value.get("html_url") if isinstance(value.get("html_url"), str) else None,
        "updated_at": value.get("updated_at") if isinstance(value.get("updated_at"), str) else None,
    }


def _issue_search_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project only bounded issue facts from GitHub's search response."""

    issue_id = value.get("id")
    number = value.get("number")
    state = value.get("state")
    title = value.get("title")
    if (
        isinstance(issue_id, bool)
        or not isinstance(issue_id, int)
        or isinstance(number, bool)
        or not isinstance(number, int)
        or number < 1
        or state not in {"open", "closed"}
        or not isinstance(title, str)
    ):
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    body = value.get("body")
    return {
        "issue_id": str(issue_id),
        "number": number,
        "state": state.upper(),
        "title": title[:500],
        "body_excerpt": body[:4_000] if isinstance(body, str) else "",
        "body_truncated": isinstance(body, str) and len(body) > 4_000,
        "html_url": value.get("html_url") if isinstance(value.get("html_url"), str) else "",
        "updated_at": value.get("updated_at") if isinstance(value.get("updated_at"), str) else None,
    }


def _job_projection(
    value: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_job_id: str | None = None,
) -> dict[str, Any]:
    """Project a workflow job and its steps with a fixed small output shape."""

    job_id = value.get("id")
    run_id = value.get("run_id")
    name = value.get("name")
    status = value.get("status")
    conclusion = value.get("conclusion")
    if (
        isinstance(job_id, bool)
        or not isinstance(job_id, int)
        or job_id < 1
        or (expected_job_id is not None and str(job_id) != expected_job_id)
        or isinstance(run_id, bool)
        or str(run_id) != expected_run_id
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
    ):
        raise GitHubApiFailure("JOB_IDENTITY_MISMATCH")
    raw_steps = value.get("steps", [])
    if not isinstance(raw_steps, list):
        raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
    steps: list[dict[str, Any]] = []
    for step in raw_steps[:50]:
        if not isinstance(step, Mapping) or not isinstance(step.get("name"), str):
            raise GitHubApiFailure("INVALID_PROVIDER_RESPONSE")
        steps.append(
            {
                "name": step["name"][:256],
                "status": _check_status(step.get("status")),
                "conclusion": _optional_check_conclusion(step.get("conclusion")),
            }
        )
    return {
        "job_id": str(job_id),
        "run_id": expected_run_id,
        "name": name[:256],
        "status": status.upper(),
        "conclusion": conclusion.upper() if isinstance(conclusion, str) else None,
        "steps": steps,
        "steps_truncated": len(raw_steps) > len(steps),
    }


def _require_branch_name(branch: str) -> None:
    """Reject malformed git refs before any branch API request."""

    if (
        not isinstance(branch, str)
        or not 1 <= len(branch) <= 255
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
        or branch.startswith("/")
        or branch.endswith(("/", "."))
        or "//" in branch
        or "@{" in branch
        or any(
            part in {"", ".", ".."} or part.startswith(".") or part.endswith(".lock")
            for part in branch.split("/")
        )
    ):
        raise GitHubApiFailure("INVALID_ARGUMENTS")


def _valid_updated_since(value: str) -> bool:
    """Validate a real UTC RFC3339 timestamp before placing it in a search query."""

    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)",
        value,
    ) is None:
        return False
    try:
        _parse_utc_timestamp(value)
    except ValueError:
        return False
    return True


def _normalize_utc_timestamp(value: str) -> str:
    """Return GitHub search syntax with an explicit UTC offset."""

    return value[:-1] + "+00:00" if value.endswith("Z") else value


def _parse_utc_timestamp(value: str) -> datetime:
    """Parse a validated UTC timestamp for date-window ordering checks."""

    return datetime.fromisoformat(_normalize_utc_timestamp(value))


def _is_task_branch(branch: str) -> bool:
    """Return whether a branch uses the reserved Cyrene assistant task namespace."""

    _require_branch_name(branch)
    return branch.casefold().startswith(TASK_BRANCH_PREFIX)


def _same_branch(left: str, right: str | None) -> bool:
    """Compare branch refs without regard to GitHub's case-insensitive repo paths."""

    return right is not None and left.casefold() == right.casefold()


def _reject_default_branch(branch: str, default_branch: str | None) -> None:
    """Deny task writes or reruns on the repository's configured default branch."""

    if _same_branch(branch, default_branch):
        raise GitHubApiFailure("PROTECTED_BRANCH")


def _require_task_branch(branch: str) -> None:
    """Require a writable task ref that cannot name an integration or release branch."""

    if not _is_task_branch(branch):
        raise GitHubApiFailure("PROTECTED_BRANCH")


def _require_safe_file_path(path: str) -> None:
    """Block hidden control files and deployment configuration from autonomous commits."""

    if (
        not isinstance(path, str)
        or not 1 <= len(path) <= 512
        or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", path)
    ):
        raise GitHubApiFailure("INVALID_ARGUMENTS")
    parts = path.split("/")
    lowered = [part.casefold() for part in parts]
    protected_dirs = {
        ".github",
        ".git",
        ".gitlab",
        ".circleci",
        ".azure",
        "deploy",
        "deployment",
        "infra",
        "infrastructure",
        "terraform",
        "helm",
        "k8s",
        "kubernetes",
        "production",
        "prod",
        "permissions",
    }
    protected_files = {
        "codeowners",
        "azure-pipelines.yml",
        "azure-pipelines.yaml",
        "bitbucket-pipelines.yml",
        "bitbucket-pipelines.yaml",
        "buildkite.yml",
        "buildkite.yaml",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "jenkinsfile",
        ".gitmodules",
    }
    sensitive_name = re.compile(
        r"(?:^|[._-])(?:prod|production|staging|release|publish|deploy|deployment|"
        r"infra|infrastructure|terraform|helm|k8s|kubernetes|permissions|authorization|"
        r"access-control)(?:[._-]|$)"
    )
    if (
        any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
        or any(part in protected_dirs for part in lowered)
        or lowered[-1] in protected_files
        or lowered[-1].startswith("dockerfile")
        or any(sensitive_name.search(part) for part in lowered)
    ):
        raise GitHubApiFailure("PROTECTED_FILE_PATH")


def _git_blob_sha(content: bytes) -> str:
    """Calculate Git's canonical blob identifier for file readback comparison."""

    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {len(content)}\0".encode("ascii"))
    digest.update(content)
    return digest.hexdigest()


def _text_sha256(value: str) -> str:
    """Bind text comparisons without returning potentially large PR bodies."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _branch_write_result(
    branch: str,
    commit_sha: str,
    *,
    already_exists: bool = False,
    reconciled: bool = False,
) -> dict[str, Any]:
    """Build a confirmed branch receipt after authoritative ref readback."""

    return {
        "status": "CONFIRMED",
        "branch": branch,
        "commit_sha": commit_sha,
        "verified": True,
        "already_exists": already_exists,
        "reconciled": reconciled,
    }


def _file_write_result(
    branch: str,
    path: str,
    blob_sha: str,
    commit_sha: str | None,
    *,
    already_applied: bool = False,
    reconciled: bool = False,
) -> dict[str, Any]:
    """Build a confirmed file receipt without returning the uploaded contents."""

    return {
        "status": "CONFIRMED",
        "branch": branch,
        "path": path,
        "blob_sha": blob_sha,
        "commit_sha": commit_sha,
        "verified": True,
        "already_applied": already_applied,
        "reconciled": reconciled,
    }


def _draft_pull_request_result(
    pull_request: Mapping[str, Any],
    *,
    already_exists: bool = False,
    reconciled: bool = False,
) -> dict[str, Any]:
    """Project a draft PR receipt without including request or response bodies."""

    return {
        "status": "CONFIRMED",
        "pull_request": {
            "number": pull_request["number"],
            "url": pull_request.get("html_url"),
            "head_sha": pull_request["head_sha"],
            "base_sha": pull_request.get("base_sha"),
            "is_draft": pull_request["draft"],
        },
        "verified": True,
        "already_exists": already_exists,
        "reconciled": reconciled,
    }


def _rerun_result(run: Mapping[str, Any]) -> dict[str, Any]:
    """Report that GitHub accepted and exposed a new or active attempt."""

    return {
        "status": run["status"],
        "conclusion": run["conclusion"],
        "run_id": run["run_id"],
        "attempt": run["run_attempt"],
        "head_sha": run["head_sha"],
        "head_branch": run["head_branch"],
        "workflow_id": run["workflow_id"],
        "workflow_name": run["name"],
        "workflow_path": run["workflow_path"],
        "verified": True,
    }


def _redact_log_line(line: str) -> str:
    """Mask common credential formats before returning CI logs to a caller."""

    line = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b", "[REDACTED_GITHUB_TOKEN]", line)
    line = re.sub(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "[REDACTED_GITHUB_TOKEN]", line)
    line = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED_AWS_KEY]", line)
    line = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}", "Bearer [REDACTED]", line)

    def redact_assignment(match: re.Match[str]) -> str:
        """Preserve JSON or shell key punctuation while replacing the value."""

        raw_value = match.group("value")
        if raw_value.startswith('"') and raw_value.endswith('"'):
            replacement = '"[REDACTED]"'
        elif raw_value.startswith("'") and raw_value.endswith("'"):
            replacement = "'[REDACTED]'"
        else:
            replacement = "[REDACTED]"
        return match.group("prefix") + replacement

    secret_assignment = re.compile(
        r"""(?i)(?P<prefix>["']?[A-Za-z0-9_-]*(?:token|secret|password|api[_-]?key|"""
        r"""authorization|credential|private[_-]?key|access[_-]?key)[A-Za-z0-9_-]*["']?\s*[:=]\s*)"""
        r"""(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}]+)"""
    )
    line = secret_assignment.sub(redact_assignment, line)
    return re.sub(r"\b[A-Za-z0-9+/]{80,}={0,2}\b", "[REDACTED_LONG_VALUE]", line)


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
