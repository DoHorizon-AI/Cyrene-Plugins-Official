"""Tests for the immutable Python reference artifact builder.

中文：不可变 Python 参考产物构建器的测试。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

TOOLS_ROOT = Path(__file__).parents[1] / "tools"
BUILDER_PATH = Path(__file__).parents[4] / "tools/ci/assemble_onebot_reference.py"
SPEC = importlib.util.spec_from_file_location("onebot_reference_builder", BUILDER_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError("cannot load OneBot reference artifact builder")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)

REPOSITORY_ROOT = Path(__file__).parents[4]


def test_reference_archive_contains_provenance_and_python_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The reference archive records provenance and stays self-contained.

        中文：参考归档会记录来源信息并保持自包含。"""

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    output = tmp_path / "reference.zip"
    BUILDER.build_reference_archive(REPOSITORY_ROOT, output)

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "reference-manifest.json" in names
        assert "reference-sbom.json" in names
        assert "reference-build-proof.json" in names
        assert "reference-runner/onebot_reference_parity.py" in names
        assert "src/onebot_v11_connector/plugin.py" in names
        assert "src/cyrene_plugin_runtime/bootstrap.py" in names
        assert not any(name.endswith(".pyc") for name in names)
        manifest = json.loads(archive.read("reference-manifest.json"))

    assert manifest["schema"] == "cyrene.onebot.python-reference.v1"
    assert manifest["plugin_id"] == "cyrene.connectors.onebot-v11"
    assert manifest["reference_runtime"] == "python"
    assert manifest["formal_runtime"] == "csharp-native-aot"
    assert manifest["onebot_real_smoke"] == "NOT_RUN"
    assert len(manifest["source_revision"]) == 40
    assert all(entry["path"] for entry in manifest["entries"])

    with zipfile.ZipFile(output) as archive:
        sbom = json.loads(archive.read("reference-sbom.json"))
        proof = json.loads(archive.read("reference-build-proof.json"))
    assert sbom["schema"] == "cyrene.onebot.python-reference-sbom.v1"
    assert proof["schema"] == "cyrene.onebot.python-reference-build-proof.v1"
    assert proof["source_revision"] == manifest["source_revision"]


def test_reference_archive_is_deterministic(tmp_path: Path, monkeypatch) -> None:
    """The same source and epoch produce byte-identical reference archives.

        中文：相同源文件和纪元会生成字节完全相同的参考归档。"""

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    BUILDER.build_reference_archive(REPOSITORY_ROOT, first)
    BUILDER.build_reference_archive(REPOSITORY_ROOT, second)

    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()
