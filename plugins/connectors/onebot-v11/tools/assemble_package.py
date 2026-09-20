"""Assemble the generic OneBot Python behavior-reference package."""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

PACKAGE_VERSION = "0.4.0"
ENTRYPOINT = "onebot_v11_connector.plugin:ConnectorPlugin"
PACKAGE_FILES = (
    "README.md",
    "configuration.schema.json",
    "pyproject.toml",
    "requirements.lock",
)


class PackageAssemblyError(ValueError):
    """Raised when a reference package input is missing or unsafe."""


def _copy(source: Path, destination: Path) -> None:
    """Copy one regular package input."""

    if source.is_symlink() or not source.is_file():
        raise PackageAssemblyError(f"required package file is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PackageAssemblyError(f"metadata is not an object: {path}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _manifest(value: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(value))
    result["version"] = PACKAGE_VERSION
    runtime = result["runtime"]
    runtime["language"] = "python"
    runtime["entrypoint"] = ENTRYPOINT
    runtime["launch"] = {
        "executable": "prepared-runtime",
        "args": [
            "-B",
            "src/cyrene_plugin_runtime/bootstrap.py",
            "--entrypoint",
            ENTRYPOINT,
        ],
    }
    runtime.pop("profiles", None)
    compatibility = result["compatibility"]
    compatibility["requiresPython"] = ">=3.11"
    return result


def _descriptor(value: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(value))
    result["package"]["version"] = PACKAGE_VERSION
    result["implementation"]["entrypoint"] = ENTRYPOINT
    result["runtime"] = {
        "kind": "subprocess-python",
        "protocol": "cyrene.plugin.runtime.v1.DirectPluginRuntime",
        "external": {
            "required": True,
            "kind": "onebot_v11_runtime",
            "artifact_ref": None,
        },
    }
    result["configuration"]["schema_ref"] = "configuration.schema.json"
    result["dependencies"]["lock"] = {"status": "NOT_REQUIRED"}
    return result


def assemble_package(repository_root: Path, output_root: Path) -> Path:
    """Assemble one self-contained generic OneBot Python reference package."""

    repository_root = repository_root.resolve(strict=True)
    connector_root = repository_root / "plugins/connectors/onebot-v11"
    output_root = output_root.resolve()
    if output_root.exists() and any(output_root.iterdir()):
        raise PackageAssemblyError(f"package output must be empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    for relative in PACKAGE_FILES:
        _copy(connector_root / relative, output_root / relative)
    _copy(connector_root / "plugin.manifest.json", output_root / "plugin.manifest.json")
    _copy(
        connector_root / "package-descriptor.json",
        output_root / "package-descriptor.json",
    )
    for relative in (
        "contracts/json/message-connector-v1-inbound-request.schema.json",
        "contracts/json/message-connector-v1-request-response.schema.json",
    ):
        _copy(repository_root / relative, output_root / relative)
    shutil.copytree(
        connector_root / "src/onebot_v11_connector",
        output_root / "src/onebot_v11_connector",
        ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
    )
    shutil.copytree(
        repository_root / "sdk/python/cyrene_plugin_runtime/src/cyrene_plugin_runtime",
        output_root / "src/cyrene_plugin_runtime",
        ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
    )
    _write(
        output_root / "plugin.manifest.json",
        _manifest(_json(output_root / "plugin.manifest.json")),
    )
    _write(
        output_root / "package-descriptor.json",
        _descriptor(_json(output_root / "package-descriptor.json")),
    )
    return output_root


def build_package_archive(repository_root: Path, output_path: Path) -> Path:
    """Build a deterministic ZIP reference archive."""

    output_path = output_path.resolve()
    with tempfile.TemporaryDirectory(prefix="cyrene-onebot-reference-") as temporary:
        root = assemble_package(repository_root, Path(temporary) / "package")
        with zipfile.ZipFile(
            output_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    info = zipfile.ZipInfo(
                        path.relative_to(root).as_posix(), (1980, 1, 1, 0, 0, 0)
                    )
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, path.read_bytes())
    return output_path
