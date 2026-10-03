#!/usr/bin/env python3
"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 select_connection_components.py                                      │
│  Module: tools.ci.select_connection_components                           │
│  Role: Select affected connection components and catalog targets.       │
│                                                                          │
│  模块职责：按变更路径和可信 catalog 选择受影响组件及发布 target。          │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

REPOSITORY = "DoHorizon-AI/Cyrene-Plugins-Official"
WORKFLOW = f"{REPOSITORY}/.github/workflows/component-release.yml"
COMPONENTS = {
    "cy-workspace-relay": {
        "package": "cyrene-workspace-relay",
        "binary": "cy-workspace-relay",
        "service": "cy-workspace-relay.service",
        "dockerfile": "runtime/rust/Dockerfile.workspace-relay",
    },
    "cy-workspace-connector": {
        "package": "cyrene-workspace-connector",
        "binary": "cyrene-workspace-connector",
        "service": "cy-workspace-connector.service",
        "dockerfile": "runtime/rust/Dockerfile.workspace-connector",
    },
    "cy-workspace-sidecar": {
        "package": "cyrene-workspace-sidecar",
        "binary": "cyrene-workspace-sidecar",
        "service": "cy-workspace-sidecar.service",
        "dockerfile": "runtime/rust/Dockerfile.workspace-sidecar",
    },
    "cy-workspace-frontend-bridge": {
        "package": "cyrene-workspace-frontend-bridge",
        "binary": "cyrene-workspace-frontend-bridge",
        "service": "cy-workspace-frontend-bridge.service",
        "dockerfile": "runtime/rust/Dockerfile.frontend-bridge",
    },
}
COMPONENT_ORDER = tuple(COMPONENTS)
NATIVE_RUNNERS = {
    "linux-ubuntu-22.04-x86_64-systemd": "ubuntu-22.04",
    "linux-ubuntu-24.04-x86_64-systemd": "ubuntu-24.04",
}
SERVICE_TO_COMPONENT = {
    f"runtime/systemd/{item['service']}": component_id
    for component_id, item in COMPONENTS.items()
}
DOCKERFILE_TO_COMPONENT = {
    item["dockerfile"]: component_id for component_id, item in COMPONENTS.items()
}


def _verified_catalog(path: Path, expected_sha256: str | None) -> dict[str, Any]:
    """Load the exact catalog bytes and check their publisher authorization.

    Returns the decoded catalog. Raises ``ValueError`` on digest, shape, or
    publisher mismatch. 按摘要和发布方约束读取 catalog。
    """
    raw = path.read_bytes()
    if expected_sha256 is not None:
        expected = expected_sha256.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(
                "catalog SHA-256 must be 64 lowercase hexadecimal characters"
            )
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(
                "verified catalog artifact does not match its metadata digest"
            )
    value = json.loads(raw.decode("utf-8", "strict"))
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ValueError("component catalog has an unsupported schema")
    publishers = value.get("publishers")
    if not isinstance(publishers, list) or not any(
        isinstance(item, dict)
        and item.get("repository") == REPOSITORY
        and item.get("workflow") == WORKFLOW
        for item in publishers
    ):
        raise ValueError("catalog does not authorize this component publisher workflow")
    return value


def _changed_paths(root: Path, base: str, head: str) -> list[str]:
    """Return changed repository-relative paths for two exact Git commits.

    A zero base SHA represents a new branch and selects all components.
    返回两个精确提交间的变更路径；新分支按全量变更处理。
    """
    for label, revision in (("base", base), ("head", head)):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError(f"{label} revision must be a full lowercase Git SHA")
    zero_sha = "0" * 40
    if base == zero_sha:
        return ["*"]
    for revision in (base, head):
        subprocess.run(
            ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACDMRT", "-z", base, head],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [
        value.decode("utf-8", "strict") for value in result.stdout.split(b"\0") if value
    ]


def _component_for_path(path: str) -> set[str]:
    """Map one repository path to the component rebuild set.

    Unknown shared runtime paths conservatively select every component.
    将单个路径映射到组件集合；共享 runtime 未知路径保守全选。
    """
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in path:
        raise ValueError(f"unsafe changed path: {path}")

    if path == "*":
        return set(COMPONENTS)
    if path in {
        ".github/workflows/component-release.yml",
        "tools/ci/connection_release.py",
        "tools/ci/preflight_component_release.py",
        "tools/ci/select_connection_components.py",
        "tools/ci/connection-release-requirements.txt",
    }:
        return set(COMPONENTS)
    if path in SERVICE_TO_COMPONENT:
        return {SERVICE_TO_COMPONENT[path]}
    if path in DOCKERFILE_TO_COMPONENT:
        return {DOCKERFILE_TO_COMPONENT[path]}
    if path.startswith("runtime/rust/cyrene-workspace-product-adapters/"):
        return {"cy-workspace-connector"}
    if path.startswith("sdk/rust/cyrene-workspace-client-sdk/"):
        return set(COMPONENTS)
    if path.startswith("contracts/"):
        return set(COMPONENTS)
    if path in {
        "Cargo.toml",
        "Cargo.lock",
        "runtime/rust/Cargo.toml",
        "runtime/rust/Cargo.lock",
    }:
        return set(COMPONENTS)

    for component_id, item in COMPONENTS.items():
        if path.startswith(f"runtime/rust/{item['package']}/"):
            return {component_id}
    if path.startswith(
        ("runtime/rust/cyrene-workspace-", "runtime/systemd/cy-workspace-")
    ):
        # Unknown packages in the shared runtime roots require a conservative rebuild.
        return set(COMPONENTS)
    return set()


def _selected_components(
    root: Path,
    requested: str | None,
    base: str | None,
    head: str | None,
) -> list[str]:
    """Resolve a manual selection or calculate the path-based impact set.

    Component order remains stable so matrix output does not depend on set
    iteration. 解析手动选择或按路径计算影响范围。
    """
    if requested is not None:
        if requested == "all":
            return list(COMPONENT_ORDER)
        if requested not in COMPONENTS:
            raise ValueError(f"unknown component selection: {requested}")
        return [requested]
    if base is None or head is None:
        raise ValueError("provide --component or both --base and --head")
    selected: set[str] = set()
    for path in _changed_paths(root, base, head):
        selected.update(_component_for_path(path))
    return [
        component_id for component_id in COMPONENT_ORDER if component_id in selected
    ]


def _matrices(
    catalog: dict[str, Any], component_ids: list[str]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build native and OCI matrices from supported catalog targets.

    Raises ``ValueError`` for unknown publisher components or unsupported
    target mappings. 按 catalog 受支持 target 生成构建矩阵。
    """
    components = catalog.get("components")
    if not isinstance(components, list):
        raise TypeError("component catalog has no component list")
    by_id: dict[str, dict[str, Any]] = {}
    for entry in components:
        if isinstance(entry, dict) and entry.get("publisher") == REPOSITORY:
            component_id = entry.get("componentId")
            if component_id not in COMPONENTS:
                raise ValueError(
                    f"catalog contains an unknown component for this publisher: {component_id}"
                )
            if component_id in by_id:
                raise ValueError(
                    f"catalog contains duplicate component entry: {component_id}"
                )
            by_id[component_id] = entry

    native: list[dict[str, str]] = []
    oci: list[dict[str, str]] = []
    for component_id in component_ids:
        component = by_id.get(component_id)
        if component is None:
            raise ValueError(
                f"verified catalog does not authorize component: {component_id}"
            )
        targets = component.get("targets")
        if not isinstance(targets, list):
            raise TypeError(f"component targets are malformed: {component_id}")
        for target in targets:
            if not isinstance(target, dict) or target.get("support") != "supported":
                continue
            target_id = target.get("targetId")
            artifact_kind = target.get("artifactKind")
            if not isinstance(target_id, str):
                raise TypeError(f"supported target lacks targetId: {component_id}")
            common = {
                "component_id": component_id,
                "target_id": target_id,
                "package": COMPONENTS[component_id]["package"],
                "binary": COMPONENTS[component_id]["binary"],
            }
            if artifact_kind == "native-binary":
                runner = NATIVE_RUNNERS.get(target_id)
                if runner is None:
                    raise ValueError(
                        f"no native runner mapping for catalog target {target_id}"
                    )
                native.append({**common, "runner": runner})
            elif artifact_kind == "oci-image":
                image_repository = component.get("ociImageRepository")
                if not isinstance(image_repository, str) or not image_repository:
                    raise ValueError(f"OCI repository is missing for {component_id}")
                if not any(
                    isinstance(candidate, dict)
                    and candidate.get("targetId") == "linux-ubuntu-22.04-x86_64-systemd"
                    and candidate.get("artifactKind") == "native-binary"
                    and candidate.get("support") == "supported"
                    for candidate in targets
                ):
                    raise ValueError(
                        f"OCI component has no supported Ubuntu 22.04 native source: {component_id}"
                    )
                oci.append(
                    {
                        **common,
                        "image_repository": image_repository,
                        "dockerfile": COMPONENTS[component_id]["dockerfile"],
                    }
                )
            else:
                raise ValueError(
                    f"unsupported artifact kind in trusted catalog: {artifact_kind}"
                )
    return native, oci


def _encode(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def main() -> int:
    """Validate inputs, print JSON outputs, and append optional GHA outputs.

    Returns 0 on success and 2 when selection cannot be trusted.
    校验输入并输出 JSON；选择数据不可信时返回 2。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--catalog-sha256")
    parser.add_argument("--component", help="one component ID or all")
    parser.add_argument("--base", help="base commit for automatic path selection")
    parser.add_argument("--head", help="head commit for automatic path selection")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        catalog = _verified_catalog(args.catalog, args.catalog_sha256)
        component_ids = _selected_components(
            args.root.resolve(), args.component, args.base, args.head
        )
        native, oci = _matrices(catalog, component_ids)
        outputs = {
            "component_ids": component_ids,
            "component_matrix": {
                "include": [
                    {
                        "component_id": value,
                        "has_oci": any(row["component_id"] == value for row in oci),
                    }
                    for value in component_ids
                ]
            },
            "native_matrix": {"include": native},
            "oci_matrix": {"include": oci},
            "component_count": len(component_ids),
            "native_count": len(native),
            "oci_count": len(oci),
        }
        if args.github_output is not None:
            with args.github_output.open("a", encoding="utf-8") as output:
                for name, value in outputs.items():
                    output.write(f"{name}={_encode(value)}\n")
        print(_encode(outputs))
    except (
        OSError,
        TypeError,
        ValueError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as error:
        print(f"component selection failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
