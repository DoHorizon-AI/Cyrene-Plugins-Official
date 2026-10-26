"""Closed activation and repository-scope validation coverage.

No test setting contains or resolves a Hugging Face token. 中文：provider 本身不接收 HF token。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from huggingface_provider.settings import (
    ConfigurationError,
    ProviderSettings,
    RepositoryScope,
    repository_scope_sha256,
)


def valid_config() -> tuple[dict[str, str], dict[str, object]]:
    """Return a local binding with independent Exchange and provider HMAC refs."""

    environment = {
        "CYRENE_CAPABILITY_BINDING_ID": "hf-binding",
        "EXCHANGE_KEY": (b"e" * 32).hex(),
        "PROVIDER_KEY": (b"p" * 32).hex(),
    }
    config: dict[str, object] = {
        "exchange_caller_id": "exchange",
        "exchange_verification_key_ref": "huggingface/exchange-verification-key",
        "account_center": {
            "binding_id": "account-center-binding",
            "connection_ref": "grpc://127.0.0.1:40000",
            "transport": "local",
            "caller_id": "huggingface-provider",
            "signing_key_ref": "huggingface/account-center-provider-signing-key",
        },
        "repositories": [
            {"repo_type": "model", "repo_id": "owner/model", "permissions": ["read", "write"]}
        ],
        "hostSecretRefs": {
            "huggingface/exchange-verification-key": "EXCHANGE_KEY",
            "huggingface/account-center-provider-signing-key": "PROVIDER_KEY",
        },
    }
    environment["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)
    return environment, config


def test_activation_resolves_only_service_keys_and_allowlisted_repo_scope() -> None:
    environ, _config = valid_config()

    settings = ProviderSettings.from_environment(environ)

    assert settings.binding_id == "hf-binding"
    assert settings.read_only is False
    assert settings.account_center_binding_id == "account-center-binding"
    assert settings.repositories[0].repo_id == "owner/model"
    assert settings.repositories[0].permissions == frozenset({"read", "write"})
    assert settings.exchange_verification_key == b"e" * 32
    assert settings.provider_signing_key == b"p" * 32


def test_read_only_binding_requires_read_only_repository_grants() -> None:
    environ, config = valid_config()
    config["read_only"] = True
    environ["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)

    with pytest.raises(ConfigurationError, match="cannot include write repository grants"):
        ProviderSettings.from_environment(environ)

    config["repositories"] = [
        {"repo_type": "model", "repo_id": "owner/model", "permissions": ["read"]}
    ]
    environ["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)
    settings = ProviderSettings.from_environment(environ)

    assert settings.read_only is True
    assert settings.repositories[0].permissions == frozenset({"read"})

    config["read_only"] = "true"
    environ["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)
    with pytest.raises(ConfigurationError, match="read_only must be boolean"):
        ProviderSettings.from_environment(environ)


def test_read_only_binding_configuration_matches_closed_schema() -> None:
    _environ, config = valid_config()
    config["read_only"] = True
    schema = json.loads(
        (Path(__file__).parents[1] / "configuration.schema.json").read_text(encoding="utf-8")
    )
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(config)

    config["repositories"] = [
        {"repo_type": "model", "repo_id": "owner/model", "permissions": ["read"]}
    ]

    Draft202012Validator(schema).validate(config)


def test_provider_rejects_hf_token_in_its_host_secret_refs() -> None:
    environ, config = valid_config()
    config["hostSecretRefs"]["huggingface/token"] = "HF_TOKEN"
    environ["HF_TOKEN"] = "hf_secret_value"
    environ["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)

    with pytest.raises(ConfigurationError, match="match the configured secret refs"):
        ProviderSettings.from_environment(environ)


def test_activation_rejects_write_permission_for_duplicate_or_unknown_scope() -> None:
    environ, config = valid_config()
    config["repositories"] = [
        {"repo_type": "model", "repo_id": "owner/model", "permissions": ["admin"]}
    ]
    environ["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)

    with pytest.raises(ConfigurationError, match="Hub repository scope is invalid"):
        ProviderSettings.from_environment(environ)


def test_repository_scope_digest_is_order_independent_and_permission_sensitive() -> None:
    model = RepositoryScope("model", "owner/model", frozenset({"write", "read"}))
    dataset = RepositoryScope("dataset", "owner/data", frozenset({"read"}))

    assert repository_scope_sha256((model, dataset)) == repository_scope_sha256(
        (dataset, model)
    )
    assert repository_scope_sha256((model,)) != repository_scope_sha256(
        (RepositoryScope("model", "owner/model", frozenset({"read"})),)
    )
