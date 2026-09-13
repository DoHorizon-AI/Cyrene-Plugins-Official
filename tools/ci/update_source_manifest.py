#!/usr/bin/env python3
"""Refresh source-manifest.json records after reviewed source changes.

The manifest stays the CI authority for the exported tree. This tool reconciles
the record list with the working tree: it adds new payloads, rewrites entries
whose bytes, mode, or symlink target changed, and drops removed payloads.

Derived digests (license, repository policy, protected files) follow their
files. Protected-file digest changes are rejected unless
--accept-protected-changes is passed explicitly after review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

MANIFEST_NAME = "source-manifest.json"
POLICY_NAME = "repository-policy.yaml"
SYMLINK_MODE = "120000"


class ManifestUpdateError(ValueError):
    """Raised when the working tree cannot be reconciled with the manifest."""


def _digest(content: bytes) -> str:
    """Hash one payload value the way the verifier does."""

    return hashlib.sha256(content).hexdigest()


def _load(manifest_path: Path) -> dict:
    """Read the manifest and require a record list."""

    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ManifestUpdateError(f"Manifest is not a regular file: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManifestUpdateError(f"Cannot parse manifest {manifest_path}: {error}") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise ManifestUpdateError("Manifest must contain a file record list")
    return manifest


def _collect(root: Path) -> dict[str, dict]:
    """Return manifest records for every payload file and symlink under root."""

    entries: dict[str, dict] = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            candidate = current_path / directory
            if directory == ".git":
                continue
            if candidate.is_symlink():
                relative = candidate.relative_to(root).as_posix()
                raise ManifestUpdateError(f"Unexpected directory symlink: {relative}")
        directories[:] = [name for name in directories if name != ".git"]
        for filename in filenames:
            candidate = current_path / filename
            relative = candidate.relative_to(root).as_posix()
            if relative == MANIFEST_NAME:
                continue
            if candidate.is_symlink():
                target = os.readlink(candidate)
                target_bytes = target.encode("utf-8")
                entries[relative] = {
                    "kind": "symlink",
                    "mode": SYMLINK_MODE,
                    "path": relative,
                    "sha256": _digest(target_bytes),
                    "size_bytes": len(target_bytes),
                    "target": target,
                }
                continue
            if not stat.S_ISREG(candidate.stat(follow_symlinks=False).st_mode):
                raise ManifestUpdateError(f"Unexpected non-regular payload: {relative}")
            payload = candidate.read_bytes()
            user_executable = bool(candidate.stat().st_mode & stat.S_IXUSR)
            entries[relative] = {
                "kind": "file",
                "mode": "100755" if user_executable else "100644",
                "path": relative,
                "sha256": _digest(payload),
                "size_bytes": len(payload),
            }
    return entries


def _existing_records(manifest: dict) -> dict[str, dict]:
    """Index current manifest records by path."""

    records: dict[str, dict] = {}
    for record in manifest["files"]:
        path = record.get("path") if isinstance(record, dict) else None
        if not isinstance(path, str) or not path:
            raise ManifestUpdateError("Manifest file record is missing a path")
        records[path] = record
    return records


def _protected_changes(manifest: dict, changed: list[str], observed: dict[str, dict]) -> list[str]:
    """List protected files whose digest moved away from the manifest record."""

    protected = {
        record.get("path"): record.get("sha256")
        for record in manifest.get("protected_files", [])
        if isinstance(record, dict)
    }
    return [
        path
        for path in changed
        if path in protected and observed.get(path, {}).get("sha256") != protected[path]
    ]


def refresh(root: Path, manifest: dict, *, accept_protected_changes: bool) -> dict:
    """Reconcile manifest records with the working tree in place."""

    observed = _collect(root)
    existing = _existing_records(manifest)
    changed = sorted(
        path
        for path, record in existing.items()
        if path in observed and observed[path] != record
    )
    added = sorted(set(observed) - set(existing))
    removed = sorted(set(existing) - set(observed))
    protected_changed = _protected_changes(manifest, changed, observed)
    if protected_changed and not accept_protected_changes:
        raise ManifestUpdateError(
            "Protected files changed: "
            + ", ".join(protected_changed)
            + "; rerun with --accept-protected-changes after review"
        )

    manifest["files"] = [observed[path] for path in sorted(observed)]
    for record in manifest.get("protected_files", []):
        if isinstance(record, dict) and record.get("path") in observed:
            record["sha256"] = observed[record["path"]]["sha256"]
    license_path = manifest.get("license", {}).get("path")
    if license_path in observed:
        manifest["license"]["sha256"] = observed[license_path]["sha256"]
    if POLICY_NAME in observed:
        manifest["policy_sha256"] = observed[POLICY_NAME]["sha256"]

    manifest["_refresh_summary"] = {
        "added": added,
        "changed": changed,
        "removed": removed,
    }
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Run source-manifest reconciliation from the command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument(
        "--accept-protected-changes",
        action="store_true",
        help="Allow protected-file digests to move after an explicit review.",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    manifest_path = args.manifest or root / MANIFEST_NAME
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    try:
        manifest = _load(manifest_path)
        manifest = refresh(
            root,
            manifest,
            accept_protected_changes=args.accept_protected_changes,
        )
    except ManifestUpdateError as error:
        print(f"SOURCE_MANIFEST_UPDATE: FAIL: {error}", file=sys.stderr)
        return 2
    summary = manifest.pop("_refresh_summary")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "SOURCE_MANIFEST_UPDATE: OK "
        f"files={len(manifest['files'])} "
        f"added={len(summary['added'])} "
        f"changed={len(summary['changed'])} "
        f"removed={len(summary['removed'])}"
    )
    for label in ("added", "changed", "removed"):
        for path in summary[label]:
            print(f"  {label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
