"""Tests for the installable OneBot/QQNT direct package candidate."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

TOOLS_ROOT = Path(__file__).parents[1] / "tools"
_BUILDER_SPEC = importlib.util.spec_from_file_location(
    "onebot_package_builder", TOOLS_ROOT / "assemble_package.py"
)
if _BUILDER_SPEC is None or _BUILDER_SPEC.loader is None:
    raise ImportError("cannot load OneBot package builder")
_BUILDER = importlib.util.module_from_spec(_BUILDER_SPEC)
_BUILDER_SPEC.loader.exec_module(_BUILDER)
assemble_package = _BUILDER.assemble_package
build_package_archive = _BUILDER.build_package_archive


REPOSITORY_ROOT = Path(__file__).parents[4]


def test_assembled_package_contains_runtime_and_resolvable_schema_refs(
    tmp_path: Path,
) -> None:
    """The unpacked Platform payload must run without a source checkout."""

    package_root = assemble_package(REPOSITORY_ROOT, tmp_path / "package")
    assert (package_root / "src/cyrene_plugin_runtime/bootstrap.py").is_file()
    assert (package_root / "src/onebot_v11_connector/plugin.py").is_file()
    assert not list(package_root.rglob("__pycache__"))
    assert not list(package_root.rglob("*.pyc"))

    manifest = json.loads(
        (package_root / "plugin.manifest.json").read_text(encoding="utf-8")
    )
    for method in manifest["methods"]:
        for key in ("inputSchema", "outputSchema"):
            reference = method.get(key)
            if not isinstance(reference, str) or not reference:
                continue
            relative = reference.split("#", 1)[0]
            assert (package_root / relative).is_file(), (
                method["name"],
                key,
                reference,
            )

    launch = manifest["runtime"]["launch"]
    bootstrap = next(
        argument
        for argument in launch["args"]
        if argument.endswith("src/cyrene_plugin_runtime/bootstrap.py")
    )
    assert (package_root / bootstrap).is_file()

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(package_root / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cyrene_plugin_runtime.bootstrap; "
            "import onebot_v11_connector.plugin",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_package_archive_is_self_contained(tmp_path: Path) -> None:
    """The candidate ZIP must contain the root manifest and both runtimes."""

    archive_path = build_package_archive(REPOSITORY_ROOT, tmp_path / "onebot.zip")
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert "plugin.manifest.json" in names
        assert "package-descriptor.json" in names
        assert "src/cyrene_plugin_runtime/bootstrap.py" in names
        assert "src/onebot_v11_connector/plugin.py" in names
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
        assert (
            "contracts/json/message-connector-v1-inbound-request.schema.json"
            in names
        )

    second_path = build_package_archive(REPOSITORY_ROOT, tmp_path / "onebot-second.zip")
    assert hashlib.sha256(archive_path.read_bytes()).digest() == hashlib.sha256(
        second_path.read_bytes()
    ).digest()


def test_package_builder_rejects_nonempty_output(tmp_path: Path) -> None:
    """A stale staging directory must never be silently overwritten."""

    output = tmp_path / "package"
    output.mkdir()
    (output / "unexpected.txt").write_text("sentinel", encoding="utf-8")
    with pytest.raises(ValueError, match="must be empty"):
        assemble_package(REPOSITORY_ROOT, output)
