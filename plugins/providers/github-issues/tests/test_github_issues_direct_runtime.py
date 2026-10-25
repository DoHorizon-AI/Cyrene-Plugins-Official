"""Exercise no-argument provider assembly through a real local gRPC endpoint.

中文:通过真实本机 gRPC Endpoint 验证无参 Provider 装配与租约兑换。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import pytest
from cyrene_account_center_contracts import canonical_json, sign_context, verify_context
from cyrene_plugin_runtime import DirectPayload
from cyrene_plugin_runtime.server import load_entrypoint, serve

from github_issues_provider._generated import tool_provider_pb2 as wire
from github_issues_provider.account_center import AccountCenterDirectLeaseClient
from github_issues_provider.settings import ConfigurationError, ProviderSettings

ACCOUNT_CENTER_KEY = b"\x11" * 32
EXCHANGE_KEY = b"\x22" * 32


class AccountCenterEndpoint:
    """Minimal provider-role endpoint that verifies real SDK-signed requests."""

    capabilities = ("account.center.v1",)
    plugin_id = "test.account-center"
    version = "1"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        **_: Any,
    ) -> tuple[bool, Any]:
        if capability != "account.center.v1" or action != "redeem_credential_lease":
            return False, "METHOD_NOT_FOUND: unsupported capability method"
        if request_type_url != (
            "type.googleapis.com/cyrene.account.center.v1.RedeemCredentialLeaseRequest"
        ):
            return False, "INVALID_REQUEST: request contract mismatch"
        envelope = json.loads(payload)
        request = envelope["payload"]
        context = envelope["trusted_context"]
        if not verify_context(
            method=action,
            payload=request,
            context=context,
            keys={"plugins.github-issues": ACCOUNT_CENTER_KEY},
            expected_caller_id="plugins.github-issues",
        ):
            return False, "PERMISSION_DENIED: signed provider context rejected"
        self.calls.append({"payload": request, "context": context})
        result = {
            "access_token": "private-github-user-token",
            "token_type": "Bearer",
            "external_subject": "github-user-42",
            "expires_at": int(time.time()) + 1800,
        }
        return True, DirectPayload(
            type_url=(
                "type.googleapis.com/cyrene.account.center.v1.RedeemCredentialLeaseResponse"
            ),
            value=canonical_json(result),
        )


class GitHubApiFixture:
    """Return one read result after the Account Center RPC succeeds."""

    def get_issue(
        self, repository: Any, issue_number: int, token: str
    ) -> dict[str, Any]:
        assert token == "private-github-user-token"
        return {
            "issue_id": "555",
            "number": issue_number,
            "state": "OPEN",
            "title": "Task",
            "html_url": "https://github.com/owner/project/issues/7",
        }


def test_runtime_entrypoint_builds_and_uses_resolved_account_center_client(
    monkeypatch: Any,
) -> None:
    """Load the manifest entrypoint from host config and redeem over gRPC."""

    endpoint = AccountCenterEndpoint()
    server, connection_ref = serve(
        endpoint,
        "account.center.v1",
        "1",
        "127.0.0.1:0",
    )
    config = {
        "exchange_caller_id": "exchange.integrations",
        "exchange_verification_key_ref": "github-issues/exchange-verification-key",
        "github_webhook_secret_ref": "github-issues/webhook-secret",
        "repositories": [{"repository_id": "12345", "owner": "owner", "name": "project"}],
        "allowed_installation_ids": ["99"],
        "allowed_events": ["issues"],
        "allowed_actions": ["opened", "closed", "reopened"],
        "source_workflow_ids": {"12345": ["22"]},
        "account_center": {
            "binding_id": "account-center.binding.one",
            "connection_ref": connection_ref,
            "transport": "local",
            "caller_id": "plugins.github-issues",
            "signing_key_ref": "github-issues/account-center-provider-signing-key",
        },
        "hostSecretRefs": {
            "github-issues/exchange-verification-key": "TEST_EXCHANGE_VERIFY_KEY",
            "github-issues/webhook-secret": "TEST_GITHUB_WEBHOOK_SECRET",
            "github-issues/account-center-provider-signing-key": "TEST_ACCOUNT_CENTER_PROVIDER_KEY",
        },
    }
    monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", "github.binding.one")
    monkeypatch.setenv("CYRENE_CAPABILITY_CONFIGURATION_JSON", json.dumps(config))
    monkeypatch.setenv("TEST_EXCHANGE_VERIFY_KEY", EXCHANGE_KEY.hex())
    monkeypatch.setenv("TEST_ACCOUNT_CENTER_PROVIDER_KEY", ACCOUNT_CENTER_KEY.hex())
    monkeypatch.setenv(
        "TEST_GITHUB_WEBHOOK_SECRET", "github webhook key with at least 32 bytes"
    )
    try:
        provider = load_entrypoint("github_issues_provider.plugin:GitHubIssuesProvider")
        assert isinstance(provider._account_center, AccountCenterDirectLeaseClient)
        assert provider._settings.host_secret_names == config["hostSecretRefs"]
        assert provider._settings.source_workflow_ids == {"12345": frozenset({"22"})}
        assert EXCHANGE_KEY.hex() not in json.dumps(config)
        assert ACCOUNT_CENTER_KEY.hex() not in json.dumps(config)
        provider._github_api = GitHubApiFixture()
        arguments = {
            "repository_id": "12345",
            "repository": "owner/project",
            "issue_number": 7,
        }
        lease_ref = "12345678-1234-1234-1234-123456789abc"
        protected = {"payload": arguments, "lease_ref": lease_ref}
        exchange_context = sign_context(
            method="call_tool",
            payload=protected,
            caller_id="exchange.integrations",
            issuer="https://identity.example.test",
            subject="member-27",
            organization_id="org-4",
            workspace_id="workspace-8",
            binding_id="github.binding.one",
            request_id="request-123",
            operation_id="github.issue.get",
            key=EXCHANGE_KEY,
            expires_at=int(time.time()) + 60,
        )
        request = wire.CallToolRequest(
            binding_id="github.binding.one",
            provider_tool_id="github.issue.get",
            arguments_json=json.dumps(
                {
                    **protected,
                    "trusted_context": exchange_context.as_dict(),
                },
                separators=(",", ":"),
            ),
        )
        ok, typed = provider.on_invoke(
            "tool.provider.v1", "call_tool", request.SerializeToString()
        )
        assert ok is True
        response = wire.CallToolResponse.FromString(typed.value)
        assert response.WhichOneof("result") == "outcome"
        assert response.outcome.is_error is False
        assert response.outcome.content[0].json.json == (
            '{"issue_id":"555","number":7,"state":"OPEN","title":"Task",'
            '"html_url":"https://github.com/owner/project/issues/7"}'
        )
        assert len(endpoint.calls) == 1
        call = endpoint.calls[0]
        assert call["payload"] == {
            "lease_ref": lease_ref,
            "operation_id": "github.issue.get",
            "resource": {
                "repository_id": "12345",
                "repository": "owner/project",
                "issue_number": 7,
            },
            "arguments_sha256": hashlib.sha256(canonical_json(arguments)).hexdigest(),
        }
        assert call["context"]["issuer"] == "https://identity.example.test"
        assert call["context"]["binding_id"] == "account-center.binding.one"
        assert call["context"]["method"] == "redeem_credential_lease"
        assert "private-github-user-token" not in response.outcome.content[0].json.json

        for source_workflow_ids in (
            {"99999": ["22"]},
            {"12345": ["0"]},
            {"12345": ["22", "22"]},
            {"12345": "22"},
        ):
            config["source_workflow_ids"] = source_workflow_ids
            monkeypatch.setenv("CYRENE_CAPABILITY_CONFIGURATION_JSON", json.dumps(config))
            with pytest.raises(ConfigurationError, match="source_workflow_ids"):
                ProviderSettings.from_environment()
        config["source_workflow_ids"] = {"12345": ["22"]}

        config["hostSecretRefs"]["github-issues/unused-secret"] = "TEST_UNUSED_SECRET"
        monkeypatch.setenv("TEST_UNUSED_SECRET", "unused test secret value")
        monkeypatch.setenv("CYRENE_CAPABILITY_CONFIGURATION_JSON", json.dumps(config))
        with pytest.raises(ConfigurationError, match="exactly the configured secret refs"):
            ProviderSettings.from_environment()
    finally:
        active_client = getattr(locals().get("provider"), "_account_center", None)
        close = getattr(active_client, "close", None)
        if callable(close):
            close()
        server.stop(0).wait()
