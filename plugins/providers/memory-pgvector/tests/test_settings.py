"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_settings.py                                                 │
│  Module: memory_pgvector_provider.tests                              │
│  Role: Check closed activation config and secret-safe diagnostics.   │
│                                                                      │
│  模块职责：验证封闭激活配置和 secret 安全处理。                       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json

import pytest

from memory_pgvector_provider.settings import ConfigurationError, ProviderSettings

_DSN = "postgresql://cyrene_test:private-password@postgres.internal:5432/cyrene_test"


def _environment(config: dict[str, object], *, dsn: str = _DSN) -> dict[str, str]:
    return {
        "CYRENE_CAPABILITY_BINDING_ID": "binding-memory-test",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(config),
        "CYRENE_MEMORY_PGVECTOR_DSN": dsn,
    }


def _config() -> dict[str, object]:
    return {
        "database_dsn_ref": "memory-pgvector/database-dsn",
        "embedding_model_id": "embed-v1",
        "embedding_dimensions": 768,
        "hostSecretRefs": {"memory-pgvector/database-dsn": "CYRENE_MEMORY_PGVECTOR_DSN"},
    }


def test_dsn_is_resolved_from_stable_host_secret_ref_and_hidden_from_repr() -> None:
    settings = ProviderSettings.from_environment(_environment(_config()))
    assert settings.database_dsn == _DSN
    assert "private-password" not in repr(settings)
    assert settings.pool_min_size == 1
    assert settings.pool_max_size == 8


@pytest.mark.parametrize(
    "config",
    [
        {**_config(), "database_dsn": _DSN},
        {**_config(), "embedding_dimensions": 4097},
        {
            **_config(),
            "hostSecretRefs": {
                **_config()["hostSecretRefs"],
                "unexpected/secret": "UNEXPECTED_SECRET",
            },
        },
        {**_config(), "database_dsn_ref": "other/database"},
    ],
)
def test_invalid_or_ambiguous_activation_fails_closed(config: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        ProviderSettings.from_environment(_environment(config))


def test_missing_secret_value_does_not_reveal_or_accept_dsn() -> None:
    with pytest.raises(ConfigurationError, match="secret is unavailable"):
        ProviderSettings.from_environment(_environment(_config(), dsn=""))


def test_query_and_document_prefixes_require_a_stable_vector_profile() -> None:
    config = {
        **_config(),
        "embedding_document_prefix": "passage: ",
        "embedding_query_prefix": "query: ",
    }
    with pytest.raises(ConfigurationError, match="embedding_profile_id"):
        ProviderSettings.from_environment(_environment(config))

    configured = ProviderSettings.from_environment(
        _environment(
            {
                **config,
                "embedding_profile_id": "embed-v1-query-passage-v1",
            }
        )
    )
    assert configured.embedding_model_id == "embed-v1"
    assert configured.vector_profile_id == "embed-v1-query-passage-v1"


def test_model_provider_selector_stays_separate_from_vector_profile() -> None:
    configured = ProviderSettings.from_environment(
        _environment(
            {
                **_config(),
                "embedding_profile_id": "embed-v1-query-passage-v1",
                "embedding_document_prefix": "passage: ",
                "embedding_query_prefix": "query: ",
            }
        )
    )
    assert configured.embedding_model_id == "embed-v1"
    assert configured.vector_profile_id == "embed-v1-query-passage-v1"
    assert configured.embedding_document_prefix == "passage: "
    assert configured.embedding_query_prefix == "query: "
