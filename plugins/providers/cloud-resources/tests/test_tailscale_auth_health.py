"""Tailscale OAuth endpoint error classification tests.

中文:验证 Tailscale OAuth token Endpoint 的身份和权限错误分类。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cyrene_integration_contracts import (
    TOOL_OPERATIONS,
    parse_authorization_health,
    serialize_trusted_caller_context,
    sign_context,
)

from cloud_resources_provider._generated import tool_provider_pb2 as wire
from cloud_resources_provider.cloud_api import CloudProviderFailure, CloudResourceReader
from cloud_resources_provider.plugin import (
    CALL_TOOL_REQUEST_TYPE_URL,
    CAPABILITY_ID,
    CloudResourcesProvider,
)
from cloud_resources_provider.settings import ProviderSettings

EXCHANGE_KEY = b"\x49" * 32


class OAuthFailureTransport:
    """Fail the fixed OAuth exchange before a provider resource GET is attempted."""

    def __init__(self, failure: CloudProviderFailure) -> None:
        self.failure = failure
        self.form_calls: list[dict[str, Any]] = []

    def post_form(
        self, url: str, *, expected_host: str, form: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        """Record the bounded token request without exposing its secret in the URL."""

        self.form_calls.append(
            {"url": url, "host": expected_host, "form": dict(form), "timeout": timeout}
        )
        raise self.failure

    def get_json(
        self, url: str, *, expected_host: str, token: str | None, timeout: float
    ) -> dict[str, Any]:
        """Fail the test if resource reads proceed without an OAuth token."""

        raise AssertionError("resource GET must not follow failed OAuth acquisition")


def make_provider(
    failure: CloudProviderFailure,
) -> tuple[CloudResourcesProvider, OAuthFailureTransport]:
    """Assemble one binding-pinned Tailscale target with an injected secret."""

    configuration = {
        "trusted_callers": [
            {
                "caller_workload": "exchange.integrations",
                "audience": "cloud-resources",
                "organization_id": "org-fixture",
                "workspace_id": "workspace-fixture",
                "issuer": "https://identity.example.test",
                "verification_key_ref": "cloud-resources/exchange-verification-key",
            }
        ],
        "azure_auth": {"mode": "managed_identity"},
        "gcp_auth": {"mode": "metadata"},
        "tailscale_auth": {
            "mode": "oauth_client",
            "client_id": "fixture-client",
            "client_secret_ref": "cloud-resources/tailscale-client-secret",
            "tailnet": "example.com",
        },
        "targets": [
            {
                "target_ref": "device-prod",
                "display_name": "Build runner",
                "provider": "tailscale",
                "kind": "tailscale.device",
                "tailnet": "example.com",
                "device_id": "device-42",
            }
        ],
        "hostSecretRefs": {
            "cloud-resources/exchange-verification-key": "TEST_EXCHANGE_KEY",
            "cloud-resources/tailscale-client-secret": "TEST_TAILSCALE_SECRET",
        },
    }
    environment = {
        "CYRENE_CAPABILITY_BINDING_ID": "cloud.binding.tailscale",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(configuration),
        "TEST_EXCHANGE_KEY": EXCHANGE_KEY.hex(),
        "TEST_TAILSCALE_SECRET": "fixture-client-secret",
    }
    settings = ProviderSettings.from_environment(environment)
    transport = OAuthFailureTransport(failure)
    reader = CloudResourceReader(settings, transport=transport)
    return CloudResourcesProvider(settings=settings, reader=reader), transport


def signed_health_request() -> bytes:
    """Create one valid workload-signed health call for the configured scope."""

    arguments = {"target_ref": "device-prod"}
    now = datetime.now(UTC)
    context = sign_context(
        method="cloud_authorization_health",
        operation_id=TOOL_OPERATIONS["cloud_authorization_health"],
        payload=arguments,
        caller_workload="exchange.integrations",
        audience="cloud-resources",
        issuer="https://identity.example.test",
        subject="member-fixture",
        organization_id="org-fixture",
        workspace_id="workspace-fixture",
        binding_id="cloud.binding.tailscale",
        request_id="tailscale-health-fixture",
        key=EXCHANGE_KEY,
        issued_at=now,
        expires_at=now + timedelta(seconds=60),
    )
    return wire.CallToolRequest(
        binding_id="cloud.binding.tailscale",
        provider_tool_id="cloud_authorization_health",
        arguments_json=json.dumps(
            {
                "caller_context": serialize_trusted_caller_context(context),
                "arguments": arguments,
            },
            separators=(",", ":"),
        ),
    ).SerializeToString()


@pytest.mark.parametrize(
    ("code", "retryable", "expected_authorization_state"),
    [
        ("AUTHENTICATION_REQUIRED", False, "REAUTHORIZATION_REQUIRED"),
        ("PERMISSION_DENIED", False, "PERMISSION_DENIED"),
        ("PROVIDER_TIMEOUT", True, "UNKNOWN"),
    ],
)
def test_tailscale_oauth_failures_preserve_secret_presence_and_scope_state(
    code: str, retryable: bool, expected_authorization_state: str
) -> None:
    """OAuth 401/403 and network failures have distinct, source-supported health."""

    provider, transport = make_provider(CloudProviderFailure(code, retryable=retryable))
    ok, typed = provider.on_invoke(
        CAPABILITY_ID,
        "call_tool",
        signed_health_request(),
        request_type_url=CALL_TOOL_REQUEST_TYPE_URL,
    )

    assert ok is True
    response = wire.CallToolResponse.FromString(typed.value)
    assert response.outcome.is_error is False
    health = parse_authorization_health(json.loads(response.outcome.content[0].json.json))
    assert health.login_state == "PRESENT"
    assert health.authorization_state == expected_authorization_state
    assert health.capabilities[0].state == expected_authorization_state
    assert health.capabilities[0].last_verified_at is None
    assert len(transport.form_calls) == 1
    assert transport.form_calls[0]["url"] == "https://api.tailscale.com/api/v2/oauth/token"
    assert "fixture-client-secret" not in transport.form_calls[0]["url"]
