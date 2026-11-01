"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_package_metadata.py                                         │
│  Module: memory_sqlite_provider.tests                                │
│  Role: Validate the independent package descriptor and closed config. │
│                                                                      │
│  模块职责：验证独立包描述和封闭式激活配置。                            │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

PACKAGE_ROOT = Path(__file__).parents[1]


def test_manifest_declares_all_seven_methods_and_state_retention() -> None:
    manifest = json.loads((PACKAGE_ROOT / "plugin.manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "cyrene.providers.memory-sqlite"
    assert manifest["capabilities"] == ["memory.provider.v1"]
    assert {method["name"] for method in manifest["methods"]} == {
        "store",
        "get",
        "recall",
        "delete",
        "prune",
        "export_batch",
        "import_batch",
    }
    assert manifest["state"]["isStateful"] is True
    assert manifest["state"]["retentionPolicyOnUninstall"] == "retain"


def test_activation_schema_requires_model_profile_and_rejects_nonlocal_endpoint() -> None:
    schema = json.loads((PACKAGE_ROOT / "configuration.schema.json").read_text(encoding="utf-8"))
    valid = {
        "database_path": "/var/lib/cyrene/memory.sqlite3",
        "embedding_model_id": "embed-v1",
        "embedding_dimensions": 768,
        "embedding_endpoint": "grpc://127.0.0.1:18720",
    }
    jsonschema.validate(valid, schema)
    assert list(jsonschema.Draft202012Validator(schema).iter_errors({**valid, "other": "unused"}))


def test_activation_schema_rejects_missing_profile_and_remote_endpoint() -> None:
    schema = json.loads((PACKAGE_ROOT / "configuration.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    assert list(validator.iter_errors({"database_path": "/tmp/memory.sqlite3"}))
    invalid = {
        "database_path": "/var/lib/cyrene/memory.sqlite3",
        "embedding_model_id": "embed-v1",
        "embedding_dimensions": 768,
        "embedding_endpoint": "grpc://model-service:18720",
    }
    assert list(validator.iter_errors(invalid))
    prefixed = {
        **invalid,
        "embedding_endpoint": "grpc://127.0.0.1:18720",
        "embedding_document_prefix": "passage: ",
    }
    assert list(validator.iter_errors(prefixed))
    jsonschema.validate({**prefixed, "embedding_profile_id": "embed-v1-passage-v1"}, schema)
