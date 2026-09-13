#!/usr/bin/env python3
"""Verify an exported source tree without consulting Git history."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_NAME = "source-manifest.json"
SHA256_LENGTH = 64
COMMIT_LENGTH = 40
ALLOWED_FILE_MODES = {"100644", "100755"}
SYMLINK_MODE = "120000"


class ManifestVerificationError(ValueError):
    """Raised when an exported tree does not match its source manifest."""


def _safe_relative(value: Any, field: str) -> str:
    """Validate a manifest path and reject traversal or platform separators."""

    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ManifestVerificationError(f"{field} must be a non-empty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ManifestVerificationError(f"{field} must not traverse the tree: {value!r}")
    return value


def _digest(content: bytes) -> str:
    """Hash one manifest payload value."""

    return hashlib.sha256(content).hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    """Read one regular JSON manifest."""

    if path.is_symlink() or not path.is_file():
        raise ManifestVerificationError(f"Manifest is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManifestVerificationError(f"Cannot parse manifest {path}: {error}") from error
    if not isinstance(value, dict):
        raise ManifestVerificationError("Manifest root must be an object")
    return value


def _verify_hex(value: Any, length: int, field: str) -> str:
    """Validate a lowercase hexadecimal digest or commit."""

    if not isinstance(value, str) or len(value) != length:
        raise ManifestVerificationError(f"{field} must be {length} hexadecimal characters")
    try:
        int(value, 16)
    except ValueError as error:
        raise ManifestVerificationError(f"{field} is not hexadecimal") from error
    return value.lower()


def _walk_payload(
    root: Path,
    manifest_path: Path,
    *,
    allow_git_metadata: bool,
) -> set[str]:
    """List regular payload files and symlinks, rejecting Git metadata."""

    observed: set[str] = set()
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        if ".git" in directories and allow_git_metadata:
            directories.remove(".git")
        elif ".git" in directories:
            relative = (current_path / ".git").relative_to(root).as_posix()
            raise ManifestVerificationError(f"Unexpected Git metadata: {relative}")
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                relative = candidate.relative_to(root).as_posix()
                raise ManifestVerificationError(f"Unexpected directory symlink: {relative}")
        for filename in filenames:
            candidate = current_path / filename
            if candidate == manifest_path:
                continue
            if filename == ".git":
                if allow_git_metadata and current_path == root:
                    continue
                relative = candidate.relative_to(root).as_posix()
                raise ManifestVerificationError(f"Unexpected Git metadata: {relative}")
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink() or stat.S_ISREG(candidate.stat(follow_symlinks=False).st_mode):
                observed.add(relative)
                continue
            raise ManifestVerificationError(f"Unexpected non-regular payload: {relative}")
    return observed


def _entry_content(path: Path, entry: dict[str, Any]) -> bytes:
    """Read one declared file or symlink in its manifest representation."""

    kind = entry.get("kind")
    if kind == "file":
        if path.is_symlink() or not path.is_file():
            raise ManifestVerificationError(f"Manifest file is not regular: {entry['path']}")
        mode = stat.S_IMODE(path.stat().st_mode)
        actual_mode = "100755" if mode & stat.S_IXUSR else "100644"
        if actual_mode != entry.get("mode"):
            raise ManifestVerificationError(
                f"Mode mismatch for {entry['path']}: expected {entry.get('mode')}, got {actual_mode}"
            )
        return path.read_bytes()
    if kind == "symlink":
        if not path.is_symlink():
            raise ManifestVerificationError(f"Manifest symlink is missing: {entry['path']}")
        target = entry.get("target")
        if _safe_relative(target, f"{entry['path']}.target") != os.readlink(path):
            raise ManifestVerificationError(f"Symlink target mismatch: {entry['path']}")
        if entry.get("mode") != SYMLINK_MODE:
            raise ManifestVerificationError(f"Invalid symlink mode: {entry['path']}")
        return str(target).encode("utf-8")
    raise ManifestVerificationError(f"Unsupported manifest kind for {entry.get('path')!r}: {kind!r}")


def verify_manifest(
    root: Path,
    manifest_path: Path | None = None,
    *,
    allow_git_metadata: bool = False,
) -> dict[str, Any]:
    """Verify every declared payload entry and reject unexpected payload files."""

    root = root.resolve()
    if not root.is_dir():
        raise ManifestVerificationError(f"Export root is not a directory: {root}")
    if manifest_path is None:
        manifest_path = root / MANIFEST_NAME
    elif not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    # Preserve a caller-provided symlink so _read_manifest can reject it.
    manifest_path = Path(os.path.abspath(manifest_path))
    try:
        manifest_path.relative_to(root)
    except ValueError as error:
        raise ManifestVerificationError("Manifest must be inside the export root") from error
    manifest = _read_manifest(manifest_path)

    if manifest.get("schema_version") != "cyrene.public-source-manifest.v1":
        raise ManifestVerificationError("Unsupported source manifest schema_version")
    if manifest.get("history_policy") != "EMPTY_HISTORY_REQUIRED":
        raise ManifestVerificationError("Source manifest does not require empty history")
    _verify_hex(manifest.get("source_commit"), COMMIT_LENGTH, "source_commit")
    _verify_hex(manifest.get("policy_sha256"), SHA256_LENGTH, "policy_sha256")
    if not isinstance(manifest.get("source_repository"), str) or not manifest["source_repository"]:
        raise ManifestVerificationError("source_repository is required")

    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ManifestVerificationError("Manifest files must be a non-empty array")
    entries: dict[str, dict[str, Any]] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ManifestVerificationError("Manifest contains a non-object file entry")
        relative = _safe_relative(raw_entry.get("path"), "file.path")
        if relative == MANIFEST_NAME or relative in entries:
            raise ManifestVerificationError(f"Duplicate or reserved manifest path: {relative}")
        _verify_hex(raw_entry.get("sha256"), SHA256_LENGTH, f"{relative}.sha256")
        size = raw_entry.get("size_bytes")
        if not isinstance(size, int) or size < 0:
            raise ManifestVerificationError(f"Invalid size for {relative}")
        kind = raw_entry.get("kind")
        if kind == "file" and raw_entry.get("mode") not in ALLOWED_FILE_MODES:
            raise ManifestVerificationError(f"Invalid regular-file mode for {relative}")
        if kind == "symlink" and raw_entry.get("mode") != SYMLINK_MODE:
            raise ManifestVerificationError(f"Invalid symlink mode for {relative}")
        entries[relative] = raw_entry

    observed = _walk_payload(
        root,
        manifest_path,
        allow_git_metadata=allow_git_metadata,
    )
    expected = set(entries)
    if observed != expected:
        missing = sorted(expected - observed)
        unexpected = sorted(observed - expected)
        raise ManifestVerificationError(
            f"Payload does not match source manifest: missing={missing}, unexpected={unexpected}"
        )

    for relative, entry in entries.items():
        path = root.joinpath(*PurePosixPath(relative).parts)
        if entry.get("kind") == "symlink":
            target = _safe_relative(entry.get("target"), f"{relative}.target")
            target_relative = posixpath.normpath(
                str(PurePosixPath(relative).parent / PurePosixPath(target))
            )
            target_entry = entries.get(target_relative)
            if target_entry is None or target_entry.get("kind") != "file":
                raise ManifestVerificationError(
                    f"Symlink target is not a declared regular file: {relative}"
                )
            target_path = root.joinpath(*PurePosixPath(target_relative).parts)
            if target_path.is_symlink() or not target_path.is_file():
                raise ManifestVerificationError(
                    f"Symlink target is not a regular file: {relative}"
                )
        content = _entry_content(path, entry)
        expected_size = entry["size_bytes"]
        expected_digest = entry["sha256"].lower()
        if len(content) != expected_size or _digest(content) != expected_digest:
            raise ManifestVerificationError(f"Digest or size mismatch for {relative}")

    license_record = manifest.get("license")
    if not isinstance(license_record, dict):
        raise ManifestVerificationError("Manifest license record is required")
    license_path = _safe_relative(license_record.get("path"), "license.path")
    license_entry = entries.get(license_path)
    if license_entry is None or license_entry.get("kind") != "file":
        raise ManifestVerificationError("Manifest license must reference a declared regular file")
    if license_entry["sha256"].lower() != _verify_hex(
        license_record.get("sha256"), SHA256_LENGTH, "license.sha256"
    ):
        raise ManifestVerificationError("License digest does not match file entry")

    protected_records = manifest.get("protected_files")
    if not isinstance(protected_records, list) or not protected_records:
        raise ManifestVerificationError("Manifest protected_files must be a non-empty array")
    for record in protected_records:
        if not isinstance(record, dict):
            raise ManifestVerificationError("Invalid protected file record")
        relative = _safe_relative(record.get("path"), "protected.path")
        entry = entries.get(relative)
        digest = _verify_hex(record.get("sha256"), SHA256_LENGTH, f"{relative}.sha256")
        if entry is None or entry.get("kind") != "file" or entry["sha256"].lower() != digest:
            raise ManifestVerificationError(f"Protected file is not verified: {relative}")

    return manifest


def main(argv: list[str] | None = None) -> int:
    """Run source-manifest verification from the command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument(
        "--allow-git-metadata",
        action="store_true",
        help="Ignore the root .git directory created by a CI checkout.",
    )
    args = parser.parse_args(argv)
    try:
        manifest = verify_manifest(
            args.root,
            args.manifest,
            allow_git_metadata=args.allow_git_metadata,
        )
    except ManifestVerificationError as error:
        print(f"SOURCE_MANIFEST: FAIL: {error}", file=sys.stderr)
        return 2
    print(
        "SOURCE_MANIFEST: PASS "
        f"commit={manifest['source_commit']} "
        f"files={len(manifest['files'])} "
        f"protected={len(manifest['protected_files'])}"
    )
    for record in manifest["protected_files"]:
        print(f"SOURCE_MANIFEST_PROTECTED: {record['path']} sha256={record['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
