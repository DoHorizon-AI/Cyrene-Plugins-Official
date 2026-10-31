"""Credential-mode tests that never contact Azure or Google metadata services.

中文:验证云身份模式，同时确保测试不访问 Azure 或 Google 元数据服务。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from cloud_resources_provider.cloud_api import (
    CloudProviderFailure,
    DefaultCredentialAcquirer,
)
from cloud_resources_provider.settings import (
    AzureVmTarget,
    CredentialMissingError,
    GcpInstanceTarget,
    ProviderSettings,
)

FIXTURE_EXPIRY = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)


class FakeGcpCredentials:
    """Replace provider credentials so refresh stays deterministic and offline."""

    def __init__(self, scopes: list[str] | None = None) -> None:
        self.scopes = scopes
        self.valid = False
        self.token: str | None = None
        self.expiry: datetime | None = None

    def refresh(self, request: Any) -> None:
        """Model one refresh result without calling the supplied auth transport."""

        assert request is not None
        self.token = "fixture-google-bearer-token"
        self.expiry = FIXTURE_EXPIRY


def settings_for(
    target: AzureVmTarget | GcpInstanceTarget,
    *,
    gcp_mode: str = "metadata",
    gcp_secret: str | None = None,
) -> ProviderSettings:
    """Build provider settings with host-owned credential environment refs."""

    env = {"TEST_EXCHANGE_KEY": "11" * 32}
    names = {"cloud-resources/exchange-verification-key": "TEST_EXCHANGE_KEY"}
    gcp_env_name = None
    if gcp_secret is not None:
        gcp_env_name = "TEST_GCP_CREDENTIAL"
        names["cloud-resources/gcp-service-account-json"] = gcp_env_name
        env[gcp_env_name] = gcp_secret
    return ProviderSettings(
        binding_id="cloud.binding.fixture",
        authorization_generation=0,
        trusted_callers={},
        azure_client_id=None,
        gcp_auth_mode=gcp_mode,
        gcp_credential_environment_name=gcp_env_name,
        hf_token_environment_name=None,
        tailscale_client_id=None,
        tailscale_client_secret_environment_name=None,
        tailscale_tailnet=None,
        runpod_api_key_environment_name=None,
        host_secret_names=names,
        targets=(target,),
        timeout_seconds=3.0,
        environ=env,
    )


def test_azure_uses_managed_identity_with_only_the_arm_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Azure token acquisition is explicit and targets the ARM resource scope."""

    import azure.identity

    calls: list[tuple[str, ...]] = []

    class FakeManagedIdentityCredential:
        def __init__(self, *, client_id: str | None, **transport_options: Any) -> None:
            assert client_id is None
            assert transport_options["connection_timeout"] == 3.0
            assert transport_options["read_timeout"] == 3.0
            assert transport_options["retry_total"] == 0
            assert transport_options["use_env_settings"] is False

        def get_token(self, *scopes: str) -> Any:
            calls.append(scopes)
            return type(
                "Token",
                (),
                {"token": "fixture-azure-token", "expires_on": FIXTURE_EXPIRY.timestamp()},
            )()

        def close(self) -> None:
            return None

    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", FakeManagedIdentityCredential)
    target = AzureVmTarget(
        "vm-prod",
        "Production VM",
        "11111111-2222-3333-4444-555555555555",
        "compute-prod",
        "worker-01",
    )

    credential = DefaultCredentialAcquirer(settings_for(target)).acquire(target)

    assert calls == [("https://management.azure.com/.default",)]
    assert credential.token == "fixture-azure-token"
    assert credential.expires_at == FIXTURE_EXPIRY


def test_gcp_workload_metadata_uses_compute_readonly_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GCE/GKE metadata credentials receive the least-privilege Compute scope."""

    import google.auth.compute_engine.credentials

    calls: list[tuple[str, ...]] = []

    class FakeComputeCredentials(FakeGcpCredentials):
        def __init__(self, *, scopes: list[str]) -> None:
            calls.append(tuple(scopes))
            super().__init__(scopes)

    monkeypatch.setattr(
        google.auth.compute_engine.credentials,
        "Credentials",
        FakeComputeCredentials,
    )
    target = GcpInstanceTarget(
        "gce-prod", "GCE worker", "project-fixture", "us-central1-a", "worker-1"
    )

    credential = DefaultCredentialAcquirer(settings_for(target)).acquire(target)

    assert calls == [("https://www.googleapis.com/auth/compute.readonly",)]
    assert credential.token == "fixture-google-bearer-token"
    assert credential.expires_at == FIXTURE_EXPIRY


def test_gcp_service_account_json_uses_fixed_token_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a service-account JSON object with Google's fixed token URI is accepted."""

    from google.oauth2 import service_account

    seen: list[dict[str, Any]] = []

    def fake_from_info(
        cls: type[Any], info: dict[str, Any], *, scopes: list[str]
    ) -> FakeGcpCredentials:
        seen.append({"info": dict(info), "scopes": list(scopes)})
        return FakeGcpCredentials(scopes)

    monkeypatch.setattr(
        service_account.Credentials,
        "from_service_account_info",
        classmethod(fake_from_info),
    )
    secret = json.dumps(
        {"type": "service_account", "token_uri": "https://oauth2.googleapis.com/token"}
    )
    target = GcpInstanceTarget(
        "gce-prod", "GCE worker", "project-fixture", "us-central1-a", "worker-1"
    )
    settings = settings_for(target, gcp_mode="service_account_json", gcp_secret=secret)

    credential = DefaultCredentialAcquirer(settings).acquire(target)

    assert seen[0]["scopes"] == ["https://www.googleapis.com/auth/compute.readonly"]
    assert credential.token == "fixture-google-bearer-token"
    assert secret not in repr(credential)

    settings = settings_for(
        target,
        gcp_mode="service_account_json",
        gcp_secret=json.dumps(
            {"type": "service_account", "token_uri": "https://attacker.invalid/token"}
        ),
    )
    with pytest.raises(CloudProviderFailure, match="INVALID_CREDENTIAL_CONFIGURATION"):
        DefaultCredentialAcquirer(settings).acquire(target)


def test_missing_gcp_credential_ref_is_reported_without_ambient_discovery() -> None:
    """Service-account mode does not fall through to developer or CLI credentials."""

    target = GcpInstanceTarget(
        "gce-prod", "GCE worker", "project-fixture", "us-central1-a", "worker-1"
    )
    settings = settings_for(target, gcp_mode="service_account_json")

    with pytest.raises(CredentialMissingError, match="service-account JSON is missing"):
        DefaultCredentialAcquirer(settings).acquire(target)
