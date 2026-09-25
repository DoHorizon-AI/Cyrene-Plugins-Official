"""Tests for the QQNT direct dependency boundary gate.

中文:QQNT 直连依赖边界门禁测试。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parents[1] / "tools"
_TOOL_PATH = TOOLS_ROOT / "verify_direct_dependency_boundary.py"
_SPEC = importlib.util.spec_from_file_location(
    "verify_direct_dependency_boundary", _TOOL_PATH
)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"cannot load dependency boundary tool: {_TOOL_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
scan_root = _MODULE.scan_root


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def test_current_direct_dependency_boundary_is_clean() -> None:
    """The checked-in direct runtime has no forbidden dependency surface.

        中文:已检入的直连运行时不包含禁止的依赖接口。"""

    assert scan_root(REPOSITORY_ROOT) == []


def test_boundary_rejects_forbidden_import_and_lineage(tmp_path: Path) -> None:
    """A copied third-party runtime cannot silently enter the direct profile.

        中文:复制的第三方运行时不能悄然进入直连配置。"""

    connector_root = tmp_path / "plugins/connectors/im"
    direct_source = connector_root / "src/qq_connector"
    direct_source.mkdir(parents=True)
    (direct_source / "qqnt_direct.py").write_text(
        "import socket\n# napcat runtime\n", encoding="utf-8"
    )
    for relative, content in {
        "pyproject.toml": "[project]\ndependencies = []\n",
        "requirements.lock": "protobuf==4.25.9\n",
        "plugin.manifest.json": "{}\n",
        "package-descriptor.json": "{}\n",
    }.items():
        (connector_root / relative).write_text(content, encoding="utf-8")

    failures = scan_root(tmp_path)

    assert any("forbidden third-party lineage" in failure for failure in failures)
    assert any("forbidden import socket" in failure for failure in failures)
