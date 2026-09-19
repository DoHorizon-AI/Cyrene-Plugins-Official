"""Tests for the immutable Python reference artifact builder."""

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
    """The reference archive records provenance and stays self-contained."""

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    output = tmp_path / "reference.zip"
    BUILDER.build_reference_archive(REPOSITORY_ROOT, output)

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "reference-manifest.json" in names
        assert "src/onebot_v11_connector/plugin.py" in names
        assert "src/cyrene_plugin_runtime/bootstrap.py" in names
        assert not any(name.endswith(".pyc") for name in names)
        manifest = json.loads(archive.read("reference-manifest.json"))

    assert manifest["schema"] == "cyrene.onebot.python-reference.v1"
    assert manifest["plugin_id"] == "cyrene.connectors.onebot-v11"
    assert manifest["reference_runtime"] == "python"
    assert manifest["formal_runtime"] == "csharp-native-aot"
    assert manifest["qqnt_real_smoke"] == "NOT_RUN"
    assert len(manifest["source_revision"]) == 40
    assert all(entry["path"] for entry in manifest["entries"])


def test_reference_archive_is_deterministic(tmp_path: Path, monkeypatch) -> None:
    """The same source and epoch produce byte-identical reference archives."""

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    BUILDER.build_reference_archive(REPOSITORY_ROOT, first)
    BUILDER.build_reference_archive(REPOSITORY_ROOT, second)

    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()
