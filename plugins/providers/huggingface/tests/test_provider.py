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
from typing import Any

from cyrene_account_center_contracts import AuthMaterial, canonical_json

from huggingface_provider._generated import tool_provider_pb2 as wire
from huggingface_provider.account_center import AccountCenterUnavailable
from huggingface_provider.huggingface_api import HuggingFaceApiFailure
from huggingface_provider.plugin import (
    CALL_TOOL_RESPONSE_TYPE_URL,
    HuggingFaceProvider,
)
from huggingface_provider.settings import ProviderSettings, RepositoryScope

EXCHANGE_KEY = b"x" * 32
PROVIDER_KEY = b"p" * 32
ACTOR = {
    "issuer": "https://identity.example.test",
    "subject": "member-27",
    "organization_id": "org-4",
    "workspace_id": "workspace-8",
}


def make_settings() -> ProviderSettings:
    """Build one Hub binding with explicit read and write scopes."""

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
            RepositoryScope("model", "owner/model", frozenset({"read", "write"})),
            RepositoryScope("dataset", "owner/data", frozenset({"read"})),
            RepositoryScope("space", "owner/demo", frozenset({"read"})),
        ),
        provider_caller_id="huggingface-provider",
        exchange_caller_id="exchange",
        exchange_verification_key=EXCHANGE_KEY,
        provider_signing_key=PROVIDER_KEY,
    )


class FakeAccountCenter:
    """Record generic lease redemptions and return a memory-only HF token."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], dict[str, Any]]] = []
        self.used: set[str] = set()

    def redeem_provider_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | int | None],
    ) -> AuthMaterial:
        lease_ref = str(payload["lease_ref"])
        if lease_ref in self.used:
            raise AccountCenterUnavailable("lease already used")
        self.used.add(lease_ref)
        self.calls.append((dict(payload), dict(trusted_context)))
        return AuthMaterial("hf_private_test_token", "Bearer", "derek-li", None)


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
        return {"repo_id": args[1], "private": True, "gated": False}

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
        "operation_id": operation_id,
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
    return provider.on_invoke(
        "tool.provider.v1", "call_tool", request.SerializeToString()
    )


def test_catalog_and_signed_model_search_keep_token_out_of_result() -> None:
    settings = make_settings()
    account_center = FakeAccountCenter()
    api = FakeHubApi()
    provider = HuggingFaceProvider(
        settings=settings, account_center=account_center, hub_api=api
    )
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
    ]

    arguments = {"search": "bert", "limit": 5}
    is_error, response = invoke_tool(
        provider, settings, "huggingface.models.list", arguments
    )
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

    is_error, result = invoke_tool(
        provider, settings, "huggingface.file.upload", arguments
    )
    assert is_error is False
    assert result["status"] == "CONFIRMED"
    lease_payload, _context = account_center.calls[0]
    assert lease_payload["target"] == {
        "repo_type": "model",
        "repo_id": "owner/model",
        "path": "reports/model.bin",
        "revision": "main",
    }
    assert lease_payload["arguments_sha256"] == hashlib.sha256(
        canonical_json(arguments)
    ).hexdigest()
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
    ok, result = invoke_invalid_tool(
        provider, settings, "huggingface.file.upload", invalid_content
    )
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
