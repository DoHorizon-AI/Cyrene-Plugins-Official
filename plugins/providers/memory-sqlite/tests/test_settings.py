"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_settings.py                                                 │
│  Module: memory_sqlite_provider.tests                                │
│  Role: Check embedding profile and query/document transform settings.│
│                                                                      │
│  模块职责：验证 embedding profile 与 query/document 前缀配置。        │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json

import pytest

from memory_sqlite_provider.settings import ConfigurationError, ProviderSettings


def _set_environment(monkeypatch: pytest.MonkeyPatch, configuration: dict[str, object]) -> None:
    monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", "memory-test-binding")
    monkeypatch.setenv("CYRENE_CAPABILITY_CONFIGURATION_JSON", json.dumps(configuration))


def _configuration() -> dict[str, object]:
    return {
        "database_path": "/tmp/cyrene-memory-test.sqlite3",
        "embedding_model_id": "intfloat/e5-small-v2",
        "embedding_dimensions": 3,
    }


def test_model_prefixes_require_a_distinct_profile_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(
        monkeypatch,
        {**_configuration(), "embedding_document_prefix": "passage: "},
    )
    with pytest.raises(ConfigurationError, match="embedding_profile_id"):
        ProviderSettings.from_environment()

    _set_environment(
        monkeypatch,
        {
            **_configuration(),
            "embedding_profile_id": "intfloat/e5-small-v2-query-passage-v1",
            "embedding_document_prefix": "passage: ",
            "embedding_query_prefix": "query: ",
        },
    )
    settings = ProviderSettings.from_environment()
    assert settings.vector_profile_id == "intfloat/e5-small-v2-query-passage-v1"
    assert settings.embedding_model_id == "intfloat/e5-small-v2"
    assert settings.embedding_document_prefix == "passage: "
    assert settings.embedding_query_prefix == "query: "


def test_default_profile_is_the_upstream_model_when_no_transform_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch, _configuration())
    settings = ProviderSettings.from_environment()
    assert settings.vector_profile_id == settings.embedding_model_id
