"""Package descriptor and configuration schema checks.

中文：确保插件声明全部图能力并限制 activation 配置。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_and_configuration_schema_are_valid() -> None:
    manifest = json.loads((ROOT / "plugin.manifest.json").read_text())
    schema = json.loads((ROOT / "configuration.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    assert manifest["id"] == "cyrene.providers.knowledge-graph"
    assert manifest["licenseExpression"] == "Apache-2.0"
    assert manifest["capabilities"] == ["knowledge.graph.v1"]
    methods = {method["name"] for method in manifest["methods"]}
    assert methods == {
        "upsert_nodes",
        "get_node",
        "search_nodes",
        "delete_node",
        "upsert_edges",
        "get_edge",
        "search_edges",
        "delete_edge",
        "expand",
        "import_batch",
        "export_batch",
    }
    jsonschema.validate({"database_path": "/var/lib/cyrene/graph.db"}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"database_path": "/var/lib/cyrene/graph.db", "arbitrary": "value"},
            schema,
        )
