"""Semantic fixture tests for signed cloud management and Runpod API behavior.

中文：使用本地夹具验证签名、profile 范围、幂等和 Runpod 状态回读。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
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
    LIST_TOOLS_METHOD,
    READ_ONLY_TOOL_IDS,
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


def _configuration(
    tmp_path: Any, *, cli_enabled: bool = False, read_only: bool = False
) -> dict[str, Any]:
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
    if read_only:
        profile["read_only"] = True
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


def _settings(
    tmp_path: Any, *, cli_enabled: bool = False, read_only: bool = False
) -> ProviderSettings:
    """Create validated test settings from temporary host secret values."""

    environment = {
        "CYRENE_CAPABILITY_BINDING_ID": "binding-cloud-test",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(
            _configuration(tmp_path, cli_enabled=cli_enabled, read_only=read_only)
        ),
        "EXCHANGE_KEY": _EXCHANGE_KEY.hex(),
        "STATE_DIR": str(tmp_path),
        "RUNPOD_API_KEY": _API_KEY,
    }
    return ProviderSettings.from_environment(environment)


def _multi_provider_settings(
    tmp_path: Any,
    *,
    azure_resource_api_versions: dict[str, str] | None = None,
    azure_resource_groups: list[str] | None = None,
    azure_subscription_id: str | None = None,
    gcp_disk_scope: bool = False,
) -> ProviderSettings:
    """Create Azure, GCP WIF, and Runpod profiles without contacting providers."""

    config = _configuration(tmp_path)
    azure_api_versions = {"microsoft.compute/virtualmachines": "2024-07-01"}
    if azure_resource_api_versions:
        azure_api_versions.update(azure_resource_api_versions)
    gcp_scope: dict[str, Any] = {
        "project_id": "cyrene-test-project",
        "zones": ["us-central1-a"],
        "instance_name_prefixes": ["cyrene-test-"],
    }
    if gcp_disk_scope:
        gcp_scope.update(
            {
                "disk_name_prefixes": ["cyrene-test-"],
                "disk_size_gb_range": {"min": 10, "max": 16},
                "disk_source_images": ["projects/debian-cloud/global/images/family/debian-12"],
            }
        )
    config["profiles"] = [
        {
            "profile_ref": "azure/test",
            "provider": "azure",
            "identity_ref": "managed-identities/azure/test",
            "scope": {
                "subscription_id": (
                    "11111111-2222-3333-4444-555555555555"
                    if azure_subscription_id is None
                    else azure_subscription_id
                ),
                "resource_groups": (
                    ["rg-test"] if azure_resource_groups is None else azure_resource_groups
                ),
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
            "scope": gcp_scope,
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



def _gcp_disk_response(
    name: str = "cyrene-test-boot",
    status: str = "READY",
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a minimal zonal Disk fixture containing an intentionally sensitive field."""

    return {
        "id": "123456789",
        "name": name,
        "zone": "https://www.googleapis.com/compute/v1/projects/cyrene-test-project/zones/us-central1-a",
        "status": status,
        "sizeGb": "10",
        "type": "https://www.googleapis.com/compute/v1/projects/cyrene-test-project/zones/us-central1-a/diskTypes/pd-standard",
        "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
        "labels": labels or {},
        "diskEncryptionKey": {"kmsKeyName": "must-not-be-returned"},
    }

def _gcp_operation_response(
    name: str,
    disk_name: str,
    client_operation_id: str,
    action: str,
    insert_time: str,
) -> dict[str, Any]:
    """Return a zonal Operation fixture whose target identity matches one test disk."""

    return {
        "kind": "compute#operation",
        "name": name,
        "zone": (
            "https://www.googleapis.com/compute/v1/projects/cyrene-test-project/zones/us-central1-a"
        ),
        "clientOperationId": client_operation_id,
        "operationType": "insert" if action == "create" else "delete",
        "targetLink": (
            "https://www.googleapis.com/compute/v1/projects/"
            f"cyrene-test-project/zones/us-central1-a/disks/{disk_name}"
        ),
        "targetId": "123456789",
        "status": "DONE",
        "insertTime": insert_time,
    }

def test_cloud_provider_health_reports_managed_identity_and_wif_expiry(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Azure and GCP health checks expose only short-lived token expiry metadata."""

    settings = _multi_provider_settings(tmp_path)
    subscription_path = "/subscriptions/11111111-2222-3333-4444-555555555555"

    def health_response(request: httpx.Request) -> httpx.Response:
        if request.url.host == provider_impl.AZURE_HOST:
            assert request.method == "GET"
            assert request.url.path == f"{subscription_path}/resourceGroups/rg-test"
            assert request.url.params["api-version"] == provider_impl.AZURE_RESOURCE_API_VERSION
            return httpx.Response(
                200,
                json={
                    "id": f"{subscription_path}/resourceGroups/RG-TEST",
                    "name": "RG-TEST",
                },
            )
        return httpx.Response(200, json={"items": []})

    client = httpx.Client(transport=httpx.MockTransport(health_response))
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


def test_azure_health_checks_every_bound_resource_group_without_subscription_get(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Azure health proves every configured group and never probes subscription scope."""

    subscription_id = "a1111111-b222-c333-d444-e55555555555"
    resource_groups = ["Rg-One", "Rg-Two"]
    settings = _multi_provider_settings(
        tmp_path,
        azure_resource_groups=resource_groups,
        azure_subscription_id=subscription_id,
    )
    requests: list[httpx.Request] = []

    def health_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        expected_prefix = f"/subscriptions/{subscription_id}/resourceGroups/"
        assert request.method == "GET"
        assert request.url.host == provider_impl.AZURE_HOST
        assert request.url.path.startswith(expected_prefix)
        group = request.url.path.removeprefix(expected_prefix)
        assert group in resource_groups
        assert request.url.params["api-version"] == provider_impl.AZURE_RESOURCE_API_VERSION
        return httpx.Response(
            200,
            json={
                "id": f"/subscriptions/{subscription_id}/resourceGroups/{group.lower()}".upper(),
                "name": group.lower(),
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(health_response))
    executor = CloudManagementExecutor(settings, http_client=client)
    monkeypatch.setattr(
        executor,
        "_azure_token",
        lambda _profile: ("fixture-azure-token", "2026-10-26T22:00:00Z"),
    )

    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": "azure/test"},
        request_id="request-azure-health-rgs",
    )

    assert [request.url.path for request in requests] == [
        f"/subscriptions/{subscription_id}/resourceGroups/Rg-One",
        f"/subscriptions/{subscription_id}/resourceGroups/Rg-Two",
    ]
    assert all("/resourceGroups/" in request.url.path for request in requests)
    assert health["authentication_state"] == "VALID"
    assert health["authorization_state"] == "VALID"
    assert health["scope_refs"] == [
        f"azure:{subscription_id}/Rg-One",
        f"azure:{subscription_id}/Rg-Two",
    ]
    executor.close()


def test_azure_health_reports_resource_group_403_as_permission_denied(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denial at the bound resource-group scope stays a permission failure."""

    settings = _multi_provider_settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(403, json={"detail": "private body"})
        )
    )
    executor = CloudManagementExecutor(settings, http_client=client)
    monkeypatch.setattr(executor, "_azure_token", lambda _profile: ("fixture", None))

    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": "azure/test"},
        request_id="request-azure-health-rg-denied",
    )

    assert health["authentication_state"] == "VALID"
    assert health["authorization_state"] == "PERMISSION_DENIED"
    assert health["reason_code"] == "SCOPE_DENIED"
    assert "private body" not in json.dumps(health)
    executor.close()


def test_cloud_profile_read_only_defaults_false_and_parses_true(tmp_path: Any) -> None:
    """The host profile flag is optional for compatibility and parsed as a boolean."""

    assert _settings(tmp_path / "default").profile("runpod/test").read_only is False
    assert _settings(tmp_path / "readonly", read_only=True).profile("runpod/test").read_only is True
    schema = json.loads(
        (Path(__file__).parents[1] / "configuration.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(_configuration(tmp_path, read_only=True))

    invalid_config = _configuration(tmp_path, read_only=True)
    invalid_config["profiles"][0]["read_only"] = "true"
    invalid_environment = {
        "CYRENE_CAPABILITY_BINDING_ID": "binding-cloud-test",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(invalid_config),
        "EXCHANGE_KEY": _EXCHANGE_KEY.hex(),
        "STATE_DIR": str(tmp_path / "invalid"),
        "RUNPOD_API_KEY": _API_KEY,
    }
    with pytest.raises(ConfigurationError, match="profile read_only must be boolean"):
        ProviderSettings.from_environment(invalid_environment)


def test_cloud_provider_health_runpod_key_has_unknown_expiry(
    tmp_path: Any,
) -> None:
    """A working static Runpod key is valid now but has no asserted expiry date."""

    settings = _settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"pods": [], "pagination": {"nextCursor": None, "hasNextPage": False}},
            )
        )
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
                lambda _request: httpx.Response(
                    200,
                    json={"pods": [], "pagination": {"nextCursor": None, "hasNextPage": False}},
                )
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
    ("profile_ref", "response"),
    [
        ("azure/test", {"error": {"code": "Denied"}}),
        ("azure/test", {}),
        (
            "azure/test",
            {
                "id": "/subscriptions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/resourceGroups/rg-test",
                "name": "rg-test",
            },
        ),
        (
            "azure/test",
            {
                "id": "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-other",
                "name": "rg-test",
            },
        ),
        (
            "azure/test",
            {
                "id": "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-test",
                "name": "rg-other",
            },
        ),
        ("gcp/test", {"items": [{}]}),
        (
            "runpod/test",
            {"pods": [{}], "pagination": {"nextCursor": None, "hasNextPage": False}},
        ),
    ],
)
def test_cloud_provider_health_does_not_accept_malformed_success_bodies(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    profile_ref: str,
    response: dict[str, Any],
) -> None:
    """A 2xx with an invalid provider shape remains UNKNOWN, never VALID."""

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

    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": profile_ref},
        request_id=f"request-health-malformed-{profile_ref}",
    )

    assert health["authentication_state"] == "UNKNOWN"
    assert health["authorization_state"] == "UNKNOWN"
    assert health["reason_code"] == "INVALID_PROVIDER_RESPONSE"
    executor.close()


@pytest.mark.parametrize(
    ("failure_type", "expected_authentication", "expected_authorization", "expected_reason"),
    [
        ("transport", "UNKNOWN", "UNKNOWN", "PROVIDER_UNAVAILABLE"),
        ("auth", "INVALID", "REAUTHORIZATION_REQUIRED", "AUTHENTICATION_FAILED"),
        ("permission", "VALID", "PERMISSION_DENIED", "SCOPE_DENIED"),
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
    if failure_type == "transport":
        error: Exception = ServiceRequestError("private transport detail")
    elif failure_type == "permission":
        error = ClientAuthenticationError(
            "private identity detail",
            response=SimpleNamespace(status_code=403, reason="Forbidden", headers={}),
        )
    else:
        error = ClientAuthenticationError("private identity detail")

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
    (
        "response_body",
        "exception_message",
        "expected_authentication",
        "expected_authorization",
        "expected_reason",
    ),
    [
        (
            '{"error":"invalid_target","error_description":"private detail"}',
            "private OAuth message",
            "INVALID",
            "REAUTHORIZATION_REQUIRED",
            "AUTHENTICATION_FAILED",
        ),
        (
            '{"error":"temporarily_unavailable","error_description":"private detail"}',
            "private OAuth message",
            "UNKNOWN",
            "UNKNOWN",
            "PROVIDER_UNAVAILABLE",
        ),
        (
            '{"error":"server_error","error_description":"private detail"}',
            "private OAuth message",
            "UNKNOWN",
            "UNKNOWN",
            "PROVIDER_UNAVAILABLE",
        ),
        (
            "not-json",
            "invalid_target private OAuth message",
            "UNKNOWN",
            "UNKNOWN",
            "PROVIDER_ERROR",
        ),
        (
            '{"error":"unrecognized_code","error_description":"private detail"}',
            "private OAuth message",
            "UNKNOWN",
            "UNKNOWN",
            "PROVIDER_ERROR",
        ),
    ],
)
def test_gcp_wif_health_classifies_only_known_structured_oauth_errors(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    response_body: str,
    exception_message: str,
    expected_authentication: str,
    expected_authorization: str,
    expected_reason: str,
) -> None:
    """Classify only bounded OAuth machine codes, never free-form provider text."""

    from google.auth.exceptions import OAuthError

    settings = _multi_provider_settings(tmp_path)
    requests: list[httpx.Request] = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={"items": []})
        )
    )
    executor = CloudManagementExecutor(settings, http_client=client)

    class _Credential:
        valid = False

        def refresh(self, _request: Any) -> None:
            raise OAuthError(exception_message, response_body)

    monkeypatch.setitem(executor._credentials, "gcp/test", _Credential())  # noqa: SLF001
    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": "gcp/test"},
        request_id=f"request-gcp-oauth-{expected_reason}",
    )

    assert health["authentication_state"] == expected_authentication
    assert health["authorization_state"] == expected_authorization
    assert health["reason_code"] == expected_reason
    assert requests == []
    assert "private" not in json.dumps(health)
    executor.close()


@pytest.mark.parametrize(
    ("response_body", "expected_reason"),
    [
        (None, "PROVIDER_ERROR"),
        ("x" * 8193, "PROVIDER_ERROR"),
    ],
)
def test_gcp_wif_health_rejects_absent_or_oversized_oauth_response_bodies(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    response_body: str | None,
    expected_reason: str,
) -> None:
    """Malformed or oversized OAuth bodies never become credential evidence."""

    from google.auth.exceptions import OAuthError

    settings = _multi_provider_settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"items": []}))
    )
    executor = CloudManagementExecutor(settings, http_client=client)

    class _Credential:
        valid = False

        def refresh(self, _request: Any) -> None:
            if response_body is None:
                raise OAuthError("invalid_target appeared only in this message")
            raise OAuthError("private OAuth message", response_body)

    monkeypatch.setitem(executor._credentials, "gcp/test", _Credential())  # noqa: SLF001
    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": "gcp/test"},
        request_id=f"request-gcp-oauth-bad-body-{expected_reason}-{response_body is None}",
    )

    assert health["authentication_state"] == "UNKNOWN"
    assert health["authorization_state"] == "UNKNOWN"
    assert health["reason_code"] == expected_reason
    assert "private" not in json.dumps(health)
    executor.close()


def test_gcp_wif_health_preserves_transport_error_classification(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google token transport failures remain retryable and distinct from OAuth rejection."""

    from google.auth.exceptions import TransportError

    settings = _multi_provider_settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"items": []}))
    )
    executor = CloudManagementExecutor(settings, http_client=client)

    class _Credential:
        valid = False

        def refresh(self, _request: Any) -> None:
            raise TransportError("private transport detail")

    monkeypatch.setitem(executor._credentials, "gcp/test", _Credential())  # noqa: SLF001
    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": "gcp/test"},
        request_id="request-gcp-oauth-transport",
    )

    assert health["authentication_state"] == "UNKNOWN"
    assert health["authorization_state"] == "UNKNOWN"
    assert health["reason_code"] == "PROVIDER_UNAVAILABLE"
    assert "private" not in json.dumps(health)
    executor.close()


def test_gcp_wif_health_reports_structured_iam_denial_as_permission_denied(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structured WIF/IAM 403 is not mislabeled as an invalid source identity."""

    from google.auth.exceptions import RefreshError

    settings = _multi_provider_settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"items": []}))
    )
    executor = CloudManagementExecutor(settings, http_client=client)

    class _Credential:
        valid = False

        def refresh(self, _request: Any) -> None:
            raise RefreshError(
                "private denial detail",
                {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "private"}},
            )

    monkeypatch.setitem(executor._credentials, "gcp/test", _Credential())  # noqa: SLF001
    health = executor.call(
        "cloud_provider_health",
        {"profile_ref": "gcp/test"},
        request_id="request-gcp-refresh-permission-denied",
    )

    assert health["authentication_state"] == "VALID"
    assert health["authorization_state"] == "PERMISSION_DENIED"
    assert health["reason_code"] == "SCOPE_DENIED"
    assert "private denial detail" not in json.dumps(health)
    assert '"message": "private"' not in json.dumps(health)
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


def test_runpod_v2_list_validates_and_preserves_pagination(tmp_path: Any) -> None:
    """A valid v2 Pod page returns scoped records and its opaque next cursor."""

    settings = _settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "pods": [_pod_response("pod-123", "cyrene-test-worker")],
                    "pagination": {"nextCursor": "page-next", "hasNextPage": True},
                },
            )
        )
    )
    executor = CloudManagementExecutor(settings, http_client=client)

    listed = executor.call(
        "cloud_resources_list",
        {"profile_ref": "runpod/test", "target": {"scope_ref": "pods"}},
        request_id="request-runpod-list-valid",
    )

    assert listed["resources"][0]["id"] == "pod-123"
    assert listed["resources"][0]["status"] == "RUNNING"
    assert listed["next_cursor"] == "page-next"
    assert listed["has_next_page"] is True
    executor.close()


@pytest.mark.parametrize(
    "pagination",
    [
        {"hasNextPage": True},
        {"nextCursor": None, "hasNextPage": True},
        {"nextCursor": "", "hasNextPage": True},
    ],
)
def test_runpod_v2_list_rejects_unfollowable_next_page(
    tmp_path: Any,
    pagination: dict[str, Any],
) -> None:
    """Runpod cannot claim another page without a usable continuation cursor."""

    settings = _settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"pods": [], "pagination": pagination})
        )
    )
    executor = CloudManagementExecutor(settings, http_client=client)

    with pytest.raises(ProviderFailure, match="INVALID_PROVIDER_RESPONSE"):
        executor.call(
            "cloud_resources_list",
            {"profile_ref": "runpod/test", "target": {"scope_ref": "pods"}},
            request_id="request-runpod-list-invalid-pagination",
        )
    executor.close()


@pytest.mark.parametrize(
    ("profile_ref", "target", "response"),
    [
        ("azure/test", {"resource_group": "rg-test"}, {"value": [{}]}),
        ("gcp/test", {"zone": "us-central1-a"}, {"items": [{}]}),
        (
            "runpod/test",
            {"scope_ref": "pods"},
            {"pods": [{}], "pagination": {"nextCursor": None, "hasNextPage": False}},
        ),
    ],
)
def test_cloud_resource_list_rejects_incomplete_provider_items(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    profile_ref: str,
    target: dict[str, str],
    response: dict[str, Any],
) -> None:
    """Incomplete success records cannot vanish during scope filtering."""

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
            request_id=f"request-list-item-{profile_ref}",
        )
    executor.close()


@pytest.mark.parametrize(
    "response",
    [
        {"id": "pod-123", "error": {"code": "Unavailable"}},
        {"id": "pod-123", "name": "cyrene-test-unit"},
    ],
)
def test_runpod_get_requires_a_complete_pod_record(
    tmp_path: Any,
    response: dict[str, Any],
) -> None:
    """A matching ID alone cannot convert a malformed v2 read into a resource."""

    settings = _settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response))
    )
    executor = CloudManagementExecutor(settings, http_client=client)

    with pytest.raises(ProviderFailure, match="INVALID_PROVIDER_RESPONSE"):
        executor.call(
            "cloud_resource_get",
            {"profile_ref": "runpod/test", "target": {"pod_id": "pod-123"}},
            request_id="request-runpod-malformed-get",
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


def test_runpod_create_malformed_201_stays_unknown_and_replay_safe(tmp_path: Any) -> None:
    """A malformed create response cannot become success or trigger a duplicate POST."""

    settings = _settings(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={"id": "new-pod-1", "name": "cyrene-test-unit"},
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

    result = executor.call(
        "cloud_resource_create", arguments, request_id="request-create-malformed"
    )
    replay = executor.call(
        "cloud_resource_create", arguments, request_id="request-create-malformed"
    )

    assert len(requests) == 1
    assert result == replay
    assert result["state"] == "UNKNOWN_RESULT"
    assert result["actual_state"] == "UNKNOWN"
    assert result["readiness"] == "UNKNOWN"
    assert result["reason_code"] == "INVALID_PROVIDER_RESPONSE"
    assert result["operation_ref"]
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


def test_read_only_profile_filters_catalog_and_server_denies_direct_mutations(
    tmp_path: Any,
) -> None:
    """The provider enforces the host profile mode even for signed direct tool calls."""

    settings = _settings(tmp_path, cli_enabled=True, read_only=True)
    assert settings.profile("runpod/test").read_only is True
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"pods": [], "pagination": {"nextCursor": None, "hasNextPage": False}},
        )

    class _ProcessRunner:
        calls = 0

        def run(self, *_args: Any, **_kwargs: Any) -> ProcessResult:
            self.calls += 1
            raise AssertionError("read-only rejection must happen before CLI execution")

    process = _ProcessRunner()
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        process_runner=process,
    )
    plugin = CloudManagementProvider(settings=settings, executor=executor)
    catalog_request = wire.ListToolsRequest(binding_id=settings.binding_id)
    accepted, catalog_payload = plugin.on_invoke(
        CAPABILITY_ID,
        LIST_TOOLS_METHOD,
        catalog_request.SerializeToString(),
    )
    assert accepted is True
    catalog = wire.ListToolsResponse.FromString(catalog_payload.value).catalog
    assert {tool.provider_tool_id for tool in catalog.tools} == READ_ONLY_TOOL_IDS

    cases = [
        (
            "cloud_resource_create",
            {
                "profile_ref": "runpod/test",
                "target": {"pod_name": "cyrene-test-unit"},
                "parameters": {
                    "image": "example/image:1",
                    "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
                    "disk": 20,
                    "dataCenterIds": ["US-KS-2"],
                },
            },
        ),
        (
            "cloud_operation_cancel",
            {"profile_ref": "runpod/test", "operation_ref": "fixture-operation-ref"},
        ),
        (
            "cloud_vendor_cli",
            {"profile_ref": "runpod/test", "argv": ["pod", "list"]},
        ),
    ]
    for index, (tool_id, arguments) in enumerate(cases):
        request_id = f"request-readonly-{index}"
        now = datetime.now(UTC)
        context = sign_context(
            method=tool_id,
            operation_id=str(TOOL_BY_ID[tool_id]["operation_id"]),
            payload=arguments,
            caller_workload="exchange",
            audience="cloud-management",
            issuer="https://directory.example",
            subject="user-1",
            organization_id="org-fixture",
            workspace_id="workspace-fixture",
            binding_id=settings.binding_id,
            request_id=request_id,
            key=_EXCHANGE_KEY,
            issued_at=now,
            expires_at=now + timedelta(seconds=60),
        )

        response = _invoke(
            plugin,
            settings.binding_id,
            arguments,
            context.as_dict(),
            tool_id=tool_id,
        )

        assert response.outcome.is_error is True
        assert json.loads(response.outcome.content[0].json.json)["error"]["code"] == (
            "READ_ONLY_PROFILE"
        )
        assert executor._ledger.get(request_id) is None  # noqa: SLF001

    list_arguments = {"profile_ref": "runpod/test", "target": {"scope_ref": "pods"}}
    now = datetime.now(UTC)
    list_context = sign_context(
        method="cloud_resources_list",
        operation_id="cloud.resources.list",
        payload=list_arguments,
        caller_workload="exchange",
        audience="cloud-management",
        issuer="https://directory.example",
        subject="user-1",
        organization_id="org-fixture",
        workspace_id="workspace-fixture",
        binding_id=settings.binding_id,
        request_id="request-readonly-list",
        key=_EXCHANGE_KEY,
        issued_at=now,
        expires_at=now + timedelta(seconds=60),
    )
    read_response = _invoke(
        plugin,
        settings.binding_id,
        list_arguments,
        list_context.as_dict(),
        tool_id="cloud_resources_list",
    )

    assert read_response.outcome.is_error is False
    assert [request.method for request in requests] == ["GET"]
    assert process.calls == 0

    attempted_override = {
        "profile_ref": "runpod/test",
        "target": {"pod_name": "cyrene-test-unit"},
        "parameters": {
            "image": "example/image:1",
            "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
            "disk": 20,
            "dataCenterIds": ["US-KS-2"],
        },
        "read_only": False,
    }
    now = datetime.now(UTC)
    override_context = sign_context(
        method="cloud_resource_create",
        operation_id="cloud.resource.create",
        payload=attempted_override,
        caller_workload="exchange",
        audience="cloud-management",
        issuer="https://directory.example",
        subject="user-1",
        organization_id="org-fixture",
        workspace_id="workspace-fixture",
        binding_id=settings.binding_id,
        request_id="request-readonly-override",
        key=_EXCHANGE_KEY,
        issued_at=now,
        expires_at=now + timedelta(seconds=60),
    )
    override_response = _invoke(
        plugin,
        settings.binding_id,
        attempted_override,
        override_context.as_dict(),
        tool_id="cloud_resource_create",
    )
    assert override_response.outcome.is_error is True
    assert json.loads(override_response.outcome.content[0].json.json)["error"]["code"] == (
        "INVALID_ARGUMENTS"
    )
    assert len(requests) == 1
    plugin.close()


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

@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [(401, "AUTHENTICATION_FAILED"), (403, "SCOPE_DENIED")],
)
def test_gcp_disk_create_surfaces_auth_and_permission_failures(
    tmp_path: Any, status_code: int, expected_code: str
) -> None:
    """GCE authentication and IAM failures remain explicit provider errors."""

    settings = _multi_provider_settings(tmp_path, gcp_disk_scope=True)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json={"error": {"code": status_code}})

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    result = executor.call(
        "cloud_resource_create",
        {
            "profile_ref": "gcp/test",
            "target": {
                "kind": "disk",
                "zone": "us-central1-a",
                "disk_name": "cyrene-test-auth-check",
            },
            "parameters": {
                "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
                "sizeGb": 10,
                "labels": {"cyrene-task": "teams-cloud-20261023"},
            },
        },
        request_id=f"request-gcp-auth-{status_code}",
    )
    assert result["error"]["code"] == expected_code
    assert len(requests) == 1
    assert requests[0].method == "POST"
    executor.close()

def test_gcp_disk_creation_is_default_denied_without_explicit_disk_scope(tmp_path: Any) -> None:
    """Legacy GCP profiles continue to manage instances but cannot create disks."""

    settings = _multi_provider_settings(tmp_path)
    requests: list[httpx.Request] = []
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: requests.append(request))
        ),
    )
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    result = executor.call(
        "cloud_resource_create",
        {
            "profile_ref": "gcp/test",
            "target": {
                "kind": "disk",
                "zone": "us-central1-a",
                "disk_name": "cyrene-test-default-denied",
            },
            "parameters": {
                "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
                "sizeGb": 10,
            },
        },
        request_id="request-gcp-default-denied",
    )
    assert result["error"]["code"] == "SCOPE_DENIED"
    assert requests == []
    executor.close()

@pytest.mark.parametrize(
    ("readback_status", "expected_state", "actual_state", "release_confirmed"),
    [(404, "SUCCEEDED", "ABSENT", True), (200, "UNKNOWN_RESULT", "PRESENT", False)],
)
def test_gcp_disk_delete_404_requires_a_fresh_absence_query(
    tmp_path: Any,
    readback_status: int,
    expected_state: str,
    actual_state: str,
    release_confirmed: bool,
) -> None:
    """An idempotent delete verifies absence and stays unknown if the disk remains."""

    settings = _multi_provider_settings(tmp_path, gcp_disk_scope=True)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(404, json={"error": {"code": 404}})
        assert request.method == "GET"
        if readback_status == 404:
            return httpx.Response(404, json={"error": {"code": 404}})
        return httpx.Response(200, json=_gcp_disk_response(status="READY"))

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    result = executor.call(
        "cloud_resource_delete",
        {
            "profile_ref": "gcp/test",
            "target": {
                "kind": "disk",
                "zone": "us-central1-a",
                "disk_name": "cyrene-test-already-absent",
            },
        },
        request_id="request-gcp-delete-absent",
    )
    assert methods == ["DELETE", "GET"]
    assert result["state"] == expected_state
    assert result["actual_state"] == actual_state
    assert result["release_confirmed"] is release_confirmed
    operation = parse_operation_ref(result["operation_ref"], _EXCHANGE_KEY)
    assert operation["request_id"] == "request-gcp-delete-absent"
    executor.close()

def test_gcp_disk_list_is_explicit_and_prefix_filtered(tmp_path: Any) -> None:
    """Disk enumeration uses its own target kind and name-prefix allowlist."""

    settings = _multi_provider_settings(tmp_path, gcp_disk_scope=True)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "items": [
                    _gcp_disk_response(labels={"owner": "yidong-derek-li"}),
                    _gcp_disk_response(name="other-disk"),
                ],
                "nextPageToken": "disk-next-page",
            },
        )

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    listed = executor.call(
        "cloud_resources_list",
        {"profile_ref": "gcp/test", "target": {"kind": "disk", "zone": "us-central1-a"}},
        request_id="request-gcp-disk-list",
    )
    assert paths == ["/compute/v1/projects/cyrene-test-project/zones/us-central1-a/disks"]
    assert listed["resource_kind"] == "disk"
    assert [item["name"] for item in listed["resources"]] == ["cyrene-test-boot"]
    assert listed["resources"][0]["labels"] == {"owner": "yidong-derek-li"}
    assert listed["next_page_token"] == "disk-next-page"
    assert "diskEncryptionKey" not in json.dumps(listed)
    executor.close()

@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("sourceImage", "projects/debian-cloud/global/images/family/debian-11"),
        ("sizeGb", "12"),
        ("labels", {"cyrene-task": "different-task"}),
    ],
)
def test_gcp_disk_operation_requires_exact_configuration_readback(
    tmp_path: Any, field: str, wrong_value: object
) -> None:
    """A READY disk with a different image, size, or owner/task label is not success."""

    settings = _multi_provider_settings(tmp_path, gcp_disk_scope=True)
    labels = {"cyrene-task": "teams-cloud-20261023"}
    operation_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            operation_body.update(
                _gcp_operation_response(
                    "disk-verify-op",
                    "cyrene-test-readback-mismatch",
                    request.url.params["requestId"],
                    "create",
                    "",
                )
            )
            operation_body["status"] = "RUNNING"
            return httpx.Response(
                200,
                json={"name": "disk-verify-op", "status": "RUNNING", "zone": "us-central1-a"},
            )
        if request.url.path.endswith("/operations/disk-verify-op"):
            return httpx.Response(200, json=operation_body)
        disk = _gcp_disk_response(labels=labels)
        disk[field] = wrong_value
        return httpx.Response(200, json=disk)

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    request_id = f"request-gcp-disk-readback-{uuid.uuid4()}"
    created = executor.call(
        "cloud_resource_create",
        {
            "profile_ref": "gcp/test",
            "target": {
                "kind": "disk",
                "zone": "us-central1-a",
                "disk_name": "cyrene-test-readback-mismatch",
            },
            "parameters": {
                "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
                "sizeGb": 10,
                "labels": labels,
            },
        },
        request_id=request_id,
    )
    operation_ref = parse_operation_ref(created["operation_ref"], _EXCHANGE_KEY)
    operation_body["insertTime"] = operation_ref["created_at"]
    operation_body["status"] = "DONE"
    status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "gcp/test", "operation_ref": created["operation_ref"]},
        request_id=f"request-gcp-disk-readback-status-{uuid.uuid4()}",
    )
    assert status["state"] == "UNKNOWN_RESULT"
    assert status["actual_state"] == "READY"
    assert status["reason_code"] == "DISK_CONFIGURATION_READBACK_MISMATCH"
    executor.close()

@pytest.mark.parametrize(
    ("target", "parameters"),
    [
        (
            {"kind": "disk", "zone": "us-central1-b", "disk_name": "cyrene-test-disk"},
            {
                "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
                "sizeGb": 10,
            },
        ),
        (
            {"kind": "disk", "zone": "us-central1-a", "disk_name": "outside-test-disk"},
            {
                "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
                "sizeGb": 10,
            },
        ),
        (
            {"kind": "disk", "zone": "us-central1-a", "disk_name": "cyrene-test-disk"},
            {"sourceImage": "projects/other-cloud/global/images/family/debian-12", "sizeGb": 10},
        ),
        (
            {"kind": "disk", "zone": "us-central1-a", "disk_name": "cyrene-test-disk"},
            {
                "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
                "sizeGb": 17,
            },
        ),
        (
            {"kind": "disk", "zone": "us-central1-a", "disk_name": "cyrene-test-disk"},
            {
                "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
                "sizeGb": 10,
                "labels": {"api_key": "sensitive"},
            },
        ),
    ],
)
def test_gcp_disk_scope_and_create_bounds_fail_before_provider_calls(
    tmp_path: Any, target: dict[str, str], parameters: dict[str, Any]
) -> None:
    """Wrong zones, names, images, sizes, and secret labels never reach GCE."""

    settings = _multi_provider_settings(tmp_path, gcp_disk_scope=True)
    requests: list[httpx.Request] = []
    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: requests.append(request))
        ),
    )
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    result = executor.call(
        "cloud_resource_create",
        {"profile_ref": "gcp/test", "target": target, "parameters": parameters},
        request_id=f"request-gcp-invalid-{uuid.uuid4()}",
    )
    assert result["error"]["code"] in {"SCOPE_DENIED", "INVALID_ARGUMENTS"}
    assert requests == []
    executor.close()

@pytest.mark.parametrize(
    ("operation_list_status", "expected_lookup_error"),
    [(200, None), (403, "SCOPE_DENIED")],
)
def test_gcp_interrupted_disk_create_is_queried_without_replay(
    tmp_path: Any, operation_list_status: int, expected_lookup_error: str | None
) -> None:
    """A matching pre-existing disk is not adopted when LRO identity is unproved."""

    settings = _multi_provider_settings(tmp_path, gcp_disk_scope=True)
    methods: list[str] = []
    labels = {"cyrene-task": "teams-cloud-20261023"}

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            raise httpx.ReadTimeout("fixture write response lost")
        if request.url.path.endswith("/zones/us-central1-a/operations"):
            if operation_list_status == 403:
                return httpx.Response(403, json={"error": {"code": 403}})
            return httpx.Response(200, json={"items": []})
        return httpx.Response(
            200,
            json=_gcp_disk_response(
                name="cyrene-test-interrupted",
                labels=labels,
            ),
        )

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    arguments = {
        "profile_ref": "gcp/test",
        "target": {
            "kind": "disk",
            "zone": "us-central1-a",
            "disk_name": "cyrene-test-interrupted",
        },
        "parameters": {
            "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
            "sizeGb": 10,
            "labels": labels,
        },
    }
    result = executor.call(
        "cloud_resource_create", arguments, request_id="request-gcp-disk-unknown"
    )
    replay = executor.call(
        "cloud_resource_create", arguments, request_id="request-gcp-disk-unknown"
    )
    assert replay == result
    assert result["state"] == "UNKNOWN_RESULT"
    assert methods == ["POST"]
    operation = parse_operation_ref(result["operation_ref"], _EXCHANGE_KEY)
    assert operation["request_id"] == "request-gcp-disk-unknown"
    assert operation["vendor_operation"]["expected_labels"] == {
        "cyrene-task": "teams-cloud-20261023"
    }

    status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "gcp/test", "operation_ref": result["operation_ref"]},
        request_id="request-gcp-disk-unknown-status",
    )
    assert methods == ["POST", "GET", "GET"]
    assert status["state"] == "UNKNOWN_RESULT"
    assert status["actual_state"] == "READY"
    assert status["reason_code"] == "DISK_OPERATION_IDENTITY_UNVERIFIED"
    assert status["operation_lookup_error"] == expected_lookup_error
    assert status["observed_configuration"] == {
        "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
        "sizeGb": "10",
        "type": "pd-standard",
        "labels": labels,
    }
    executor.close()

@pytest.mark.parametrize(
    ("target_id", "time_offset_minutes", "expected_state", "expected_reason", "request_count"),
    [
        ("123456789", 0, "SUCCEEDED", None, 4),
        ("987654321", 0, "UNKNOWN_RESULT", "DISK_TARGET_ID_READBACK_MISMATCH", 4),
        (
            "123456789",
            10,
            "UNKNOWN_RESULT",
            "DISK_OPERATION_IDENTITY_UNVERIFIED",
            3,
        ),
    ],
)
def test_gcp_interrupted_disk_create_requires_exact_operation_identity(
    tmp_path: Any,
    target_id: str,
    time_offset_minutes: int,
    expected_state: str,
    expected_reason: str | None,
    request_count: int,
) -> None:
    """A lost POST needs exact request, target, action, time, and disk incarnation matches."""

    settings = _multi_provider_settings(tmp_path, gcp_disk_scope=True)
    request_id = "request-gcp-disk-lro-proof"
    client_operation_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"cyrene-cloud-management:gcp:gcp/test:{request_id}",
        )
    )
    operation_resource: dict[str, Any] = {
        "kind": "compute#operation",
        "name": "disk-insert-confirmed",
        "zone": (
            "https://www.googleapis.com/compute/v1/projects/cyrene-test-project/zones/us-central1-a"
        ),
        "clientOperationId": client_operation_id,
        "operationType": "insert",
        "targetLink": (
            "https://www.googleapis.com/compute/v1/projects/"
            "cyrene-test-project/zones/us-central1-a/disks/cyrene-test-lro-proof"
        ),
        "targetId": target_id,
        "status": "DONE",
        "insertTime": "",
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert request.url.params["requestId"] == client_operation_id
            raise httpx.ReadTimeout("fixture write response lost")
        if request.url.path.endswith("/zones/us-central1-a/operations"):
            assert request.url.params["maxResults"] == "100"
            assert request.url.params["orderBy"] == "creationTimestamp desc"
            return httpx.Response(200, json={"items": [operation_resource]})
        if request.url.path.endswith("/operations/disk-insert-confirmed"):
            return httpx.Response(200, json=operation_resource)
        return httpx.Response(
            200,
            json=_gcp_disk_response(
                name="cyrene-test-lro-proof",
                labels={"cyrene-task": "teams-cloud-20261023"},
            ),
        )

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    created = executor.call(
        "cloud_resource_create",
        {
            "profile_ref": "gcp/test",
            "target": {
                "kind": "disk",
                "zone": "us-central1-a",
                "disk_name": "cyrene-test-lro-proof",
            },
            "parameters": {
                "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
                "sizeGb": 10,
                "labels": {"cyrene-task": "teams-cloud-20261023"},
            },
        },
        request_id=request_id,
    )
    operation_ref = parse_operation_ref(created["operation_ref"], _EXCHANGE_KEY)
    created_at = datetime.fromisoformat(operation_ref["created_at"].replace("Z", "+00:00"))
    operation_resource["insertTime"] = (
        (created_at + timedelta(minutes=time_offset_minutes)).isoformat().replace("+00:00", "Z")
    )

    status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "gcp/test", "operation_ref": created["operation_ref"]},
        request_id="request-gcp-disk-lro-proof-status",
    )
    assert created["state"] == "UNKNOWN_RESULT"
    assert status["state"] == expected_state
    assert status["actual_state"] == "READY"
    assert len(requests) == request_count
    if expected_reason is None:
        assert "reason_code" not in status
    else:
        assert status["reason_code"] == expected_reason
    assert requests[0].method == "POST"
    assert all(request.method == "GET" for request in requests[1:])
    assert sum(request.method == "POST" for request in requests) == 1
    executor.close()

@pytest.mark.parametrize(
    "source",
    [
        "projects/other-cloud/zones/us-central1-a/disks/cyrene-test-boot",
        "projects/cyrene-test-project/zones/us-central1-b/disks/cyrene-test-boot",
        "projects/cyrene-test-project/zones/us-central1-a/disks/outside-test-boot",
    ],
)
def test_gcp_vm_cannot_attach_disk_outside_project_zone_or_prefix(
    tmp_path: Any, source: str
) -> None:
    """Existing-disk attachment validates project, zone, and disk prefix before insert."""

    settings = _multi_provider_settings(tmp_path, gcp_disk_scope=True)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    result = executor.call(
        "cloud_resource_create",
        {
            "profile_ref": "gcp/test",
            "target": {"zone": "us-central1-a", "instance_name": "cyrene-test-vm"},
            "parameters": {
                "machineType": "zones/us-central1-a/machineTypes/e2-medium",
                "disks": [{"boot": True, "source": source, "autoDelete": False}],
                "networkInterfaces": [],
            },
        },
        request_id=f"request-gcp-vm-disk-scope-{uuid.uuid4()}",
    )
    assert result["error"]["code"] == "SCOPE_DENIED"
    assert requests == []
    executor.close()

def test_gcp_zonal_disk_create_attach_delete_and_confirm_absence(tmp_path: Any) -> None:
    """Create, poll, attach, and delete one scoped boot disk through fixed REST routes."""

    settings = _multi_provider_settings(tmp_path, gcp_disk_scope=True)
    requests: list[httpx.Request] = []
    delete_requested = [False]
    labels = {
        "cyrene-task": "teams-cloud-20261023",
        "author": "yidong-derek-li",
        "authorization": "managed",
    }
    expected_source = "projects/cyrene-test-project/zones/us-central1-a/disks/cyrene-test-boot"
    create_lro: dict[str, Any] = {}
    delete_lro: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/zones/us-central1-a/disks"):
            assert request.headers["Authorization"] == "Bearer fixture-token"
            assert request.url.params["sourceImage"] == (
                "projects/debian-cloud/global/images/family/debian-12"
            )
            request_uuid = uuid.UUID(request.url.params["requestId"])
            assert request_uuid.int != 0
            body = json.loads(request.content)
            assert body == {
                "name": "cyrene-test-boot",
                "sizeGb": "10",
                "type": "zones/us-central1-a/diskTypes/pd-standard",
                "labels": labels,
            }
            create_lro.update(
                _gcp_operation_response(
                    "disk-insert-op",
                    "cyrene-test-boot",
                    request.url.params["requestId"],
                    "create",
                    "",
                )
            )
            create_lro["status"] = "RUNNING"
            return httpx.Response(200, json={"name": "disk-insert-op", "status": "RUNNING"})
        if request.method == "GET" and path.endswith("/operations/disk-insert-op"):
            return httpx.Response(200, json=create_lro)
        if request.method == "GET" and path.endswith("/operations/disk-delete-op"):
            return httpx.Response(200, json=delete_lro)
        if request.method == "GET" and path.endswith("/disks/cyrene-test-boot"):
            if delete_requested[0]:
                return httpx.Response(404, json={"error": {"code": 404}})
            return httpx.Response(200, json=_gcp_disk_response(labels=labels))
        if request.method == "POST" and path.endswith("/zones/us-central1-a/instances"):
            body = json.loads(request.content)
            assert body["disks"] == [{"boot": True, "source": expected_source, "autoDelete": False}]
            assert body["name"] == "cyrene-test-vm"
            return httpx.Response(
                200,
                json={"name": "instance-insert-op", "status": "RUNNING", "zone": "us-central1-a"},
            )
        if request.method == "DELETE" and path.endswith("/disks/cyrene-test-boot"):
            delete_requested[0] = True
            assert uuid.UUID(request.url.params["requestId"]).int != 0
            delete_lro.update(
                _gcp_operation_response(
                    "disk-delete-op",
                    "cyrene-test-boot",
                    request.url.params["requestId"],
                    "delete",
                    "",
                )
            )
            delete_lro["status"] = "RUNNING"
            return httpx.Response(
                200,
                json={"name": "disk-delete-op", "status": "RUNNING", "zone": "us-central1-a"},
            )
        raise AssertionError(f"unexpected GCE request: {request.method} {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    executor = CloudManagementExecutor(settings, http_client=client)
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    disk_target = {
        "kind": "disk",
        "zone": "us-central1-a",
        "disk_name": "cyrene-test-boot",
    }
    create_arguments = {
        "profile_ref": "gcp/test",
        "target": disk_target,
        "parameters": {
            "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
            "sizeGb": 10,
            "type": "pd-standard",
            "labels": labels,
        },
    }
    created = executor.call(
        "cloud_resource_create", create_arguments, request_id="request-gcp-disk-create"
    )
    replay = executor.call(
        "cloud_resource_create", create_arguments, request_id="request-gcp-disk-create"
    )
    assert created == replay
    assert created["state"] == "PENDING"
    assert created["expected_state"] == "READY"
    assert created["desired_state"] == "READY"
    assert created["actual_state"] == "UNKNOWN"
    assert created["readiness"] == "UNKNOWN"
    create_operation = parse_operation_ref(created["operation_ref"], _EXCHANGE_KEY)
    create_lro["insertTime"] = create_operation["created_at"]
    create_lro["status"] = "DONE"
    assert create_operation["request_id"] == "request-gcp-disk-create"
    assert create_operation["vendor_operation"]["expected_labels"] == labels
    assert create_operation["vendor_operation"]["expected_source_image"] == (
        "projects/debian-cloud/global/images/family/debian-12"
    )
    create_request = requests[0]
    assert create_request.url.params["requestId"] == str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "cyrene-cloud-management:gcp:gcp/test:request-gcp-disk-create",
        )
    )

    create_status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "gcp/test", "operation_ref": created["operation_ref"]},
        request_id="request-gcp-disk-status",
    )
    assert create_status["state"] == "SUCCEEDED"
    assert create_status["expected_state"] == "READY"
    assert create_status["desired_state"] == "READY"
    assert create_status["actual_state"] == "READY"
    assert create_status["readiness"] == "READY"
    assert create_status["release_confirmed"] is False

    readback = executor.call(
        "cloud_resource_get",
        {
            "profile_ref": "gcp/test",
            "target": {"zone": "us-central1-a", "disk_name": "cyrene-test-boot"},
        },
        request_id="request-gcp-disk-get",
    )
    assert readback["resource"]["status"] == "READY"
    assert readback["resource"]["sourceImage"] == (
        "projects/debian-cloud/global/images/family/debian-12"
    )
    assert readback["resource"]["labels"] == labels
    assert "diskEncryptionKey" not in json.dumps(readback)
    assert "must-not-be-returned" not in json.dumps(readback)

    instance = executor.call(
        "cloud_resource_create",
        {
            "profile_ref": "gcp/test",
            "target": {"zone": "us-central1-a", "instance_name": "cyrene-test-vm"},
            "parameters": {
                "machineType": "zones/us-central1-a/machineTypes/e2-medium",
                "disks": [{"boot": True, "source": expected_source, "autoDelete": False}],
                "networkInterfaces": [],
            },
        },
        request_id="request-gcp-instance-create",
    )
    assert instance["state"] == "PENDING"

    deleted = executor.call(
        "cloud_resource_delete",
        {"profile_ref": "gcp/test", "target": disk_target},
        request_id="request-gcp-disk-delete",
    )
    assert deleted["state"] == "PENDING"
    assert deleted["desired_state"] == "ABSENT"
    assert deleted["actual_state"] == "UNKNOWN"
    delete_operation = parse_operation_ref(deleted["operation_ref"], _EXCHANGE_KEY)
    delete_lro["insertTime"] = delete_operation["created_at"]
    delete_lro["status"] = "DONE"
    assert delete_operation["request_id"] == "request-gcp-disk-delete"
    delete_request = next(request for request in requests if request.method == "DELETE")
    assert delete_request.url.params["requestId"] == str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "cyrene-cloud-management:gcp:gcp/test:request-gcp-disk-delete",
        )
    )
    delete_status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "gcp/test", "operation_ref": deleted["operation_ref"]},
        request_id="request-gcp-disk-delete-status",
    )
    assert delete_status["state"] == "SUCCEEDED"
    assert delete_status["actual_state"] == "ABSENT"
    assert delete_status["release_confirmed"] is True
    assert (
        len(
            [
                request
                for request in requests
                if request.method == "POST" and "/disks" in request.url.path
            ]
        )
        == 1
    )
    assert len([request for request in requests if request.method == "DELETE"]) == 1
    executor.close()

def test_gcp_zonal_disk_create_without_labels_is_allowed(tmp_path: Any) -> None:
    """An otherwise scoped persistent disk does not require provider-invented labels."""

    settings = _multi_provider_settings(tmp_path, gcp_disk_scope=True)
    requests: list[httpx.Request] = []
    operation_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body == {"name": "cyrene-test-no-labels", "sizeGb": "10"}
            operation_body.update(
                _gcp_operation_response(
                    "disk-no-label-op",
                    "cyrene-test-no-labels",
                    request.url.params["requestId"],
                    "create",
                    "",
                )
            )
            operation_body["status"] = "RUNNING"
            return httpx.Response(
                200,
                json={"name": "disk-no-label-op", "status": "RUNNING", "zone": "us-central1-a"},
            )
        if request.url.path.endswith("/operations/disk-no-label-op"):
            return httpx.Response(200, json=operation_body)
        return httpx.Response(200, json=_gcp_disk_response(name="cyrene-test-no-labels"))

    executor = CloudManagementExecutor(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    executor._gcp_token = lambda _profile: ("fixture-token", None)  # noqa: SLF001
    created = executor.call(
        "cloud_resource_create",
        {
            "profile_ref": "gcp/test",
            "target": {
                "kind": "disk",
                "zone": "us-central1-a",
                "disk_name": "cyrene-test-no-labels",
            },
            "parameters": {
                "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
                "sizeGb": 10,
            },
        },
        request_id="request-gcp-disk-no-labels",
    )
    operation_ref = parse_operation_ref(created["operation_ref"], _EXCHANGE_KEY)
    operation_body["insertTime"] = operation_ref["created_at"]
    operation_body["status"] = "DONE"
    status = executor.call(
        "cloud_operation_status",
        {"profile_ref": "gcp/test", "operation_ref": created["operation_ref"]},
        request_id="request-gcp-disk-no-labels-status",
    )
    assert len(requests) == 3
    assert status["state"] == "SUCCEEDED"
    assert status["actual_state"] == "READY"
    assert status["observed_configuration"]["labels"] == {}
    executor.close()


def test_gcp_disk_cli_uses_one_documented_zone_flag(tmp_path: Any) -> None:
    """Disk list uses one scoped --zones value; describe uses the singular --zone."""

    settings = _multi_provider_settings(tmp_path, gcp_disk_scope=True)
    executor = CloudManagementExecutor(settings)
    profile = settings.profile("gcp/test")

    executor._validate_cli_scope(  # noqa: SLF001
        profile, ["compute", "disks", "list", "--zones", "us-central1-a"]
    )
    executor._validate_cli_scope(  # noqa: SLF001
        profile, ["compute", "disks", "list", "--zones=us-central1-a"]
    )
    executor._validate_cli_scope(  # noqa: SLF001
        profile,
        ["compute", "disks", "describe", "cyrene-test-boot", "--zone=us-central1-a"],
    )

    invalid_argv = [
        ["compute", "disks", "list", "--zone=us-central1-a"],
        ["compute", "disks", "list", "-z", "us-central1-a"],
        ["compute", "disks", "list", "--zon=us-central1-a"],
        [
            "compute",
            "disks",
            "list",
            "--zones=us-central1-a",
            "--zones=us-central1-a",
        ],
        ["compute", "disks", "list", "--zones=us-central1-a,us-central1-b"],
        ["compute", "disks", "list", "--zones=us-central1-b"],
        [
            "compute",
            "disks",
            "list",
            "--zones=us-central1-a",
            "--zone=us-central1-a",
        ],
        ["compute", "disks", "describe", "cyrene-test-boot", "--zones=us-central1-a"],
        ["compute", "disks", "describe", "cyrene-test-boot", "-z", "us-central1-a"],
        ["compute", "disks", "describe", "cyrene-test-boot", "--zon=us-central1-a"],
    ]
    for argv in invalid_argv:
        with pytest.raises(ProviderFailure, match="SCOPE_DENIED"):
            executor._validate_cli_scope(profile, argv)  # noqa: SLF001
    executor.close()
