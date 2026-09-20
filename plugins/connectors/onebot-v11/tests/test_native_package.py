"""Tests for the isolated Native AOT migration package candidate."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

TOOLS_ROOT = Path(__file__).parents[1] / "tools"
_BUILDER_SPEC = importlib.util.spec_from_file_location(
    "onebot_native_package_builder",
    TOOLS_ROOT / "assemble_native_package.py",
)
if _BUILDER_SPEC is None or _BUILDER_SPEC.loader is None:
    raise ImportError("cannot load Native AOT package builder")
_BUILDER = importlib.util.module_from_spec(_BUILDER_SPEC)
_BUILDER_SPEC.loader.exec_module(_BUILDER)
assemble_native_package = _BUILDER.assemble_native_package
build_native_package_archive = _BUILDER.build_native_package_archive
NativePackageAssemblyError = _BUILDER.NativePackageAssemblyError

REPOSITORY_ROOT = Path(__file__).parents[4]


def _executable(tmp_path: Path) -> Path:
    """Use the current interpreter as an executable-only build fixture."""

    binary = tmp_path / "cyrene-onebot-v11"
    binary.write_bytes(Path(sys.executable).read_bytes())
    binary.chmod(0o755)
    return binary


def test_source_metadata_is_formal_native_with_explicit_python_rollback() -> None:
    """The source projection is Native AOT while Python metadata is archived."""

    manifest = json.loads(
        (
            REPOSITORY_ROOT
            / "plugins/connectors/onebot-v11/plugin.manifest.json"
        ).read_text()
    )
    descriptor = json.loads(
        (
            REPOSITORY_ROOT
            / "plugins/connectors/onebot-v11/package-descriptor.json"
        ).read_text()
    )
    rollback_manifest = json.loads(
        (
            REPOSITORY_ROOT
            / "plugins/connectors/onebot-v11/rollback/python-0.2.0/plugin.manifest.json"
        ).read_text()
    )

    assert manifest["version"] == "0.4.0"
    assert manifest["runtime"]["language"] == "csharp"
    assert manifest["runtime"]["launch"] == {
        "executable": "bin/cyrene-onebot-v11"
    }
    assert "requiresPython" not in manifest["compatibility"]
    assert descriptor["package"]["version"] == "0.4.0"
    assert descriptor["runtime"]["kind"] == "native-executable"
    assert rollback_manifest["version"] == "0.2.0"
    assert rollback_manifest["runtime"]["language"] == "python"
    assert (
        rollback_manifest["runtime"]["entrypoint"]
        == "onebot_v11_connector.plugin:ConnectorPlugin"
    )


def test_native_candidate_contains_only_native_runtime_and_resolvable_refs(
    tmp_path: Path,
) -> None:
    package_root = assemble_native_package(
        REPOSITORY_ROOT,
        _executable(tmp_path),
        tmp_path / "package",
        "linux-x64",
    )

    assert (package_root / "bin/cyrene-onebot-v11").is_file()
    assert (package_root / "NATIVE_AOT_OPERATIONS.md").is_file()
    assert not list(package_root.rglob("*.py"))
    assert not list(package_root.rglob("*.pyc"))
    assert not (package_root / "src").exists()

    manifest = json.loads((package_root / "plugin.manifest.json").read_text())
    assert manifest["version"] == "0.4.0"
    assert manifest["runtime"] == {
        **manifest["runtime"],
        "language": "csharp",
        "launch": {"executable": "bin/cyrene-onebot-v11"},
    }
    assert "requiresPython" not in manifest["compatibility"]
    assert manifest["compatibility"]["architectures"] == ["x86_64"]
    for method in manifest["methods"]:
        for key in ("inputSchema", "outputSchema"):
            reference = method.get(key)
            if not isinstance(reference, str):
                continue
            assert (package_root / reference.split("#", 1)[0]).is_file()

    descriptor = json.loads((package_root / "package-descriptor.json").read_text())
    assert descriptor["runtime"]["kind"] == "native-executable"
    assert descriptor["runtime"]["entrypoint"] == "bin/cyrene-onebot-v11"
    assert descriptor["implementation"]["entrypoint"] == "bin/cyrene-onebot-v11"
    assert descriptor["dependencies"]["lock"]["status"] == "NOT_REQUIRED"
    assert "subprocess-python" not in json.dumps(descriptor)


def test_native_candidate_records_binary_digest_and_rid(tmp_path: Path) -> None:
    binary = _executable(tmp_path)
    package_root = assemble_native_package(
        REPOSITORY_ROOT,
        binary,
        tmp_path / "package",
        "linux-arm64",
    )
    metadata = json.loads((package_root / "native-artifact.json").read_text())
    assert metadata["rid"] == "linux-arm64"
    assert metadata["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert metadata["byte_size"] == binary.stat().st_size


def test_native_archive_is_deterministic_and_marks_executable(tmp_path: Path) -> None:
    binary = _executable(tmp_path)
    first = build_native_package_archive(
        REPOSITORY_ROOT,
        binary,
        tmp_path / "first.zip",
        "linux-x64",
    )
    second = build_native_package_archive(
        REPOSITORY_ROOT,
        binary,
        tmp_path / "second.zip",
        "linux-x64",
    )
    assert (
        hashlib.sha256(first.read_bytes()).digest()
        == hashlib.sha256(second.read_bytes()).digest()
    )
    with zipfile.ZipFile(first) as archive:
        executable = archive.getinfo("bin/cyrene-onebot-v11")
        assert executable.external_attr >> 16 == 0o100755
        assert not any(name.endswith(".py") for name in archive.namelist())


@pytest.mark.parametrize("rid", ["win-x64", "linux-mips64"])
def test_native_candidate_rejects_unsupported_rid(tmp_path: Path, rid: str) -> None:
    with pytest.raises(NativePackageAssemblyError, match="unsupported Native AOT RID"):
        assemble_native_package(
            REPOSITORY_ROOT,
            _executable(tmp_path),
            tmp_path / "package",
            rid,
        )


def test_native_candidate_rejects_non_executable_binary(tmp_path: Path) -> None:
    binary = tmp_path / "not-executable"
    binary.write_bytes(b"fixture")
    with pytest.raises(NativePackageAssemblyError, match="not executable"):
        assemble_native_package(
            REPOSITORY_ROOT,
            binary,
            tmp_path / "package",
            "linux-x64",
        )


def test_native_candidate_rejects_nonempty_output(tmp_path: Path) -> None:
    output = tmp_path / "package"
    output.mkdir()
    (output / "sentinel").write_text("do not overwrite")
    with pytest.raises(NativePackageAssemblyError, match="must be empty"):
        assemble_native_package(
            REPOSITORY_ROOT,
            _executable(tmp_path),
            output,
            "linux-x64",
        )
