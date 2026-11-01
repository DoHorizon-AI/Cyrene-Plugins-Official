"""Package descriptor and configuration schema checks.

中文：确保独立插件制品元数据完整并限制配置字段。
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
    assert manifest["id"] == "cyrene.providers.knowledge-retrieval"
    assert manifest["licenseExpression"] == "Apache-2.0"
    assert manifest["capabilities"] == ["tool.provider.v1"]
    assert {method["name"] for method in manifest["methods"]} == {"list_tools", "call_tool"}
    digest = "sha256:" + "a" * 64
    configuration = {
        "artifact_root": "/var/lib/cyrene/knowledge",
        "trusted_caller": {
            "caller_workload": "navigator.work",
            "audience": "knowledge-retrieval",
            "issuer": "https://identity.example.test",
            "organization_id": "org-1",
            "workspace_id": "workspace-1",
            "verification_key_ref": "knowledge-retrieval/caller-context-verification-key",
        },
        "trusted_principals": [
            {
                "issuer": "https://identity.example.test",
                "subject": "user-1",
                "organization_id": "org-1",
                "workspace_id": "workspace-1",
                "principal_refs": ["user:user-1"],
            }
        ],
        "bundle_registry": [
            {
                "library_selector": "research",
                "bundle_locator": "research.zip",
                "artifact_ref": {
                    "uri": "artifact://sha256/" + "a" * 64,
                    "digest": digest,
                    "size_bytes": 120,
                    "kind": "application/zip",
                },
            }
        ],
        "hostSecretRefs": {
            "knowledge-retrieval/caller-context-verification-key": "KNOWLEDGE_CONTEXT_KEY"
        },
    }
    jsonschema.validate(configuration, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {**configuration, "untrusted_path": "/tmp"},
            schema,
        )
