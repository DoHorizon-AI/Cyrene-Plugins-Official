from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from cyrene_bilibili.settings import ConfigurationError, ProviderSettings


def _configuration() -> dict[str, object]:
    return {
        "trusted_callers": [
            {
                "caller_workload": "cyrene.exchange.integrations",
                "audience": "cyrene.connectors.bilibili",
                "organization_id": "org-one",
                "workspace_id": "workspace-one",
                "issuer": "https://id.example.test",
                "verification_key_ref": "bilibili/exchange-verification-key",
            }
        ],
        "account_center": {
            "binding_id": "account-center-binding",
            "connection_ref": "grpc://127.0.0.1:50051",
            "transport": "local",
            "caller_id": "cyrene.connectors.bilibili",
            "signing_key_ref": "bilibili/account-center-provider-signing-key",
        },
        "hostSecretRefs": {
            "bilibili/exchange-verification-key": "BILI_EXCHANGE_KEY",
            "bilibili/account-center-provider-signing-key": "BILI_AC_KEY",
            "bilibili/state-dir": "BILI_STATE_DIR",
        },
    }


def test_activation_resolves_pinned_actor_scopes_and_private_state(tmp_path) -> None:
    config = _configuration()
    schema = json.loads((Path(__file__).parents[1] / "configuration.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(config)
    env = {
        "CYRENE_CAPABILITY_BINDING_ID": "bili-binding",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(config),
        "BILI_EXCHANGE_KEY": "11" * 32,
        "BILI_AC_KEY": "22" * 32,
        "BILI_STATE_DIR": str(tmp_path / "state"),
    }

    settings = ProviderSettings.from_environment(env)

    assert settings.binding_id == "bili-binding"
    assert settings.trusted_callers[0].caller_workload == "cyrene.exchange.integrations"
    assert settings.trusted_callers[0].verification_key == bytes.fromhex("11" * 32)
    assert settings.provider_signing_key == bytes.fromhex("22" * 32)
    assert settings.state_dir.is_dir()
    assert settings.state_dir.stat().st_mode & 0o077 == 0


def test_activation_rejects_model_or_request_selected_caller_endpoints(tmp_path) -> None:
    config = _configuration()
    config["api_origin"] = "https://attacker.example"
    env = {
        "CYRENE_CAPABILITY_BINDING_ID": "bili-binding",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(config),
        "BILI_EXCHANGE_KEY": "11" * 32,
        "BILI_AC_KEY": "22" * 32,
        "BILI_STATE_DIR": str(tmp_path / "state"),
    }
    with pytest.raises(ConfigurationError, match="unsupported fields"):
        ProviderSettings.from_environment(env)


def test_activation_rejects_untrusted_secret_references(tmp_path) -> None:
    config = _configuration()
    config["trusted_callers"][0]["verification_key_ref"] = "attacker/secret"  # type: ignore[index]
    env = {
        "CYRENE_CAPABILITY_BINDING_ID": "bili-binding",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(config),
        "BILI_EXCHANGE_KEY": "11" * 32,
        "BILI_AC_KEY": "22" * 32,
        "BILI_STATE_DIR": str(tmp_path / "state"),
    }
    with pytest.raises(ConfigurationError, match="key reference"):
        ProviderSettings.from_environment(env)
