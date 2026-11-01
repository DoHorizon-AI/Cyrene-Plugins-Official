"""Activation configuration validation for the SQLite graph provider.

中文：验证数据库路径、有限资源配置及拒绝未知配置项。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_graph_provider.settings import ConfigurationError, GraphSettings


def test_settings_validate_absolute_database_path_and_bounds(tmp_path: Path) -> None:
    settings = GraphSettings.from_mapping(
        {"database_path": str(tmp_path / "graph.db"), "max_page_size": 64}
    )
    assert settings.database_path == tmp_path / "graph.db"
    assert settings.max_page_size == 64
    assert settings.max_expand_edges == 1_000
    assert settings.max_expand_response_bytes == 1_048_576

    with pytest.raises(ConfigurationError, match="absolute"):
        GraphSettings.from_mapping({"database_path": "relative.db"})
    with pytest.raises(ConfigurationError, match="unsupported"):
        GraphSettings.from_mapping({"database_path": "/tmp/graph.db", "sql": "anything"})
    with pytest.raises(ConfigurationError, match="max_page_size"):
        GraphSettings.from_mapping({"database_path": "/tmp/graph.db", "max_page_size": 1001})
    with pytest.raises(ConfigurationError, match="max_expand_edges"):
        GraphSettings.from_mapping({"database_path": "/tmp/graph.db", "max_expand_edges": 10_001})


def test_settings_load_standard_plugin_environment(tmp_path: Path) -> None:
    database_path = tmp_path / "graph.db"
    settings = GraphSettings.from_environment(
        {
            "CYRENE_CAPABILITY_BINDING_ID": "graph-binding",
            "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(
                {"database_path": str(database_path)}
            ),
        }
    )
    assert settings.database_path == database_path

    with pytest.raises(ConfigurationError, match="binding ID"):
        GraphSettings.from_environment(
            {"CYRENE_CAPABILITY_CONFIGURATION_JSON": '{"database_path":"/tmp/graph.db"}'}
        )
