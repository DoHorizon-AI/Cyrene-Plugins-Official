"""
┌────────────────────────────────────────────────────────────────────────────┐
│  File: test_contract_schema.py                                             │
│  Module: account_center.tests.contract_schema                              │
│  Role: Verify manifest and local JSON Schema references resolve.            │
│                                                                            │
│  模块职责：校验 Account Center 方法声明及 JSON Schema 的本地引用可解析。       │
└────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def test_manifest_and_account_schemas_resolve_all_local_definition_refs() -> None:
    """Keep every manifest method and nested schema reference loadable."""

    package_root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (package_root / "plugin.manifest.json").read_text(encoding="utf-8")
    )
    schema = json.loads(
        (package_root / "contracts/v1/schema.json").read_text(encoding="utf-8")
    )
    definitions = schema["$defs"]

    for method in manifest["methods"]:
        for field in ("inputSchema", "outputSchema"):
            definition_ref = method[field].split("#", maxsplit=1)[1]
            assert definition_ref.startswith("/$defs/")
            assert definition_ref.removeprefix("/$defs/") in definitions

    pending: list[Any] = [definitions]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/"):
                current: Any = schema
                for part in reference.removeprefix("#/").split("/"):
                    current = current[part]
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
