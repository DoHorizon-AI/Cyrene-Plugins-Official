"""Assemble the isolated Python 0.2.0 rollback package.

The formal package is now the C# Native AOT artifact assembled by
``assemble_native_package.py``. This builder deliberately retains the last
Python package as an independently assembled rollback artifact. The SDK
runtime is owned by this repository but is not duplicated in the connector
source tree, so this builder copies it into temporary staging and rewrites
only repository-relative schema references. It never copies a QQ installation,
native Host, account data, or credentials.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

PACKAGE_RELATIVE_FILES = (
    "configuration.schema.json",
    "pyproject.toml",
    "requirements.lock",
)
ROLLBACK_METADATA_ROOT = "rollback/python-0.2.0"
SHARED_SCHEMA_FILES = (
    "contracts/json/message-connector-v1-inbound-request.schema.json",
    "contracts/json/message-connector-v1-request-response.schema.json",
)
LOCAL_SCHEMA_FILES = ("contracts/v1/schema.json",)


class PackageAssemblyError(ValueError):
    """Raised when a package candidate cannot be assembled safely."""


def _read_json(path: Path) -> dict[str, Any]:
    """Read one package metadata document as a JSON object."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PackageAssemblyError(
            f"cannot read JSON metadata {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise PackageAssemblyError(f"JSON metadata is not an object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Write normalized package metadata with stable UTF-8 formatting."""

    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _rewrite_manifest_refs(manifest: dict[str, Any]) -> None:
    """Make shared schema references resolve from the package root."""

    for method in manifest.get("methods", []):
        if not isinstance(method, dict):
            continue
        for key in ("inputSchema", "outputSchema"):
            reference = method.get(key)
            if isinstance(reference, str):
                method[key] = reference.replace(
                    "../../../contracts/json/", "contracts/json/"
                )


def _rewrite_descriptor_refs(descriptor: dict[str, Any]) -> None:
    """Make descriptor references self-contained in an installed archive."""

    package = descriptor.get("package")
    if isinstance(package, dict):
        package["manifest_ref"] = "plugin.manifest.json"
    configuration = descriptor.get("configuration")
    if isinstance(configuration, dict):
        configuration["schema_ref"] = "configuration.schema.json"


def _copy_required_file(source: Path, destination: Path) -> None:
    """Copy one required file and fail instead of creating a partial package."""

    if not source.is_file():
        raise PackageAssemblyError(f"required package file is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def assemble_package(repository_root: Path, output_root: Path) -> Path:
    """Assemble one unpacked package root and return its resolved path.

    Args:
        repository_root: Plugins repository containing the connector and SDK.
        output_root: Empty directory that will receive the package payload.

    Returns:
        The resolved package root.

    Raises:
        PackageAssemblyError: If inputs are missing or output is not empty.
    """

    repository_root = repository_root.resolve(strict=True)
    connector_root = repository_root / "plugins/connectors/onebot-v11"
    runtime_root = (
        repository_root
        / "sdk/python/cyrene_plugin_runtime/src/cyrene_plugin_runtime"
    )
    output_root = output_root.resolve()
    if output_root.exists() and any(output_root.iterdir()):
        raise PackageAssemblyError(f"package output must be empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    for relative in PACKAGE_RELATIVE_FILES:
        _copy_required_file(connector_root / relative, output_root / relative)
    _copy_required_file(
        connector_root / ROLLBACK_METADATA_ROOT / "README.md",
        output_root / "README.md",
    )
    for relative in ("plugin.manifest.json", "package-descriptor.json"):
        _copy_required_file(
            connector_root / ROLLBACK_METADATA_ROOT / relative,
            output_root / relative,
        )
    for relative in LOCAL_SCHEMA_FILES:
        _copy_required_file(connector_root / relative, output_root / relative)
    for relative in SHARED_SCHEMA_FILES:
        _copy_required_file(repository_root / relative, output_root / relative)

    source_package = connector_root / "src/onebot_v11_connector"
    if not source_package.is_dir():
        raise PackageAssemblyError(
            f"connector source package is missing: {source_package}"
        )
    shutil.copytree(
        source_package,
        output_root / "src/onebot_v11_connector",
        ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
    )
    if not runtime_root.is_dir():
        raise PackageAssemblyError(f"SDK runtime package is missing: {runtime_root}")
    shutil.copytree(
        runtime_root,
        output_root / "src/cyrene_plugin_runtime",
        ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
    )

    manifest_path = output_root / "plugin.manifest.json"
    manifest = _read_json(manifest_path)
    _rewrite_manifest_refs(manifest)
    _write_json(manifest_path, manifest)
    descriptor_path = output_root / "package-descriptor.json"
    descriptor = _read_json(descriptor_path)
    _rewrite_descriptor_refs(descriptor)
    _write_json(descriptor_path, descriptor)
    return output_root


def build_package_archive(repository_root: Path, output_path: Path) -> Path:
    """Build a deterministic ZIP archive from an assembled package candidate."""

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cyrene-onebot-package-") as temporary:
        package_root = assemble_package(repository_root, Path(temporary) / "package")
        with zipfile.ZipFile(
            output_path, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for path in sorted(package_root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(package_root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
    return output_path


def _parse_args() -> argparse.Namespace:
    """Parse the package candidate command-line arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--directory",
        action="store_true",
        help="write an unpacked package directory instead of a ZIP archive",
    )
    return parser.parse_args()


def main() -> int:
    """Build one candidate package without publishing or mutating repository files."""

    args = _parse_args()
    if args.directory:
        assemble_package(args.repository_root, args.output)
    else:
        build_package_archive(args.repository_root, args.output)
    print(f"PACKAGE_ASSEMBLY: PASS output={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
