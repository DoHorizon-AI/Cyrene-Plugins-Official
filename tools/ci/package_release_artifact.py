#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 package_release_artifact.py                                      │
│  Module: tools.ci                                                    │
│  Role: Create a provenance-bound immutable CI payload and manifest.   │
│                                                                     │
│  模块职责：生成绑定 CI provenance 的不可变产物、清单和 SHA-256。          │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import tarfile
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    """Parse the staged CI payload and immutable provenance fields.

        中文：解析已暂存的 CI 载荷和不可变来源字段。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--workflow-run-attempt", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--abi-version", required=True)
    parser.add_argument("--contract-version", required=True)
    parser.add_argument("--rid", required=True)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one file.

        中文：返回一个文件的 SHA-256 摘要。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_epoch() -> int:
    """Resolve a reproducible archive timestamp from SOURCE_DATE_EPOCH.

        中文：根据 SOURCE_DATE_EPOCH 确定可复现的归档时间戳。"""

    return int(os.environ.get("SOURCE_DATE_EPOCH", "0"))


def _write_reproducible_tar(source: Path, target: Path) -> None:
    """Write a gzip tar archive with normalized metadata and sorted entries.

        中文：写入元数据已规范化且条目已排序的 gzip tar 归档。"""

    epoch = _source_epoch()
    with (
        target.open("wb") as raw_stream,
        gzip.GzipFile(fileobj=raw_stream, mode="wb", mtime=epoch) as gzip_stream,
        tarfile.open(fileobj=gzip_stream, mode="w") as archive,
    ):
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"CI payload must not contain symbolic links: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(source).as_posix()
            info = archive.gettarinfo(str(path), arcname=relative)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = epoch
            with path.open("rb") as stream:
                archive.addfile(info, stream)


def _payload_entries(source: Path) -> list[dict[str, object]]:
    """Describe every payload file so the manifest is independently reviewable.

        中文：描述载荷中的每个文件，使清单可以独立审核。"""

    entries: list[dict[str, object]] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"CI payload must not contain symbolic links: {path}")
        if not path.is_file():
            continue
        entries.append(
            {
                "path": path.relative_to(source).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return entries


def _write_checksums(output: Path) -> None:
    """Write checksums for the manifest and payload archive.

        中文：为清单和载荷归档写入校验和。"""

    lines = [
        f"{_sha256(output / 'payload.tar.gz')}  payload.tar.gz",
        f"{_sha256(output / 'release-manifest.json')}  release-manifest.json",
    ]
    (output / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    """Assemble a content-addressed payload and provenance manifest.

        中文：组装内容寻址的载荷和来源清单。"""

    args = _parse_args()
    source = args.input_dir.resolve()
    output = args.output_dir.resolve()
    if not source.is_dir():
        raise SystemExit(f"CI payload input directory does not exist: {source}")
    entries = _payload_entries(source)
    if not entries:
        raise SystemExit(f"CI payload input directory is empty: {source}")

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    payload_path = output / "payload.tar.gz"
    _write_reproducible_tar(source, payload_path)
    manifest = {
        "schema_version": "cyrene.plugin.release-manifest.v1",
        "repository": args.repository,
        "workflow": "ci",
        "workflow_run_id": args.workflow_run_id,
        "workflow_run_attempt": args.workflow_run_attempt,
        "commit_sha": args.commit_sha,
        "artifact_name": args.artifact_name,
        "abi_version": args.abi_version,
        "contract_version": args.contract_version,
        "rid": args.rid,
        "payload_file": payload_path.name,
        "payload_sha256": _sha256(payload_path),
        "payload_size_bytes": payload_path.stat().st_size,
        "payload_entries": entries,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    (output / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_checksums(output)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
