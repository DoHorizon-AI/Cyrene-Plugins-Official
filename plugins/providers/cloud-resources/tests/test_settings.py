"""Binding allowlist and secret-reference tests for the cloud provider.

中文:验证云提供方绑定白名单和主机密钥引用。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cloud_resources_provider.settings import (
    ConfigurationError,
    CredentialMissingError,
    ProviderSettings,
    RunpodPodTarget,
    TailscaleDeviceTarget,
)

EXCHANGE_KEY = "11" * 32


def base_configuration(targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a safe default binding for fixture-only provider tests."""

    return {
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
        "targets": targets,
        "hostSecretRefs": {"cloud-resources/exchange-verification-key": "TEST_EXCHANGE_KEY"},
    }


def environment(config: dict[str, Any]) -> dict[str, str]:
    """Build host activation variables without placing secret values in config."""

    return {
        "CYRENE_CAPABILITY_BINDING_ID": "cloud.binding.fixture",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(config),
        "TEST_EXCHANGE_KEY": EXCHANGE_KEY,
    }


def azure_target(target_ref: str = "vm-prod") -> dict[str, str]:
    """Return one fixed Azure VM target with fixture-only identity values."""

    return {
        "target_ref": target_ref,
        "display_name": "Production VM",
        "provider": "azure",
        "kind": "azure.vm",
        "subscription_id": "11111111-2222-3333-4444-555555555555",
        "resource_group": "compute-prod",
        "vm_name": "worker-01",
    }


def test_binding_resolves_only_opaque_configured_target_refs() -> None:
    """A query selector resolves to a fixed Azure identity, never a URL."""

    config = base_configuration([azure_target()])
    settings = ProviderSettings.from_environment(environment(config))

    target = settings.target("vm-prod")
    assert target.provider == "azure"
    assert target.kind == "azure.vm"
    assert settings.authorization_generation == 0
    assert settings.trusted_callers["exchange.integrations"].organization_id == "org-fixture"
    assert EXCHANGE_KEY not in repr(settings)
    with pytest.raises(ValueError, match="not configured"):
        settings.target("https://management.azure.com/subscriptions/other")


def test_unknown_target_fields_and_duplicate_selectors_fail_closed() -> None:
    """The activation boundary rejects extensible targets and ambiguous selectors."""

    unexpected = azure_target()
    unexpected["url"] = "https://attacker.invalid/"
    with pytest.raises(ConfigurationError, match="unsupported or missing fields"):
        ProviderSettings.from_environment(environment(base_configuration([unexpected])))

    duplicate = base_configuration([azure_target(), azure_target()])
    with pytest.raises(ConfigurationError, match="unique opaque selectors"):
        ProviderSettings.from_environment(environment(duplicate))


def test_credential_refs_are_exact_and_secret_values_stay_out_of_activation_json() -> None:
    """Only stable refs map to host variable names and the HMAC key stays private."""

    config = base_configuration([azure_target()])
    config["hostSecretRefs"]["cloud-resources/unused"] = "TEST_UNUSED"
    with pytest.raises(ConfigurationError, match="exactly the configured secret refs"):
        ProviderSettings.from_environment(environment(config))

    values = environment(base_configuration([azure_target()]))
    values["TEST_EXCHANGE_KEY"] = ""
    with pytest.raises(CredentialMissingError, match="verification key is missing"):
        ProviderSettings.from_environment(values)


def test_tailscale_target_requires_fixed_oauth_secret_reference() -> None:
    """Tailscale device support is bound to one OAuth credential and fixed device."""

    target = {
        "target_ref": "device-prod",
        "display_name": "Build runner",
        "provider": "tailscale",
        "kind": "tailscale.device",
        "tailnet": "example.com",
        "device_id": "device-42",
    }
    config = base_configuration([target])
    config["tailscale_auth"] = {
        "mode": "oauth_client",
        "client_id": "client-fixture",
        "client_secret_ref": "cloud-resources/tailscale-client-secret",
        "tailnet": "example.com",
    }
    config["hostSecretRefs"]["cloud-resources/tailscale-client-secret"] = "TEST_TS_SECRET"
    values = environment(config)
    values["TEST_TS_SECRET"] = "private-tailscale-client-secret"
    settings = ProviderSettings.from_environment(values)

    assert isinstance(settings.target("device-prod"), TailscaleDeviceTarget)
    assert settings.tailscale_tailnet == "example.com"
    assert settings.secret_value("cloud-resources/tailscale-client-secret") == (
        "private-tailscale-client-secret"
    )
    assert "private-tailscale-client-secret" not in repr(settings)

    config["tailscale_auth"]["client_secret_ref"] = "cloud-resources/other-secret"
    with pytest.raises(ConfigurationError, match="stable manifest ref"):
        ProviderSettings.from_environment(environment(config))

    config["tailscale_auth"]["client_secret_ref"] = "cloud-resources/tailscale-client-secret"
    config["targets"][0]["tailnet"] = "other.example.com"
    with pytest.raises(ConfigurationError, match="must match the configured OAuth binding"):
        ProviderSettings.from_environment(environment(config))


def test_runpod_target_requires_a_host_injected_read_only_key_ref() -> None:
    """Runpod activation accepts only the documented stable API-key reference."""

    target = {
        "target_ref": "pod-prod",
        "display_name": "Training pod",
        "provider": "runpod",
        "kind": "runpod.pod",
        "pod_id": "pod-42_fixture",
    }
    config = base_configuration([target])
    config["runpod_auth"] = {
        "mode": "read_only_api_key",
        "api_key_ref": "cloud-resources/runpod-api-key",
    }
    config["hostSecretRefs"]["cloud-resources/runpod-api-key"] = "TEST_RUNPOD_KEY"
    values = environment(config)
    values["TEST_RUNPOD_KEY"] = "private-runpod-api-key"
    settings = ProviderSettings.from_environment(values)

    assert isinstance(settings.target("pod-prod"), RunpodPodTarget)
    assert settings.secret_value("cloud-resources/runpod-api-key") == "private-runpod-api-key"
    assert "private-runpod-api-key" not in repr(settings)

    config["runpod_auth"]["mode"] = "full_access_api_key"
    with pytest.raises(ConfigurationError, match="Read Only API-key ref"):
        ProviderSettings.from_environment(environment(config))
