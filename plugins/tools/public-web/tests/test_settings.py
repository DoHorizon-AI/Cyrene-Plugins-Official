from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from cyrene_public_web.settings import ConfigurationError, PublicWebSettings


def _config(search_endpoint: str) -> dict[str, object]:
    return {
        "trusted_callers": [
            {
                "caller_workload": "cyrene.exchange.integrations",
                "audience": "cyrene.tools.public-web",
                "organization_id": "org-one",
                "workspace_id": "workspace-one",
                "issuer": "https://id.example.test",
                "verification_key_ref": "public-web/caller-verification-key",
            }
        ],
        "search_endpoint": search_endpoint,
        "search_bearer_token_ref": "public-web/search-bearer-token",
        "artifact_bridge_origin": "http://127.0.0.1:45678",
        "loopback_test_origins": ["http://127.0.0.1:45679"],
        "hostSecretRefs": {
            "public-web/caller-verification-key": "PUBLIC_WEB_CALLER_KEY",
            "public-web/cursor-hmac-key": "PUBLIC_WEB_CURSOR_KEY",
            "public-web/state-dir": "PUBLIC_WEB_STATE_DIR",
            "public-web/artifact-bridge-token": "PUBLIC_WEB_ARTIFACT_TOKEN",
            "public-web/search-bearer-token": "PUBLIC_WEB_SEARCH_TOKEN",
        },
    }


def _environment(config: dict[str, object], tmp_path: Path) -> dict[str, str]:
    return {
        "CYRENE_CAPABILITY_BINDING_ID": "public-web-binding",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(config),
        "PUBLIC_WEB_CALLER_KEY": "11" * 32,
        "PUBLIC_WEB_CURSOR_KEY": "22" * 32,
        "PUBLIC_WEB_STATE_DIR": str(tmp_path / "private-state"),
        "PUBLIC_WEB_ARTIFACT_TOKEN": "aa" * 32,
        "PUBLIC_WEB_SEARCH_TOKEN": "peer-search-secret",
    }


def test_activation_accepts_server_set_search_and_exact_loopback_test_origin(tmp_path) -> None:
    config = _config("http://127.0.0.1:45679/search")
    schema = json.loads((Path(__file__).parents[1] / "configuration.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(config)

    settings = PublicWebSettings.from_environment(_environment(config, tmp_path))

    assert settings.search_endpoint == "http://127.0.0.1:45679/search"
    assert settings.search_bearer_token == "peer-search-secret"
    assert settings.loopback_test_origins == frozenset({"http://127.0.0.1:45679"})
    assert settings.trusted_callers[0].verification_key == bytes.fromhex("11" * 32)
    assert settings.state_dir.stat().st_mode & 0o077 == 0


def test_activation_rejects_private_search_endpoint_outside_explicit_test_origin(tmp_path) -> None:
    config = _config("http://10.0.0.5:45679/search")
    with pytest.raises(ConfigurationError, match="private host"):
        PublicWebSettings.from_environment(_environment(config, tmp_path))


def test_activation_rejects_model_selectable_endpoint_configuration(tmp_path) -> None:
    config = _config("https://search.example.test/api")
    config["api_origin"] = "https://model-selected.invalid"
    with pytest.raises(ConfigurationError, match="unsupported fields"):
        PublicWebSettings.from_environment(_environment(config, tmp_path))
