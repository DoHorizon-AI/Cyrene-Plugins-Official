"""HF anonymous and authenticated health classification tests.

中文:验证 Hugging Face 匿名访问和带令牌访问的权限状态分类。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

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

EXCHANGE_KEY = b"\x29" * 32


class HfFixtureTransport:
    """Return one Hub metadata result or a sanitized fixture failure."""

    def __init__(self) -> None:
        self.response: dict[str, Any] | CloudProviderFailure = {
            "id": "org-fixture/private-model",
            "private": False,
            "gated": False,
            "disabled": False,
        }
        self.tokens: list[str | None] = []

    def get_json(
        self, url: str, *, expected_host: str, token: str | None, timeout: float
    ) -> dict[str, Any]:
        """Record the credential presence and return the response fixture."""

        assert expected_host == "huggingface.co"
        assert url == "https://huggingface.co/api/models/org-fixture/private-model"
        self.tokens.append(token)
        if isinstance(self.response, CloudProviderFailure):
            raise self.response
        return self.response


def make_provider() -> tuple[CloudResourcesProvider, HfFixtureTransport, dict[str, str]]:
    """Build one HF target whose optional token is mapped only by host config."""

    configuration = {
        "trusted_callers": [
            {
                "caller_workload": "exchange.integrations",
                "audience": "cloud-resources",
                "organization_id": "org-fixture",
                "workspace_id": "workspace-fixture",
                "verification_key_ref": "cloud-resources/exchange-verification-key",
            }
        ],
        "azure_auth": {"mode": "managed_identity"},
        "gcp_auth": {"mode": "metadata"},
        "hf_token_ref": "cloud-resources/hf-read-token",
        "targets": [
            {
                "target_ref": "hf-private",
                "display_name": "Hub model",
                "provider": "hf",
                "kind": "hf.model",
                "repo_id": "org-fixture/private-model",
            }
        ],
        "hostSecretRefs": {
            "cloud-resources/exchange-verification-key": "TEST_EXCHANGE_KEY",
            "cloud-resources/hf-read-token": "TEST_HF_READ_TOKEN",
        },
    }
    environment = {
        "CYRENE_CAPABILITY_BINDING_ID": "cloud.binding.hf",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(configuration),
        "TEST_EXCHANGE_KEY": EXCHANGE_KEY.hex(),
    }
    settings = ProviderSettings.from_environment(environment)
    transport = HfFixtureTransport()
    reader = CloudResourceReader(settings, transport=transport)
    return CloudResourcesProvider(settings=settings, reader=reader), transport, environment


def call_health(provider: CloudResourcesProvider) -> Any:
    """Call the provider's signed health tool with one fixture caller context."""

    arguments = {"target_ref": "hf-private"}
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
        binding_id="cloud.binding.hf",
        request_id="request-hf-health",
        key=EXCHANGE_KEY,
        issued_at=now,
        expires_at=now + timedelta(seconds=60),
    )
    request = wire.CallToolRequest(
        binding_id="cloud.binding.hf",
        provider_tool_id="cloud_authorization_health",
        arguments_json=json.dumps(
            {
                "caller_context": serialize_trusted_caller_context(context),
                "arguments": arguments,
            },
            separators=(",", ":"),
        ),
    )
    ok, payload = provider.on_invoke(
        CAPABILITY_ID,
        "call_tool",
        request.SerializeToString(),
        request_type_url=CALL_TOOL_REQUEST_TYPE_URL,
    )
    assert ok is True
    response = wire.CallToolResponse.FromString(payload.value)
    return parse_authorization_health(json.loads(response.outcome.content[0].json.json))


def test_public_hf_can_be_valid_anonymously_with_login_missing() -> None:
    """Anonymous metadata access proves a public capability, not a user login."""

    provider, transport, _ = make_provider()

    health = call_health(provider)

    assert health.login_state == "MISSING"
    assert health.authorization_state == "VALID"
    assert health.checked_at is not None
    assert health.expires_at is None
    assert health.generation == 0
    assert health.capabilities[0].state == "VALID"
    assert health.capabilities[0].expires_at is None
    assert transport.tokens == [None]


def test_private_hf_metadata_never_marks_anonymous_scope_valid() -> None:
    """A private repository response without a token keeps authorization unknown."""

    provider, transport, _ = make_provider()
    transport.response = {
        "id": "org-fixture/private-model",
        "private": True,
        "gated": True,
        "disabled": False,
    }

    health = call_health(provider)

    assert health.login_state == "MISSING"
    assert health.authorization_state == "UNKNOWN"
    assert health.capabilities[0].state == "UNKNOWN"
    assert health.capabilities[0].reason_code == "PUBLIC_SCOPE_NOT_PROVEN"


def test_rejected_configured_hf_token_is_distinct_from_missing_login() -> None:
    """A vendor 401 for an injected token asks for reauthorization, not MISSING."""

    provider, transport, environment = make_provider()
    environment["TEST_HF_READ_TOKEN"] = "fixture-scoped-read-token"
    transport.response = CloudProviderFailure("AUTHENTICATION_REQUIRED")

    health = call_health(provider)

    assert health.login_state == "PRESENT"
    assert health.authorization_state == "REAUTHORIZATION_REQUIRED"
    assert health.capabilities[0].state == "REAUTHORIZATION_REQUIRED"
    assert transport.tokens == ["fixture-scoped-read-token"]


def test_hf_permission_denial_is_not_misreported_as_invalid_credentials() -> None:
    """A source 403 means access is denied, not that the token is necessarily invalid."""

    provider, transport, environment = make_provider()
    environment["TEST_HF_READ_TOKEN"] = "fixture-scoped-read-token"
    transport.response = CloudProviderFailure("PERMISSION_DENIED")

    health = call_health(provider)

    assert health.login_state == "PRESENT"
    assert health.authorization_state == "PERMISSION_DENIED"
    assert health.capabilities[0].state == "PERMISSION_DENIED"
    assert health.capabilities[0].reason_code == "CLOUD_PERMISSION_DENIED"


def test_hf_transport_failure_keeps_configured_login_presence_unknown_for_auth() -> None:
    """A timeout preserves configured login presence but leaves Hub access unknown."""

    provider, transport, environment = make_provider()
    environment["TEST_HF_READ_TOKEN"] = "fixture-scoped-read-token"
    transport.response = CloudProviderFailure("PROVIDER_TIMEOUT", retryable=True)

    health = call_health(provider)

    assert health.login_state == "PRESENT"
    assert health.authorization_state == "UNKNOWN"
    assert health.capabilities[0].state == "UNKNOWN"
    assert health.capabilities[0].reason_code == "CLOUD_PROVIDER_TIMEOUT"
