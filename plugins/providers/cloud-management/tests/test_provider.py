"""Semantic fixture tests for signed cloud management and Runpod API behavior.

中文：使用本地夹具验证签名、profile 范围、幂等和 Runpod 状态回读。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cyrene_integration_contracts import canonical_json, sign_context
from jsonschema import Draft202012Validator

from cloud_management_provider import providers as provider_impl
from cloud_management_provider._generated import tool_provider_pb2 as wire
from cloud_management_provider.operations import InvalidOperationRef, parse_operation_ref
from cloud_management_provider.plugin import CloudManagementProvider
from cloud_management_provider.process import ProcessResult
from cloud_management_provider.providers import CloudManagementExecutor, ProviderFailure
from cloud_management_provider.settings import ConfigurationError, ProviderSettings
from cloud_management_provider.tool_catalog import (
    CALL_TOOL_METHOD,
    CALL_TOOL_RESPONSE_TYPE_URL,
    CAPABILITY_ID,
    TOOL_BY_ID,
)

_API_KEY = "fixture-" + "x" * 32
_EXCHANGE_KEY = bytes.fromhex("a5" * 32)


class _PartialThenTimeoutStream(httpx.SyncByteStream):
    """Yield one complete SSE event, then model a quiet streaming read timeout."""

    def __iter__(self):
        yield b'data: {"ts":"2026-10-23T00:00:00Z","source":"container","line":"tail"}\n\n'
        raise httpx.ReadTimeout("fixture quiet timeout")

    def close(self) -> None:
        pass


def _configuration(tmp_path: Any, *, cli_enabled: bool = False) -> dict[str, Any]:
    """Build one host-only Runpod profile with explicit resource limits."""

    profile = {
        "profile_ref": "runpod/test",
        "provider": "runpod",
        "identity_ref": "accounts/runpod/test",
        "scope": {
            "pod_ids": ["pod-123"],
            "pod_id_prefixes": ["cyrene-test-"],
            "pod_name_prefixes": ["cyrene-test-"],
            "gpu_type_ids": ["NVIDIA GeForce RTX 4090"],
            "data_center_ids": ["US-KS-2"],
            "max_gpu_count": 1,
            "max_container_disk_gb": 64,
            "max_volume_disk_gb": 128,
        },
        "auth": {
            "mode": "api_key",
            "api_key_ref": "cloud-management/runpod-api-key",
        },
    }
    if cli_enabled:
        profile["cli"] = {
            "enabled": True,
            "executable": "/usr/bin/runpodctl",
            "timeout_seconds": 5,
        }
    return {
        "trusted_callers": [
            {
                "caller_workload": "exchange",
                "audience": "cloud-management",
                "organization_id": "org-fixture",
                "workspace_id": "workspace-fixture",
                "issuer": "https://directory.example",
                "verification_key_ref": "cloud-management/exchange-verification-key",
            }
        ],
        "profiles": [profile],
        "hostSecretRefs": {
            "cloud-management/exchange-verification-key": "EXCHANGE_KEY",
            "cloud-management/state-dir": "STATE_DIR",
            "cloud-management/runpod-api-key": "RUNPOD_API_KEY",
        },
    }


def _settings(tmp_path: Any, *, cli_enabled: bool = False) -> ProviderSettings:
    """Create validated test settings from temporary host secret values."""

    environment = {
        "CYRENE_CAPABILITY_BINDING_ID": "binding-cloud-test",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(
            _configuration(tmp_path, cli_enabled=cli_enabled)
        ),
        "EXCHANGE_KEY": _EXCHANGE_KEY.hex(),
        "STATE_DIR": str(tmp_path),
        "RUNPOD_API_KEY": _API_KEY,
    }
    return ProviderSettings.from_environment(environment)


def _multi_provider_settings(
    tmp_path: Any, *, azure_resource_api_versions: dict[str, str] | None = None
) -> ProviderSettings:
    """Create Azure, GCP WIF, and Runpod profiles without contacting providers."""

    config = _configuration(tmp_path)
    azure_api_versions = {"microsoft.compute/virtualmachines": "2024-07-01"}
    if azure_resource_api_versions:
        azure_api_versions.update(azure_resource_api_versions)
    config["profiles"] = [
        {
            "profile_ref": "azure/test",
            "provider": "azure",
            "identity_ref": "managed-identities/azure/test",
            "scope": {
                "subscription_id": "11111111-2222-3333-4444-555555555555",
                "resource_groups": ["rg-test"],
                "resource_api_versions": azure_api_versions,
            },
            "auth": {"mode": "managed_identity"},
            "cli": {
                "enabled": True,
                "executable": "/usr/bin/az",
                "timeout_seconds": 5,
            },
        },
        {
            "profile_ref": "gcp/test",
            "provider": "gcp",
            "identity_ref": "service-accounts/gcp/test",
            "scope": {
                "project_id": "cyrene-test-project",
                "zones": ["us-central1-a"],
                "instance_name_prefixes": ["cyrene-test-"],
            },
            "auth": {
                "mode": "workload_identity_federation",
                "credential_config_ref": "cloud-management/gcp-wif-credential-config",
            },
            "cli": {
                "enabled": True,
                "executable": "/usr/bin/gcloud",
                "timeout_seconds": 5,
            },
        },
    ]
    config["hostSecretRefs"].pop("cloud-management/runpod-api-key")
    config["hostSecretRefs"]["cloud-management/gcp-wif-credential-config"] = "GCP_WIF_CONFIG"
    schema = json.loads(
        (Path(__file__).parents[1] / "configuration.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(config)
    environment = {
        "CYRENE_CAPABILITY_BINDING_ID": "binding-cloud-test",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(config),
        "EXCHANGE_KEY": _EXCHANGE_KEY.hex(),
        "STATE_DIR": str(tmp_path),
        "RUNPOD_API_KEY": _API_KEY,
        "GCP_WIF_CONFIG": str(tmp_path / "fixture-wif.json"),
    }
    return ProviderSettings.from_environment(environment)


def _pod_response(
    pod_id: str = "pod-123",
    name: str = "cyrene-test-unit",
    status: str = "RUNNING",
) -> dict[str, Any]:
    """Return a minimal Runpod v2 Pod document without credentials or env vars."""

    return {
        "id": pod_id,
        "name": name,
        "image": "example/image:1",
        "status": status,
        "cost": 0.2,
        "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
    }


def test_cloud_provider_health_reports_managed_identity_and_wif_expiry(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Azure and GCP health checks expose only short-lived token expiry metadata."""

    settings = _multi_provider_settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"items": []}))
    )
    executor = CloudManagementExecutor(settings, http_client=client)
    azure_expiry_epoch = datetime(2026, 10, 26, 22, 0, tzinfo=UTC).timestamp()
    gcp_expiry = datetime(2026, 10, 26, 22, 5, tzinfo=UTC)

    class _AzureCredential:
        def get_token(self, scope: str) -> SimpleNamespace:
            assert scope == "https://management.azure.com/.default"
            return SimpleNamespace(token="fixture-azure-token", expires_on=azure_expiry_epoch)

    class _GcpCredential:
        valid = False
        token: str | None = None
        expiry: datetime | None = None

        def refresh(self, _request: Any) -> None:
            self.valid = True
            self.token = "fixture-gcp-token"
            self.expiry = gcp_expiry

    monkeypatch.setitem(executor._credentials, "azure/test", _AzureCredential())  # noqa: SLF001
    monkeypatch.setitem(executor._credentials, "gcp/test", _GcpCredential())  # noqa: SLF001

    azure = executor.call(
        "cloud_provider_health",
        {"profile_ref": "azure/test"},
        request_id="request-azure-health",
    )
    gcp = executor.call(
        "cloud_provider_health",
        {"profile_ref": "gcp/test"},
        request_id="request-gcp-health",
    )

    expected_fields = {
        "profile_ref",
        "provider",
        "identity_ref",
        "authentication_state",
        "authorization_state",
        "scope_refs",
        "checked_at",
        "expires_at",
        "reason_code",
    }
    assert set(azure) == expected_fields
    assert set(gcp) == expected_fields
    assert azure["authentication_state"] == "VALID"
    assert azure["authorization_state"] == "VALID"
    assert azure["expires_at"] == "2026-10-26T22:00:00Z"
    assert gcp["authentication_state"] == "VALID"
    assert gcp["authorization_state"] == "VALID"
    assert gcp["expires_at"] == "2026-10-26T22:05:00Z"
    executor.close()


def test_cloud_provider_health_runpod_key_has_unknown_expiry(
    tmp_path: Any,
) -> None:
    """A working static Runpod key is valid now but has no asserted expiry date."""

    settings = _settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"pods": []}))
    )
    executor = CloudManagementExecutor(settings, http_client=client)

    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": "runpod/test"},
        request_id="request-runpod-health",
    )

    assert health["authentication_state"] == "VALID"
    assert health["authorization_state"] == "VALID"
    assert health["expires_at"] is None
    assert health["reason_code"] is None
    executor.close()


def test_cloud_provider_health_missing_runpod_key_requests_reauthorization(
    tmp_path: Any,
) -> None:
    """A missing host key is actionable and distinct from an unknown network result."""

    settings = ProviderSettings.from_environment(
        {
            "CYRENE_CAPABILITY_BINDING_ID": "binding-cloud-test",
            "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(_configuration(tmp_path)),
            "EXCHANGE_KEY": _EXCHANGE_KEY.hex(),
            "STATE_DIR": str(tmp_path),
            "RUNPOD_API_KEY": "",
        }
    )
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"pods": []})
            )
        ),
    )

    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": "runpod/test"},
        request_id="request-runpod-health-missing",
    )

    assert health["authentication_state"] == "MISSING"
    assert health["authorization_state"] == "REAUTHORIZATION_REQUIRED"
    assert health["expires_at"] is None
    assert health["reason_code"] == "CREDENTIAL_MISSING"
    executor.close()


@pytest.mark.parametrize(
    ("status_code", "expected_authentication", "expected_authorization", "reason_code"),
    [
        (401, "INVALID", "REAUTHORIZATION_REQUIRED", "AUTHENTICATION_FAILED"),
        (403, "VALID", "PERMISSION_DENIED", "SCOPE_DENIED"),
        (302, "UNKNOWN", "UNKNOWN", "PROVIDER_REDIRECT_REJECTED"),
        (503, "UNKNOWN", "UNKNOWN", "PROVIDER_ERROR"),
    ],
)
def test_cloud_provider_health_distinguishes_auth_scope_and_upstream_failures(
    tmp_path: Any,
    status_code: int,
    expected_authentication: str,
    expected_authorization: str,
    reason_code: str,
) -> None:
    """401, 403, and provider failure keep distinct safe health outcomes."""

    settings = _settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status_code, json={"detail": "private body"})
        )
    )
    executor = CloudManagementExecutor(settings, http_client=client)

    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": "runpod/test"},
        request_id=f"request-runpod-health-{status_code}",
    )

    assert health["authentication_state"] == expected_authentication
    assert health["authorization_state"] == expected_authorization
    assert health["reason_code"] == reason_code
    assert "private body" not in json.dumps(health)
    executor.close()


def test_cloud_provider_health_network_failure_is_unknown_not_invalid(
    tmp_path: Any,
) -> None:
    """A network failure cannot be reported as an invalid Runpod API key."""

    settings = _settings(tmp_path)

    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private network detail")

    client = httpx.Client(transport=httpx.MockTransport(fail))
    executor = CloudManagementExecutor(settings, http_client=client)

    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": "runpod/test"},
        request_id="request-runpod-health-network",
    )

    assert health["authentication_state"] == "UNKNOWN"
    assert health["authorization_state"] == "UNKNOWN"
    assert health["reason_code"] == "PROVIDER_UNAVAILABLE"
    assert "private network detail" not in json.dumps(health)
    executor.close()


@pytest.mark.parametrize(
    ("failure_type", "expected_authentication", "expected_authorization", "expected_reason"),
    [
        ("transport", "UNKNOWN", "UNKNOWN", "PROVIDER_UNAVAILABLE"),
        ("auth", "INVALID", "REAUTHORIZATION_REQUIRED", "AUTHENTICATION_FAILED"),
    ],
)
def test_azure_managed_identity_health_does_not_misreport_token_transport_errors(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: str,
    expected_authentication: str,
    expected_authorization: str,
    expected_reason: str,
) -> None:
    """Managed identity token transport and authentication failures remain distinct."""

    from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError

    settings = _multi_provider_settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    )
    executor = CloudManagementExecutor(settings, http_client=client)
    error: Exception = (
        ServiceRequestError("private transport detail")
        if failure_type == "transport"
        else ClientAuthenticationError("private identity detail")
    )

    class _Credential:
        def get_token(self, _scope: str) -> Any:
            raise error

    monkeypatch.setitem(executor._credentials, "azure/test", _Credential())  # noqa: SLF001
    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": "azure/test"},
        request_id=f"request-azure-token-{failure_type}",
    )

    assert health["authentication_state"] == expected_authentication
    assert health["authorization_state"] == expected_authorization
    assert health["reason_code"] == expected_reason
    assert "private" not in json.dumps(health)
    executor.close()


@pytest.mark.parametrize(
    ("retryable", "expected_authentication", "expected_authorization", "expected_reason"),
    [
        (True, "UNKNOWN", "UNKNOWN", "PROVIDER_UNAVAILABLE"),
        (False, "INVALID", "REAUTHORIZATION_REQUIRED", "AUTHENTICATION_FAILED"),
    ],
)
def test_gcp_wif_health_treats_retryable_refresh_errors_as_unknown(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    retryable: bool,
    expected_authentication: str,
    expected_authorization: str,
    expected_reason: str,
) -> None:
    """WIF refresh retries are not mistaken for an invalid long-lived identity."""

    from google.auth.exceptions import RefreshError

    settings = _multi_provider_settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"items": []}))
    )
    executor = CloudManagementExecutor(settings, http_client=client)

    class _Credential:
        valid = False

        def refresh(self, _request: Any) -> None:
            raise RefreshError("private refresh detail", retryable=retryable)

    monkeypatch.setitem(executor._credentials, "gcp/test", _Credential())  # noqa: SLF001
    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": "gcp/test"},
        request_id=f"request-gcp-refresh-{retryable}",
    )

    assert health["authentication_state"] == expected_authentication
    assert health["authorization_state"] == expected_authorization
    assert health["reason_code"] == expected_reason
    assert "private refresh detail" not in json.dumps(health)
    executor.close()


@pytest.mark.parametrize(
    ("profile_ref", "target", "response"),
    [
        ("azure/test", {"resource_group": "rg-test"}, {"error": {"code": "Denied"}}),
        ("gcp/test", {"zone": "us-central1-a"}, {"error": {"code": "denied"}}),
        ("runpod/test", {"scope_ref": "pods"}, {"error": {"status": 403}}),
    ],
)
def test_cloud_resource_list_does_not_turn_error_envelopes_into_empty_lists(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    profile_ref: str,
    target: dict[str, str],
    response: dict[str, Any],
) -> None:
    """HTTP success with an error or malformed envelope never becomes resources=[]."""

    settings = (
        _multi_provider_settings(tmp_path)
        if profile_ref != "runpod/test"
        else _settings(tmp_path)
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response))
    )
    executor = CloudManagementExecutor(settings, http_client=client)
    monkeypatch.setattr(executor, "_azure_token", lambda _profile: ("fixture", None))
    monkeypatch.setattr(executor, "_gcp_token", lambda _profile: ("fixture", None))

    with pytest.raises(ProviderFailure, match="INVALID_PROVIDER_RESPONSE"):
        executor.call(
            "cloud_resources_list",
            {"profile_ref": profile_ref, "target": target},
            request_id=f"request-list-envelope-{profile_ref}",
        )
    executor.close()


def test_runpod_readback_does_not_treat_error_envelope_as_missing_pod(tmp_path: Any) -> None:
    """A malformed Pod list cannot falsely confirm that a new resource is absent."""

    settings = _settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"error": {"code": "Unavailable"}})
        )
    )
    executor = CloudManagementExecutor(settings, http_client=client)

    with pytest.raises(ProviderFailure, match="INVALID_PROVIDER_RESPONSE"):
        executor._find_runpod_by_name(  # noqa: SLF001
            settings.profile("runpod/test"), "cyrene-test-unit"
        )
    executor.close()


def test_runpod_create_is_scope_bounded_and_replay_safe(tmp_path: Any) -> None:
    """One signed create calls Runpod once and caches the exact typed result."""

    settings = _settings(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.runpod.io"
        assert request.headers["Authorization"] == f"Bearer {_API_KEY}"
        payload = json.loads(request.content)
        assert payload["name"] == "cyrene-test-unit"
        assert payload["gpu"]["count"] == 1
        return httpx.Response(
            200,
            json=_pod_response("new-pod-1", "cyrene-test-unit", "CREATING"),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    executor = CloudManagementExecutor(settings, http_client=client)
    arguments = {
        "profile_ref": "runpod/test",
        "target": {"pod_name": "cyrene-test-unit"},
        "parameters": {
            "image": "example/image:1",
            "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
            "disk": 20,
            "dataCenterIds": ["US-KS-2"],
        },
    }

    result = executor.call("cloud_resource_create", arguments, request_id="request-create-1")
    replay = executor.call("cloud_resource_create", arguments, request_id="request-create-1")

    assert len(requests) == 1
    assert result == replay
    assert result["state"] == "SUCCEEDED"
    assert result["expected_state"] == "RUNNING"
    assert result["actual_state"] == "CREATING"
    assert result["readiness"] == "NOT_READY"
    assert result["release_confirmed"] is False
    assert _API_KEY not in json.dumps(result)
    operation = parse_operation_ref(result["operation_ref"], _EXCHANGE_KEY)
    assert operation["provider"] == "runpod"
    assert operation["profile_ref"] == "runpod/test"
    executor.close()


def test_activation_schema_accepts_the_typed_provider_profile(tmp_path: Any) -> None:
    """The published activation schema matches the provider's strict parser."""

    schema = json.loads(
        (Path(__file__).parents[1] / "configuration.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(_configuration(tmp_path))


def test_ledger_capacity_is_host_configurable_with_fixed_bounds(tmp_path: Any) -> None:
    """The deployment can size the ledger without allowing unbounded limits."""

    environment = {
        "CYRENE_CAPABILITY_BINDING_ID": "binding-cloud-test",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(_configuration(tmp_path)),
        "EXCHANGE_KEY": _EXCHANGE_KEY.hex(),
        "STATE_DIR": str(tmp_path),
        "RUNPOD_API_KEY": _API_KEY,
        "CYRENE_CLOUD_MANAGEMENT_LEDGER_MAX_ROWS": "42",
        "CYRENE_CLOUD_MANAGEMENT_LEDGER_MAX_BYTES": str(4 * 1024 * 1024),
        "CYRENE_CLOUD_MANAGEMENT_LEDGER_TERMINAL_RETENTION_DAYS": "45",
    }
    settings = ProviderSettings.from_environment(environment)
    assert settings.ledger_max_rows == 42
    assert settings.ledger_max_bytes == 4 * 1024 * 1024
    assert settings.ledger_terminal_retention_seconds == 45 * 24 * 60 * 60

    environment["CYRENE_CLOUD_MANAGEMENT_LEDGER_MAX_ROWS"] = "1000000000"
    with pytest.raises(ConfigurationError, match="outside its allowed range"):
        ProviderSettings.from_environment(environment)


def test_runpod_stop_reports_actual_state_and_does_not_claim_release(tmp_path: Any) -> None:
    """A Pod stop can release the GPU while preserving the paid storage resource."""

    settings = _settings(tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=_pod_response(status="RUNNING"))
        assert json.loads(request.content) == {"action": "stop"}
        return httpx.Response(200, json=_pod_response(status="EXITED"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    executor = CloudManagementExecutor(settings, http_client=client)
    result = executor.call(
        "cloud_resource_stop",
        {"profile_ref": "runpod/test", "target": {"pod_id": "pod-123"}},
        request_id="request-stop-1",
    )

    assert seen == ["GET", "POST"]
    assert result["state"] == "SUCCEEDED"
    assert result["desired_state"] == "EXITED"
    assert result["actual_state"] == "EXITED"
    assert result["readiness"] == "NOT_READY"
    assert result["release_confirmed"] is False
    executor.close()


def test_runpod_restart_uses_vendor_restart_action(tmp_path: Any) -> None:
    """Restart remains distinct from start and reports the observed Pod state."""

    settings = _settings(tmp_path)
    actions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_pod_response(status="EXITED"))
        action = json.loads(request.content)["action"]
        actions.append(action)
        return httpx.Response(200, json=_pod_response(status="RUNNING"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    executor = CloudManagementExecutor(settings, http_client=client)
    result = executor.call(
        "cloud_resource_restart",
        {"profile_ref": "runpod/test", "target": {"pod_id": "pod-123"}},
        request_id="request-restart-1",
    )

    assert actions == ["restart"]
    assert result["state"] == "SUCCEEDED"
    assert result["expected_state"] == "RUNNING"
    assert result["actual_state"] == "RUNNING"
    assert result["readiness"] == "UNKNOWN"
    assert result["release_confirmed"] is False
    executor.close()


def test_runpod_update_uses_independent_v2_configuration_readback(tmp_path: Any) -> None:
    """A Pod update succeeds only after a fresh v2 GET confirms every requested field."""

    settings = _settings(tmp_path)
    calls: list[str] = []
    current = _pod_response()
    current["disk"] = 1
    current["args"] = "original"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=dict(current))
        assert request.method == "PATCH"
        assert json.loads(request.content) == {
            "disk": 2,
            "args": "--token=private-argument-fixture",
        }
        current = {
            **current,
            "disk": 2,
            "args": "--token=private-argument-fixture",
        }
        return httpx.Response(200, json=_pod_response(status="RUNNING"))

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = executor.call(
        "cloud_resource_update",
        {
            "profile_ref": "runpod/test",
            "target": {"pod_id": "pod-123"},
            "parameters": {
                "disk": 2,
                "args": "--token=private-argument-fixture",
            },
        },
        request_id="request-runpod-update-readback",
    )
    replay = executor.call(
        "cloud_resource_update",
        {
            "profile_ref": "runpod/test",
            "target": {"pod_id": "pod-123"},
            "parameters": {
                "disk": 2,
                "args": "--token=private-argument-fixture",
            },
        },
        request_id="request-runpod-update-readback",
    )

    assert calls == ["GET", "PATCH", "GET"]
    assert replay == result
    assert result["state"] == "SUCCEEDED"
    assert result["configuration_match"] is True
    assert result["observed_configuration"] == {"disk": 2}
    assert "expected_state" not in result
    assert "UPDATED" not in json.dumps(result)
    assert "private-argument-fixture" not in json.dumps(result)
    operation = parse_operation_ref(result["operation_ref"], _EXCHANGE_KEY)
    details = operation["vendor_operation"]
    assert details["expected_configuration"] == {"disk": 2}
    assert details["expected_fields"] == ["args", "disk"]
    assert "private-argument-fixture" not in json.dumps(operation)
    polled = executor.call(
        "cloud_operation_status",
        {"profile_ref": "runpod/test", "operation_ref": result["operation_ref"]},
        request_id="request-runpod-update-with-args-poll",
    )
    assert polled["state"] == "SUCCEEDED"
    assert polled["configuration_match"] is True
    assert "private-argument-fixture" not in json.dumps(polled)
    assert calls == ["GET", "PATCH", "GET", "GET"]
    executor.close()


def test_runpod_update_mismatch_stays_pollable_until_configuration_matches(
    tmp_path: Any,
) -> None:
    """A mismatch remains pending and a later independent poll can confirm the update."""

    settings = _settings(tmp_path)
    current = _pod_response()
    current["disk"] = 1
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "PATCH":
            assert json.loads(request.content) == {"disk": 2}
            return httpx.Response(200, json={"status": "RUNNING"})
        return httpx.Response(200, json=dict(current))

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    request_id = "request-runpod-update-pending"
    arguments = {
        "profile_ref": "runpod/test",
        "target": {"pod_id": "pod-123"},
        "parameters": {"disk": 2},
    }
    result = executor.call("cloud_resource_update", arguments, request_id=request_id)
    operation_ref = result["operation_ref"]
    operation = parse_operation_ref(operation_ref, _EXCHANGE_KEY)
    assert calls == ["GET", "PATCH", "GET"]
    assert operation["request_id"] == request_id
    assert result["state"] == "PENDING"
    assert result["configuration_match"] is False
    assert result["observed_configuration"] == {"disk": 1}

    first_poll = executor.call(
        "cloud_operation_status",
        {"profile_ref": "runpod/test", "operation_ref": operation_ref},
        request_id="request-runpod-update-poll-1",
    )
    assert first_poll["state"] == "PENDING"
    assert first_poll["configuration_match"] is False

    current["disk"] = 2
    second_poll = executor.call(
        "cloud_operation_status",
        {"profile_ref": "runpod/test", "operation_ref": operation_ref},
        request_id="request-runpod-update-poll-2",
    )
    assert second_poll["state"] == "SUCCEEDED"
    assert second_poll["configuration_match"] is True
    assert second_poll["operation_ref"] == operation_ref
    assert second_poll["observed_configuration"] == {"disk": 2}

    with sqlite3.connect(settings.state_dir / "cloud-management.sqlite3") as connection:
        ledger_row = connection.execute(
            "SELECT terminal, operation_ref FROM cloud_write_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    assert ledger_row == (1, operation_ref)

    replay = executor.call("cloud_resource_update", arguments, request_id=request_id)
    assert replay == result
    assert replay["operation_ref"] == operation_ref
    assert calls == ["GET", "PATCH", "GET", "GET", "GET"]
    executor.close()


def test_runpod_update_missing_readback_marks_original_sqlite_receipt_terminal(
    tmp_path: Any,
) -> None:
    """A missing immediate readback can settle the pre-dispatch SQLite receipt by polling."""

    settings = _settings(tmp_path)
    calls: list[str] = []
    current = _pod_response()
    current["disk"] = 2
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        calls.append(request.method)
        if request.method == "PATCH":
            assert json.loads(request.content) == {"disk": 2}
            return httpx.Response(200, json={"status": "RUNNING"})
        get_count += 1
        if get_count == 2:
            raise httpx.ReadTimeout("fixture immediate readback timeout")
        return httpx.Response(200, json=dict(current))

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    request_id = "request-runpod-update-readback-timeout"
    arguments = {
        "profile_ref": "runpod/test",
        "target": {"pod_id": "pod-123"},
        "parameters": {"disk": 2},
    }
    result = executor.call("cloud_resource_update", arguments, request_id=request_id)
    operation_ref = result["operation_ref"]
    receipt = parse_operation_ref(operation_ref, _EXCHANGE_KEY)

    assert result["state"] == "REMOTE_STATE_UNVERIFIED"
    assert result["reason_code"] == "READBACK_UNAVAILABLE"
    assert receipt["request_id"] == request_id
    assert calls == ["GET", "PATCH", "GET"]

    status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "runpod/test", "operation_ref": operation_ref},
        request_id="request-runpod-update-readback-timeout-status",
    )
    assert status["state"] == "SUCCEEDED"
    assert status["operation_ref"] == operation_ref

    with sqlite3.connect(settings.state_dir / "cloud-management.sqlite3") as connection:
        ledger_row = connection.execute(
            "SELECT terminal, operation_ref FROM cloud_write_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    assert ledger_row == (1, operation_ref)

    replay = executor.call("cloud_resource_update", arguments, request_id=request_id)
    assert replay == result
    assert replay["operation_ref"] == operation_ref
    assert calls == ["GET", "PATCH", "GET", "GET"]
    executor.close()


@pytest.mark.parametrize(
    ("observed_disk", "expected_state", "expected_match"),
    [(1, "PENDING", False), (2, "SUCCEEDED", True)],
)
def test_runpod_update_timeout_returns_pollable_write_ahead_receipt(
    tmp_path: Any, observed_disk: int, expected_state: str, expected_match: bool
) -> None:
    """An ambiguous PATCH has a safe pre-dispatch receipt and polling never retries it."""

    settings = _settings(tmp_path)
    calls: list[str] = []
    current = _pod_response()
    current["disk"] = observed_disk
    current["args"] = "fixture-argument-value"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=dict(current))
        assert request.method == "PATCH"
        assert json.loads(request.content) == {
            "disk": 2,
            "args": "fixture-argument-value",
        }
        raise httpx.ReadTimeout("fixture ambiguous write timeout")

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    arguments = {
        "profile_ref": "runpod/test",
        "target": {"pod_id": "pod-123"},
        "parameters": {"disk": 2, "args": "fixture-argument-value"},
    }
    result = executor.call(
        "cloud_resource_update", arguments, request_id="request-runpod-ambiguous-update"
    )
    replay = executor.call(
        "cloud_resource_update", arguments, request_id="request-runpod-ambiguous-update"
    )

    assert result["state"] == "UNKNOWN_RESULT"
    assert result["reason_code"] == "PROVIDER_TIMEOUT"
    assert replay == result
    assert calls == ["GET", "PATCH"]
    assert "fixture-argument-value" not in json.dumps(result)
    receipt = parse_operation_ref(result["operation_ref"], _EXCHANGE_KEY)
    assert receipt["operation"] == "update"
    assert receipt["vendor_operation"]["expected_configuration"] == {"disk": 2}
    assert receipt["vendor_operation"]["expected_fields"] == ["args", "disk"]
    assert "fixture-argument-value" not in json.dumps(receipt)

    status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "runpod/test", "operation_ref": result["operation_ref"]},
        request_id="request-runpod-ambiguous-update-status",
    )
    assert status["state"] == expected_state
    assert status["configuration_match"] is expected_match
    assert "fixture-argument-value" not in json.dumps(status)
    assert calls == ["GET", "PATCH", "GET"]
    executor.close()


def test_runpod_update_rejects_incomplete_mount_replacement(tmp_path: Any) -> None:
    """Mount replacement requires its complete size and path and rejects unknown fields."""

    settings = _settings(tmp_path)
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: pytest.fail("network was called"))
        ),
    )
    for index, parameters in enumerate(
        ({"mounts": {"persistent": {"size": 2}}}, {"unknown": True})
    ):
        result = executor.call(
            "cloud_resource_update",
            {
                "profile_ref": "runpod/test",
                "target": {"pod_id": "pod-123"},
                "parameters": parameters,
            },
            request_id=f"request-runpod-invalid-update-{index}",
        )
        assert result["error"]["code"] == "INVALID_ARGUMENTS"
    executor.close()


def test_runpod_logs_decode_sse_payload_and_redact_credentials(tmp_path: Any) -> None:
    """Runpod log data is decoded into the documented fields and secrets are redacted."""

    settings = _settings(tmp_path)
    payload = (
        "data: "
        + json.dumps(
            {
                "ts": "2026-10-23T00:00:00Z",
                "source": "container",
                "line": f"worker started {_API_KEY}",
                "env": {"ignored": "private"},
            }
        )
        + "\n\n"
    ).encode()
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=payload,
                )
            )
        ),
    )
    operation_ref = executor._new_operation_ref(  # noqa: SLF001
        settings.profile("runpod/test"),
        "cloud_resource_start",
        {"pod_id": "pod-123"},
        "request-runpod-logs-decode",
    )
    result = executor.call(
        "cloud_operation_logs",
        {"profile_ref": "runpod/test", "operation_ref": operation_ref},
        request_id="request-runpod-logs-decode-call",
    )

    assert result["available"] is True
    assert result["timed_out"] is False
    assert result["truncated"] is False
    assert result["logs"] == [
        {
            "ts": "2026-10-23T00:00:00Z",
            "source": "container",
            "line": "worker started [REDACTED]",
        }
    ]
    executor.close()


def test_runpod_logs_keep_received_records_on_quiet_timeout(tmp_path: Any) -> None:
    """A read timeout ends the stream without discarding complete log events."""

    settings = _settings(tmp_path)
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_PartialThenTimeoutStream(),
                )
            )
        ),
    )
    operation_ref = executor._new_operation_ref(  # noqa: SLF001
        settings.profile("runpod/test"),
        "cloud_resource_start",
        {"pod_id": "pod-123"},
        "request-runpod-logs-timeout",
    )
    result = executor.call(
        "cloud_operation_logs",
        {"profile_ref": "runpod/test", "operation_ref": operation_ref},
        request_id="request-runpod-logs-timeout-call",
    )

    assert result["available"] is True
    assert result["timed_out"] is True
    assert result["truncated"] is False
    assert result["logs"] == [{"ts": "2026-10-23T00:00:00Z", "source": "container", "line": "tail"}]
    executor.close()


def test_runpod_logs_report_actual_byte_truncation(tmp_path: Any) -> None:
    """The truncation flag reflects the enforced raw-byte limit, not the number of lines."""

    settings = _settings(tmp_path)
    event = ("data: " + json.dumps({"ts": "t", "source": "c", "line": "x" * 900}) + "\n\n").encode()
    payload = event * 20
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=payload,
                )
            )
        ),
    )
    operation_ref = executor._new_operation_ref(  # noqa: SLF001
        settings.profile("runpod/test"),
        "cloud_resource_start",
        {"pod_id": "pod-123"},
        "request-runpod-logs-truncated",
    )
    result = executor.call(
        "cloud_operation_logs",
        {"profile_ref": "runpod/test", "operation_ref": operation_ref},
        request_id="request-runpod-logs-truncated-call",
    )

    assert result["available"] is True
    assert result["truncated"] is True
    assert result["timed_out"] is False
    assert result["logs"]
    assert len(result["logs"]) < 20
    executor.close()


def test_runpod_logs_bound_amplification_from_malformed_json_arrays(tmp_path: Any) -> None:
    """Unsupported array entries never duplicate the complete SSE payload into output."""

    settings = _settings(tmp_path)
    payload = ("data: " + json.dumps([0] * 2000) + "\n\n").encode()
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=payload,
                )
            )
        ),
    )
    operation_ref = executor._new_operation_ref(  # noqa: SLF001
        settings.profile("runpod/test"),
        "cloud_resource_start",
        {"pod_id": "pod-123"},
        "request-runpod-logs-array-amplification",
    )
    result = executor.call(
        "cloud_operation_logs",
        {"profile_ref": "runpod/test", "operation_ref": operation_ref},
        request_id="request-runpod-logs-array-amplification-call",
    )

    assert result["logs"] == []
    assert len(canonical_json(result)) < 256
    assert result["truncated"] is False
    executor.close()


def test_runpod_logs_bound_record_count_for_many_tiny_events(tmp_path: Any) -> None:
    """The tail returns at most 100 records and reports when it stops at that limit."""

    settings = _settings(tmp_path)
    payload = b"".join(b'data: {"line":"x"}\n\n' for _ in range(150))
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=payload,
                )
            )
        ),
    )
    operation_ref = executor._new_operation_ref(  # noqa: SLF001
        settings.profile("runpod/test"),
        "cloud_resource_start",
        {"pod_id": "pod-123"},
        "request-runpod-logs-record-limit",
    )
    result = executor.call(
        "cloud_operation_logs",
        {"profile_ref": "runpod/test", "operation_ref": operation_ref},
        request_id="request-runpod-logs-record-limit-call",
    )

    assert len(result["logs"]) == 100
    assert result["truncated"] is True
    assert len(canonical_json(result["logs"])) <= provider_impl._RUNPOD_LOG_MAX_OUTPUT_BYTES
    executor.close()


def test_runpod_logs_bound_serialized_output_size(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The encoded log list stops before its configured serialized output budget."""

    settings = _settings(tmp_path)
    monkeypatch.setattr(provider_impl, "_RUNPOD_LOG_MAX_OUTPUT_BYTES", 512)
    payload = b"".join(
        ("data: " + json.dumps({"line": "x" * 32}) + "\n\n").encode() for _ in range(30)
    )
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=payload,
                )
            )
        ),
    )
    operation_ref = executor._new_operation_ref(  # noqa: SLF001
        settings.profile("runpod/test"),
        "cloud_resource_start",
        {"pod_id": "pod-123"},
        "request-runpod-logs-output-limit",
    )
    result = executor.call(
        "cloud_operation_logs",
        {"profile_ref": "runpod/test", "operation_ref": operation_ref},
        request_id="request-runpod-logs-output-limit-call",
    )

    assert result["logs"]
    assert result["truncated"] is True
    assert len(canonical_json(result["logs"])) <= 512
    executor.close()


def test_runpod_delete_requires_absence_readback(tmp_path: Any) -> None:
    """A delete is complete only after the Pod ID reads back as absent."""

    settings = _settings(tmp_path)
    calls: list[tuple[str, str]] = []
    deleted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deleted
        calls.append((request.method, request.url.path))
        if request.method == "DELETE":
            deleted = True
            return httpx.Response(204)
        if request.method == "GET" and deleted:
            return httpx.Response(404, json={"title": "Not Found"})
        return httpx.Response(200, json=_pod_response())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    executor = CloudManagementExecutor(settings, http_client=client)
    result = executor.call(
        "cloud_resource_delete",
        {"profile_ref": "runpod/test", "target": {"pod_id": "pod-123"}},
        request_id="request-delete-1",
    )

    assert [method for method, _ in calls] == ["GET", "DELETE", "GET"]
    assert result["state"] == "SUCCEEDED"
    assert result["actual_state"] == "ABSENT"
    assert result["release_confirmed"] is True
    executor.close()


def test_profile_resource_scope_is_checked_before_network(tmp_path: Any) -> None:
    """A valid profile does not allow an unrelated Pod identifier."""

    settings = _settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("network was called"))
    )
    executor = CloudManagementExecutor(settings, http_client=client)
    with pytest.raises(ProviderFailure, match="SCOPE_DENIED"):
        executor.call(
            "cloud_resource_get",
            {"profile_ref": "runpod/test", "target": {"pod_id": "unrelated-pod"}},
            request_id="request-outside-scope",
        )
    executor.close()


def test_vendor_cli_uses_profile_scope_instead_of_claiming_one_vm(tmp_path: Any) -> None:
    """A CLI call grants the host profile scope and signs the complete argv."""

    settings = _multi_provider_settings(tmp_path)
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: pytest.fail("network was called"))
        ),
    )
    azure = settings.profile("azure/test")
    cli_schema = TOOL_BY_ID["cloud_vendor_cli"]["input_schema"]
    assert "target" not in cli_schema["properties"]
    assert cli_schema["required"] == ["profile_ref", "argv"]
    executor._validate_cli_scope(azure, ["vm", "delete", "-g", "rg-test", "-n", "vm-b"])
    with pytest.raises(ProviderFailure, match="SCOPE_DENIED"):
        executor._validate_cli_scope(azure, ["vm", "delete", "-g", "other-rg", "-n", "vm-b"])
    with pytest.raises(ProviderFailure, match="SCOPE_DENIED"):
        executor._validate_cli_scope(
            azure,
            ["rest", "--method", "GET", "--url", "https://attacker.example/steal"],
        )
    with pytest.raises(ProviderFailure, match="SCOPE_DENIED"):
        executor._validate_cli_scope(
            azure,
            [
                "rest",
                "--method",
                "GET",
                "--resource-group",
                "rg-test",
                "--url",
                "https://attacker.example/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-test/resources?redirect_uri=https://collector.example",
            ],
        )
    with pytest.raises(ProviderFailure, match="SCOPE_DENIED"):
        executor._validate_cli_scope(
            azure,
            [
                "rest",
                "--method",
                "GET",
                "--url",
                "https://management.azure.com/subscriptions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/resourceGroups/rg-test/resources",
            ],
        )
    with pytest.raises(ProviderFailure, match="SCOPE_DENIED"):
        executor._validate_cli_scope(
            azure,
            ["vm", "delete", "--resource-group=other-rg", "--name", "vm-a"],
        )
    with pytest.raises(ProviderFailure, match="SCOPE_DENIED"):
        executor._validate_cli_scope(
            azure,
            [
                "vm",
                "delete",
                "-s",
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "-g",
                "rg-test",
                "-n",
                "vm-a",
            ],
        )
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        executor._validate_cli_scope(
            azure,
            ["storage", "account", "keys", "list", "--resource-group", "rg-test"],
        )
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        executor._validate_cli_scope(
            azure,
            [
                "rest",
                "--method",
                "POST",
                "--url",
                "https://management.azure.com/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-test/providers/Microsoft.Storage/storageAccounts/acct/listKeys?api-version=2023-01-01",
            ],
        )
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        executor._validate_cli_scope(
            azure,
            ["keyvault", "secret", "show", "--vault-name", "vault", "--name", "db-password"],
        )
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        executor._validate_cli_scope(azure, ["configure", "--defaults", "group=other-rg"])
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        executor._validate_cli_scope(
            azure,
            [
                "role",
                "assignment",
                "create",
                "--scope",
                "/subscriptions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ],
        )
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        executor._validate_cli_scope(
            azure,
            [
                "rest",
                "--method",
                "PUT",
                "--url",
                "https://management.azure.com/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-test/providers/Microsoft.Authorization/roleAssignments/role-1",
            ],
        )

    gcp = settings.profile("gcp/test")
    executor._validate_cli_scope(
        gcp, ["compute", "instances", "delete", "vm-b", "--zone", "us-central1-a"]
    )
    with pytest.raises(ProviderFailure, match="SCOPE_DENIED"):
        executor._validate_cli_scope(  # noqa: SLF001
            gcp,
            ["compute", "instances", "delete", "other-vm", "--project", "other-project"],
        )
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        executor._validate_cli_scope(gcp, ["secrets", "versions", "access", "latest"])
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        executor._validate_cli_scope(gcp, ["auth", "print-access-token"])
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        executor._validate_cli_scope(
            gcp,
            ["projects", "add-iam-policy-binding", "cyrene-test-project", "--member=user:x"],
        )
    with pytest.raises(ProviderFailure, match="SCOPE_DENIED"):
        executor._validate_cli_scope(
            gcp, ["compute", "instances", "list", "--project=other-project"]
        )
    with pytest.raises(ProviderFailure, match="SCOPE_DENIED"):
        executor._validate_cli_scope(gcp, ["compute", "instances", "list", "--zone=us-central1-b"])
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        executor._validate_cli_scope(
            gcp, ["compute", "instances", "list", "--url", "https://attacker.example"]
        )

    runpod_settings = _settings(tmp_path / "runpod")
    runpod_executor = CloudManagementExecutor(
        runpod_settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: pytest.fail("network was called"))
        ),
    )
    runpod = runpod_settings.profile("runpod/test")
    runpod_executor._validate_cli_scope(runpod, ["pod", "delete", "other-account-pod"])
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        runpod_executor._validate_cli_scope(runpod, ["serverless", "list"])
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        runpod_executor._validate_cli_scope(
            runpod, ["pod", "update", "pod-123", "--env", "SECRET=value"]
        )
    with pytest.raises(ProviderFailure, match="CLI_REJECTED"):
        runpod_executor._validate_cli_scope(
            runpod, ["pod", "list", "--api-url", "https://collector.example"]
        )
    runpod_executor.close()
    executor.close()


class _SecretOutputProcessRunner:
    """Return fixture output containing a credential to prove it is suppressed."""

    def __init__(self) -> None:
        self.argv: list[str] = []

    def run(self, executable: str, argv: list[str], **_: Any) -> ProcessResult:
        self.argv = list(argv)
        return ProcessResult(
            exit_code=0,
            stdout=f"pod list {_API_KEY}",
            stderr=f"diagnostic {_API_KEY}",
            duration_ms=7,
            timed_out=False,
            cancelled=False,
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_bytes=len(f"pod list {_API_KEY}"),
            stderr_bytes=len(f"diagnostic {_API_KEY}"),
        )


def test_cloud_cli_suppresses_all_raw_stdout_and_stderr(tmp_path: Any) -> None:
    """Secret-bearing native CLI text is never included in a tool result."""

    settings = _settings(tmp_path, cli_enabled=True)
    process = _SecretOutputProcessRunner()
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: pytest.fail("network was called"))
        ),
        process_runner=process,  # type: ignore[arg-type]
    )
    result = executor.call(
        "cloud_vendor_cli",
        {"profile_ref": "runpod/test", "argv": ["pod", "list"]},
        request_id="request-cli-output-1",
    )

    assert process.argv == ["pod", "list"]
    assert result["state"] == "COMMAND_COMPLETED"
    assert result["command_state"] == "COMMAND_COMPLETED"
    assert result["remote_state"] == "NOT_APPLICABLE"
    assert result["stdout_suppressed"] is True
    assert result["stderr_suppressed"] is True
    assert "stdout" not in result and "stderr" not in result
    assert _API_KEY not in json.dumps(result)
    executor.close()


class _StaticProcessRunner:
    """Return bounded fixture output and record every actual argv dispatch."""

    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def run(self, executable: str, argv: list[str], **_: Any) -> ProcessResult:
        self.calls.append(list(argv))
        return ProcessResult(
            exit_code=0,
            stdout=self.stdout,
            stderr="",
            duration_ms=5,
            timed_out=False,
            cancelled=False,
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_bytes=len(self.stdout.encode("utf-8")),
            stderr_bytes=0,
        )


def test_cli_no_wait_exit_zero_keeps_remote_state_unverified_and_receipt(tmp_path: Any) -> None:
    """Exit zero proves process completion only; no-wait writes remain reconcilable."""

    settings = _multi_provider_settings(tmp_path)
    process = _StaticProcessRunner()
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: pytest.fail("network was called"))
        ),
        process_runner=process,  # type: ignore[arg-type]
    )
    arguments = {
        "profile_ref": "azure/test",
        "argv": ["vm", "create", "--resource-group", "rg-test", "--name", "vm-a", "--no-wait"],
    }

    result = executor.call("cloud_vendor_cli", arguments, request_id="request-no-wait")
    replay = executor.call("cloud_vendor_cli", arguments, request_id="request-no-wait")

    assert result["state"] == "REMOTE_STATE_UNVERIFIED"
    assert result["command_state"] == "COMMAND_COMPLETED"
    assert result["remote_state"] == "UNKNOWN"
    assert isinstance(result.get("operation_ref"), str)
    assert replay == result
    assert process.calls.count(arguments["argv"]) == 1
    assert process.calls[0] == ["login", "--identity"]
    assert process.calls[1] == [
        "account",
        "set",
        "--subscription",
        "11111111-2222-3333-4444-555555555555",
    ]
    assert "--no-wait" in process.calls[2]

    status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "azure/test", "operation_ref": result["operation_ref"]},
        request_id="request-no-wait-status",
    )
    assert status["state"] == "REMOTE_STATE_UNVERIFIED"
    assert status["remote_state"] == "UNKNOWN"
    assert status["reason_code"] == "NATIVE_OPERATION_REFERENCE_UNAVAILABLE"
    assert len(process.calls) == 3
    executor.close()


def test_runpod_cli_create_exit_zero_is_pending_until_v2_readback(tmp_path: Any) -> None:
    """A CLI Pod create is not verified until the native API confirms its state."""

    settings = _settings(tmp_path, cli_enabled=True)
    process = _StaticProcessRunner(
        json.dumps({"id": "created-pod-123", "name": "cyrene-test-unit", "status": "CREATING"})
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        assert request.headers["Authorization"] == f"Bearer {_API_KEY}"
        assert request.url.path == "/v2/pods/created-pod-123"
        return httpx.Response(200, json=_pod_response("created-pod-123", "cyrene-test-unit"))

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        process_runner=process,  # type: ignore[arg-type]
    )
    arguments = {
        "profile_ref": "runpod/test",
        "argv": [
            "pod",
            "create",
            "--name",
            "cyrene-test-unit",
            "--gpu-id",
            "NVIDIA GeForce RTX 4090",
            "--gpu-count",
            "1",
            "--container-disk-in-gb",
            "20",
            "--data-center-ids",
            "US-KS-2",
        ],
    }

    result = executor.call("cloud_vendor_cli", arguments, request_id="request-runpod-cli-create")
    assert result["state"] == "PENDING"
    assert result["command_state"] == "COMMAND_COMPLETED"
    assert result["remote_state"] == "PENDING"
    assert isinstance(result.get("operation_ref"), str)
    assert "stdout" not in result and "stderr" not in result
    assert _API_KEY not in json.dumps(result)
    assert requested == []

    status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "runpod/test", "operation_ref": result["operation_ref"]},
        request_id="request-runpod-cli-status",
    )
    assert status["state"] == "SUCCEEDED"
    assert status["command_state"] == "COMMAND_COMPLETED"
    assert status["remote_state"] == "RUNNING"
    assert status["actual_state"] == "RUNNING"
    assert status["resource_ref"] == "created-pod-123"
    assert requested == ["/v2/pods/created-pod-123"]
    assert len(process.calls) == 1
    executor.close()


def test_gcloud_async_operation_is_polled_from_safe_json_reference(tmp_path: Any) -> None:
    """Gcloud async JSON yields only a validated Compute operation reference."""

    settings = _multi_provider_settings(tmp_path)
    process = _StaticProcessRunner(
        json.dumps(
            {
                "kind": "compute#operation",
                "name": "operation-123",
                "zone": (
                    "https://www.googleapis.com/compute/v1/projects/"
                    "cyrene-test-project/zones/us-central1-a/operations/operation-123"
                ),
                "status": "RUNNING",
                "privateOutput": "must never be returned",
            }
        )
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer fixture-token"
        return httpx.Response(
            200,
            json={"name": "operation-123", "status": "DONE", "zone": "us-central1-a"},
        )

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        process_runner=process,  # type: ignore[arg-type]
    )
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    result = executor.call(
        "cloud_vendor_cli",
        {
            "profile_ref": "gcp/test",
            "argv": [
                "compute",
                "instances",
                "create",
                "vm-b",
                "--zone",
                "us-central1-a",
                "--async",
                "--format=json",
            ],
        },
        request_id="request-gcloud-async",
    )

    assert result["state"] == "PENDING"
    assert result["command_state"] == "COMMAND_COMPLETED"
    assert result["remote_state"] == "PENDING"
    assert "privateOutput" not in json.dumps(result)
    assert "stdout" not in result and "stderr" not in result
    assert "--async" in process.calls[0]
    assert requested == []

    status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "gcp/test", "operation_ref": result["operation_ref"]},
        request_id="request-gcloud-async-status",
    )
    assert status["state"] == "SUCCEEDED"
    assert status["remote_state"] == "SUCCEEDED"
    assert status["resource_state"] == "UNKNOWN"
    assert requested == [
        "/compute/v1/projects/cyrene-test-project/zones/us-central1-a/operations/operation-123"
    ]
    executor.close()


@pytest.mark.parametrize("resource_group", ["rg-test", "rg-foreign"])
def test_azure_nested_deployment_scope_is_checked_before_auth_or_put(
    tmp_path: Any, resource_group: str
) -> None:
    """Preserve local deployments and reject a hidden foreign target before dispatch."""

    settings = _multi_provider_settings(tmp_path)
    paths: list[str] = []
    token_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.method == "PUT"
        return httpx.Response(202, json={"properties": {"provisioningState": "Accepted"}})

    def token(_profile: Any) -> tuple[str, None]:
        token_calls.append("auth")
        return "fixture-token", None

    executor = CloudManagementExecutor(
        settings, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    executor._azure_token = token  # noqa: SLF001
    result = executor.call(
        "cloud_resource_deploy",
        {
            "profile_ref": "azure/test",
            "target": {"resource_group": "rg-test", "deployment_name": "scope-test"},
            "parameters": {
                "template": {
                    "resources": [
                        {
                            "type": "Microsoft.Resources/deployments",
                            "name": "child",
                            "resourceGroup": resource_group,
                            "properties": {"template": {"resources": []}},
                        }
                    ]
                }
            },
        },
        request_id="request-azure-scope",
    )
    if resource_group == "rg-test":
        assert result["state"] == "PENDING"
        assert token_calls == ["auth"]
        assert len(paths) == 1
        assert "/resourceGroups/rg-test/" in paths[0]
    else:
        assert result == {"error": {"code": "SCOPE_DENIED", "retryable": False}}
        assert paths == []
        assert token_calls == []
    executor.close()


@pytest.mark.parametrize("tool_id", ["cloud_resource_create", "cloud_resource_update"])
def test_azure_deployment_resource_cannot_bypass_scoped_deploy_route(
    tmp_path: Any, tool_id: str
) -> None:
    """Generic create/update rejects ARM deployments before Azure auth or HTTP calls."""

    settings = _multi_provider_settings(
        tmp_path,
        azure_resource_api_versions={"microsoft.resources/deployments": "2024-04-01"},
    )
    calls: list[str] = []
    token_calls: list[str] = []
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (calls.append(request.method), pytest.fail("ARM was called"))
            )
        ),
    )

    def token_provider(_profile: Any) -> tuple[str, None]:
        token_calls.append("token")
        return "fixture-token", None

    executor._azure_token = token_provider  # noqa: SLF001
    result = executor.call(
        tool_id,
        {
            "profile_ref": "azure/test",
            "target": {
                "resource_id": (
                    "/subscriptions/11111111-2222-3333-4444-555555555555/"
                    "resourceGroups/rg-test/providers/Microsoft.Resources/deployments/"
                    "cyrene-test-deployment"
                )
            },
            "parameters": {
                "properties": {
                    "template": {
                        "resources": [
                            {
                                "type": "Microsoft.Compute/virtualMachines",
                                "name": "cyrene-test-vm",
                                "resourceGroup": "foreign-rg",
                            }
                        ]
                    }
                }
            },
        },
        request_id=f"request-azure-generic-deployment-{tool_id}",
    )

    assert result == {"error": {"code": "UNSUPPORTED_OPERATION", "retryable": False}}
    assert calls == []
    assert token_calls == []
    executor.close()


@pytest.mark.parametrize("tool_id", ["cloud_resource_create", "cloud_resource_update"])
def test_azure_generic_mutations_still_allow_ordinary_resources(
    tmp_path: Any, tool_id: str
) -> None:
    """The deployment-route guard leaves configured ordinary ARM resource types usable."""

    settings = _multi_provider_settings(tmp_path)
    calls: list[tuple[str, str]] = []
    token_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={"properties": {"provisioningState": "Succeeded"}},
        )

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    def token_provider(_profile: Any) -> tuple[str, None]:
        token_calls.append("token")
        return "fixture-token", None

    executor._azure_token = token_provider  # noqa: SLF001
    result = executor.call(
        tool_id,
        {
            "profile_ref": "azure/test",
            "target": {
                "resource_id": (
                    "/subscriptions/11111111-2222-3333-4444-555555555555/"
                    "resourceGroups/rg-test/providers/Microsoft.Compute/virtualMachines/"
                    "cyrene-test-vm"
                )
            },
            "parameters": {"tags": {"purpose": "fixture"}},
        },
        request_id=f"request-azure-generic-resource-{tool_id}",
    )

    assert result["state"] == "SUCCEEDED"
    assert calls and len(calls) == 1 and calls[0][0] == "PUT"
    assert "/providers/Microsoft.Compute/virtualMachines/" in calls[0][1]
    assert token_calls == ["token"]
    executor.close()


def test_azure_vm_create_returns_pollable_vendor_operation(tmp_path: Any) -> None:
    """Azure async operation URLs are accepted only on the fixed ARM host/scope."""

    settings = _multi_provider_settings(tmp_path)
    paths: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.method, request.url.path))
        if request.method == "PUT":
            return httpx.Response(
                202,
                headers={
                    "Azure-AsyncOperation": (
                        "https://management.azure.com/subscriptions/"
                        "11111111-2222-3333-4444-555555555555/providers/"
                        "Microsoft.Compute/locations/eastus/operations/op-123"
                        "?api-version=2024-07-01"
                    )
                },
                json={"status": "Accepted"},
            )
        if request.url.path.endswith("/operations/op-123"):
            return httpx.Response(200, json={"status": "Succeeded"})
        return httpx.Response(
            200,
            json={
                "id": request.url.path,
                "name": "cyrene-test-vm",
                "properties": {"provisioningState": "Succeeded"},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    executor = CloudManagementExecutor(settings, http_client=client)
    executor._azure_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    created = executor.call(
        "cloud_resource_create",
        {
            "profile_ref": "azure/test",
            "target": {
                "resource_id": (
                    "/subscriptions/11111111-2222-3333-4444-555555555555/"
                    "resourceGroups/rg-test/providers/Microsoft.Compute/virtualMachines/"
                    "cyrene-test-vm"
                )
            },
            "parameters": {"location": "eastus", "properties": {}},
        },
        request_id="request-azure-create",
    )
    status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "azure/test", "operation_ref": created["operation_ref"]},
        request_id="request-azure-status",
    )

    assert created["state"] == "PENDING"
    assert status["state"] == "SUCCEEDED"
    assert paths[0][0] == "PUT"
    assert paths[1][0] == "GET"
    assert paths[2][0] == "GET"
    assert all(
        path.startswith("/subscriptions/11111111-2222-3333-4444-555555555555/") for _, path in paths
    )
    executor.close()


def test_gcp_list_filters_results_to_profile_name_scope(tmp_path: Any) -> None:
    """GCE list results cannot expose instances outside configured name prefixes."""

    settings = _multi_provider_settings(tmp_path)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "items": [
                    {"name": "cyrene-test-worker", "zone": "us-central1-a", "status": "RUNNING"},
                    {"name": "unrelated-worker", "zone": "us-central1-a", "status": "RUNNING"},
                ],
                "nextPageToken": "next-page",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    executor = CloudManagementExecutor(settings, http_client=client)
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    listed = executor.call(
        "cloud_resources_list",
        {"profile_ref": "gcp/test", "target": {"zone": "us-central1-a"}},
        request_id="request-gcp-list",
    )

    assert len(requested) == 1
    assert listed["provider"] == "gcp"
    assert [resource["name"] for resource in listed["resources"]] == ["cyrene-test-worker"]
    assert listed["next_page_token"] == "next-page"
    executor.close()


def test_gcp_instance_operation_status_uses_zone_and_operation_name(tmp_path: Any) -> None:
    """GCE operation polling reconstructs only fixed project/zone REST paths."""

    settings = _multi_provider_settings(tmp_path)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"name": "operation-123", "status": "RUNNING", "zone": "us-central1-a"},
            )
        if "/operations/" in request.url.path:
            return httpx.Response(200, json={"name": "operation-123", "status": "DONE"})
        return httpx.Response(
            200,
            json={"name": "cyrene-test-vm", "status": "RUNNING", "zone": "us-central1-a"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    executor = CloudManagementExecutor(settings, http_client=client)
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    created = executor.call(
        "cloud_resource_create",
        {
            "profile_ref": "gcp/test",
            "target": {"zone": "us-central1-a", "instance_name": "cyrene-test-vm"},
            "parameters": {
                "machineType": "zones/us-central1-a/machineTypes/e2-medium",
                "disks": [],
                "networkInterfaces": [],
            },
        },
        request_id="request-gcp-create",
    )
    status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "gcp/test", "operation_ref": created["operation_ref"]},
        request_id="request-gcp-operation-status",
    )

    assert created["state"] == "PENDING"
    assert status["state"] == "SUCCEEDED"
    assert paths[0].endswith("/zones/us-central1-a/instances")
    assert paths[1].endswith("/zones/us-central1-a/operations/operation-123")
    assert paths[2].endswith("/zones/us-central1-a/instances/cyrene-test-vm")
    assert all(path.startswith("/compute/v1/projects/cyrene-test-project/") for path in paths)
    executor.close()


class _RecordingExecutor:
    """Test seam that records already authenticated provider arguments."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def call(self, tool_id: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.calls.append({"tool_id": tool_id, "arguments": arguments})
        return {"state": "SUCCEEDED", "resource_ref": "pod-123"}

    def close(self) -> None:
        return None


def test_plugin_verifies_actor_operation_and_complete_argument_hash(tmp_path: Any) -> None:
    """The provider accepts one exact Exchange signature and rejects payload edits."""

    settings = _settings(tmp_path)
    executor = _RecordingExecutor()
    plugin = CloudManagementProvider(settings=settings, executor=executor)
    arguments = {"profile_ref": "runpod/test", "target": {"pod_id": "pod-123"}}
    now = datetime.now(UTC)
    context = sign_context(
        method="cloud_resource_get",
        operation_id="cloud.resource.get",
        payload=arguments,
        caller_workload="exchange",
        audience="cloud-management",
        issuer="https://directory.example",
        subject="user-1",
        organization_id="org-fixture",
        workspace_id="workspace-fixture",
        binding_id=settings.binding_id,
        request_id="request-plugin-1",
        key=_EXCHANGE_KEY,
        issued_at=now,
        expires_at=now + timedelta(seconds=60),
    )

    valid = _invoke(plugin, settings.binding_id, arguments, context.as_dict())
    assert valid.outcome.is_error is False
    assert len(executor.calls) == 1

    modified = {**arguments, "target": {"pod_id": "different-pod"}}
    invalid = _invoke(plugin, settings.binding_id, modified, context.as_dict())
    assert invalid.outcome.is_error is True
    assert (
        json.loads(invalid.outcome.content[0].json.json)["error"]["code"] == "AUTHENTICATION_DENIED"
    )
    assert len(executor.calls) == 1
    plugin.close()


def test_cli_signature_binds_complete_argv_without_model_resource_target(tmp_path: Any) -> None:
    """Exchange signs the exact native argv while the provider grant stays profile-scoped."""

    settings = _settings(tmp_path)
    executor = _RecordingExecutor()
    plugin = CloudManagementProvider(settings=settings, executor=executor)
    tool_id = "cloud_vendor_cli"
    arguments = {
        "profile_ref": "runpod/test",
        "argv": ["pod", "delete", "pod-a"],
    }
    now = datetime.now(UTC)
    context = sign_context(
        method=tool_id,
        operation_id="cloud.vendor.cli.execute",
        payload=arguments,
        caller_workload="exchange",
        audience="cloud-management",
        issuer="https://directory.example",
        subject="user-1",
        organization_id="org-fixture",
        workspace_id="workspace-fixture",
        binding_id=settings.binding_id,
        request_id="request-cli-signature-1",
        key=_EXCHANGE_KEY,
        issued_at=now,
        expires_at=now + timedelta(seconds=240),
    )

    valid = _invoke(plugin, settings.binding_id, arguments, context.as_dict(), tool_id=tool_id)
    assert valid.outcome.is_error is False
    assert executor.calls[0]["arguments"]["argv"] == arguments["argv"]
    assert "target" not in executor.calls[0]["arguments"]

    tampered = {**arguments, "argv": ["pod", "delete", "pod-b"]}
    invalid = _invoke(plugin, settings.binding_id, tampered, context.as_dict(), tool_id=tool_id)
    assert invalid.outcome.is_error is True
    assert (
        json.loads(invalid.outcome.content[0].json.json)["error"]["code"] == "AUTHENTICATION_DENIED"
    )
    assert len(executor.calls) == 1
    plugin.close()


def _invoke(
    plugin: CloudManagementProvider,
    binding_id: str,
    arguments: dict[str, Any],
    context: dict[str, Any],
    *,
    tool_id: str = "cloud_resource_get",
) -> wire.CallToolResponse:
    """Encode one signed tool envelope and decode the typed provider response."""

    request = wire.CallToolRequest(
        binding_id=binding_id,
        provider_tool_id=tool_id,
        arguments_json=json.dumps({"caller_context": context, "arguments": arguments}),
    )
    accepted, response = plugin.on_invoke(
        CAPABILITY_ID,
        CALL_TOOL_METHOD,
        request.SerializeToString(),
        request_type_url="type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest",
    )
    assert accepted is True
    assert response.type_url == CALL_TOOL_RESPONSE_TYPE_URL
    decoded = wire.CallToolResponse()
    decoded.ParseFromString(response.value)
    return decoded


def test_operation_ref_is_bound_to_exchange_key(tmp_path: Any) -> None:
    """A caller cannot modify the resource or provider in a pollable reference."""

    settings = _settings(tmp_path)
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ),
    )
    reference = executor._new_operation_ref(  # noqa: SLF001
        settings.profile("runpod/test"),
        "cloud_resource_start",
        {"pod_id": "pod-123"},
        "request-opref-1",
    )
    assert parse_operation_ref(reference, _EXCHANGE_KEY)["target"] == {"pod_id": "pod-123"}
    parts = reference.split(".")
    tampered = f"{parts[0]}.{parts[1]}.{('0' if parts[2][0] != '0' else '1') + parts[2][1:]}"
    with pytest.raises(InvalidOperationRef):
        parse_operation_ref(tampered, _EXCHANGE_KEY)
    executor.close()
