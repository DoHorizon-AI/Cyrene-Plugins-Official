"""End-to-end signed tool invocation through the local Plugin runtime.

中文:通过本地 Plugin Runtime 端到端验证带签名的工具调用。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cyrene_integration_contracts import (
    TOOL_OPERATIONS,
    parse_authorization_health,
    parse_cloud_query_result,
    serialize_trusted_caller_context,
    sign_context,
)
from cyrene_plugin_runtime.client import DirectPayload, DirectPluginClient
from cyrene_plugin_runtime.server import load_entrypoint, serve

from cloud_resources_provider import plugin as plugin_module
from cloud_resources_provider._generated import tool_provider_pb2 as wire
from cloud_resources_provider.cloud_api import (
    BearerCredential,
    CloudProviderFailure,
    CloudResourceReader,
)
from cloud_resources_provider.plugin import (
    CALL_TOOL_REQUEST_TYPE_URL,
    CALL_TOOL_RESPONSE_TYPE_URL,
    CAPABILITY_ID,
    LIST_TOOLS_REQUEST_TYPE_URL,
)
from cloud_resources_provider.settings import (
    ProviderSettings,
)

EXCHANGE_KEY = b"\x19" * 32
CHECKED_AT = datetime(2026, 10, 14, 14, 30, tzinfo=UTC)


class FixtureTransport:
    """Return one Azure instance-view fixture and record read-only calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.document: dict[str, Any] | CloudProviderFailure = {
            "statuses": [{"code": "PowerState/running", "time": "2026-10-14T14:29:00Z"}]
        }

    def get_json(
        self, url: str, *, expected_host: str, token: str | None, timeout: float
    ) -> dict[str, Any]:
        """Return the configured fixture without opening a network connection."""

        assert expected_host == "management.azure.com"
        assert token == "fixture-azure-token"
        assert timeout == 3.0
        self.calls.append(url)
        if isinstance(self.document, CloudProviderFailure):
            raise self.document
        return self.document


class FixtureCredentials:
    """Provide a synthetic bearer token for the fixture Azure adapter."""

    def acquire(self, target: Any) -> BearerCredential:
        """Return an in-memory token with a source-known expiry."""

        assert target.provider == "azure"
        return BearerCredential("fixture-azure-token", CHECKED_AT.replace(hour=16))


def activation_configuration() -> dict[str, Any]:
    """Return one valid host-injected Azure binding for entrypoint loading."""

    return {
        "authorization_generation": 7,
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
        "targets": [
            {
                "target_ref": "vm-prod",
                "display_name": "Production VM",
                "provider": "azure",
                "kind": "azure.vm",
                "subscription_id": "11111111-2222-3333-4444-555555555555",
                "resource_group": "compute-prod",
                "vm_name": "worker-01",
            }
        ],
        "timeout_seconds": 3.0,
        "hostSecretRefs": {"cloud-resources/exchange-verification-key": "TEST_EXCHANGE_KEY"},
    }


def signed_tool_call(
    tool_id: str,
    arguments: dict[str, Any],
    *,
    organization_id: str = "org-fixture",
    workspace_id: str = "workspace-fixture",
    tamper_after_signing: bool = False,
) -> bytes:
    """Serialize a tool request with a caller context signed by the fixture key."""

    now = datetime.now(UTC)
    signed = sign_context(
        method=tool_id,
        operation_id=TOOL_OPERATIONS[tool_id],
        payload=arguments,
        caller_workload="exchange.integrations",
        audience="cloud-resources",
        issuer="https://identity.example.test",
        subject="member-fixture",
        organization_id=organization_id,
        workspace_id=workspace_id,
        binding_id="cloud.binding.fixture",
        request_id=f"request-{tool_id}",
        key=EXCHANGE_KEY,
        issued_at=now,
        expires_at=now + timedelta(seconds=60),
    )
    context = serialize_trusted_caller_context(signed)
    if tamper_after_signing:
        context["workspace_id"] = "workspace-tampered"
    request = wire.CallToolRequest(
        binding_id="cloud.binding.fixture",
        provider_tool_id=tool_id,
        arguments_json=json.dumps(
            {"caller_context": context, "arguments": arguments},
            separators=(",", ":"),
        ),
    )
    return request.SerializeToString()


def invoke(
    client: DirectPluginClient, method: str, payload: bytes, type_url: str
) -> wire.CallToolResponse:
    """Call one typed capability method through the actual local gRPC runtime."""

    response = client.invoke(
        capability=CAPABILITY_ID,
        interface_version="1",
        method=method,
        request=DirectPayload(type_url=type_url, value=payload),
        deadline_seconds=5.0,
    )
    assert response.type_url == CALL_TOOL_RESPONSE_TYPE_URL
    return wire.CallToolResponse.FromString(response.value)


def test_signed_tools_run_through_loaded_entrypoint_and_local_runtime_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local runtime returns typed observations and scoped IAM health."""

    fixture = FixtureTransport()

    def reader_factory(settings: ProviderSettings) -> CloudResourceReader:
        """Replace cloud I/O with a fixture while retaining no-argument assembly."""

        return CloudResourceReader(
            settings,
            transport=fixture,
            credentials=FixtureCredentials(),
            clock=lambda: CHECKED_AT,
        )

    monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", "cloud.binding.fixture")
    monkeypatch.setenv(
        "CYRENE_CAPABILITY_CONFIGURATION_JSON", json.dumps(activation_configuration())
    )
    monkeypatch.setenv("TEST_EXCHANGE_KEY", EXCHANGE_KEY.hex())
    monkeypatch.setattr(plugin_module, "CloudResourceReader", reader_factory)
    provider = load_entrypoint("cloud_resources_provider.plugin:CloudResourcesProvider")
    server, connection_ref = serve(provider, CAPABILITY_ID, "1", "127.0.0.1:0")
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    try:
        catalog_request = wire.ListToolsRequest(binding_id="cloud.binding.fixture")
        catalog_payload = client.invoke(
            capability=CAPABILITY_ID,
            interface_version="1",
            method="list_tools",
            request=DirectPayload(
                type_url=LIST_TOOLS_REQUEST_TYPE_URL,
                value=catalog_request.SerializeToString(),
            ),
            deadline_seconds=5.0,
        )
        catalog = wire.ListToolsResponse.FromString(catalog_payload.value)
        assert [tool.provider_tool_id for tool in catalog.catalog.tools] == [
            "cloud_resources_query",
            "cloud_authorization_health",
        ]

        query_response = invoke(
            client,
            "call_tool",
            signed_tool_call("cloud_resources_query", {"target_ref": "vm-prod"}),
            CALL_TOOL_REQUEST_TYPE_URL,
        )
        assert query_response.outcome.is_error is False
        query_json = json.loads(query_response.outcome.content[0].json.json)
        query = parse_cloud_query_result(query_json)
        assert query.provider == "azure"
        assert len(query.resources) == 1
        assert query.resources[0].status == "RUNNING"
        assert query.resources[0].external_ref == "vm-prod"
        assert len(fixture.calls) == 1

        health_response = invoke(
            client,
            "call_tool",
            signed_tool_call("cloud_authorization_health", {"target_ref": "vm-prod"}),
            CALL_TOOL_REQUEST_TYPE_URL,
        )
        health = parse_authorization_health(
            json.loads(health_response.outcome.content[0].json.json)
        )
        assert health.authorization_state == "VALID"
        assert health.login_state == "PRESENT"
        assert health.generation == 7

        fixture.document = CloudProviderFailure("PERMISSION_DENIED")
        denied_response = invoke(
            client,
            "call_tool",
            signed_tool_call("cloud_authorization_health", {"target_ref": "vm-prod"}),
            CALL_TOOL_REQUEST_TYPE_URL,
        )
        denied_health = parse_authorization_health(
            json.loads(denied_response.outcome.content[0].json.json)
        )
        assert denied_health.login_state == "PRESENT"
        assert denied_health.authorization_state == "PERMISSION_DENIED"
        assert denied_health.capabilities[0].state == "PERMISSION_DENIED"

        extra_field_response = invoke(
            client,
            "call_tool",
            signed_tool_call(
                "cloud_resources_query",
                {"target_ref": "vm-prod", "unexpected": "ignored-no"},
            ),
            CALL_TOOL_REQUEST_TYPE_URL,
        )
        assert extra_field_response.outcome.is_error is True
        extra_field_error = json.loads(extra_field_response.outcome.content[0].json.json)
        assert extra_field_error["error"]["code"] == "INVALID_ARGUMENTS"

        tampered_response = invoke(
            client,
            "call_tool",
            signed_tool_call(
                "cloud_resources_query",
                {"target_ref": "vm-prod"},
                tamper_after_signing=True,
            ),
            CALL_TOOL_REQUEST_TYPE_URL,
        )
        assert tampered_response.outcome.is_error is True
        tampered_error = json.loads(tampered_response.outcome.content[0].json.json)
        assert tampered_error["error"]["code"] == "AUTHENTICATION_DENIED"

        wrong_scope = invoke(
            client,
            "call_tool",
            signed_tool_call(
                "cloud_resources_query",
                {"target_ref": "vm-prod"},
                organization_id="org-other",
            ),
            CALL_TOOL_REQUEST_TYPE_URL,
        )
        assert wrong_scope.outcome.is_error is True
        scope_error = json.loads(wrong_scope.outcome.content[0].json.json)
        assert scope_error["error"]["code"] == "AUTHENTICATION_DENIED"
        assert len(fixture.calls) == 3
    finally:
        client.close()
        server.stop(0).wait()
