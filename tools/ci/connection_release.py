#!/usr/bin/env python3
"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 connection_release.py                                                │
│  Module: tools.ci.connection_release                                    │
│  Role: Package independent connection releases and strict v2 metadata.   │
│                                                                          │
│  模块职责：打包连接组件独立发布制品并生成严格的 V2 元数据。                 │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import rfc8785

SEMVER_TERM = re.compile(
    r"^(?:=|>=|>|<=|<|\^|~)?[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$"
)
CONNECTION_COMPONENT_IDS = frozenset(
    {
        "cy-workspace-relay",
        "cy-workspace-connector",
        "cy-workspace-sidecar",
        "cy-workspace-frontend-bridge",
    }
)
# Match each native binary's loopback health defaults in runtime/rust/*/src/main.rs.
# 中文：健康地址属于组件源码固定默认值，不从发布参数或 artifact URI 推导。
NATIVE_HTTP_READINESS: dict[str, dict[str, Any]] = {
    "cy-workspace-relay": {"kind": "http", "port": 18080, "path": "/readyz"},
    "cy-workspace-connector": {"kind": "http", "port": 18081, "path": "/readyz"},
    "cy-workspace-frontend-bridge": {
        "kind": "http",
        "port": 18082,
        "path": "/readyz",
    },
    "cy-workspace-sidecar": {"kind": "http", "port": 18083, "path": "/readyz"},
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _jcs_digest(document: dict[str, Any], field: str) -> str:
    unsigned = {key: value for key, value in document.items() if key != field}
    return _sha256(rfc8785.dumps(unsigned))


def _expected_release_id(
    channel: str, source_commit: str, component_id: str | None = None
) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("source.commit must be a full 40-character lowercase Git SHA")
    if channel not in {"preview", "stable"}:
        raise ValueError("release channel is unsupported")
    if component_id is None:
        return f"{channel}-{source_commit}"
    if component_id not in CONNECTION_COMPONENT_IDS:
        raise ValueError(f"unknown connection component: {component_id}")
    return f"{channel}-{component_id}-{source_commit}"


def _verify_protocol_lock(group: dict[str, Any]) -> dict[str, Any]:
    """Verify the protocol pin against its exact Workspace commit bytes."""
    lock = group.get("contractLock")
    if not isinstance(lock, dict):
        raise TypeError("trusted compatibility group has no immutable contractLock pin")
    if lock.get("repository") != "DoHorizon-AI/Cyrene-Workspace":
        raise ValueError("connection protocol lock must be pinned to Cyrene-Workspace")
    commit = lock.get("commit")
    path = lock.get("path")
    digest = lock.get("sha256")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(
            "trusted contractLock commit must be a full lower-case Git SHA"
        )
    if path != "governance/workspace-connection-protocols-v2.lock.json":
        raise ValueError("trusted contractLock path is not the frozen protocol lock")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(
            "trusted contractLock sha256 must be sha256:<64 lowercase hex>"
        )
    try:
        with tempfile.TemporaryDirectory(prefix="cyrene-protocol-lock-") as temporary:
            git_dir = Path(temporary) / "workspace.git"
            subprocess.run(
                ["git", "init", "--bare", "--quiet", str(git_dir)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(git_dir),
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--depth=1",
                    "https://github.com/DoHorizon-AI/Cyrene-Workspace.git",
                    commit,
                ],
                check=True,
                capture_output=True,
            )
            fetched_commit = subprocess.run(
                ["git", "--git-dir", str(git_dir), "rev-parse", "FETCH_HEAD^{commit}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if fetched_commit != commit:
                raise ValueError(
                    "fetched protocol lock source differs from its pinned commit"
                )
            raw = subprocess.run(
                ["git", "--git-dir", str(git_dir), "show", f"{commit}:{path}"],
                check=True,
                capture_output=True,
            ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(
            "cannot read protocol lock from its exact pinned Workspace commit"
        ) from error
    if _sha256(raw) != digest:
        raise ValueError(
            "protocol lock bytes do not match trusted catalog contractLock.sha256"
        )
    return dict(lock)


def _files(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for current, dirs, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*dirs, *names]:
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"payload must not contain symbolic links: {path}")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"payload must contain regular files only: {path}")
            relative = path.relative_to(root).as_posix()
            safe = PurePosixPath(relative)
            if safe.is_absolute() or ".." in safe.parts or "\\" in relative:
                raise ValueError(f"payload contains an unsafe path: {relative}")
            result[relative] = _sha256(path.read_bytes())
    if not result:
        raise ValueError(f"payload directory is empty: {root}")
    return dict(sorted(result.items()))


def _catalog_component(
    path: Path, expected_sha256: str, repository: str, component_id: str
) -> dict[str, Any]:
    """Return a component only when its exact catalog bytes authorize this publisher."""
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError(
            "pinned component catalog digest does not match its source bytes"
        )
    catalog = json.loads(raw.decode("utf-8", "strict"))
    if not isinstance(catalog, dict) or catalog.get("schemaVersion") != 1:
        raise ValueError("component catalog has an unsupported schema")
    publishers = catalog.get("publishers")
    components = catalog.get("components")
    publisher = (
        next(
            (entry for entry in publishers if entry.get("repository") == repository),
            None,
        )
        if isinstance(publishers, list)
        else None
    )
    component = (
        next(
            (entry for entry in components if entry.get("componentId") == component_id),
            None,
        )
        if isinstance(components, list)
        else None
    )
    expected_workflow = f"{repository}/.github/workflows/component-release.yml"
    if (
        not isinstance(publisher, dict)
        or publisher.get("workflow") != expected_workflow
    ):
        raise ValueError("component catalog does not authorize this publisher workflow")
    if not isinstance(component, dict) or component.get("publisher") != repository:
        raise ValueError(
            f"component catalog does not authorize publisher for {component_id}"
        )
    if not isinstance(component.get("dependencies"), list) or not isinstance(
        component.get("restart"), dict
    ):
        raise TypeError(
            f"component catalog dependencies/restart are malformed for {component_id}"
        )
    seen: set[str] = set()
    for dependency in component["dependencies"]:
        if not isinstance(dependency, dict) or set(dependency) != {
            "componentId",
            "versionRange",
        }:
            raise ValueError(
                f"dependency must pin componentId and versionRange for {component_id}"
            )
        dependency_id = dependency["componentId"]
        version_range = dependency["versionRange"]
        if (
            not isinstance(dependency_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", dependency_id)
            or dependency_id in seen
            or not isinstance(version_range, str)
            or not version_range.strip()
            or len(version_range) > 128
            or any(
                not SEMVER_TERM.fullmatch(term.strip())
                for term in version_range.split(",")
            )
        ):
            raise ValueError(f"dependency versionRange is invalid for {component_id}")
        seen.add(dependency_id)
    return component


def _archive(root: Path, destination: Path) -> None:
    entries = _files(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        destination.open("wb") as raw,
        gzip.GzipFile(
            filename="", fileobj=raw, mode="wb", mtime=0, compresslevel=9
        ) as stream,
        tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for relative in entries:
            source = root / relative
            metadata = source.stat()
            item = tarfile.TarInfo(relative)
            item.size = metadata.st_size
            item.mtime = 0
            item.uid = 0
            item.gid = 0
            item.uname = ""
            item.gname = ""
            item.mode = 0o755 if metadata.st_mode & 0o111 else 0o644
            with source.open("rb") as contents:
                archive.addfile(item, contents)


def _base_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--channel", choices=("stable", "preview"), required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument(
        "--component-release",
        action="store_true",
        help="bind release identity to --component-id; omission preserves legacy repository releases",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--version", required=True)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--catalog-sha256", required=True)


def create_manifest(args: argparse.Namespace) -> dict[str, Any]:
    """Build the strict v2 manifest using trusted catalog policy and payload identity."""
    target = json.loads(args.target.read_text(encoding="utf-8"))
    descriptor = json.loads(args.metadata.read_text(encoding="utf-8"))
    if not isinstance(target, dict) or not isinstance(descriptor, dict):
        raise TypeError("target and metadata files must contain JSON objects")
    component_id = descriptor.get("componentId")
    if component_id not in CONNECTION_COMPONENT_IDS:
        raise ValueError(f"unknown connection component: {component_id}")
    requested_component = getattr(args, "component_id", None)
    if requested_component is not None and requested_component != component_id:
        raise ValueError("manifest component differs from the selected component")
    component_release = bool(getattr(args, "component_release", False))
    expected_release_id = _expected_release_id(
        args.channel, args.source_commit, component_id if component_release else None
    )
    if args.release_id != expected_release_id:
        raise ValueError(
            "release ID does not match channel, component scope, and source.commit"
        )
    catalog_component = _catalog_component(
        args.catalog, args.catalog_sha256, args.repository, component_id
    )
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    group_id = catalog_component.get("compatibilityGroup")
    compatibility_group = (
        next(
            (
                entry
                for entry in catalog.get("compatibilityGroups", [])
                if entry.get("groupId") == group_id
            ),
            None,
        )
        if isinstance(group_id, str)
        else None
    )
    protocol_version = catalog_component.get("protocolVersion")
    if protocol_version is None and isinstance(compatibility_group, dict):
        member = next(
            (
                entry
                for entry in compatibility_group.get("members", [])
                if isinstance(entry, dict) and entry.get("componentId") == component_id
            ),
            None,
        )
        protocol_version = (
            member.get("protocolVersion") if isinstance(member, dict) else None
        )
    if not isinstance(protocol_version, str) or not protocol_version:
        raise ValueError(
            "catalog component does not pin protocolVersion or compatibility group"
        )
    targets = catalog_component.get("targets")
    supported = any(
        isinstance(entry, dict)
        and entry.get("targetId") == args.target_id
        and entry.get("support") == "supported"
        and entry.get("artifactKind") == descriptor["artifact"].get("kind")
        for entry in targets or []
    )
    if not supported:
        raise ValueError(
            f"component catalog does not mark target/artifact supported: {args.target_id}"
        )
    if descriptor["artifact"].get("kind") == "oci-image":
        expected_repository = catalog_component.get("ociImageRepository")
        if descriptor["artifact"].get("repository") != expected_repository:
            raise ValueError("OCI repository differs from the pinned component catalog")
    artifact_digest = descriptor["artifact"].get(
        "sha256", descriptor["artifact"].get("digest")
    )
    if descriptor["contentDigest"] != artifact_digest:
        raise ValueError("contentDigest must equal the exact artifact payload digest")
    allowed_refs = {
        "preview": {"refs/heads/develop"},
        "stable": {"refs/heads/main", "refs/heads/release"},
    }
    if args.source_ref not in allowed_refs[args.channel]:
        raise ValueError("source.ref does not match the immutable release channel")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        raise ValueError("repository must be owner/name")
    if not re.fullmatch(r"[0-9]+", str(args.run_id)) or args.run_attempt < 1:
        raise ValueError("workflow run identity is invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", args.version):
        raise ValueError("version is not a valid release version")
    repository_url = f"https://github.com/{args.repository}"
    workflow = f"{args.repository}/.github/workflows/component-release.yml"
    run_url = f"{repository_url}/actions/runs/{args.run_id}/attempts/{args.run_attempt}"
    document: dict[str, Any] = {
        "schemaVersion": 2,
        "releaseId": args.release_id,
        "componentId": component_id,
        "version": args.version,
        "channel": args.channel,
        "target": target,
        "protocolVersion": protocol_version,
        "contentDigest": descriptor["contentDigest"],
        "artifact": descriptor["artifact"],
        "dependencies": catalog_component["dependencies"],
        "restart": catalog_component["restart"],
        "source": {
            "repository": repository_url,
            "ref": args.source_ref,
            "commit": args.source_commit,
        },
        "provenance": {
            "attestation": {
                "kind": "github-artifact-attestation",
                "subjectName": descriptor["attestationSubject"],
                "repository": args.repository,
                "workflow": workflow,
                "predicateType": "https://slsa.dev/provenance/v1",
                "run": {"id": args.run_id, "attempt": args.run_attempt, "url": run_url},
            }
        },
    }
    if descriptor["artifact"].get("kind") == "native-binary":
        health = NATIVE_HTTP_READINESS.get(component_id)
        if health is None:
            raise ValueError(
                f"native component has no trusted HTTP readiness endpoint: {component_id}"
            )
        document["health"] = dict(health)
    if isinstance(compatibility_group, dict):
        contract_lock = _verify_protocol_lock(compatibility_group)
        document["compatibility"] = {
            "groupId": compatibility_group["groupId"],
            "groupVersion": compatibility_group["groupVersion"],
            "contractApiVersion": compatibility_group["contractApiVersion"],
            "wireApiVersion": compatibility_group["wireApiVersion"],
            "contractLock": contract_lock,
        }
    document["manifestDigest"] = _jcs_digest(document, "manifestDigest")
    return document


def command_package(args: argparse.Namespace) -> None:
    """Create the deterministic native archive and its publisher-owned manifest."""
    source = args.payload.resolve()
    output = args.output.resolve()
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"payload directory is missing or unsafe: {source}")
    if output.exists():
        shutil.rmtree(output)
    artifact_dir = output / "artifacts"
    manifest_dir = output / "manifests"
    artifact_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    component = args.component_id
    target_slug = re.sub(r"[^a-z0-9-]+", "-", args.target_id.lower()).strip("-")
    asset_name = f"{component}-{target_slug}.tar.gz"
    artifact_path = artifact_dir / asset_name
    _archive(source, artifact_path)
    files = _files(source)
    artifact = {
        "kind": "native-binary",
        "format": "tar.gz",
        "uri": f"https://github.com/{args.repository}/releases/download/{args.release_id}/{asset_name}",
        "sha256": _sha256(artifact_path.read_bytes()),
        "sizeBytes": artifact_path.stat().st_size,
        "files": files,
        "entrypoint": args.entrypoint,
    }
    metadata = {
        "componentId": component,
        "contentDigest": artifact["sha256"],
        "artifact": artifact,
        "attestationSubject": asset_name,
    }
    descriptor_path = output / "descriptor.json"
    descriptor_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest = create_manifest(
        argparse.Namespace(
            repository=args.repository,
            source_ref=args.source_ref,
            source_commit=args.source_commit,
            channel=args.channel,
            release_id=args.release_id,
            component_id=args.component_id,
            component_release=args.component_release,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            version=args.version,
            target=args.target,
            metadata=descriptor_path,
            target_id=args.target_id,
            catalog=args.catalog,
            catalog_sha256=args.catalog_sha256,
        )
    )
    name = f"{component}-{target_slug}.manifest.json"
    (manifest_dir / name).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    descriptor_path.unlink()


def command_manifest(args: argparse.Namespace) -> None:
    manifest = create_manifest(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def command_index(args: argparse.Namespace) -> None:
    """Create the v1 index for one component or a legacy repository release."""
    expected_release_id = _expected_release_id(
        args.channel, args.source_commit, args.component_id
    )
    if args.release_id != expected_release_id:
        raise ValueError(
            "release ID does not match channel, component scope, and source.commit"
        )
    allowed_refs = {
        "preview": {"refs/heads/develop"},
        "stable": {"refs/heads/main", "refs/heads/release"},
    }
    if args.source_ref not in allowed_refs[args.channel]:
        raise ValueError("source.ref does not match the immutable release channel")
    rows: list[dict[str, Any]] = []
    found_components: set[str] = set()
    for path in sorted(args.manifests.glob("*.manifest.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        component_id = manifest.get("componentId")
        if component_id not in CONNECTION_COMPONENT_IDS:
            raise ValueError(f"unknown component in release manifest: {component_id}")
        if args.component_id is not None and component_id != args.component_id:
            raise ValueError(
                "component-scoped index contains a manifest for another component"
            )
        if (
            manifest.get("releaseId") != args.release_id
            or manifest.get("channel") != args.channel
        ):
            raise ValueError(
                f"manifest release identity differs from requested index: {path.name}"
            )
        source = manifest.get("source")
        if not isinstance(source, dict) or (
            source.get("commit") != args.source_commit
            or source.get("ref") != args.source_ref
            or source.get("repository") != f"https://github.com/{args.repository}"
        ):
            raise ValueError(
                f"manifest source differs from requested index: {path.name}"
            )
        found_components.add(component_id)
        rows.append(
            {
                "componentId": manifest["componentId"],
                "version": manifest["version"],
                "target": manifest["target"],
                "manifestUri": f"https://github.com/{args.repository}/releases/download/{args.release_id}/{path.name}",
                "manifestDigest": manifest["manifestDigest"],
            }
        )
    if not rows:
        raise ValueError("no component manifests found")
    if args.component_id is not None and found_components != {args.component_id}:
        raise ValueError(
            "component index must contain at least one manifest for its selected component"
        )
    index: dict[str, Any] = {
        "schemaVersion": 1,
        "repository": args.repository,
        "channel": args.channel,
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": {
            "repository": f"https://github.com/{args.repository}",
            "ref": args.source_ref,
            "commit": args.source_commit,
        },
        "provenance": {
            "attestation": {
                "kind": "github-artifact-attestation",
                "subjectName": "component-release-index-v1.json",
                "repository": args.repository,
                "workflow": f"{args.repository}/.github/workflows/component-release.yml",
                "predicateType": "https://slsa.dev/provenance/v1",
                "run": {
                    "id": args.run_id,
                    "attempt": args.run_attempt,
                    "url": f"https://github.com/{args.repository}/actions/runs/{args.run_id}/attempts/{args.run_attempt}",
                },
            }
        },
        "releases": rows,
        "compatibilityGroups": [],
    }
    index["indexDigest"] = _jcs_digest(index, "indexDigest")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    package = commands.add_parser(
        "package", help="create a reproducible binary archive and its v2 manifest"
    )
    package.add_argument("--component-id", required=True)
    package.add_argument("--payload", required=True, type=Path)
    package.add_argument("--output", required=True, type=Path)
    package.add_argument("--entrypoint", required=True)
    package.add_argument("--compatibility", type=Path)
    _base_args(package)
    package.set_defaults(handler=command_package)
    manifest = commands.add_parser("manifest", help="create an OCI v2 manifest")
    manifest.add_argument("--output", required=True, type=Path)
    manifest.add_argument("--metadata", required=True, type=Path)
    manifest.add_argument("--component-id")
    _base_args(manifest)
    manifest.set_defaults(handler=command_manifest)
    index = commands.add_parser("index", help="create the publisher-owned v1 index")
    index.add_argument("--repository", required=True)
    index.add_argument("--release-id", required=True)
    index.add_argument("--channel", choices=("stable", "preview"), required=True)
    index.add_argument("--source-ref", required=True)
    index.add_argument("--source-commit", required=True)
    index.add_argument("--run-id", required=True)
    index.add_argument("--run-attempt", required=True, type=int)
    index.add_argument("--component-id")
    index.add_argument("--manifests", required=True, type=Path)
    index.add_argument("--output", required=True, type=Path)
    index.set_defaults(handler=command_index)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.handler(args)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise SystemExit(f"connection release: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
