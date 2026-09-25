"""Tests for the isolated IM Native AOT package candidate.

中文：隔离 IM Native AOT 候选包的测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

TOOLS_ROOT = Path(__file__).parents[1] / "tools"
_SPEC = importlib.util.spec_from_file_location(
    "im_native_package_builder", TOOLS_ROOT / "assemble_native_package.py"
)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("cannot load IM Native AOT package builder")
_BUILDER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BUILDER)
assemble_native_package = _BUILDER.assemble_native_package

REPOSITORY_ROOT = Path(__file__).parents[4]


def _executable(tmp_path: Path) -> Path:
    """Use the current interpreter as an executable-only build fixture.

        中文：使用当前解释器作为仅供执行的构建夹具。"""

    binary = tmp_path / "cyrene-im"
    binary.write_bytes(Path(sys.executable).read_bytes())
    binary.chmod(0o755)
    return binary


def test_im_native_candidate_contains_only_native_runtime_and_resolvable_refs(
    tmp_path: Path,
) -> None:
    """The IM package is native-only and contains both capability contracts.

        中文：IM 软件包仅包含原生组件，并包含两份能力契约。"""

    package_root = assemble_native_package(
        REPOSITORY_ROOT, _executable(tmp_path), tmp_path / "package", "linux-x64"
    )

    assert (package_root / "bin/cyrene-im").is_file()
    assert (package_root / "QQNT_DIRECT_PROTOCOL.md").is_file()
    assert not list(package_root.rglob("*.py"))
    assert not list(package_root.rglob("*.pyc"))
    assert not (package_root / "src").exists()

    manifest = json.loads((package_root / "plugin.manifest.json").read_text())
    assert manifest["id"] == "cyrene.connectors.im"
    assert manifest["version"] == "0.1.0"
    assert manifest["capabilities"] == ["message.connector.v1", "qq.client.v1"]
    assert manifest["runtime"]["launch"] == {"executable": "bin/cyrene-im"}
    assert manifest["compatibility"]["architectures"] == ["x86_64"]
    for method in manifest["methods"]:
        for key in ("inputSchema", "outputSchema"):
            reference = method.get(key)
            if not isinstance(reference, str):
                continue
            assert (package_root / reference.split("#", 1)[0]).is_file()

    descriptor = json.loads((package_root / "package-descriptor.json").read_text())
    assert descriptor["runtime"]["kind"] == "native-executable"
    assert descriptor["runtime"]["entrypoint"] == "bin/cyrene-im"
    assert descriptor["compatibility"]["capability_interface"] == [
        "message.connector.v1@1",
        "qq.client.v1@1",
    ]
    assert "subprocess-python" not in json.dumps(descriptor)
