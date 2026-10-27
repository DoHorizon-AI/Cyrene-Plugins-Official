"""Provider catalog, signed lease, and redaction regression coverage.

All Hub calls are faked; tests do not access remote accounts or repositories.
中文：只验证工具边界，不访问真实 Hub 资源。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from cyrene_account_center_contracts import canonical_json
from cyrene_plugin_runtime import DirectPayload
from cyrene_plugin_runtime.vendor_cli import VendorCliResult

from huggingface_provider._generated import tool_provider_pb2 as wire
from huggingface_provider.account_center import (
    AccountCenterDirectLeaseClient,
    AccountCenterUnavailable,
    _auth_material,
)
from huggingface_provider.huggingface_api import HuggingFaceApiFailure
from huggingface_provider.plugin import (
    CALL_TOOL_RESPONSE_TYPE_URL,
    HuggingFaceProvider,
)
from huggingface_provider.settings import (
    ProviderSettings,
    RepositoryScope,
    repository_scope_sha256,
)
from huggingface_provider.vendor_cli import validate_huggingface_cli_argv

EXCHANGE_KEY = b"x" * 32
PROVIDER_KEY = b"p" * 32
ACTOR = {
    "issuer": "https://identity.example.test",
    "subject": "member-27",
    "organization_id": "org-4",
    "workspace_id": "workspace-8",
}


def make_settings(*, read_only: bool = False) -> ProviderSettings:
    """Build one Hub binding with explicit read and write scopes."""

    model_permissions = {"read"} if read_only else {"read", "write"}
    return ProviderSettings(
        binding_id="hf.binding.test",
        account_center_binding_id="account-center.binding.test",
        account_center_connection_ref="grpc://127.0.0.1:40000",
        account_center_transport="local",
        account_center_root_cert_ref=None,
        account_center_client_cert_ref=None,
        account_center_client_key_ref=None,
        host_secret_names={},
        repositories=(
            RepositoryScope("model", "owner/model", frozenset(model_permissions)),
            RepositoryScope("dataset", "owner/data", frozenset({"read"})),
            RepositoryScope("space", "owner/demo", frozenset({"read"})),
        ),
        provider_caller_id="huggingface-provider",
        exchange_caller_id="exchange",
        exchange_verification_key=EXCHANGE_KEY,
        provider_signing_key=PROVIDER_KEY,
        read_only=read_only,
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
                        "external_subject": "hf-owner",
                        "expires_at": 2_000_000_000,
                    },
                    separators=(",", ":"),
                ).encode(),
            )

    transport = DirectClient()
    client = AccountCenterDirectLeaseClient(settings, client=transport)
    payload = {
        "lease_ref": "00000000-0000-4000-8000-000000000002",
        "profile_ref": "00000000-0000-4000-8000-000000000001",
        "operation_id": "huggingface.repo.get",
        "target": {"repo_type": "model", "repo_id": "owner/model"},
        "arguments_sha256": "b" * 64,
    }
    actor = {
        **ACTOR,
        "binding_id": settings.account_center_binding_id,
    }

    material = client.redeem_provider_credential_lease(payload, actor)

    assert material.external_subject == "hf-owner"
    assert transport.call is not None
    assert transport.call["method"] == "redeem_provider_credential_lease"
    envelope = json.loads(transport.call["request"].value)
    assert envelope["payload"] == payload
    assert envelope["trusted_context"]["binding_id"] == settings.account_center_binding_id


class FakeAccountCenter:
    """Record generic lease redemptions and return a memory-only HF token."""

    def __init__(self, profile_scope_sha256: str | None = None) -> None:
        self.calls: list[tuple[dict[str, object], dict[str, Any]]] = []
        self.used: set[str] = set()
        self.profile_scope_sha256 = profile_scope_sha256

    def redeem_provider_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | int | None],
    ) -> Any:
        lease_ref = str(payload["lease_ref"])
        if lease_ref in self.used:
            raise AccountCenterUnavailable("lease already used")
        self.used.add(lease_ref)
        self.calls.append((dict(payload), dict(trusted_context)))
        return _FakeAuthMaterial(
            "hf_private_test_token",
            "Bearer",
            "derek-li",
            None,
            self.profile_scope_sha256,
        )


@dataclass(frozen=True)
class _FakeAuthMaterial:
    """Expose only private token and optional trusted profile-scope metadata."""

    access_token: str
    token_type: str
    external_subject: str
    expires_at: int | None
    profile_scope_sha256: str | None


class FakeHubApi:
    """Record API calls and return bounded fixture values."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.upload_error: HuggingFaceApiFailure | None = None

    def list_repositories(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list", args, kwargs))
        return {"repositories": [{"repo_id": "public/model", "private": False}]}

    def get_repository(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("repo", args, {}))
        return {"repo_id": args[1], "private": True, "gated": False, "file_count": 10}

    def download_file(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("download", args, {}))
        return {
            "repo_id": args[0],
            "revision": args[1],
            "path": args[2],
            "byte_length": 5,
            "content_base64": base64.b64encode(b"hello").decode("ascii"),
        }

    def upload_file(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("upload", args, {}))
        if self.upload_error is not None:
            raise self.upload_error
        return {"status": "CONFIRMED", "repo_id": args[1], "path": args[3]}


class FakeCliRunner:
    """Capture exact invocation metadata without running a vendor binary."""

    def __init__(self, result: VendorCliResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result or VendorCliResult(0, 9, False, False, 64, 3)

    def run(self, executable: str, argv: list[str], **kwargs: Any) -> VendorCliResult:
        self.calls.append({"executable": executable, "argv": list(argv), **kwargs})
        return self.result


def signed_context(
    settings: ProviderSettings,
    arguments: Mapping[str, Any],
    profile_ref: str,
    lease_ref: str,
    operation_id: str,
) -> dict[str, Any]:
    """Sign the exact actor/profile/lease/tool argument envelope."""

    protected = {
        "payload": dict(arguments),
        "profile_ref": profile_ref,
        "lease_ref": lease_ref,
    }
    value: dict[str, Any] = {
        "method": "call_tool",
        "payload_sha256": hashlib.sha256(canonical_json(protected)).hexdigest(),
        "binding_id": settings.binding_id,
        "caller_id": settings.exchange_caller_id,
        "issuer": ACTOR["issuer"],
        "subject": ACTOR["subject"],
        "organization_id": ACTOR["organization_id"],
        "workspace_id": ACTOR["workspace_id"],
        "request_id": "request-123",
        "issued_at": int(time.time()),
        "expires_at": int(time.time()) + 60,
        "operation_id": (
            "huggingface.vendor.cli.execute" if operation_id == "hf_vendor_cli" else operation_id
        ),
    }
    value["signature"] = hmac.new(
        settings.exchange_verification_key, canonical_json(value), hashlib.sha256
    ).hexdigest()
    return value


def invoke_tool(
    provider: HuggingFaceProvider,
    settings: ProviderSettings,
    tool_id: str,
    arguments: Mapping[str, Any],
    *,
    profile_ref: str = "00000000-0000-4000-8000-000000000001",
    lease_ref: str = "00000000-0000-4000-8000-000000000002",
    context: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Call through the same protobuf request used by DirectPluginRuntime."""

    envelope = {
        "payload": dict(arguments),
        "profile_ref": profile_ref,
        "lease_ref": lease_ref,
        "trusted_context": dict(
            context or signed_context(settings, arguments, profile_ref, lease_ref, tool_id)
        ),
    }
    request = wire.CallToolRequest(
        binding_id=settings.binding_id,
        provider_tool_id=tool_id,
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )
    ok, payload = provider.on_invoke("tool.provider.v1", "call_tool", request.SerializeToString())
    assert ok is True
    assert payload.type_url == CALL_TOOL_RESPONSE_TYPE_URL
    response = wire.CallToolResponse.FromString(payload.value)
    return response.outcome.is_error, json.loads(response.outcome.content[0].json.json)


def invoke_invalid_tool(
    provider: HuggingFaceProvider,
    settings: ProviderSettings,
    tool_id: str,
    arguments: Mapping[str, Any],
) -> tuple[bool, Any]:
    """Invoke one invalid request before lease redemption."""

    profile_ref = "00000000-0000-4000-8000-000000000001"
    lease_ref = "00000000-0000-4000-8000-000000000002"
    envelope = {
        "payload": dict(arguments),
        "profile_ref": profile_ref,
        "lease_ref": lease_ref,
        "trusted_context": signed_context(settings, arguments, profile_ref, lease_ref, tool_id),
    }
    request = wire.CallToolRequest(
        binding_id=settings.binding_id,
        provider_tool_id=tool_id,
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )
    return provider.on_invoke("tool.provider.v1", "call_tool", request.SerializeToString())


def test_catalog_and_signed_model_search_keep_token_out_of_result() -> None:
    settings = make_settings()
    account_center = FakeAccountCenter()
    api = FakeHubApi()
    provider = HuggingFaceProvider(settings=settings, account_center=account_center, hub_api=api)
    request = wire.ListToolsRequest(binding_id=settings.binding_id)

    ok, result = provider.on_invoke("tool.provider.v1", "list_tools", request.SerializeToString())
    catalog = wire.ListToolsResponse.FromString(result.value).catalog
    assert ok is True
    assert [item.provider_tool_id for item in catalog.tools] == [
        "huggingface.models.list",
        "huggingface.datasets.list",
        "huggingface.spaces.list",
        "huggingface.repo.get",
        "huggingface.file.download",
        "huggingface.file.upload",
        "hf_vendor_cli",
    ]

    arguments = {"search": "bert", "limit": 5}
    is_error, response = invoke_tool(provider, settings, "huggingface.models.list", arguments)
    assert is_error is False
    assert response["repositories"][0]["repo_id"] == "public/model"
    assert "hf_private_test_token" not in json.dumps(response)
    lease_payload, account_context = account_center.calls[0]
    assert lease_payload == {
        "lease_ref": "00000000-0000-4000-8000-000000000002",
        "profile_ref": "00000000-0000-4000-8000-000000000001",
        "operation_id": "huggingface.models.list",
        "target": {"repo_type": "model"},
        "arguments_sha256": hashlib.sha256(canonical_json(arguments)).hexdigest(),
    }
    assert account_context["method"] == "redeem_provider_credential_lease"
    assert account_context["binding_id"] == settings.account_center_binding_id
    assert api.calls[0][1][1] == "hf_private_test_token"


def test_repo_get_forwards_file_count_from_hub_projection() -> None:
    """Expose the Hub projection through the signed provider tool response."""
    settings = make_settings()
    account_center = FakeAccountCenter()
    api = FakeHubApi()
    provider = HuggingFaceProvider(settings=settings, account_center=account_center, hub_api=api)

    is_error, result = invoke_tool(
        provider,
        settings,
        "huggingface.repo.get",
        {"repo_type": "model", "repo_id": "owner/model"},
    )

    assert is_error is False
    assert result["file_count"] == 10
    assert api.calls == [("repo", ("model", "owner/model", "hf_private_test_token"), {})]


def test_read_only_binding_hides_and_rejects_upload_and_native_cli() -> None:
    """A host read-only flag filters the catalog and blocks direct signed mutations."""

    settings = make_settings(read_only=True)
    account_center = FakeAccountCenter()
    api = FakeHubApi()
    cli = FakeCliRunner()
    provider = HuggingFaceProvider(
        settings=settings,
        account_center=account_center,
        hub_api=api,
        cli_runner=cli,
    )
    request = wire.ListToolsRequest(binding_id=settings.binding_id)

    ok, result = provider.on_invoke("tool.provider.v1", "list_tools", request.SerializeToString())
    catalog = wire.ListToolsResponse.FromString(result.value).catalog

    assert ok is True
    assert {item.provider_tool_id for item in catalog.tools} == {
        "huggingface.models.list",
        "huggingface.datasets.list",
        "huggingface.spaces.list",
        "huggingface.repo.get",
        "huggingface.file.download",
    }

    upload_error, upload_result = invoke_tool(
        provider,
        settings,
        "huggingface.file.upload",
        {
            "repo_type": "model",
            "repo_id": "owner/model",
            "revision": "main",
            "path": "weights.bin",
            "content_base64": base64.b64encode(b"fixture").decode("ascii"),
            "read_only": False,
        },
    )
    cli_error, cli_result = invoke_tool(
        provider,
        settings,
        "hf_vendor_cli",
        {"argv": ["models", "list"]},
    )

    assert upload_error is True
    assert upload_result["error"]["code"] == "READ_ONLY_PROFILE"
    assert cli_error is True
    assert cli_result["error"]["code"] == "READ_ONLY_PROFILE"
    assert account_center.calls == []
    assert api.calls == []
    assert cli.calls == []


def test_account_center_profile_scope_digest_is_validated_before_provider_use() -> None:
    response = {
        "access_token": "hf_private_test_token",
        "token_type": "Bearer",
        "external_subject": "derek-li",
        "expires_at": None,
        "profile_scope_sha256": "a" * 64,
    }

    credential = _auth_material(response)

    assert credential.profile_scope_sha256 == "a" * 64
    with pytest.raises(AccountCenterUnavailable, match="auth response is invalid"):
        _auth_material({**response, "profile_scope_sha256": "not-a-digest"})
    with pytest.raises(AccountCenterUnavailable, match="auth response shape is invalid"):
        _auth_material({**response, "unexpected": "value"})


def test_vendor_cli_uses_profile_lease_and_returns_only_a_safe_read_receipt() -> None:
    settings = make_settings()
    account_center = FakeAccountCenter(repository_scope_sha256(settings.repositories))
    runner = FakeCliRunner()
    provider = HuggingFaceProvider(
        settings=settings,
        account_center=account_center,
        cli_runner=runner,
    )
    arguments = {"argv": ["models", "info", "owner/model"]}

    is_error, result = invoke_tool(provider, settings, "hf_vendor_cli", arguments)

    assert is_error is False
    assert result["state"] == "COMMAND_COMPLETED"
    assert result["remote_state"] == "NOT_APPLICABLE"
    assert result["stdout_suppressed"] is True
    assert result["stderr_suppressed"] is True
    assert "hf_private_test_token" not in json.dumps(result)
    lease_payload, _context = account_center.calls[0]
    assert lease_payload == {
        "lease_ref": "00000000-0000-4000-8000-000000000002",
        "profile_ref": "00000000-0000-4000-8000-000000000001",
        "operation_id": "huggingface.vendor.cli.execute",
        "target": {},
        "arguments_sha256": hashlib.sha256(canonical_json(arguments)).hexdigest(),
    }
    assert runner.calls[0]["executable"] == "/usr/local/bin/hf"
    assert runner.calls[0]["argv"] == arguments["argv"]
    assert runner.calls[0]["credential_environment"] == {"HF_TOKEN": "hf_private_test_token"}


def test_vendor_cli_mutation_exit_zero_stays_remote_state_unverified() -> None:
    settings = make_settings()
    account_center = FakeAccountCenter(repository_scope_sha256(settings.repositories))
    runner = FakeCliRunner(VendorCliResult(0, 12, False, False, 0, 0))
    provider = HuggingFaceProvider(
        settings=settings,
        account_center=account_center,
        cli_runner=runner,
    )

    is_error, result = invoke_tool(
        provider,
        settings,
        "hf_vendor_cli",
        {"argv": ["repos", "delete", "owner/model", "--type", "model", "--yes"]},
    )

    assert is_error is False
    assert result["state"] == "REMOTE_STATE_UNVERIFIED"
    assert result["command_state"] == "COMMAND_COMPLETED"
    assert result["remote_state"] == "UNKNOWN_RESULT"
    assert result["operation_ref"].startswith("hfcli_")
    assert result["reconciliation_available"] is False


@pytest.mark.parametrize("scope_digest", [None, "0" * 64])
def test_vendor_cli_fails_closed_when_account_center_scope_digest_is_missing_or_differs(
    scope_digest: str | None,
) -> None:
    settings = make_settings()
    account_center = FakeAccountCenter(scope_digest)
    runner = FakeCliRunner()
    provider = HuggingFaceProvider(
        settings=settings,
        account_center=account_center,
        cli_runner=runner,
    )

    is_error, result = invoke_tool(
        provider,
        settings,
        "hf_vendor_cli",
        {"argv": ["models", "info", "owner/model"]},
    )

    assert is_error is True
    assert result["error"]["code"] == "PROFILE_SCOPE_MISMATCH"
    assert runner.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["auth", "login", "--token", "hf_fixture_token_12345678901234567890"],
        ["models", "info", "owner/model", "--endpoint", "https://attacker.invalid"],
        ["download", "owner/model", "--local-dir", "/tmp/private"],
        ["upload", "owner/model", "./private.bin"],
        ["jobs", "run", "--secrets", "TOKEN=secret"],
        ["jobs", "run", "-e", "HF_TOKEN=hf_fixture_token_12345678901234567890"],
        ["jobs", "uv", "run", "private_training.py"],
    ],
)
def test_vendor_cli_rejects_credential_endpoint_and_local_file_commands(
    argv: list[str],
) -> None:
    settings = make_settings()
    account_center = FakeAccountCenter()
    runner = FakeCliRunner()
    provider = HuggingFaceProvider(
        settings=settings,
        account_center=account_center,
        cli_runner=runner,
    )

    ok, result = invoke_invalid_tool(provider, settings, "hf_vendor_cli", {"argv": argv})

    assert ok is False
    assert result == "INVALID_ARGUMENTS: tool arguments do not match the schema"
    assert account_center.calls == []
    assert runner.calls == []


@pytest.mark.parametrize(
    ("argv", "read_only"),
    [
        (["models", "info", "owner/model"], True),
        (["models", "info", "hf://models/owner/model@refs%2Fheads%2Frelease"], True),
        (["models", "list", "--limit", "10", "owner/model"], True),
        (["datasets", "info", "https://huggingface.co/datasets/owner/data"], True),
        (["spaces", "card", "hf://spaces/owner/demo"], True),
        (["repos", "delete", "owner/model", "--type", "model", "--yes"], False),
        (["repos", "tag", "list", "owner/model", "v1", "--type", "model"], True),
        (["--help"], True),
        (["version"], True),
    ],
)
def test_vendor_cli_only_allows_explicit_scoped_repository_commands(
    argv: list[str], read_only: bool
) -> None:
    settings = make_settings()

    assert validate_huggingface_cli_argv(argv, settings.repositories) is read_only


@pytest.mark.parametrize(
    "argv",
    [
        ["models", "list"],
        ["models", "info", "other/model"],
        ["models", "info", "https://huggingface.co/datasets/owner/data"],
        ["repos", "delete", "owner/data", "--type", "dataset", "--yes"],
        ["repos", "list", "--type", "model"],
        ["models", "info", "https://attacker.invalid/owner/model"],
    ],
)
def test_vendor_cli_rejects_unscoped_wrong_type_and_read_only_targets(
    argv: list[str],
) -> None:
    settings = make_settings()

    with pytest.raises(ValueError):
        validate_huggingface_cli_argv(argv, settings.repositories)


def test_private_file_targets_and_upload_hash_are_bound_to_profile_lease() -> None:
    settings = make_settings()
    account_center = FakeAccountCenter()
    api = FakeHubApi()
    provider = HuggingFaceProvider(settings=settings, account_center=account_center, hub_api=api)
    arguments = {
        "repo_type": "model",
        "repo_id": "owner/model",
        "path": "reports/model.bin",
        "revision": "main",
        "content_base64": base64.b64encode(b"small artifact").decode("ascii"),
    }

    is_error, result = invoke_tool(provider, settings, "huggingface.file.upload", arguments)
    assert is_error is False
    assert result["status"] == "CONFIRMED"
    lease_payload, _context = account_center.calls[0]
    assert lease_payload["target"] == {
        "repo_type": "model",
        "repo_id": "owner/model",
        "path": "reports/model.bin",
        "revision": "main",
    }
    assert (
        lease_payload["arguments_sha256"] == hashlib.sha256(canonical_json(arguments)).hexdigest()
    )
    assert api.calls[0][0] == "upload"
    assert api.calls[0][1][4] == b"small artifact"
    assert "hf_private_test_token" not in json.dumps(result)


def test_invalid_base64_and_unconfigured_resources_are_rejected_before_lease() -> None:
    settings = make_settings()
    account_center = FakeAccountCenter()
    provider = HuggingFaceProvider(
        settings=settings, account_center=account_center, hub_api=FakeHubApi()
    )

    invalid_content = {
        "repo_type": "model",
        "repo_id": "owner/model",
        "path": "config.json",
        "content_base64": "%%%",
    }
    ok, result = invoke_invalid_tool(provider, settings, "huggingface.file.upload", invalid_content)
    assert ok is False
    assert result == "INVALID_ARGUMENTS: tool arguments do not match the schema"
    assert account_center.calls == []

    outside_scope = {"repo_type": "model", "repo_id": "other/model"}
    ok, result = invoke_invalid_tool(provider, settings, "huggingface.repo.get", outside_scope)
    assert ok is False
    assert result == "INVALID_ARGUMENTS: tool arguments do not match the schema"
    assert account_center.calls == []


def test_tampered_actor_is_denied_and_uncertain_upload_is_reported_without_retry() -> None:
    settings = make_settings()
    account_center = FakeAccountCenter()
    api = FakeHubApi()
    api.upload_error = HuggingFaceApiFailure("UPSTREAM_UNCERTAIN", uncertain=True)
    provider = HuggingFaceProvider(settings=settings, account_center=account_center, hub_api=api)
    arguments = {
        "repo_type": "model",
        "repo_id": "owner/model",
        "path": "config.json",
        "content_base64": base64.b64encode(b"data").decode("ascii"),
    }
    tampered = signed_context(
        settings,
        arguments,
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
        "huggingface.file.upload",
    )
    tampered["subject"] = "attacker"
    is_error, denied = invoke_tool(
        provider, settings, "huggingface.file.upload", arguments, context=tampered
    )
    assert is_error is True
    assert denied["error"]["code"] == "AUTHENTICATION_DENIED"
    assert account_center.calls == []

    is_error, unknown = invoke_tool(provider, settings, "huggingface.file.upload", arguments)
    assert is_error is True
    assert unknown["error"] == {
        "code": "UPSTREAM_UNCERTAIN",
        "retryable": False,
        "outcome": "UNKNOWN",
    }
    assert len(account_center.calls) == 1
    assert len(api.calls) == 1
