"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_package_metadata.py                                         │
│  Module: memory_pgvector_provider.tests                              │
│  Role: Validate the standalone manifest and closed activation schema.│
│                                                                      │
│  模块职责：验证独立包 manifest 和封闭激活 schema。                    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

PACKAGE_ROOT = Path(__file__).parents[1]


def test_manifest_declares_all_methods_and_retained_state() -> None:
    manifest = json.loads((PACKAGE_ROOT / "plugin.manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "cyrene.providers.memory-pgvector"
    assert manifest["capabilities"] == ["memory.provider.v1"]
    assert {item["name"] for item in manifest["methods"]} == {
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


def test_schema_restricts_pgvector_dimensions_and_requires_secret_ref() -> None:
    schema = json.loads((PACKAGE_ROOT / "configuration.schema.json").read_text(encoding="utf-8"))
    valid = {
        "database_dsn_ref": "memory-pgvector/database-dsn",
        "embedding_model_id": "embed-v1",
        "embedding_dimensions": 768,
        "hostSecretRefs": {"memory-pgvector/database-dsn": "CYRENE_MEMORY_PGVECTOR_DSN"},
    }
    jsonschema.validate(valid, schema)
    jsonschema.validate({**valid, "embedding_dimensions": 4096}, schema)
    invalid = {**valid, "embedding_dimensions": 4097}
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(invalid))
    prefixed = {**valid, "embedding_document_prefix": "passage: "}
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(prefixed))
    jsonschema.validate({**prefixed, "embedding_profile_id": "embed-v1-passage-v1"}, schema)
