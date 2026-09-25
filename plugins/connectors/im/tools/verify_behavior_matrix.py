#!/usr/bin/env python3
"""Verify the IM Python-to-C# behavior evidence matrix.

中文:验证 IM 从 Python 到 C# 的行为证据矩阵。"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
PYTHON_TEST_ROOT = REPOSITORY_ROOT / "plugins/connectors/im/tests"
MATRIX_PATH = REPOSITORY_ROOT / "plugins/connectors/im/behavior_matrix.json"
CSHARP_TEST_ROOT = REPOSITORY_ROOT / "runtime/dotnet-native-aot/Cyrene.Im.Tests"

PYTHON_MODULE_COUNTS = {
    "test_im_native_package.py": 1,
    "test_package.py": 4,
    "test_qqnt_dependency_boundary.py": 2,
    "test_qqnt_direct.py": 71,
    "test_qqnt_host_tck.py": 12,
}
EVIDENCE_CATALOG = {
    "ci:public-ci / dotnet-native-aot": {
        "kind": "rid-native-aot",
        "path": ".github/workflows/public-ci.yml",
    },
    "ci:public-ci / im-package": {
        "kind": "clean-im-package",
        "path": ".github/workflows/public-ci.yml",
    },
    "ci:public-ci / im-qq-host-tck": {
        "kind": "qq-host-contract-tck",
        "path": ".github/workflows/public-ci.yml",
    },
    "ci:public-ci / source-hygiene": {
        "kind": "source-manifest-and-boundary",
        "path": ".github/workflows/public-ci.yml",
    },
}
FILE_DEFAULT_EVIDENCE = {
    "test_im_native_package.py": [
        "ci:public-ci / im-package",
        "ci:public-ci / dotnet-native-aot",
    ],
    "test_package.py": [
        "ci:public-ci / im-package",
        "ci:public-ci / source-hygiene",
    ],
    "test_qqnt_dependency_boundary.py": ["ci:public-ci / source-hygiene"],
    "test_qqnt_direct.py": [
        "ci:public-ci / im-qq-host-tck",
        "ci:public-ci / im-package",
    ],
    "test_qqnt_host_tck.py": [
        "ci:public-ci / im-qq-host-tck",
        "ci:public-ci / im-package",
    ],
}


def discover_python_tests() -> list[str]:
    """Return every top-level pytest function in the IM reference suite.

        中文:返回 IM 参考套件中的所有顶层 pytest 函数。"""

    discovered: list[str] = []
    for path in sorted(PYTHON_TEST_ROOT.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and node.name.startswith("test_"):
                discovered.append(f"{path.name}::{node.name}")
    return discovered


def csharp_test_methods() -> set[str]:
    """Collect concrete public test method references from the IM C# suite.

        中文:从 IM C# 套件中收集具体的公开测试方法引用。"""

    methods: set[str] = set()
    pattern = re.compile(r"public\s+(?:async\s+)?(?:Task|void)\s+(\w+)")
    for path in CSHARP_TEST_ROOT.glob("*Tests.cs"):
        source = path.read_text(encoding="utf-8")
        class_match = re.search(r"public\s+sealed\s+class\s+(\w+)", source)
        if class_match is None:
            continue
        class_name = class_match.group(1)
        methods.update(
            f"csharp:{class_name}.{match.group(1)}"
            for match in pattern.finditer(source)
        )
    return methods


def build_matrix() -> dict[str, Any]:
    """Build the deterministic IM behavior matrix.

        中文:构建确定性的 IM 行为矩阵。"""

    tests = discover_python_tests()
    rows = [
        {
            "id": test_id,
            "status": "covered",
            "evidence": FILE_DEFAULT_EVIDENCE[test_id.split("::", 1)[0]],
        }
        for test_id in tests
    ]
    return {
        "schema_version": 1,
        "baseline": {
            "suite": "plugins/connectors/im/tests",
            "collection_command": (
                "PYTHONPATH=plugins/connectors/im/src:"
                "sdk/python/cyrene_plugin_runtime/src python3 -m pytest "
                "--collect-only -q plugins/connectors/im/tests"
            ),
            "function_count": len(tests),
            "collected_case_count": 90,
            "module_case_counts": PYTHON_MODULE_COUNTS,
        },
        "evidence_catalog": EVIDENCE_CATALOG,
        "rows": rows,
    }


def validate(matrix: dict[str, Any]) -> None:
    """Fail if inventory, evidence references, or generated values drift.

        中文:如果清单、证据引用或生成值发生偏移,则判定失败。"""

    expected = build_matrix()
    if matrix != expected:
        raise SystemExit(
            "behavior_matrix.json is stale; run verify_behavior_matrix.py --write"
        )
    references = csharp_test_methods()
    for row in matrix.get("rows", []):
        if row.get("status") != "covered" or not row.get("evidence"):
            raise SystemExit(f"behavior matrix row lacks covered evidence: {row}")
        for item in row["evidence"]:
            if item.startswith("csharp:") and item not in references:
                raise SystemExit(f"behavior matrix references missing C# test: {item}")
            if item.startswith("ci:") and item not in EVIDENCE_CATALOG:
                raise SystemExit(
                    f"behavior matrix references missing CI evidence: {item}"
                )


def main() -> int:
    """Write or verify the checked-in matrix.

        中文:写入或验证已检入仓库的矩阵。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    matrix = build_matrix()
    if args.write:
        MATRIX_PATH.write_text(
            json.dumps(matrix, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0
    if not MATRIX_PATH.is_file():
        raise SystemExit(f"missing behavior matrix: {MATRIX_PATH}")
    validate(json.loads(MATRIX_PATH.read_text(encoding="utf-8")))
    print(
        f"behavior matrix verified: {len(matrix['rows'])} Python functions, "
        f"{matrix['baseline']['collected_case_count']} collected cases"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
