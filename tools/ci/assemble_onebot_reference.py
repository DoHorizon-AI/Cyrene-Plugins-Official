#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 assemble_onebot_reference.py                                    │
│  Module: tools.ci                                                   │
│  Role: Build an immutable Python OneBot reference artifact.          │
│                                                                     │
│  模块职责：生成带精确源码证明的 Python OneBot 外部参考制品。               │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

PLUGIN_ID = "cyrene.connectors.onebot-v11"
REFERENCE_VERSION = "0.2.0"
REFERENCE_SCHEMA = "cyrene.onebot.python-reference.v1"
PACKAGE_BUILDER = (
    "plugins/connectors/onebot-v11/tools/assemble_package.py"
)
REFERENCE_RUNNER = "tools/ci/onebot_reference_parity.py"


class ReferenceAssemblyError(ValueError):
    """Raised when a Python reference artifact cannot be assembled safely."""


def _load_package_builder(repository_root: Path) -> Any:
    """Load the existing package copier without importing repository packages."""

    builder_path = repository_root / PACKAGE_BUILDER
    spec = importlib.util.spec_from_file_location(
        "onebot_python_reference_package_builder", builder_path
    )
    if spec is None or spec.loader is None:
        raise ReferenceAssemblyError(f"cannot load package builder: {builder_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one regular file."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_revision(repository_root: Path) -> str:
    """Read the exact Git revision used to build the reference artifact."""

    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReferenceAssemblyError(
            "cannot resolve the exact source revision"
        ) from error
    revision = result.stdout.strip()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ReferenceAssemblyError("Git source revision is not a full SHA-1")
    return revision


def _source_date_epoch() -> int:
    """Resolve a reproducible timestamp from SOURCE_DATE_EPOCH."""

    raw = os.environ.get("SOURCE_DATE_EPOCH", "0")
    try:
        value = int(raw)
    except ValueError as error:
        raise ReferenceAssemblyError("SOURCE_DATE_EPOCH must be an integer") from error
    if value < 0:
        raise ReferenceAssemblyError("SOURCE_DATE_EPOCH must not be negative")
    return value


def _entries(root: Path) -> list[dict[str, object]]:
    """Describe every staged reference file in stable lexical order."""

    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ReferenceAssemblyError(f"reference artifact contains a symlink: {path}")
        if not path.is_file():
            continue
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    if not entries:
        raise ReferenceAssemblyError("reference artifact staging directory is empty")
    return entries


def _write_manifest(root: Path, repository_root: Path) -> None:
    """Write provenance metadata after the Python payload has been staged."""

    epoch = _source_date_epoch()
    manifest = {
        "schema": REFERENCE_SCHEMA,
        "plugin_id": PLUGIN_ID,
        "reference_version": REFERENCE_VERSION,
        "source_revision": _source_revision(repository_root),
        "source_date_epoch": epoch,
        "formal_runtime": "csharp-native-aot",
        "reference_runtime": "python",
        "rollback_identity": f"{PLUGIN_ID}@{REFERENCE_VERSION}",
        "qqnt_real_smoke": "NOT_RUN",
        "entries": _entries(root),
    }
    (root / "reference-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_build_metadata(root: Path, repository_root: Path) -> None:
    """Write deterministic SBOM and build-proof records into the artifact."""

    source_revision = _source_revision(repository_root)
    builder_path = repository_root / PACKAGE_BUILDER
    runner_path = repository_root / REFERENCE_RUNNER
    if not builder_path.is_file() or not runner_path.is_file():
        raise ReferenceAssemblyError("reference builder or runner is missing")
    sbom = {
        "schema": "cyrene.onebot.python-reference-sbom.v1",
        "source_revision": source_revision,
        "components": [
            {"name": "python", "version": "3.12", "scope": "runtime"},
            {"name": "grpcio", "version": ">=1.62,<1.63", "scope": "runtime"},
            {"name": "protobuf", "version": ">=4.21,<5", "scope": "runtime"},
        ],
        "payload_files": _entries(root),
    }
    (root / "reference-sbom.json").write_text(
        json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    proof = {
        "schema": "cyrene.onebot.python-reference-build-proof.v1",
        "source_revision": source_revision,
        "source_date_epoch": _source_date_epoch(),
        "builder": {
            "path": PACKAGE_BUILDER,
            "sha256": _sha256(builder_path),
        },
        "runner": {
            "path": REFERENCE_RUNNER,
            "sha256": _sha256(runner_path),
        },
        "command": [
            "python3",
            "tools/ci/assemble_onebot_reference.py",
            "--repository-root",
            ".",
            "--output",
            "<immutable-output>.zip",
        ],
    }
    (root / "reference-build-proof.json").write_text(
        json.dumps(proof, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _zip_timestamp(epoch: int) -> tuple[int, int, int, int, int, int]:
    """Convert one epoch to ZIP's minimum-safe timestamp representation."""

    import datetime as dt

    value = dt.datetime.fromtimestamp(epoch, dt.UTC)
    return (
        max(value.year, 1980),
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
    )


def _write_archive(root: Path, output: Path, epoch: int) -> None:
    """Write a deterministic ZIP archive without symlink indirection."""

    output.parent.mkdir(parents=True, exist_ok=True)
    timestamp = _zip_timestamp(epoch)
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def build_reference_archive(repository_root: Path, output: Path) -> Path:
    """Build one self-contained Python reference archive.

    Args:
        repository_root: Plugins repository containing the Python reference.
        output: Destination ZIP path outside the repository checkout.

    Returns:
        The resolved archive path.

    Raises:
        ReferenceAssemblyError: If the source or destination is unsafe.
    """

    repository_root = repository_root.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise ReferenceAssemblyError(f"reference output already exists: {output}")
    epoch = _source_date_epoch()
    builder = _load_package_builder(repository_root)
    with tempfile.TemporaryDirectory(prefix="cyrene-onebot-reference-") as temporary:
        staged = Path(temporary) / "reference"
        builder.assemble_package(repository_root, staged)
        runner_destination = staged / "reference-runner/onebot_reference_parity.py"
        runner_destination.parent.mkdir(parents=True, exist_ok=True)
        runner_destination.write_bytes((repository_root / REFERENCE_RUNNER).read_bytes())
        _write_build_metadata(staged, repository_root)
        _write_manifest(staged, repository_root)
        _write_archive(staged, output, epoch)
    return output


def _parse_args() -> argparse.Namespace:
    """Parse repository and immutable output paths."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    """Build one external reference artifact without modifying the checkout."""

    args = _parse_args()
    archive = build_reference_archive(args.repository_root, args.output)
    print(
        json.dumps(
            {
                "artifact": str(archive),
                "sha256": _sha256(archive),
                "schema": REFERENCE_SCHEMA,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
