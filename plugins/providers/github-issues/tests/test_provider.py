"""Security and fixed-transport tests for GitHub Issues provider behavior.

中文:验证 GitHub Issues 提供方的权限、固定传输和 Webhook 验签行为。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Any

import pytest
from cyrene_account_center_contracts import AuthMaterial, canonical_json
from cyrene_plugin_runtime import DirectPayload
from cyrene_plugin_runtime.vendor_cli import VendorCliResult

from github_issues_provider._generated import tool_provider_pb2 as wire
from github_issues_provider.account_center import (
    AccountCenterDirectLeaseClient,
    AccountCenterUnavailable,
)
from github_issues_provider.github_api import (
    MAX_PULL_REQUEST_BATCH_PAGES,
    MAX_PULL_REQUEST_PAGE_SIZE,
    MAX_RESPONSE_BYTES,
    GitHubApi,
    GitHubApiFailure,
    HttpResponse,
    _pull_request_projection,
)
from github_issues_provider.plugin import (
    CALL_TOOL_RESPONSE_TYPE_URL,
    WEBHOOK_REQUEST_TYPE_URL,
    GitHubIssuesProvider,
)
from github_issues_provider.settings import ProviderSettings, Repository


def make_settings() -> ProviderSettings:
    """Build one repo-scoped binding with distinct test keys."""

    return ProviderSettings(
        binding_id="github.binding.one",
        account_center_binding_id="account-center.binding.one",
        account_center_connection_ref="grpc://127.0.0.1:40000",
        account_center_transport="local",
        account_center_root_cert_ref=None,
        account_center_client_cert_ref=None,
        account_center_client_key_ref=None,
        host_secret_names={},
        repositories=(Repository("12345", "owner", "project"),),
        provider_caller_id="plugins.github-issues",
        exchange_caller_id="exchange.integrations",
        exchange_verification_key=b"\x22" * 32,
        provider_signing_key=b"\x33" * 32,
        github_webhook_secret=b"webhook test key with at least 32 bytes",
        allowed_installation_ids=frozenset({"99"}),
        allowed_events=frozenset({"issues"}),
        allowed_actions=frozenset({"opened", "closed", "reopened"}),
        source_workflow_ids={"12345": frozenset({"22"})},
    )


def test_account_center_direct_lease_rpc_preserves_profile_reference() -> None:
    settings = make_settings()

    class DirectClient:
        def __init__(self) -> None:
            self.call: dict[str, Any] | None = None

        def invoke(self, **kwargs: Any) -> DirectPayload:
            self.call = kwargs
            return DirectPayload(
                type_url=(
                    "type.googleapis.com/cyrene.account.center.v1."
                    "RedeemProviderCredentialLeaseResponse"
                ),
                value=json.dumps(
                    {
                        "access_token": "synthetic-provider-token",
                        "token_type": "Bearer",
                        "external_subject": "github-owner",
                        "expires_at": 2_000_000_000,
                    },
                    separators=(",", ":"),
                ).encode(),
            )

    transport = DirectClient()
    client = AccountCenterDirectLeaseClient(settings, client=transport)
    payload = {
        "lease_ref": "12345678-1234-1234-1234-123456789abc",
        "profile_ref": "00000000-0000-4000-8000-000000000001",
        "operation_id": "github.repository.get",
        "target": {"repository_id": "12345", "repository": "owner/project"},
        "arguments_sha256": "a" * 64,
    }
    actor = {
        "issuer": "https://identity.example.test",
        "subject": "member-7",
        "organization_id": "org-2",
        "workspace_id": "workspace-9",
        "binding_id": settings.account_center_binding_id,
    }

    material = client.redeem_provider_credential_lease(payload, actor)

    assert material.external_subject == "github-owner"
    assert transport.call is not None
    assert transport.call["method"] == "redeem_provider_credential_lease"
    envelope = json.loads(transport.call["request"].value)
    assert envelope["payload"] == payload
    assert envelope["trusted_context"]["binding_id"] == settings.account_center_binding_id


def test_workflow_allowlist_is_optional_for_existing_settings_constructors() -> None:
    configured = make_settings()
    previous_constructor_arguments = {
        item.name: getattr(configured, item.name)
        for item in fields(ProviderSettings)
        if item.name != "source_workflow_ids"
    }

    settings = ProviderSettings(**previous_constructor_arguments)

    assert settings.source_workflow_ids == {}


class FakeAccountCenter:
    """Validate actor and digest bindings while enforcing one-use references."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], dict[str, str | int]]] = []
        self.used: set[str] = set()

    def redeem_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | int],
    ) -> AuthMaterial:
        lease_ref = str(payload["lease_ref"])
        if lease_ref in self.used:
            raise AccountCenterUnavailable("lease is already used")
        self.used.add(lease_ref)
        self.calls.append((dict(payload), dict(trusted_context)))
        return AuthMaterial("secret-token-must-not-escape", "Bearer", "github-user-42", None)

    def redeem_provider_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | int],
    ) -> AuthMaterial:
        lease_ref = str(payload["lease_ref"])
        if lease_ref in self.used:
            raise AccountCenterUnavailable("lease is already used")
        self.used.add(lease_ref)
        self.calls.append((dict(payload), dict(trusted_context)))
        return AuthMaterial(
            "installation-token-must-not-escape", "Bearer", "acme", int(time.time()) + 3600
        )


class FakeGitHubApi:
    """Record methods and return response shapes consumed by tool methods."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def list_repositories(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("repositories", args))
        return {
            "repositories": [
                {
                    "repository_id": "12345",
                    "repository": "owner/project",
                    "private": True,
                }
            ]
        }

    def get_issue(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("get", args))
        return {
            "issue_id": "555",
            "number": 7,
            "state": "OPEN",
            "title": "Task",
            "html_url": "https://github.com/owner/project/issues/7",
        }

    def create_comment(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("comment", args))
        return {
            "status": "CONFIRMED",
            "comment_id": "777",
            "issue_number": 7,
            "repository_id": "12345",
            "body": args[2],
        }

    def update_state(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("state", args))
        return {
            "status": "CONFIRMED",
            "issue": {
                "issue_id": "555",
                "number": 7,
                "state": args[2],
                "title": "Task",
                "html_url": "https://github.com/owner/project/issues/7",
            },
        }

    def get_pull_request(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("pull_request", args))
        return {"pull_request": {"number": args[1], "state": "OPEN"}}

    def list_pull_requests(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("pull_requests", (*args, kwargs)))
        return {
            "pull_requests": [],
            "page_cursor": kwargs.get("page_cursor") or "1",
            "pages_read": 1,
            "complete": True,
            "truncated": False,
            "has_more": False,
            "next_cursor": None,
            "next_cursor_source": None,
            "result_limit_reached": False,
        }

    def search_issues(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("issues_search", (*args, kwargs)))
        return {
            "issues": [],
            "total_count": 0,
            "truncated": False,
            "complete": True,
            "next_cursor": None,
        }

    def get_branch(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("branch_get", args))
        return {"branch": args[1], "exists": True, "commit_sha": "a" * 40}

    def get_file(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("file_get", args))
        return {"branch": args[1], "path": args[2], "exists": False}

    def list_workflow_jobs(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("jobs_list", args))
        return {"jobs": [], "total_count": 0, "truncated": False}

    def read_job_log_excerpt(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("job_logs", args))
        return {"job_id": args[2], "excerpt": "", "truncated": False}

    def create_branch(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("branch_create", args))
        return {"status": "CONFIRMED", "verified": True}

    def delete_branch(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("branch_delete", args))
        return {"status": "CONFIRMED", "verified": True}

    def commit_file(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("file_commit", args))
        return {"status": "CONFIRMED", "verified": True}

    def create_draft_pull_request(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("draft_pr_create", args))
        return {"status": "CONFIRMED", "verified": True}

    def find_draft_pull_request(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("draft_pr_find", args))
        return {"exists": False, "count": 0, "duplicate": False, "pull_requests": []}

    def rerun_failed_jobs(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("rerun_failed", args))
        return {"status": "QUEUED", "verified": True}


class FakeCliRunner:
    """Record the fixed process request and return local-only process evidence."""

    def __init__(self, result: VendorCliResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result or VendorCliResult(0, 12, False, False, 128, 0)

    def run(self, executable: str, argv: list[str], **kwargs: Any) -> VendorCliResult:
        self.calls.append({"executable": executable, "argv": list(argv), **kwargs})
        return self.result


def signed_context(
    settings: ProviderSettings,
    arguments: Mapping[str, Any],
    lease_ref: str,
    operation_id: str,
    profile_ref: str | None = None,
) -> dict[str, Any]:
    """Sign the private Exchange envelope using the configured canonical form."""

    protected = {"payload": dict(arguments), "lease_ref": lease_ref}
    if profile_ref is not None:
        protected["profile_ref"] = profile_ref
    value: dict[str, Any] = {
        "method": "call_tool",
        "payload_sha256": hashlib.sha256(canonical_json(protected)).hexdigest(),
        "binding_id": settings.binding_id,
        "caller_id": settings.exchange_caller_id,
        "issuer": "https://identity.example.test",
        "subject": "member-27",
        "organization_id": "org-4",
        "workspace_id": "workspace-8",
        "request_id": "request-123",
        "issued_at": int(time.time()),
        "expires_at": int(time.time()) + 60,
        "operation_id": (
            "github.vendor.cli.execute" if operation_id == "github_vendor_cli" else operation_id
        ),
    }
    value["signature"] = hmac.new(
        settings.exchange_verification_key,
        canonical_json(value),
        hashlib.sha256,
    ).hexdigest()
    return value


def invoke_tool(
    provider: GitHubIssuesProvider,
    settings: ProviderSettings,
    tool_id: str,
    arguments: Mapping[str, Any],
    *,
    lease_ref: str = "12345678-1234-1234-1234-123456789abc",
    profile_ref: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Call through the same protobuf boundary used by DirectPluginRuntime."""

    envelope = {
        "payload": dict(arguments),
        "lease_ref": lease_ref,
        "trusted_context": dict(context or signed_context(settings, arguments, lease_ref, tool_id)),
    }
    if profile_ref is not None:
        envelope["profile_ref"] = profile_ref
        envelope["trusted_context"] = dict(
            context
            or signed_context(settings, arguments, lease_ref, tool_id, profile_ref=profile_ref)
        )
    request = wire.CallToolRequest(
        binding_id=settings.binding_id,
        provider_tool_id=tool_id,
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )
    ok, payload = provider.on_invoke("tool.provider.v1", "call_tool", request.SerializeToString())
    assert ok is True
    assert payload.type_url == CALL_TOOL_RESPONSE_TYPE_URL
    response = wire.CallToolResponse.FromString(payload.value)
    part = response.outcome.content[0]
    assert part.WhichOneof("content") == "json"
    return response.outcome.is_error, json.loads(part.json.json)


def test_catalog_has_exact_tools_and_closed_schemas() -> None:
    settings = make_settings()
    provider = GitHubIssuesProvider(settings=settings)
    request = wire.ListToolsRequest(binding_id=settings.binding_id)

    ok, payload = provider.on_invoke("tool.provider.v1", "list_tools", request.SerializeToString())

    assert ok is True
    catalog = wire.ListToolsResponse.FromString(payload.value).catalog
    assert [tool.provider_tool_id for tool in catalog.tools] == [
        "github.issue.get",
        "github.issue.comment.create",
        "github.issue.state.update",
        "github.repository.list",
        "github.repository.get",
        "github.branches.list",
        "github.pull_requests.list",
        "github.pull_request.get",
        "github.actions.runs.list",
        "github.ci.status",
        "github.issues.search",
        "github.branch.get",
        "github.file.get",
        "github.actions.jobs.list",
        "github.actions.job.logs.read",
        "github.branch.create",
        "github.branch.delete",
        "github.file.commit",
        "github.pull_request.draft.create",
        "github.pull_request.draft.find",
        "github.actions.rerun_failed",
        "github_vendor_cli",
    ]
    schemas = [json.loads(tool.input_schema_json) for tool in catalog.tools]
    assert all(schema["additionalProperties"] is False for schema in schemas)
    assert schemas[2]["properties"]["state"]["enum"] == ["OPEN", "CLOSED"]
    pull_requests_tool = next(
        tool for tool in catalog.tools if tool.provider_tool_id == "github.pull_requests.list"
    )
    pull_requests_schema = json.loads(pull_requests_tool.input_schema_json)
    assert "page_cursor" in pull_requests_schema["properties"]
    assert "page_cursor" not in pull_requests_schema["required"]
    assert pull_requests_schema["properties"]["page_cursor"]["pattern"] == (
        "^(?:[1-9][0-9]{0,2}|1000)$"
    )
    workflow_runs_tool = next(
        tool for tool in catalog.tools if tool.provider_tool_id == "github.actions.runs.list"
    )
    workflow_runs_schema = json.loads(workflow_runs_tool.input_schema_json)
    assert "page" not in workflow_runs_schema["properties"]
    assert "per_page" not in workflow_runs_schema["properties"]
    rerun_tool = next(
        tool for tool in catalog.tools if tool.provider_tool_id == "github.actions.rerun_failed"
    )
    rerun_schema = json.loads(rerun_tool.input_schema_json)
    assert "expected_workflow_id" in rerun_schema["required"]
    assert "expected_workflow_id" in rerun_schema["properties"]
    assert "workflow_id" not in rerun_schema["properties"]
    assert schemas[-1]["required"] == ["argv"]
    assert schemas[-1]["additionalProperties"] is False


def test_repository_list_dispatch_accepts_schema_without_required_fields() -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    api = FakeGitHubApi()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, github_api=api)

    is_error, result = invoke_tool(
        provider,
        settings,
        "github.repository.list",
        {},
        profile_ref="00000000-0000-4000-8000-000000000001",
    )

    assert is_error is False
    assert result["repositories"] == [
        {"repository_id": "12345", "repository": "owner/project", "private": True}
    ]
    assert len(account.calls) == 1
    assert api.calls[0][0] == "repositories"
    assert "installation-token-must-not-escape" not in json.dumps(result)


def test_pull_request_list_keeps_state_only_calls_on_first_page_and_accepts_cursor() -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    api = FakeGitHubApi()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, github_api=api)
    repository = settings.repositories[0]
    profile_ref = "00000000-0000-4000-8000-000000000001"

    first_page_error, first_page = invoke_tool(
        provider,
        settings,
        "github.pull_requests.list",
        {
            "repository_id": repository.repository_id,
            "repository": repository.slug,
            "state": "ALL",
        },
        profile_ref=profile_ref,
    )
    second_page_error, second_page = invoke_tool(
        provider,
        settings,
        "github.pull_requests.list",
        {
            "repository_id": repository.repository_id,
            "repository": repository.slug,
            "state": "ALL",
            "page_cursor": "2",
        },
        lease_ref="22345678-1234-1234-1234-123456789abc",
        profile_ref=profile_ref,
    )

    assert first_page_error is False
    assert first_page["page_cursor"] == "1"
    assert first_page["complete"] is True
    assert api.calls[0][1][-1]["page_cursor"] is None
    assert second_page_error is False
    assert second_page["page_cursor"] == "2"
    assert len(api.calls) == 2
    assert api.calls[0][0] == "pull_requests"
    assert api.calls[0][1][-1]["page_cursor"] is None
    assert api.calls[1][1][-1]["page_cursor"] == "2"


def test_rerun_tool_rejects_noncanonical_workflow_id_argument_name() -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    api = FakeGitHubApi()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, github_api=api)
    arguments = {
        "repository_id": "12345",
        "repository": "owner/project",
        "run_id": "99",
        "workflow_id": "22",
        "expected_head_branch": "cyrene/assistant/task",
        "expected_head_sha": "a" * 40,
        "expected_attempt": 1,
    }

    lease_ref = "12345678-1234-1234-1234-123456789abc"
    profile_ref = "00000000-0000-4000-8000-000000000001"
    envelope = {
        "payload": arguments,
        "lease_ref": lease_ref,
        "profile_ref": profile_ref,
        "trusted_context": signed_context(
            settings,
            arguments,
            lease_ref,
            "github.actions.rerun_failed",
            profile_ref=profile_ref,
        ),
    }
    request = wire.CallToolRequest(
        binding_id=settings.binding_id,
        provider_tool_id="github.actions.rerun_failed",
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )

    ok, result = provider.on_invoke(
        "tool.provider.v1", "call_tool", request.SerializeToString()
    )

    assert ok is False
    assert result == "INVALID_ARGUMENTS: tool arguments do not match the schema"
    assert account.calls == []
    assert api.calls == []


def test_vendor_cli_uses_profile_lease_and_returns_suppressed_receipt() -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    runner = FakeCliRunner()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, cli_runner=runner)
    arguments = {"argv": ["pr", "list", "--repo", "owner/project"]}
    profile_ref = "00000000-0000-4000-8000-000000000001"

    is_error, result = invoke_tool(
        provider,
        settings,
        "github_vendor_cli",
        arguments,
        profile_ref=profile_ref,
    )

    assert is_error is False
    assert result["state"] == "COMMAND_COMPLETED"
    assert result["remote_state"] == "NOT_APPLICABLE"
    assert result["stdout_suppressed"] is True
    assert result["stderr_suppressed"] is True
    assert "secret-token-must-not-escape" not in json.dumps(result)
    assert account.calls[0][0] == {
        "lease_ref": "12345678-1234-1234-1234-123456789abc",
        "profile_ref": profile_ref,
        "operation_id": "github.vendor.cli.execute",
        "target": {},
        "arguments_sha256": hashlib.sha256(canonical_json(arguments)).hexdigest(),
    }
    assert runner.calls[0]["executable"] == "/usr/local/bin/gh"
    assert runner.calls[0]["argv"] == arguments["argv"]
    assert runner.calls[0]["credential_environment"] == {
        "GH_TOKEN": "installation-token-must-not-escape"
    }


def test_vendor_cli_mutation_exit_zero_stays_remote_state_unverified() -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    runner = FakeCliRunner(VendorCliResult(0, 10, False, False, 0, 0))
    provider = GitHubIssuesProvider(settings=settings, account_center=account, cli_runner=runner)

    is_error, result = invoke_tool(
        provider,
        settings,
        "github_vendor_cli",
        {"argv": ["issue", "close", "7", "--repo", "owner/project"]},
        profile_ref="00000000-0000-4000-8000-000000000001",
    )

    assert is_error is False
    assert result["state"] == "REMOTE_STATE_UNVERIFIED"
    assert result["command_state"] == "COMMAND_COMPLETED"
    assert result["remote_state"] == "UNKNOWN_RESULT"
    assert result["operation_ref"].startswith("ghcli_")
    assert result["reconciliation_available"] is False


@pytest.mark.parametrize(
    "argv",
    [
        ["auth", "token"],
        ["api", "https://attacker.invalid/exfiltrate"],
        ["release", "upload", "v1", "./secret.bin"],
        ["pr", "list", "--repo", "other/private"],
        ["api", "/user", "--hostname", "attacker.invalid"],
        ["issue", "create", "--body-file", "/tmp/body.md"],
    ],
)
def test_vendor_cli_rejects_credentials_external_hosts_local_files_and_scope_escape(
    argv: list[str],
) -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    runner = FakeCliRunner()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, cli_runner=runner)

    envelope = {
        "payload": {"argv": argv},
        "profile_ref": "00000000-0000-4000-8000-000000000001",
        "lease_ref": "12345678-1234-1234-1234-123456789abc",
        "trusted_context": signed_context(
            settings,
            {"argv": argv},
            "12345678-1234-1234-1234-123456789abc",
            "github_vendor_cli",
            profile_ref="00000000-0000-4000-8000-000000000001",
        ),
    }
    request = wire.CallToolRequest(
        binding_id=settings.binding_id,
        provider_tool_id="github_vendor_cli",
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )
    ok, result = provider.on_invoke(
        "tool.provider.v1", "call_tool", request.SerializeToString()
    )

    assert ok is False
    assert result == "INVALID_ARGUMENTS: tool arguments do not match the schema"
    assert account.calls == []
    assert runner.calls == []


def test_repository_owner_scope_and_arbitrary_url_are_rejected_before_redeem() -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    api = FakeGitHubApi()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, github_api=api)
    invalid = {"repository_id": "12345", "repository": "other/project", "issue_number": 7}

    is_error, result = invoke_tool(provider, settings, "github.issue.get", invalid)

    assert is_error is True
    assert result["error"]["code"] == "INVALID_ARGUMENTS"
    assert account.calls == []
    assert api.calls == []

    arbitrary = {
        "repository_id": "12345",
        "repository": "owner/project",
        "issue_number": 7,
        "url": "https://attacker.invalid/steal",
    }
    lease_ref = "12345678-1234-1234-1234-123456789abc"
    envelope = {
        "payload": arbitrary,
        "lease_ref": lease_ref,
        "trusted_context": signed_context(settings, arbitrary, lease_ref, "github.issue.get"),
    }
    request = wire.CallToolRequest(
        binding_id=settings.binding_id,
        provider_tool_id="github.issue.get",
        arguments_json=json.dumps(envelope),
    )
    ok, result = provider.on_invoke("tool.provider.v1", "call_tool", request.SerializeToString())
    assert ok is False
    assert result == "INVALID_ARGUMENTS: tool arguments do not match the schema"
    assert account.calls == []


def test_bad_trusted_context_is_denied_without_installation_fallback() -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    api = FakeGitHubApi()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, github_api=api)
    arguments = {"repository_id": "12345", "repository": "owner/project", "issue_number": 7}
    context = signed_context(
        settings,
        arguments,
        "12345678-1234-1234-1234-123456789abc",
        "github.issue.get",
    )
    context["subject"] = "attacker"

    is_error, result = invoke_tool(
        provider, settings, "github.issue.get", arguments, context=context
    )

    assert is_error is True
    assert result["error"]["code"] == "AUTHENTICATION_DENIED"
    assert account.calls == []
    assert api.calls == []


def test_missing_account_port_fails_closed_and_never_uses_installation_credentials() -> None:
    settings = make_settings()
    api = FakeGitHubApi()
    provider = GitHubIssuesProvider(settings=settings, github_api=api)
    arguments = {"repository_id": "12345", "repository": "owner/project", "issue_number": 7}

    is_error, result = invoke_tool(provider, settings, "github.issue.get", arguments)

    assert is_error is True
    assert result["error"]["code"] == "AUTHORIZATION_UNAVAILABLE"
    assert api.calls == []


def test_user_token_stays_private_and_lease_payload_is_actor_operation_scoped() -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    api = FakeGitHubApi()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, github_api=api)
    arguments = {"repository_id": "12345", "repository": "owner/project", "issue_number": 7}

    is_error, result = invoke_tool(provider, settings, "github.issue.get", arguments)

    assert is_error is False
    assert result["issue_id"] == "555"
    assert "secret-token-must-not-escape" not in json.dumps(result)
    payload, trusted_context = account.calls[0]
    assert payload == {
        "lease_ref": "12345678-1234-1234-1234-123456789abc",
        "operation_id": "github.issue.get",
        "resource": {"repository_id": "12345", "repository": "owner/project", "issue_number": 7},
        "arguments_sha256": hashlib.sha256(canonical_json(arguments)).hexdigest(),
    }
    assert trusted_context["subject"] == "member-27"
    assert trusted_context["caller_id"] == settings.provider_caller_id
    assert trusted_context["binding_id"] == settings.account_center_binding_id
    assert api.calls[0][0] == "get"


def test_profile_lease_binds_exact_target_and_reads_pull_request() -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    api = FakeGitHubApi()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, github_api=api)
    arguments = {
        "repository_id": "12345",
        "repository": "owner/project",
        "pull_number": 4,
    }
    lease_ref = "12345678-1234-1234-1234-123456789abc"
    profile_ref = "00000000-0000-4000-8000-000000000001"

    is_error, result = invoke_tool(
        provider,
        settings,
        "github.pull_request.get",
        arguments,
        lease_ref=lease_ref,
        profile_ref=profile_ref,
    )

    assert is_error is False
    assert result["pull_request"]["number"] == 4
    lease_payload, trusted_context = account.calls[0]
    assert lease_payload == {
        "lease_ref": lease_ref,
        "profile_ref": profile_ref,
        "operation_id": "github.pull_request.get",
        "target": {
            "repository_id": "12345",
            "repository": "owner/project",
            "pull_number": 4,
        },
        "arguments_sha256": hashlib.sha256(canonical_json(arguments)).hexdigest(),
    }
    assert trusted_context["method"] == "redeem_provider_credential_lease"
    assert api.calls[0][0] == "pull_request"


def test_new_github_tools_use_profile_leases_with_locator_targets_and_full_digests() -> None:
    settings = make_settings()
    profile_ref = "00000000-0000-4000-8000-000000000001"
    repo = {"repository_id": "12345", "repository": "owner/project"}
    branch = "cyrene/assistant/lease-test"
    sha = "a" * 40
    cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        (
            "github.issues.search",
            {
                **repo,
                "query": "workflow status",
                "state": "OPEN",
                "labels": ["bug"],
                "updated_since": "2026-10-24T12:00:00Z",
                "updated_until": "2026-10-24T12:05:00+00:00",
                "page_cursor": "1",
                "limit": 20,
            },
            {**repo},
        ),
        ("github.branch.get", {**repo, "branch": branch}, {**repo, "branch": branch}),
        (
            "github.file.get",
            {**repo, "branch": branch, "path": "src/main.py"},
            {**repo, "branch": branch, "path": "src/main.py"},
        ),
        (
            "github.actions.jobs.list",
            {**repo, "run_id": "12345", "limit": 20},
            {**repo, "run_id": "12345"},
        ),
        (
            "github.actions.job.logs.read",
            {**repo, "run_id": "12345", "job_id": "67890", "max_lines": 50},
            {**repo, "run_id": "12345", "job_id": "67890"},
        ),
        (
            "github.branch.create",
            {**repo, "branch": branch, "from_sha": sha},
            {**repo, "branch": branch},
        ),
        (
            "github.branch.delete",
            {**repo, "branch": branch, "expected_sha": sha},
            {**repo, "branch": branch},
        ),
        (
            "github.file.commit",
            {
                **repo,
                "branch": branch,
                "path": "src/main.py",
                "message": "Update source",
                "content_base64": "aGVsbG8=",
            },
            {**repo, "branch": branch, "path": "src/main.py"},
        ),
        (
            "github.pull_request.draft.create",
            {
                **repo,
                "head_branch": branch,
                "base_branch": "develop",
                "title": "Automated task",
                "body": "Evidence and changes",
            },
            {**repo, "head_branch": branch, "base_branch": "develop"},
        ),
        (
            "github.pull_request.draft.find",
            {**repo, "head_branch": branch, "base_branch": "develop"},
            {**repo, "head_branch": branch, "base_branch": "develop"},
        ),
        (
            "github.actions.rerun_failed",
            {
                **repo,
                "run_id": "12345",
                "expected_workflow_id": "22",
                "expected_head_branch": branch,
                "expected_head_sha": sha,
                "expected_attempt": 2,
            },
            {**repo, "run_id": "12345"},
        ),
    ]

    for index, (tool_id, arguments, expected_target) in enumerate(cases):
        account = FakeAccountCenter()
        api = FakeGitHubApi()
        provider = GitHubIssuesProvider(settings=settings, account_center=account, github_api=api)
        error, result = invoke_tool(
            provider,
            settings,
            tool_id,
            arguments,
            lease_ref=f"12345678-1234-1234-1234-{index:012d}",
            profile_ref=profile_ref,
        )

        assert error is False, (tool_id, result)
        payload, trusted_context = account.calls[0]
        assert payload["profile_ref"] == profile_ref
        assert payload["operation_id"] == tool_id
        assert payload["target"] == expected_target
        assert payload["arguments_sha256"] == hashlib.sha256(
            canonical_json(arguments)
        ).hexdigest()
        assert trusted_context["method"] == "redeem_provider_credential_lease"
        assert "installation-token-must-not-escape" not in json.dumps(result)
        assert api.calls


@pytest.mark.parametrize(
    ("tool_id", "arguments"),
    [
        (
            "github.branch.create",
            {
                "repository_id": "12345",
                "repository": "owner/project",
                "branch": "main",
                "from_sha": "a" * 40,
            },
        ),
        (
            "github.branch.create",
            {
                "repository_id": "12345",
                "repository": "owner/project",
                "branch": "feature/customer-fix",
                "from_sha": "a" * 40,
            },
        ),
        (
            "github.file.commit",
            {
                "repository_id": "12345",
                "repository": "owner/project",
                "branch": "cyrene/assistant/task",
                "path": ".github/workflows/ci.yml",
                "message": "change workflow",
                "content_base64": "aGVsbG8=",
            },
        ),
        (
            "github.file.commit",
            {
                "repository_id": "12345",
                "repository": "owner/project",
                "branch": "cyrene/assistant/task",
                "path": "infrastructure.yaml",
                "message": "update deployment",
                "content_base64": "aGVsbG8=",
            },
        ),
        (
            "github.file.commit",
            {
                "repository_id": "12345",
                "repository": "owner/project",
                "branch": "cyrene/assistant/task",
                "path": "config/app-prod.json",
                "message": "change production config",
                "content_base64": "aGVsbG8=",
            },
        ),
        (
            "github.file.commit",
            {
                "repository_id": "12345",
                "repository": "owner/project",
                "branch": "cyrene/assistant/task",
                "path": "src/main.py",
                "message": "write",
                "content_base64": "not base64",
            },
        ),
    ],
)
def test_github_write_scope_is_rejected_before_lease(
    tool_id: str, arguments: Mapping[str, Any]
) -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    api = FakeGitHubApi()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, github_api=api)

    lease_ref = "12345678-1234-1234-1234-123456789abc"
    profile_ref = "00000000-0000-4000-8000-000000000001"
    envelope = {
        "payload": dict(arguments),
        "lease_ref": lease_ref,
        "profile_ref": profile_ref,
        "trusted_context": signed_context(
            settings, arguments, lease_ref, tool_id, profile_ref=profile_ref
        ),
    }
    request = wire.CallToolRequest(
        binding_id=settings.binding_id,
        provider_tool_id=tool_id,
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )
    is_error, result = provider.on_invoke(
        "tool.provider.v1", "call_tool", request.SerializeToString()
    )

    assert is_error is False
    assert result == "INVALID_ARGUMENTS: tool arguments do not match the schema"
    assert account.calls == []
    assert api.calls == []


@pytest.mark.parametrize(
    ("tool_id", "arguments", "profile_ref"),
    [
        (
            "github.issues.search",
            {
                "repository_id": "12345",
                "repository": "owner/project",
                "state": [],
                "limit": 1,
            },
            "00000000-0000-4000-8000-000000000001",
        ),
        (
            "github.issue.state.update",
            {
                "repository_id": "12345",
                "repository": "owner/project",
                "issue_number": 7,
                "state": {},
            },
            None,
        ),
        (
            "github.pull_requests.list",
            {
                "repository_id": "12345",
                "repository": "owner/project",
                "state": [],
            },
            "00000000-0000-4000-8000-000000000001",
        ),
        (
            "github.pull_requests.list",
            {
                "repository_id": "12345",
                "repository": "owner/project",
                "state": "ALL",
                "page_cursor": "1001",
            },
            "00000000-0000-4000-8000-000000000001",
        ),
    ],
)
def test_invalid_state_or_cursor_values_are_rejected_without_provider_access(
    tool_id: str,
    arguments: Mapping[str, Any],
    profile_ref: str | None,
) -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    api = FakeGitHubApi()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, github_api=api)
    lease_ref = "12345678-1234-1234-1234-123456789abc"
    envelope: dict[str, Any] = {
        "payload": dict(arguments),
        "lease_ref": lease_ref,
        "trusted_context": signed_context(settings, arguments, lease_ref, tool_id, profile_ref),
    }
    if profile_ref is not None:
        envelope["profile_ref"] = profile_ref
    request = wire.CallToolRequest(
        binding_id=settings.binding_id,
        provider_tool_id=tool_id,
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )

    is_error, result = provider.on_invoke(
        "tool.provider.v1", "call_tool", request.SerializeToString()
    )

    assert is_error is False
    assert result == "INVALID_ARGUMENTS: tool arguments do not match the schema"
    assert account.calls == []
    assert api.calls == []


def test_issue_search_rejects_an_inverted_updated_window_before_lease() -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    api = FakeGitHubApi()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, github_api=api)
    arguments = {
        "repository_id": "12345",
        "repository": "owner/project",
        "query": "bug",
        "state": "OPEN",
        "updated_since": "2026-10-24T12:05:00Z",
        "updated_until": "2026-10-24T12:00:00Z",
        "limit": 20,
    }
    lease_ref = "12345678-1234-1234-1234-123456789abc"
    profile_ref = "00000000-0000-4000-8000-000000000001"
    envelope = {
        "payload": arguments,
        "lease_ref": lease_ref,
        "profile_ref": profile_ref,
        "trusted_context": signed_context(
            settings, arguments, lease_ref, "github.issues.search", profile_ref
        ),
    }
    request = wire.CallToolRequest(
        binding_id=settings.binding_id,
        provider_tool_id="github.issues.search",
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )

    is_error, result = provider.on_invoke(
        "tool.provider.v1", "call_tool", request.SerializeToString()
    )

    assert is_error is False
    assert result == "INVALID_ARGUMENTS: tool arguments do not match the schema"
    assert account.calls == []
    assert api.calls == []


def test_lease_reference_is_one_use_and_not_redeemed_again_after_replay() -> None:
    settings = make_settings()
    account = FakeAccountCenter()
    api = FakeGitHubApi()
    provider = GitHubIssuesProvider(settings=settings, account_center=account, github_api=api)
    arguments = {"repository_id": "12345", "repository": "owner/project", "issue_number": 7}
    lease = "12345678-1234-1234-1234-123456789abc"

    invoke_tool(provider, settings, "github.issue.get", arguments, lease_ref=lease)
    is_error, result = invoke_tool(
        provider, settings, "github.issue.get", arguments, lease_ref=lease
    )

    assert is_error is True
    assert result["error"]["code"] == "AUTHORIZATION_UNAVAILABLE"
    assert len(account.calls) == 1
    assert len(api.calls) == 1


def test_webhook_signature_is_checked_before_raw_json_decode() -> None:
    settings = make_settings()
    provider = GitHubIssuesProvider(settings=settings)
    raw_body = b"not-json"
    request = {
        "delivery_id": "delivery-1",
        "event": "issues",
        "signature": "sha256=" + "0" * 64,
        "raw_body_base64": base64.b64encode(raw_body).decode("ascii"),
    }

    ok, payload = provider.on_invoke(
        "tool.provider.v1",
        "normalize_webhook",
        json.dumps(request, separators=(",", ":")).encode(),
        request_type_url=WEBHOOK_REQUEST_TYPE_URL,
    )

    assert ok is True
    response = json.loads(payload.value)
    assert response == {"error": {"code": "INVALID_SIGNATURE"}}


def test_valid_webhook_is_normalized_only_after_signature_and_allowlist_checks() -> None:
    settings = make_settings()
    provider = GitHubIssuesProvider(settings=settings)
    raw_body = json.dumps(
        {
            "action": "opened",
            "installation": {"id": 99},
            "repository": {"id": 12345, "full_name": "owner/project"},
            "issue": {
                "id": 555,
                "number": 7,
                "state": "open",
                "title": "Task title",
                "body": "Task body",
            },
            "sender": {"id": 700, "login": "reporter"},
        },
        separators=(",", ":"),
    ).encode()
    signature = (
        "sha256=" + hmac.new(settings.github_webhook_secret, raw_body, hashlib.sha256).hexdigest()
    )
    request = {
        "delivery_id": "delivery-2",
        "event": "issues",
        "signature": signature,
        "raw_body_base64": base64.b64encode(raw_body).decode("ascii"),
    }

    ok, payload = provider.on_invoke(
        "tool.provider.v1",
        "normalize_webhook",
        json.dumps(request, separators=(",", ":")).encode(),
        request_type_url=WEBHOOK_REQUEST_TYPE_URL,
    )

    assert ok is True
    assert json.loads(payload.value) == {
        "event_type": "issues",
        "action": "opened",
        "repository_id": "12345",
        "issue_number": 7,
        "sender_id": "700",
        "sender_login": "reporter",
        "title": "Task title",
        "body": "Task body",
        "self_authored": False,
    }


def test_issue_comment_webhook_returns_a_deterministic_closed_projection() -> None:
    settings = replace(
        make_settings(),
        allowed_events=frozenset({"issue_comment"}),
        allowed_actions=frozenset({"created"}),
    )
    provider = GitHubIssuesProvider(settings=settings)
    raw_body = json.dumps(
        {
            "action": "created",
            "installation": {"id": 99},
            "repository": {"id": 12345, "full_name": "owner/project"},
            "issue": {
                "id": 555,
                "number": 7,
                "title": "Task title",
                "body": "Original issue body",
                "user": {"id": 500, "login": "original-author"},
            },
            "comment": {"id": 888, "body": "A member comment"},
            "sender": {"id": 700, "login": "comment-author"},
        },
        separators=(",", ":"),
    ).encode()
    signature = (
        "sha256=" + hmac.new(settings.github_webhook_secret, raw_body, hashlib.sha256).hexdigest()
    )

    def normalize(delivery_id: str) -> dict[str, Any]:
        request = {
            "delivery_id": delivery_id,
            "event": "issue_comment",
            "signature": signature,
            "raw_body_base64": base64.b64encode(raw_body).decode("ascii"),
        }
        ok, payload = provider.on_invoke(
            "tool.provider.v1",
            "normalize_webhook",
            json.dumps(request, separators=(",", ":")).encode(),
            request_type_url=WEBHOOK_REQUEST_TYPE_URL,
        )
        assert ok is True
        return json.loads(payload.value)

    expected = {
        "event_type": "issue_comment",
        "action": "created",
        "repository_id": "12345",
        "issue_number": 7,
        "sender_id": "700",
        "sender_login": "comment-author",
        "title": "Task title",
        "body": "A member comment",
        "self_authored": False,
    }
    assert normalize("delivery-a") == expected
    assert normalize("delivery-b") == expected


def test_webhook_envelope_rejects_unbound_connection_identity() -> None:
    settings = make_settings()
    provider = GitHubIssuesProvider(settings=settings)
    raw_body = b"{}"
    request = {
        "connection_id": "caller-selected-connection",
        "delivery_id": "delivery-1",
        "event": "issues",
        "signature": "sha256="
        + hmac.new(settings.github_webhook_secret, raw_body, hashlib.sha256).hexdigest(),
        "raw_body_base64": base64.b64encode(raw_body).decode("ascii"),
    }

    ok, result = provider.on_invoke(
        "tool.provider.v1",
        "normalize_webhook",
        json.dumps(request, separators=(",", ":")).encode(),
        request_type_url=WEBHOOK_REQUEST_TYPE_URL,
    )

    assert ok is True
    assert result.type_url.endswith("WebhookResponse")
    assert json.loads(result.value) == {"error": {"code": "INVALID_REQUEST"}}


def test_github_transport_uses_only_fixed_host_and_sanitizes_token_and_errors() -> None:
    settings = make_settings()
    observed: list[tuple[str, str, str, dict[str, str], bytes | None]] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        observed.append((method, host, path, dict(headers), body))
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        return HttpResponse(403, {}, b'{"message":"private token value must not escape"}')

    api = GitHubApi(settings, transport=transport)
    try:
        api.get_issue(settings.repositories[0], 7, "private-token-value")
    except GitHubApiFailure as error:
        assert error.code == "AUTHORIZATION_DENIED"
        assert "private-token-value" not in str(error)
    else:
        raise AssertionError("authorization failure was not raised")

    assert [call[1] for call in observed] == ["api.github.com", "api.github.com"]
    assert observed[0][2] == "/repos/owner/project"
    assert observed[1][2] == "/repos/owner/project/issues/7"
    assert observed[0][3]["Authorization"] == "Bearer private-token-value"


def test_github_repository_branch_pull_and_ci_reads_use_fixed_allowlisted_paths() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    observed: list[str] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        assert host == "api.github.com"
        assert method == "GET"
        observed.append(path)
        if path == "/installation/repositories?per_page=100":
            return HttpResponse(
                200,
                {},
                b'{"repositories":[{"id":12345,"full_name":"owner/project","private":true,"default_branch":"main","html_url":"https://github.com/owner/project"}]}',
            )
        if path == "/repos/owner/project":
            return HttpResponse(
                200,
                {},
                b'{"id":12345,"full_name":"owner/project","private":true,"default_branch":"main","html_url":"https://github.com/owner/project"}',
            )
        if path.endswith("/branches?per_page=100"):
            return HttpResponse(200, {}, b'[{"name":"main","commit":{"sha":"abc1234567890"}}]')
        if path.endswith(
            f"/pulls?state=open&per_page={MAX_PULL_REQUEST_PAGE_SIZE}&page=1"
        ):
            return HttpResponse(
                200,
                {},
                b'[{"number":4,"state":"open","title":"Review","head":{"sha":"deadbeef1234567"},"base":{"ref":"main"},"html_url":"https://github.com/owner/project/pull/4","draft":false}]',
            )
        if path.endswith("/pulls/4"):
            return HttpResponse(
                200,
                {},
                b'{"number":4,"state":"open","title":"Review","head":{"sha":"deadbeef1234567","ref":"cyrene/assistant/task","repo":{"id":12345,"full_name":"owner/project"}},"base":{"sha":"feedface1234567","ref":"main","repo":{"id":12345,"full_name":"owner/project"}},"repository":{"id":12345,"full_name":"owner/project"}}',
            )
        if path.endswith("/actions/runs?per_page=100&branch=main"):
            return HttpResponse(
                200,
                {},
                b'{"total_count":1,"workflow_runs":[{"id":99,"workflow_id":11,"name":"CI","path":".github/workflows/ci.yml","status":"completed","conclusion":"success","head_sha":"deadbeef1234567","head_branch":"main","run_attempt":1}]}',
            )
        if path.endswith("/commits/deadbeef1234567/status?per_page=100"):
            return HttpResponse(
                200,
                {},
                b'{"state":"success","total_count":1,"statuses":[{"context":"tests","state":"success"}]}',
            )
        if path.endswith("/commits/deadbeef1234567/check-runs?per_page=100"):
            return HttpResponse(
                200,
                {},
                b'{"total_count":1,"check_runs":[{"name":"unit","status":"completed","conclusion":"success"}]}',
            )
        raise AssertionError(path)

    api = GitHubApi(settings, transport=transport)
    assert api.list_repositories("token")["repositories"][0]["repository_id"] == "12345"
    assert api.get_repository(repository, "token")["default_branch"] == "main"
    assert api.list_branches(repository, "token")["branches"][0]["name"] == "main"
    pull_request_page = api.list_pull_requests(repository, "OPEN", "token")
    listed_pull_request = pull_request_page["pull_requests"][0]
    assert listed_pull_request["number"] == 4
    assert listed_pull_request["merge_status"] == "OPEN"
    assert pull_request_page["page_cursor"] == "1"
    assert pull_request_page["complete"] is True
    assert pull_request_page["truncated"] is False
    assert pull_request_page["has_more"] is False
    assert pull_request_page["next_cursor"] is None
    assert "merged" not in listed_pull_request
    assert "merged_at" not in listed_pull_request
    assert "merge_commit_sha" not in listed_pull_request
    pull_request = api.get_pull_request(repository, 4, "token")
    assert pull_request["pull_request"]["head_sha"] == "deadbeef1234567"
    assert pull_request["pull_request"]["merge_status"] == "OPEN"
    assert "merged" not in pull_request["pull_request"]
    workflow_runs = api.list_workflow_runs(repository, "token", branch="main")
    assert workflow_runs["workflow_runs"][0]["status"] == "COMPLETED"
    assert workflow_runs["complete"] is True
    assert workflow_runs["truncated"] is False
    ci = api.get_ci_status(repository, "deadbeef1234567", "token")
    assert ci["state"] == "SUCCESS"
    assert ci["complete"] is True
    assert ci["truncated"] is False
    assert observed.count("/repos/owner/project") == 6
    assert all(
        path.startswith(("/repos/owner/project", "/installation/repositories")) for path in observed
    )


def test_pull_request_pages_stay_under_response_cap_and_report_next_cursor() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    synthetic = [
        {
            "number": index + 1,
            "state": "closed",
            "title": f"Synthetic pull request {index + 1}",
            "body": "x" * 18_000,
            "head": {"sha": f"{index + 1:040x}", "ref": f"feature-{index + 1}"},
            "base": {"sha": "b" * 40, "ref": "develop"},
        }
        for index in range(100)
    ]
    legacy_body = json.dumps(synthetic, separators=(",", ":")).encode()
    returned_items = synthetic[:25]
    page_one_body = json.dumps(
        returned_items[:MAX_PULL_REQUEST_PAGE_SIZE], separators=(",", ":")
    ).encode()
    assert MAX_RESPONSE_BYTES < len(legacy_body) < 2_000_000
    assert len(page_one_body) < MAX_RESPONSE_BYTES
    observed_paths: list[str] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        assert method == "GET"
        assert host == "api.github.com"
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        observed_paths.append(path)
        page = 1 if path.endswith("page=1") else 2 if path.endswith("page=2") else None
        assert page is not None
        start = (page - 1) * MAX_PULL_REQUEST_PAGE_SIZE
        selected = returned_items[start : start + MAX_PULL_REQUEST_PAGE_SIZE]
        response_body = json.dumps(selected, separators=(",", ":")).encode()
        assert len(response_body) <= MAX_RESPONSE_BYTES
        response_headers = (
            {
                "link": (
                    "<https://api.github.com/repositories/12345/pulls?state=all&"
                    f"per_page={MAX_PULL_REQUEST_PAGE_SIZE}&page={page + 1}>; rel=\"next\""
                )
            }
            if page == 1
            else {}
        )
        return HttpResponse(200, response_headers, response_body)

    api = GitHubApi(settings, transport=transport)
    first = api.list_pull_requests(repository, "ALL", "token", page_cursor="1")
    second = api.list_pull_requests(repository, "ALL", "token", page_cursor="2")

    assert observed_paths == [
        f"/repos/owner/project/pulls?state=all&per_page={MAX_PULL_REQUEST_PAGE_SIZE}&page=1",
        f"/repos/owner/project/pulls?state=all&per_page={MAX_PULL_REQUEST_PAGE_SIZE}&page=2",
    ]
    assert first["page_cursor"] == "1"
    assert len(first["pull_requests"]) == MAX_PULL_REQUEST_PAGE_SIZE
    assert first["complete"] is False
    assert first["truncated"] is True
    assert first["has_more"] is True
    assert first["next_cursor"] == "2"
    assert first["next_cursor_source"] == "github_link"
    assert first["result_limit_reached"] is False
    assert second["page_cursor"] == "2"
    assert len(second["pull_requests"]) == 5
    assert second["complete"] is False
    assert second["truncated"] is True
    assert second["has_more"] is False
    assert second["next_cursor"] is None


def test_state_only_pull_request_list_batches_five_pages_with_a_hard_result_cap() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    synthetic = [
        {
            "number": index + 1,
            "state": "closed",
            "title": f"Synthetic pull request {index + 1}",
            "body": "x" * 18_000,
            "head": {"sha": f"{index + 1:040x}", "ref": f"feature-{index + 1}"},
            "base": {"sha": "b" * 40, "ref": "develop"},
        }
        for index in range(120)
    ]
    legacy_body = json.dumps(synthetic[:100], separators=(",", ":")).encode()
    assert len(legacy_body) > MAX_RESPONSE_BYTES
    observed_pages: list[int] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        assert method == "GET"
        assert host == "api.github.com"
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        page = int(path.rsplit("page=", 1)[1])
        observed_pages.append(page)
        start = (page - 1) * MAX_PULL_REQUEST_PAGE_SIZE
        selected = synthetic[start : start + MAX_PULL_REQUEST_PAGE_SIZE]
        response_body = json.dumps(selected, separators=(",", ":")).encode()
        assert len(response_body) <= MAX_RESPONSE_BYTES
        response_headers = {
            "link": (
                "<https://api.github.com/repositories/12345/pulls?state=all&"
                f"per_page={MAX_PULL_REQUEST_PAGE_SIZE}&page={page + 1}>; rel=\"next\""
            )
        }
        return HttpResponse(200, response_headers, response_body)

    result = GitHubApi(settings, transport=transport).list_pull_requests(
        repository, "ALL", "token"
    )

    assert observed_pages == list(range(1, MAX_PULL_REQUEST_BATCH_PAGES + 1))
    assert len(result["pull_requests"]) == 100
    assert result["pages_read"] == MAX_PULL_REQUEST_BATCH_PAGES
    assert result["complete"] is False
    assert result["truncated"] is True
    assert result["has_more"] is True
    assert result["next_cursor"] == "6"
    assert result["result_limit_reached"] is True
    assert len(json.dumps(result, separators=(",", ":")).encode()) < MAX_RESPONSE_BYTES


def test_state_only_pull_request_batch_fails_on_a_later_page_error() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    pull_requests = [
        {
            "number": index + 1,
            "state": "closed",
            "title": f"Synthetic pull request {index + 1}",
            "body": "",
            "head": {"sha": f"{index + 1:040x}", "ref": "feature"},
            "base": {"sha": "b" * 40, "ref": "develop"},
        }
        for index in range(MAX_PULL_REQUEST_PAGE_SIZE)
    ]
    requested_pages: list[int] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        page = int(path.rsplit("page=", 1)[1])
        requested_pages.append(page)
        if page == 1:
            return HttpResponse(
                200,
                {
                    "link": (
                        "<https://api.github.com/repositories/12345/pulls?state=all&"
                        f"per_page={MAX_PULL_REQUEST_PAGE_SIZE}&page=2>; rel=\"next\""
                    )
                },
                json.dumps(pull_requests).encode(),
            )
        return HttpResponse(206, {}, b"[]")

    with pytest.raises(GitHubApiFailure, match="INVALID_PROVIDER_RESPONSE"):
        GitHubApi(settings, transport=transport).list_pull_requests(
            repository, "ALL", "token"
        )
    assert requested_pages == [1, 2]


@pytest.mark.parametrize(
    "link",
    [
        (
            "<https://example.invalid/repositories/12345/pulls?state=all&"
            'per_page=20&page=2>; rel="next"'
        ),
        (
            "<https://api.github.com/repositories/99999/pulls?state=all&"
            'per_page=20&page=2>; rel="next"'
        ),
        (
            "<https://api.github.com/repositories/12345/pulls?state=all&"
            'per_page=100&page=2>; rel="next"'
        ),
        (
            "<https://api.github.com/repositories/12345/pulls?state=all&"
            'per_page=20&page=3>; rel="next"'
        ),
    ],
)
def test_pull_request_list_rejects_untrusted_or_inconsistent_next_link(link: str) -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    pull_request = {
        "number": 1,
        "state": "closed",
        "title": "Synthetic",
        "body": "",
        "head": {"sha": "a" * 40, "ref": "feature"},
        "base": {"sha": "b" * 40, "ref": "develop"},
    }

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        return HttpResponse(200, {"link": link}, json.dumps([pull_request]).encode())

    with pytest.raises(GitHubApiFailure, match="INVALID_PROVIDER_RESPONSE"):
        GitHubApi(settings, transport=transport).list_pull_requests(
            repository, "ALL", "token"
        )


@pytest.mark.parametrize("status", [202, 206])
def test_pull_request_list_rejects_non_200_success_as_incomplete(status: int) -> None:
    settings = make_settings()
    repository = settings.repositories[0]

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        return HttpResponse(status, {}, b"[]")

    with pytest.raises(GitHubApiFailure, match="INVALID_PROVIDER_RESPONSE"):
        GitHubApi(settings, transport=transport).list_pull_requests(
            repository, "ALL", "token"
        )


def _pull_request_projection_input(
    *, state: str = "closed", **overrides: Any
) -> dict[str, Any]:
    """Build the minimum valid upstream PR shape for projection tests.

    中文：生成 PR projection 测试所需的最小有效上游响应。
    """

    value: dict[str, Any] = {
        "number": 17,
        "state": state,
        "title": "Merge-state projection",
        "body": "Evidence",
        "head": {"sha": "a" * 40, "ref": "feature"},
        "base": {"sha": "b" * 40, "ref": "develop"},
    }
    value.update(overrides)
    return value


def test_pull_request_merge_projection_distinguishes_closed_states_and_unknown() -> None:
    closed_unmerged = _pull_request_projection(
        _pull_request_projection_input(merged=False, merged_at=None)
    )
    merged = _pull_request_projection(
        _pull_request_projection_input(
            merged=True,
            merged_at="2026-10-26T12:30:00Z",
            merge_commit_sha="C" * 40,
        )
    )
    merged_at_only = _pull_request_projection(
        _pull_request_projection_input(merged_at="2026-10-26T12:30:00Z")
    )
    unknown_with_test_merge_sha = _pull_request_projection(
        _pull_request_projection_input(merge_commit_sha="D" * 40)
    )

    assert closed_unmerged["state"] == "CLOSED"
    assert closed_unmerged["merged"] is False
    assert closed_unmerged["merge_status"] == "CLOSED_UNMERGED"
    assert "merged_at" not in closed_unmerged

    assert merged["merged"] is True
    assert merged["merge_status"] == "MERGED"
    assert merged["merged_at"] == "2026-10-26T12:30:00Z"
    assert merged["merge_commit_sha"] == "c" * 40

    assert "merged" not in merged_at_only
    assert merged_at_only["merge_status"] == "MERGED"
    assert merged_at_only["merged_at"] == "2026-10-26T12:30:00Z"

    assert "merged" not in unknown_with_test_merge_sha
    assert "merged_at" not in unknown_with_test_merge_sha
    assert unknown_with_test_merge_sha["merge_status"] == "UNKNOWN"
    assert unknown_with_test_merge_sha["merge_commit_sha"] == "d" * 40


def test_pull_request_merge_projection_omits_unknown_source_fields() -> None:
    projected = _pull_request_projection(
        _pull_request_projection_input(state="closed")
    )
    closed_at_null = _pull_request_projection(
        _pull_request_projection_input(state="closed", merged_at=None)
    )

    assert projected["merge_status"] == "UNKNOWN"
    assert "merged" not in projected
    assert "merged_at" not in projected
    assert "merge_commit_sha" not in projected

    assert closed_at_null["merge_status"] == "CLOSED_UNMERGED"
    assert "merged" not in closed_at_null
    assert "merged_at" not in closed_at_null


@pytest.mark.parametrize(
    "overrides",
    [
        {"merge_commit_sha": "a" * 39},
        {"merge_commit_sha": "g" * 40},
        {"merged": "true"},
        {"merged_at": "not-a-timestamp"},
        {"state": "open", "merged": True},
        {"state": "open", "merged_at": "2026-10-26T12:30:00Z"},
        {"merged": True, "merged_at": None},
        {"merged": False, "merged_at": "2026-10-26T12:30:00Z"},
    ],
)
def test_pull_request_merge_projection_rejects_malformed_or_conflicting_facts(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(GitHubApiFailure, match="INVALID_PROVIDER_RESPONSE"):
        _pull_request_projection(_pull_request_projection_input(**overrides))


def test_workflow_run_list_reports_truncated_first_page() -> None:
    settings = make_settings()
    repository = settings.repositories[0]

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path == "/repos/owner/project/actions/runs?per_page=100":
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "total_count": 101,
                        "workflow_runs": [json.loads(_workflow_run_response())],
                    }
                ).encode(),
            )
        raise AssertionError((method, path))

    result = GitHubApi(settings, transport=transport).list_workflow_runs(
        repository, "token"
    )

    assert result["total_count"] == 101
    assert result["complete"] is False
    assert result["truncated"] is True


def test_ci_status_reports_incomplete_check_run_pages_instead_of_implying_green() -> None:
    settings = make_settings()
    repository = settings.repositories[0]

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path.endswith(
            "/commits/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/status?per_page=100"
        ):
            return HttpResponse(
                200,
                {},
                b'{"state":"success","total_count":0,"statuses":[]}',
            )
        if path.endswith(
            "/commits/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/check-runs?per_page=100"
        ):
            checks = [
                {"name": f"check-{index}", "status": "completed", "conclusion": "success"}
                for index in range(100)
            ]
            return HttpResponse(
                200,
                {},
                json.dumps({"total_count": 101, "check_runs": checks}).encode(),
            )
        raise AssertionError(path)

    result = GitHubApi(settings, transport=transport).get_ci_status(
        repository, "a" * 40, "token"
    )

    assert result["state"] == "SUCCESS"
    assert len(result["check_runs"]) == 100
    assert result["check_runs_total_count"] == 101
    assert result["complete"] is False
    assert result["truncated"] is True


def test_ci_pending_without_status_contexts_does_not_claim_required_contexts_are_missing() -> None:
    settings = make_settings()
    repository = settings.repositories[0]

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path.endswith(
            "/commits/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/status?per_page=100"
        ):
            return HttpResponse(200, {}, b'{"state":"pending","total_count":0,"statuses":[]}')
        if path.endswith(
            "/commits/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/check-runs?per_page=100"
        ):
            checks = [
                {"name": "unit", "status": "completed", "conclusion": "success"},
                {"name": "optional-smoke", "status": "completed", "conclusion": "skipped"},
            ]
            return HttpResponse(
                200,
                {},
                json.dumps({"total_count": len(checks), "check_runs": checks}).encode(),
            )
        raise AssertionError(path)

    result = GitHubApi(settings, transport=transport).get_ci_status(
        repository, "a" * 40, "token"
    )

    assert result["state"] == "PENDING"
    assert result["statuses"] == []
    assert result["status_contexts_present"] is False
    assert result["status_contexts_complete"] is True
    assert result["check_runs_complete"] is True
    assert result["check_runs_terminal"] is True
    assert result["required_contexts_verified"] is False
    assert "no status contexts" in result["state_note"]
    assert "required branch-protection contexts were not queried" in result["state_note"]


def _workflow_run_response(
    *,
    status: str = "completed",
    conclusion: str | None = "failure",
    attempt: int = 1,
    branch: str = "cyrene/assistant/task",
    head_sha: str = "a" * 40,
    workflow_id: int = 22,
) -> bytes:
    """Build a stable API response for bounded Actions lifecycle tests."""

    return json.dumps(
        {
            "id": 99,
            "workflow_id": workflow_id,
            "name": "Source CI",
            "path": ".github/workflows/ci.yml",
            "status": status,
            "conclusion": conclusion,
            "head_sha": head_sha,
            "head_branch": branch,
            "run_attempt": attempt,
        }
    ).encode()


def test_issue_search_is_repo_qualified_and_projects_only_issue_results() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    observed: list[str] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        observed.append(path)
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        assert host == "api.github.com"
        assert method == "GET"
        return HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [
                        {
                            "id": 555,
                            "number": 7,
                            "state": "open",
                            "title": "Investigate",
                            "body": "Issue details",
                            "html_url": "https://github.com/owner/project/issues/7",
                            "repository_url": "https://api.github.com/repos/owner/project",
                            "updated_at": "2026-10-24T12:00:00Z",
                        }
                    ],
                }
            ).encode(),
        )

    api = GitHubApi(settings, transport=transport)
    result = api.search_issues(
        repository,
        "workflow status",
        "OPEN",
        ["bug"],
        1,
        "token",
        updated_since="2026-10-24T12:00:00Z",
        updated_until="2026-10-24T12:05:00+00:00",
        page_cursor="1",
    )

    assert result["total_count"] == 1
    assert result["complete"] is True
    assert result["truncated"] is False
    assert result["incomplete_results"] is False
    assert result["result_limit_reached"] is False
    assert result["next_cursor"] is None
    assert result["issues"][0]["number"] == 7
    assert result["issues"][0]["body_excerpt"] == "Issue details"
    assert "repository_url" not in result["issues"][0]
    assert "repo%3Aowner%2Fproject+is%3Aissue+is%3Aopen+workflow+status" in observed[-1]
    assert "label%3A%22bug%22" in observed[-1]
    assert "updated%3A%3E%3D2026-10-24T12%3A00%3A00%2B00%3A00" in observed[-1]
    assert "updated%3A%3C%3D2026-10-24T12%3A05%3A00%2B00%3A00" in observed[-1]
    assert "page=1" in observed[-1]


def test_issue_search_terminal_page_does_not_claim_to_contain_prior_page() -> None:
    settings = make_settings()
    repository = settings.repositories[0]

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        issue = {
            "id": 555,
            "number": 7,
            "state": "open",
            "title": "Last issue",
            "repository_url": "https://api.github.com/repos/owner/project",
        }
        return HttpResponse(
            200,
            {},
            json.dumps(
                {"total_count": 2, "incomplete_results": False, "items": [issue]}
            ).encode(),
        )

    result = GitHubApi(settings, transport=transport).search_issues(
        repository, "", "ALL", [], 1, "token", page_cursor="2"
    )

    assert result["page_cursor"] == "2"
    assert result["complete"] is False
    assert result["truncated"] is True
    assert result["next_cursor"] is None
    assert result["incomplete_results"] is False


def test_issue_search_marks_the_github_1000_result_ceiling_incomplete() -> None:
    settings = make_settings()
    repository = settings.repositories[0]

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        assert path.endswith("per_page=1&page=1000")
        issue = {
            "id": 555,
            "number": 7,
            "state": "open",
            "title": "Last issue",
            "repository_url": "https://api.github.com/repos/owner/project",
        }
        return HttpResponse(
            200,
            {},
            json.dumps(
                {"total_count": 1001, "incomplete_results": False, "items": [issue]}
            ).encode(),
        )

    result = GitHubApi(settings, transport=transport).search_issues(
        repository, "", "ALL", [], 1, "token", page_cursor="1000"
    )

    assert result["complete"] is False
    assert result["truncated"] is True
    assert result["result_limit_reached"] is True
    assert result["incomplete_results"] is False
    assert result["next_cursor"] is None


def test_draft_pull_request_creation_reads_back_exact_same_repository_branches() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    head_sha = "e" * 40
    base_sha = "f" * 40
    pull_request = {
        "number": 4,
        "state": "open",
        "title": "Automated task",
        "body": "Evidence",
        "draft": True,
        "html_url": "https://github.com/owner/project/pull/4",
        "head": {
            "sha": head_sha,
            "ref": "cyrene/assistant/task",
            "repo": {"id": 12345, "full_name": "owner/project"},
        },
        "base": {
            "sha": base_sha,
            "ref": "develop",
            "repo": {"id": 12345, "full_name": "owner/project"},
        },
        "repository": {"id": 12345, "full_name": "owner/project"},
    }

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path.endswith("/git/ref/heads/cyrene%2Fassistant%2Ftask"):
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {"ref": "refs/heads/cyrene/assistant/task", "object": {"sha": head_sha}}
                ).encode(),
            )
        if path.endswith("/git/ref/heads/develop"):
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {"ref": "refs/heads/develop", "object": {"sha": base_sha}}
                ).encode(),
            )
        if path.startswith("/repos/owner/project/pulls?"):
            return HttpResponse(200, {}, b"[]")
        if method == "POST" and path == "/repos/owner/project/pulls":
            assert json.loads(body or b"{}") == {
                "title": "Automated task",
                "body": "Evidence",
                "head": "cyrene/assistant/task",
                "base": "develop",
                "draft": True,
            }
            return HttpResponse(201, {}, json.dumps(pull_request).encode())
        if path.endswith("/pulls/4"):
            return HttpResponse(200, {}, json.dumps(pull_request).encode())
        raise AssertionError((method, path))

    result = GitHubApi(settings, transport=transport).create_draft_pull_request(
        repository, "cyrene/assistant/task", "develop", "Automated task", "Evidence", "token"
    )

    assert result["status"] == "CONFIRMED"
    assert result["verified"] is True
    assert result["pull_request"] == {
        "number": 4,
        "url": "https://github.com/owner/project/pull/4",
        "head_sha": head_sha,
        "base_sha": base_sha,
        "is_draft": True,
    }


def test_uncertain_draft_pr_is_not_confirmed_after_head_branch_advances() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    original_sha = "1" * 40
    advanced_sha = "2" * 40
    base_sha = "3" * 40
    pull_request = {
        "number": 8,
        "state": "open",
        "title": "Automated task",
        "body": "Evidence",
        "draft": True,
        "html_url": "https://github.com/owner/project/pull/8",
        "head": {
            "sha": advanced_sha,
            "ref": "cyrene/assistant/task",
            "repo": {"id": 12345, "full_name": "owner/project"},
        },
        "base": {
            "sha": base_sha,
            "ref": "develop",
            "repo": {"id": 12345, "full_name": "owner/project"},
        },
        "repository": {"id": 12345, "full_name": "owner/project"},
    }
    pr_search_count = 0

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        nonlocal pr_search_count
        if path == "/repos/owner/project":
            return HttpResponse(
                200,
                {},
                b'{"id":12345,"full_name":"owner/project","default_branch":"develop"}',
            )
        if path.endswith("/git/ref/heads/cyrene%2Fassistant%2Ftask"):
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "ref": "refs/heads/cyrene/assistant/task",
                        "object": {"sha": original_sha},
                    }
                ).encode(),
            )
        if path.endswith("/git/ref/heads/develop"):
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {"ref": "refs/heads/develop", "object": {"sha": base_sha}}
                ).encode(),
            )
        if path.startswith("/repos/owner/project/pulls?"):
            pr_search_count += 1
            if pr_search_count == 1:
                return HttpResponse(200, {}, b"[]")
            return HttpResponse(200, {}, json.dumps([pull_request]).encode())
        if method == "POST" and path == "/repos/owner/project/pulls":
            raise TimeoutError("PR was created at the advanced head before response loss")
        raise AssertionError((method, path))

    with pytest.raises(GitHubApiFailure) as failure:
        GitHubApi(settings, transport=transport).create_draft_pull_request(
            repository,
            "cyrene/assistant/task",
            "develop",
            "Automated task",
            "Evidence",
            "token",
        )

    assert failure.value.code == "UPSTREAM_UNCERTAIN"
    assert failure.value.uncertain is True


def test_task_branch_delete_is_refused_while_same_repo_open_pr_uses_branch() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    branch = "cyrene/assistant/task"
    sha = "4" * 40
    observed: list[tuple[str, str]] = []
    open_pull_request = {
        "number": 9,
        "state": "open",
        "head": {
            "ref": branch,
            "repo": {"id": 12345, "full_name": "owner/project"},
        },
        "base": {
            "ref": "develop",
            "repo": {"id": 12345, "full_name": "owner/project"},
        },
    }

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        observed.append((method, path))
        if path == "/repos/owner/project":
            return HttpResponse(
                200,
                {},
                b'{"id":12345,"full_name":"owner/project","default_branch":"develop"}',
            )
        if path.endswith("/git/ref/heads/cyrene%2Fassistant%2Ftask"):
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "ref": f"refs/heads/{branch}",
                        "object": {"sha": sha},
                    }
                ).encode(),
            )
        if path.startswith("/repos/owner/project/pulls?"):
            return HttpResponse(200, {}, json.dumps([open_pull_request]).encode())
        raise AssertionError((method, path))

    with pytest.raises(GitHubApiFailure, match="BRANCH_HAS_OPEN_PULL_REQUEST"):
        GitHubApi(settings, transport=transport).delete_branch(
            repository, branch, sha, "token"
        )

    assert not any(method == "DELETE" for method, _ in observed)


def test_task_branch_delete_rechecks_head_after_pull_request_scan() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    branch = "cyrene/assistant/task"
    expected_sha = "4" * 40
    advanced_sha = "5" * 40
    reference_reads = 0
    observed: list[tuple[str, str]] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        nonlocal reference_reads
        observed.append((method, path))
        if path == "/repos/owner/project":
            return HttpResponse(
                200,
                {},
                b'{"id":12345,"full_name":"owner/project","default_branch":"develop"}',
            )
        if path.endswith("/git/ref/heads/cyrene%2Fassistant%2Ftask"):
            reference_reads += 1
            sha = expected_sha if reference_reads == 1 else advanced_sha
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}
                ).encode(),
            )
        if path.startswith("/repos/owner/project/pulls?"):
            return HttpResponse(200, {}, b"[]")
        raise AssertionError((method, path))

    with pytest.raises(GitHubApiFailure, match="BRANCH_HEAD_MISMATCH"):
        GitHubApi(settings, transport=transport).delete_branch(
            repository, branch, expected_sha, "token"
        )

    assert reference_reads == 2
    assert not any(method == "DELETE" for method, _ in observed)


def test_task_branch_delete_rechecks_default_branch_after_pull_request_scan() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    branch = "cyrene/assistant/task"
    sha = "4" * 40
    repository_reads = 0
    observed: list[tuple[str, str]] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        nonlocal repository_reads
        observed.append((method, path))
        if path == "/repos/owner/project":
            repository_reads += 1
            default_branch = "develop" if repository_reads == 1 else branch
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "id": 12345,
                        "full_name": "owner/project",
                        "default_branch": default_branch,
                    }
                ).encode(),
            )
        if path.endswith("/git/ref/heads/cyrene%2Fassistant%2Ftask"):
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}
                ).encode(),
            )
        if path.startswith("/repos/owner/project/pulls?"):
            return HttpResponse(200, {}, b"[]")
        raise AssertionError((method, path))

    with pytest.raises(GitHubApiFailure, match="PROTECTED_BRANCH"):
        GitHubApi(settings, transport=transport).delete_branch(
            repository, branch, sha, "token"
        )

    assert repository_reads == 2
    assert not any(method == "DELETE" for method, _ in observed)


def test_actions_jobs_and_logs_are_run_scoped_bounded_and_redacted() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    observed: list[tuple[str, str, Mapping[str, str]]] = []
    signed_log_url = (
        "https://productionresultssa30.blob.core.windows.net/job/log.txt?sig=short-lived"
    )
    fake_token = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789"

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        observed.append((method, host, dict(headers)))
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path.endswith("/actions/runs/99"):
            return HttpResponse(200, {}, _workflow_run_response())
        if path.endswith("/actions/runs/99/jobs?per_page=10"):
            return HttpResponse(
                200,
                {},
                b'{"total_count":1,"jobs":[{"id":100,"run_id":99,"name":"test","status":"completed","conclusion":"failure","steps":[]}]}',
            )
        if path.endswith("/actions/jobs/100"):
            return HttpResponse(
                200,
                {},
                b'{"id":100,"run_id":99,"name":"test","status":"completed","conclusion":"failure","steps":[]}',
            )
        if path.endswith("/actions/jobs/100/logs"):
            return HttpResponse(302, {"location": signed_log_url}, b"")
        assert host == "productionresultssa30.blob.core.windows.net"
        return HttpResponse(
            200,
            {},
            b"start\nAWS_SECRET_ACCESS_KEY=aws-secret-value\n"
            + f"failure {fake_token}\nlast line\n".encode()
            + b'{"AWS_SECRET_ACCESS_KEY":"json-aws-secret"}\n'
            + b'{"token":"short-secret-value"}\n'
            + json.dumps({"token": 'prefix"LEAKED_SUFFIX'}).encode()
            + b"\n",
        )

    api = GitHubApi(settings, transport=transport)
    jobs = api.list_workflow_jobs(repository, "99", 10, "token")
    logs = api.read_job_log_excerpt(repository, "99", "100", 6, "token")

    assert jobs["jobs"][0]["job_id"] == "100"
    assert jobs["complete"] is True
    assert jobs["truncated"] is False
    assert logs["job_id"] == "100"
    assert "failure [REDACTED_GITHUB_TOKEN]" in logs["excerpt"]
    assert "AWS_SECRET_ACCESS_KEY=[REDACTED]" in logs["excerpt"]
    assert '"AWS_SECRET_ACCESS_KEY":"[REDACTED]"' in logs["excerpt"]
    assert '"token":"[REDACTED]"' in logs["excerpt"]
    assert "aws-secret-value" not in logs["excerpt"]
    assert "json-aws-secret" not in logs["excerpt"]
    assert "short-secret-value" not in logs["excerpt"]
    assert "LEAKED_SUFFIX" not in logs["excerpt"]
    assert "start" not in logs["excerpt"]
    external_requests = [item for item in observed if item[1] != "api.github.com"]
    assert len(external_requests) == 1
    assert "Authorization" not in external_requests[0][2]


def test_actions_log_rejects_untrusted_redirect_hosts() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    observed_hosts: list[str] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        observed_hosts.append(host)
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path.endswith("/actions/runs/99"):
            return HttpResponse(200, {}, _workflow_run_response())
        if path.endswith("/actions/jobs/100"):
            return HttpResponse(
                200,
                {},
                b'{"id":100,"run_id":99,"name":"test","status":"completed","conclusion":"failure","steps":[]}',
            )
        return HttpResponse(302, {"location": "https://attacker.invalid/logs"}, b"")

    api = GitHubApi(settings, transport=transport)
    with pytest.raises(GitHubApiFailure, match="LOG_DOWNLOAD_REDIRECT_REJECTED"):
        api.read_job_log_excerpt(repository, "99", "100", 20, "token")
    assert "attacker.invalid" not in observed_hosts


def test_branch_create_reconciles_exact_ref_and_delete_checks_expected_head() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    sha = "a" * 40
    observed: list[tuple[str, str]] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        observed.append((method, path))
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path.endswith("/commits/" + sha):
            return HttpResponse(200, {}, json.dumps({"sha": sha}).encode())
        if path.endswith("/git/ref/heads/cyrene%2Fassistant%2Ftask") and method == "GET":
            if any(item[0] == "POST" for item in observed):
                return HttpResponse(
                    200,
                    {},
                    json.dumps(
                        {"ref": "refs/heads/cyrene/assistant/task", "object": {"sha": sha}}
                    ).encode(),
                )
            return HttpResponse(404, {}, b"")
        if path.endswith("/git/refs") and method == "POST":
            assert json.loads(body or b"{}") == {
                "ref": "refs/heads/cyrene/assistant/task",
                "sha": sha,
            }
            return HttpResponse(
                201,
                {},
                json.dumps(
                    {"ref": "refs/heads/cyrene/assistant/task", "object": {"sha": sha}}
                ).encode(),
            )
        if method == "DELETE":
            raise TimeoutError("connection ended after request")
        raise AssertionError((method, path))

    api = GitHubApi(settings, transport=transport)
    created = api.create_branch(repository, "cyrene/assistant/task", sha, "token")
    assert created["verified"] is True
    assert created["commit_sha"] == sha


def test_branch_delete_reconciles_ambiguous_delete_to_absent() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    sha = "b" * 40
    delete_sent = False

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        nonlocal delete_sent
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path.startswith("/repos/owner/project/pulls?"):
            return HttpResponse(200, {}, b"[]")
        if path.endswith("/git/ref/heads/cyrene%2Fassistant%2Ftask"):
            if delete_sent:
                return HttpResponse(404, {}, b"")
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {"ref": "refs/heads/cyrene/assistant/task", "object": {"sha": sha}}
                ).encode(),
            )
        if method == "DELETE":
            delete_sent = True
            raise TimeoutError("unknown delete result")
        raise AssertionError((method, path))

    api = GitHubApi(settings, transport=transport)
    result = api.delete_branch(repository, "cyrene/assistant/task", sha, "token")
    assert result == {
        "status": "CONFIRMED",
        "branch": "cyrene/assistant/task",
        "deleted": True,
        "already_absent": False,
        "verified": True,
    }


def test_repository_renamed_default_branch_is_protected_from_task_deletion() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    observed: list[str] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        observed.append(path)
        return HttpResponse(
            200,
            {},
            b'{"id":12345,"full_name":"owner/project","default_branch":"trunk"}',
        )

    api = GitHubApi(settings, transport=transport)
    with pytest.raises(GitHubApiFailure, match="PROTECTED_BRANCH"):
        api.delete_branch(repository, "trunk", "a" * 40, "token")

    assert observed == ["/repos/owner/project"]


def test_file_commit_uses_blob_precondition_and_readback() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    content = b"print('hello')\n"
    encoded = base64.b64encode(content).decode()
    blob_sha = hashlib.sha1(
        f"blob {len(content)}\0".encode() + content, usedforsecurity=False
    ).hexdigest()
    commit_sha = "c" * 40
    write_sent = False

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        nonlocal write_sent
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path.endswith("/contents/src/main.py?ref=cyrene%2Fassistant%2Ftask"):
            if not write_sent:
                return HttpResponse(404, {}, b"")
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "path": "src/main.py",
                        "sha": blob_sha,
                        "encoding": "base64",
                        "content": encoded,
                    }
                ).encode(),
            )
        if method == "PUT":
            write_sent = True
            assert json.loads(body or b"{}") == {
                "message": "Add source",
                "content": encoded,
                "branch": "cyrene/assistant/task",
            }
            return HttpResponse(
                201,
                {},
                json.dumps(
                    {
                        "content": {"sha": blob_sha},
                        "commit": {"sha": commit_sha},
                    }
                ).encode(),
            )
        raise AssertionError((method, path))

    api = GitHubApi(settings, transport=transport)
    result = api.commit_file(
        repository, "cyrene/assistant/task", "src/main.py", "Add source", encoded, None, "token"
    )
    assert result["verified"] is True
    assert result["blob_sha"] == blob_sha
    assert result["commit_sha"] == commit_sha


def test_file_update_uses_expected_blob_sha_and_reconciles_lost_response() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    old_content = b"old source\n"
    new_content = b"updated source\n"
    old_encoded = base64.b64encode(old_content).decode()
    new_encoded = base64.b64encode(new_content).decode()
    old_sha = hashlib.sha1(
        f"blob {len(old_content)}\0".encode() + old_content, usedforsecurity=False
    ).hexdigest()
    new_sha = hashlib.sha1(
        f"blob {len(new_content)}\0".encode() + new_content, usedforsecurity=False
    ).hexdigest()
    write_reached_github = False

    def file_response(encoded: str, blob_sha: str) -> HttpResponse:
        return HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "path": "src/main.py",
                    "sha": blob_sha,
                    "encoding": "base64",
                    "content": encoded,
                }
            ).encode(),
        )

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        nonlocal write_reached_github
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path.endswith("/contents/src/main.py?ref=cyrene%2Fassistant%2Ftask"):
            if write_reached_github:
                return file_response(new_encoded, new_sha)
            return file_response(old_encoded, old_sha)
        if method == "PUT":
            assert json.loads(body or b"{}") == {
                "message": "Update source",
                "content": new_encoded,
                "branch": "cyrene/assistant/task",
                "sha": old_sha,
            }
            write_reached_github = True
            raise TimeoutError("server applied the commit before the response was lost")
        raise AssertionError((method, path))

    result = GitHubApi(settings, transport=transport).commit_file(
        repository,
        "cyrene/assistant/task",
        "src/main.py",
        "Update source",
        new_encoded,
        old_sha,
        "token",
    )

    assert result["status"] == "CONFIRMED"
    assert result["blob_sha"] == new_sha
    assert result["commit_sha"] is None
    assert result["reconciled"] is True


def test_failed_ci_rerun_requires_exact_task_branch_attempt_and_reads_new_attempt() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    sha = "d" * 40
    run_read_count = 0
    rerun_sent = False

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        nonlocal run_read_count, rerun_sent
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path.endswith("/actions/runs/99") and method == "GET":
            run_read_count += 1
            if rerun_sent:
                return HttpResponse(
                    200,
                    {},
                    _workflow_run_response(
                        status="queued", conclusion=None, attempt=2, head_sha=sha
                    ),
                )
            return HttpResponse(
                200, {}, _workflow_run_response(head_sha=sha, attempt=1)
            )
        if path.endswith("/rerun-failed-jobs") and method == "POST":
            rerun_sent = True
            return HttpResponse(201, {}, b"")
        raise AssertionError((method, path))

    api = GitHubApi(settings, transport=transport)
    result = api.rerun_failed_jobs(
        repository, "99", "22", "cyrene/assistant/task", sha, 1, "token"
    )
    assert result == {
        "status": "QUEUED",
        "conclusion": None,
        "run_id": "99",
        "attempt": 2,
        "head_sha": sha,
        "head_branch": "cyrene/assistant/task",
        "workflow_id": "22",
        "workflow_name": "Source CI",
        "workflow_path": ".github/workflows/ci.yml",
        "verified": True,
    }
    assert run_read_count == 2


def test_ci_rerun_rejects_a_workflow_id_mismatch_with_the_exact_run_receipt() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    sha = "e" * 40
    rerun_calls = 0

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        nonlocal rerun_calls
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path.endswith("/actions/runs/99") and method == "GET":
            return HttpResponse(
                200,
                {},
                _workflow_run_response(head_sha=sha, attempt=1, workflow_id=23),
            )
        if path.endswith("/rerun-failed-jobs"):
            rerun_calls += 1
            return HttpResponse(201, {}, b"")
        raise AssertionError((method, path))

    api = GitHubApi(settings, transport=transport)
    with pytest.raises(GitHubApiFailure, match="RUN_PRECONDITION_FAILED"):
        api.rerun_failed_jobs(
            repository, "99", "22", "cyrene/assistant/task", sha, 1, "token"
        )

    assert rerun_calls == 0


def test_ci_rerun_fails_closed_when_repository_has_no_source_workflow_allowlist() -> None:
    settings = replace(make_settings(), source_workflow_ids={})
    repository = settings.repositories[0]
    observed_paths: list[str] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        observed_paths.append(path)
        return HttpResponse(
            200,
            {},
            b'{"id":12345,"full_name":"owner/project","default_branch":"develop"}',
        )

    with pytest.raises(GitHubApiFailure, match="WORKFLOW_NOT_ALLOWLISTED"):
        GitHubApi(settings, transport=transport).rerun_failed_jobs(
            repository,
            "99",
            "22",
            "cyrene/assistant/task",
            "a" * 40,
            1,
            "token",
        )

    assert observed_paths == ["/repos/owner/project"]


def test_failed_ci_rerun_reports_missing_actions_write_permission_truthfully() -> None:
    settings = make_settings()
    repository = settings.repositories[0]
    sha = "d" * 40
    rerun_calls = 0

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        nonlocal rerun_calls
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if path.endswith("/actions/runs/99") and method == "GET":
            return HttpResponse(
                200, {}, _workflow_run_response(head_sha=sha, attempt=1)
            )
        if path.endswith("/rerun-failed-jobs") and method == "POST":
            rerun_calls += 1
            return HttpResponse(403, {}, b'{"message":"Actions: write permission required"}')
        raise AssertionError((method, path))

    api = GitHubApi(settings, transport=transport)
    with pytest.raises(GitHubApiFailure) as failure:
        api.rerun_failed_jobs(
            repository, "99", "22", "cyrene/assistant/task", sha, 1, "token"
        )

    assert failure.value.code == "AUTHORIZATION_DENIED"
    assert failure.value.uncertain is False
    assert rerun_calls == 1


def test_rate_limit_and_server_failures_are_bounded_and_retry_hints_are_preserved() -> None:
    settings = make_settings()

    for response, expected_code, expected_retry, expected_after in (
        (HttpResponse(429, {"retry-after": "37"}, b"secret detail"), "RATE_LIMITED", True, 37),
        (
            HttpResponse(503, {"retry-after": "500000"}, b"secret detail"),
            "UPSTREAM_UNAVAILABLE",
            True,
            86_400,
        ),
    ):

        def transport(
            method: str,
            host: str,
            path: str,
            headers: Mapping[str, str],
            body: bytes | None,
            *,
            failure: HttpResponse = response,
        ) -> HttpResponse:
            if path == "/repos/owner/project":
                return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
            return failure

        api = GitHubApi(settings, transport=transport)
        try:
            api.get_issue(settings.repositories[0], 7, "user-token")
        except GitHubApiFailure as error:
            assert error.code == expected_code
            assert error.retryable is expected_retry
            assert error.retry_after == expected_after
            assert "secret detail" not in str(error)
        else:
            raise AssertionError("provider failure was not raised")


def test_state_and_comment_receipts_require_real_provider_ids_or_readback() -> None:
    settings = make_settings()

    def uncertain_transport(
        method: str, host: str, path: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResponse:
        if path == "/repos/owner/project":
            return HttpResponse(200, {}, b'{"id":12345,"full_name":"owner/project"}')
        if method == "POST":
            raise TimeoutError("write outcome uncertain")
        return HttpResponse(200, {}, b'{"id":55,"number":7,"state":"closed","title":"T"}')

    api = GitHubApi(settings, transport=uncertain_transport)
    try:
        api.create_comment(settings.repositories[0], 7, "hello", "token")
    except GitHubApiFailure as error:
        assert error.code == "UPSTREAM_UNCERTAIN"
        assert error.uncertain is True
        assert error.retryable is False
    else:
        raise AssertionError("ambiguous comment write must not report receipt")

    result = api.update_state(settings.repositories[0], 7, "CLOSED", "token")
    assert result["status"] == "CONFIRMED"
    assert result["issue"]["state"] == "CLOSED"
