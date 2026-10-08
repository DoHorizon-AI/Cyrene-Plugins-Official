"""
Build and verify immutable Package Runtime releases for official Python plugins.

This producer packages source-owned plugin modules, vendored SDK source,
exact runtime locks, Package Spec descriptors, SBOMs, and component release
metadata from one Plugins commit.
中文：从同一 Plugins commit 构建可独立安装的官方 Python 插件包与发布证明。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import tomllib
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

if __package__ in {None, ""}:
    # Direct script execution starts at tooling/release rather than the repo root.
    # 中文：脚本路径执行时需显式加入仓库根目录，才能复用同仓发布 helper。
    repository_root = str(Path(__file__).resolve().parents[2])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)

from tooling.release import training_llama_factory_package as training_release

REPOSITORY = "DoHorizon-AI/Cyrene-Plugins-Official"
SOURCE_REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
WORKFLOW_PATH = ".github/workflows/data-tools-package-release.yml"
PRODUCER_PATH = "tooling/release/official_plugin_package.py"
TRAINING_PRODUCER_PATH = "tooling/release/training_llama_factory_package.py"
BUILD_LOCK_PATH = "tooling/release/build-requirements.lock"
JCS_LOCK_PATH = "tools/ci/connection-release-requirements.txt"
TARGET_ID = "linux-ubuntu-24.04-x86_64-python-3.12"
TARGET = {
    "os": "linux",
    "osVersion": "24.04",
    "distribution": "ubuntu",
    "distributionVersion": "24.04",
    "architecture": "x86_64",
    "abi": "glibc-2.39",
    "runtime": "python:3.12",
}
SUPPORTED_CHANNELS = {"candidate", "preview", "stable"}
CAPABILITY_PROTOCOL = "cyrene.plugin.runtime.v1"
PREPARER_DISTRIBUTION = "cyrene-plugin-runtime"
PREPARER_VERSION = "0.2.0"
PREPARER_WHEEL = "cyrene_plugin_runtime-0.2.0-py3-none-any.whl"
SBOM_SPEC_VERSION = "1.6"
SBOM_FORMAT = "CycloneDX"
RELEASE_INDEX_NAME = "component-release-index-v1.json"
COMPONENT_MANIFEST_NAME = "component-release-manifest-v2.json"
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
LOCK_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)$")


@dataclass(frozen=True)
class VendoredSdk:
    """Identify one internal Python distribution copied into a package ZIP."""

    distribution: str
    project_path: str
    source_root: str
    archive_root: str


@dataclass(frozen=True)
class PackageSpec:
    """Bind a dotted package identity to its explicit repository paths."""

    package_id: str
    component_id: str
    source_dir: str
    requirements_input: str
    vendored_sdks: tuple[VendoredSdk, ...]


RUNTIME_SDK = VendoredSdk(
    distribution="cyrene-plugin-runtime",
    project_path="sdk/python/cyrene_plugin_runtime/pyproject.toml",
    source_root="sdk/python/cyrene_plugin_runtime/src/cyrene_plugin_runtime",
    archive_root="src/cyrene_plugin_runtime",
)
MODEL_PROVIDER_SDK = VendoredSdk(
    distribution="cyrene-model-provider-contracts",
    project_path="sdk/python/cyrene_model_provider_contracts/pyproject.toml",
    source_root="sdk/python/cyrene_model_provider_contracts/src/cyrene_model_provider_contracts",
    archive_root="src/cyrene_model_provider_contracts",
)

# These mappings are explicit contract data. Never derive a package/component
# identity by replacing punctuation in the other identity.
PACKAGE_SPECS: dict[str, PackageSpec] = {
    "cyrene.tools.dataset-preparation": PackageSpec(
        package_id="cyrene.tools.dataset-preparation",
        component_id="cyrene-tools-dataset-preparation",
        source_dir="plugins/tools/dataset-preparation",
        requirements_input="tooling/release/requirements/dataset-preparation.in",
        vendored_sdks=(RUNTIME_SDK,),
    ),
    "cyrene.tools.document-parsing": PackageSpec(
        package_id="cyrene.tools.document-parsing",
        component_id="cyrene-tools-document-parsing",
        source_dir="plugins/tools/document-parsing",
        requirements_input="tooling/release/requirements/document-parsing.in",
        vendored_sdks=(RUNTIME_SDK,),
    ),
    "cyrene.tools.dataset-generation": PackageSpec(
        package_id="cyrene.tools.dataset-generation",
        component_id="cyrene-tools-dataset-generation",
        source_dir="plugins/tools/dataset-generation",
        requirements_input="tooling/release/requirements/dataset-generation.in",
        vendored_sdks=(RUNTIME_SDK, MODEL_PROVIDER_SDK),
    ),
    "cyrene.tools.knowledge-preparation": PackageSpec(
        package_id="cyrene.tools.knowledge-preparation",
        component_id="cyrene-tools-knowledge-preparation",
        source_dir="plugins/tools/knowledge-preparation",
        requirements_input="tooling/release/requirements/knowledge-preparation.in",
        vendored_sdks=(RUNTIME_SDK,),
    ),
    "cyrene.evaluation.exact-match": PackageSpec(
        package_id="cyrene.evaluation.exact-match",
        component_id="cyrene-evaluation-exact-match",
        source_dir="plugins/evaluation/exact-match",
        requirements_input="tooling/release/requirements/exact-match.in",
        vendored_sdks=(RUNTIME_SDK,),
    ),
}

PRODUCT_SERVICE_COMPONENTS = {
    "Cyrene-Catalyst": "cyrene-catalyst",
    "Cyrene-Echo": "cyrene-echo",
}


class PackageBuildError(ValueError):
    """Raised when package inputs or release outputs violate the frozen contract."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PackageBuildError(f"cannot read JSON metadata {path}: {error}") from error
    if not isinstance(value, dict):
        raise PackageBuildError(f"JSON metadata must be an object: {path}")
    return value


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _canonical_json(value: Any) -> bytes:
    """Serialize the restricted manifest/index values using RFC 8785 JCS."""

    try:
        import rfc8785
    except ImportError:
        # All fields emitted here are strings, booleans, integers, lists, and
        # objects with ASCII keys; with floats excluded, this is JCS-compatible.
        def reject_floats(item: Any) -> None:
            if isinstance(item, float):
                raise PackageBuildError(
                    "JCS metadata may not contain floating-point values"
                )
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str) or not key.isascii():
                        raise PackageBuildError("JCS object keys must be ASCII strings")
                    reject_floats(child)
            elif isinstance(item, list):
                for child in item:
                    reject_floats(child)

        reject_floats(value)
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    return rfc8785.dumps(value)


def _jcs_digest(value: dict[str, Any], self_field: str) -> str:
    unsigned = {key: item for key, item in value.items() if key != self_field}
    return training_release.sha256_bytes(_canonical_json(unsigned))


def _read_package_spec(
    root: Path, spec: PackageSpec
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = root / spec.source_dir / "plugin.manifest.json"
    pyproject_path = root / spec.source_dir / "pyproject.toml"
    manifest = _read_json(manifest_path)
    try:
        project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise PackageBuildError(
            f"cannot read Plugin project metadata: {error}"
        ) from error
    if manifest.get("id") != spec.package_id:
        raise PackageBuildError(
            f"explicit component mapping disagrees with Plugin manifest: {spec.package_id}"
        )
    project_record = project.get("project")
    if not isinstance(project_record, dict):
        raise PackageBuildError(
            f"Plugin pyproject has no [project] table: {pyproject_path}"
        )
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version
    ):
        raise PackageBuildError(f"Plugin version is not valid SemVer: {version!r}")
    if project_record.get("version") != version:
        raise PackageBuildError(
            "Plugin manifest and Python distribution versions differ"
        )
    capabilities = manifest.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or len(capabilities) != 1
        or not all(isinstance(item, str) and item for item in capabilities)
    ):
        raise PackageBuildError("each official package must declare one capability")
    methods = manifest.get("methods")
    if (
        not isinstance(methods, list)
        or not methods
        or any(
            not isinstance(method, dict) or method.get("interfaceVersion") != "1"
            for method in methods
        )
    ):
        raise PackageBuildError("each official package must expose interface version 1")
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        raise PackageBuildError("Plugin manifest runtime is missing")
    entrypoint = runtime.get("entrypoint")
    launch = runtime.get("launch")
    if (
        runtime.get("language") != "python"
        or runtime.get("protocol") != "cyrene.plugin.runtime.v1.DirectPluginRuntime"
        or not isinstance(entrypoint, str)
        or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*", entrypoint
        )
        or not isinstance(launch, dict)
        or launch.get("executable") != "prepared-runtime"
        or launch.get("args")
        != [
            "-B",
            "src/cyrene_plugin_runtime/bootstrap.py",
            "--entrypoint",
            entrypoint,
        ]
    ):
        raise PackageBuildError(
            "Plugin runtime launch differs from the packaged SDK layout"
        )
    services = manifest.get("supportedServices")
    if (
        not isinstance(services, list)
        or not services
        or any(service not in PRODUCT_SERVICE_COMPONENTS for service in services)
    ):
        raise PackageBuildError("Plugin supportedServices are missing or unmapped")
    distribution = project_record.get("name")
    if not isinstance(distribution, str) or not distribution:
        raise PackageBuildError("Python distribution name is missing")
    return manifest, {
        "package_id": spec.package_id,
        "component_id": spec.component_id,
        "version": version,
        "capability": capabilities[0],
        "entrypoint": entrypoint,
        "distribution": distribution,
        "supported_services": services,
    }


def _regular_source(path: Path, root: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PackageBuildError(f"required package source is missing or unsafe: {path}")
    if not path.resolve(strict=True).is_relative_to(root):
        raise PackageBuildError(f"package source resolves outside repository: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise PackageBuildError(
            f"cannot read package source {path}: {error}"
        ) from error


def _walk_python_sources(path: Path, root: Path) -> dict[str, bytes]:
    if not path.is_dir() or path.is_symlink():
        raise PackageBuildError(
            f"internal SDK source directory is missing or unsafe: {path}"
        )
    files: dict[str, bytes] = {}
    for candidate in sorted(path.rglob("*")):
        if candidate.is_symlink():
            raise PackageBuildError(
                f"source tree may not contain a symlink: {candidate}"
            )
        if candidate.is_file() and (
            candidate.suffix == ".py" or candidate.name == "py.typed"
        ):
            files[candidate.relative_to(root).as_posix()] = _regular_source(
                candidate, root
            )
    if not files:
        raise PackageBuildError(f"internal SDK has no Python source files: {path}")
    return files


def _source_files(
    root: Path, spec: PackageSpec
) -> tuple[dict[str, bytes], dict[str, dict[str, bytes]]]:
    """Collect exactly the plugin, SDK, lock, and publisher inputs to the release."""

    root = root.resolve(strict=True)
    package_root = root / spec.source_dir
    candidates: set[Path] = {
        root / "LICENSE",
        root / spec.source_dir / "README.md",
        root / spec.source_dir / "pyproject.toml",
        root / spec.source_dir / "plugin.manifest.json",
        root / spec.source_dir / "requirements.lock",
        root / spec.requirements_input,
        root / BUILD_LOCK_PATH,
        root / JCS_LOCK_PATH,
        root / PRODUCER_PATH,
        root / TRAINING_PRODUCER_PATH,
        root / WORKFLOW_PATH,
    }
    for path in package_root.rglob("*"):
        if path.is_symlink():
            raise PackageBuildError(
                f"Plugin source tree may not contain a symlink: {path}"
            )
        if not path.is_file():
            continue
        relative = path.relative_to(package_root)
        if (
            (path.suffix == ".py" and "tests" not in relative.parts)
            or (path.suffix == ".json" and "contracts" in relative.parts)
            or path.name == "PROVENANCE.md"
        ):
            candidates.add(path)

    files = {
        path.relative_to(root).as_posix(): _regular_source(path, root)
        for path in sorted(candidates)
    }
    sdk_files: dict[str, dict[str, bytes]] = {}
    for sdk in spec.vendored_sdks:
        sdk_files[sdk.distribution] = _walk_python_sources(root / sdk.source_root, root)
        project_path = root / sdk.project_path
        files[project_path.relative_to(root).as_posix()] = _regular_source(
            project_path, root
        )
        files.update(sdk_files[sdk.distribution])
    return files, sdk_files


def _lock_lines(payload: bytes, label: str) -> list[str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeError as error:
        raise PackageBuildError(f"dependency lock is not UTF-8: {label}") from error
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    pins = [line for line in lines if not line.startswith("#")]
    if not pins or any(LOCK_PIN.fullmatch(line) is None for line in pins):
        raise PackageBuildError(
            f"dependency lock must contain only exact Name==version pins: {label}"
        )
    normalized = [canonicalize_name(LOCK_PIN.fullmatch(line).group(1)) for line in pins]
    if len(normalized) != len(set(normalized)):
        raise PackageBuildError(f"dependency lock repeats a distribution: {label}")
    if not {"grpcio", "protobuf"}.issubset(normalized):
        raise PackageBuildError(
            "runtime SDK dependencies grpcio and protobuf must be locked"
        )
    return pins


def _verify_lock_inputs(spec: PackageSpec, source_files: dict[str, bytes]) -> list[str]:
    lock_path = f"{spec.source_dir}/requirements.lock"
    lock = source_files[lock_path]
    pin_lines = _lock_lines(lock, lock_path)
    input_bytes = source_files[spec.requirements_input]
    try:
        input_requirements = [
            Requirement(line.strip())
            for line in input_bytes.decode("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except (UnicodeError, ValueError) as error:
        raise PackageBuildError(
            f"dependency input is invalid: {spec.requirements_input}"
        ) from error
    pin_versions = {
        canonicalize_name(match.group(1)): match.group(2)
        for line in pin_lines
        if (match := LOCK_PIN.fullmatch(line)) is not None
    }
    for requirement in input_requirements:
        name = canonicalize_name(requirement.name)
        version = pin_versions.get(name)
        if version is None:
            raise PackageBuildError(f"lock omits direct runtime requirement: {name}")
        if requirement.specifier and not requirement.specifier.contains(
            version, prereleases=True
        ):
            raise PackageBuildError(
                f"lock pin {name}=={version} violates input range {requirement.specifier}"
            )

    package_project = tomllib.loads(
        source_files[f"{spec.source_dir}/pyproject.toml"].decode("utf-8")
    )["project"]
    project_requirements = [
        Requirement(value) for value in package_project.get("dependencies", [])
    ]
    sdk_by_name = {
        canonicalize_name(sdk.distribution): sdk for sdk in spec.vendored_sdks
    }
    input_by_name = {
        canonicalize_name(requirement.name): requirement
        for requirement in input_requirements
    }
    input_comments = input_bytes.decode("utf-8")
    for requirement in project_requirements:
        name = canonicalize_name(requirement.name)
        sdk = sdk_by_name.get(name)
        if sdk is not None:
            project = tomllib.loads(source_files[sdk.project_path].decode("utf-8"))
            sdk_version = project.get("project", {}).get("version")
            if not isinstance(sdk_version, str) or not requirement.specifier.contains(
                sdk_version, prereleases=True
            ):
                raise PackageBuildError(
                    f"vendored SDK version does not satisfy Plugin dependency: {sdk.distribution}"
                )
            if f"{sdk.distribution}=={sdk_version}" not in input_comments:
                raise PackageBuildError(
                    f"lock input must bind vendored SDK source version: {sdk.distribution}"
                )
            for sdk_dependency in project.get("project", {}).get("dependencies", []):
                dependency = Requirement(sdk_dependency)
                dependency_name = canonicalize_name(dependency.name)
                lock_version = pin_versions.get(dependency_name)
                if lock_version is None or (
                    dependency.specifier
                    and not dependency.specifier.contains(
                        lock_version, prereleases=True
                    )
                ):
                    raise PackageBuildError(
                        f"lock does not satisfy vendored SDK requirement: {dependency_name}"
                    )
            continue

        lock_input = input_by_name.get(name)
        if (
            lock_input is None
            or lock_input.extras != requirement.extras
            or str(lock_input.specifier) != str(requirement.specifier)
        ):
            raise PackageBuildError(
                f"runtime lock input does not preserve Plugin dependency: {name}"
            )

    if not input_requirements:
        raise PackageBuildError("runtime lock input has no external requirements")
    if len(input_by_name) != len(input_requirements):
        raise PackageBuildError("runtime lock input repeats a distribution")
    missing = sorted(set(input_by_name).difference(pin_versions))
    if missing:
        raise PackageBuildError(f"lock omits direct runtime requirements: {missing}")
    return pin_lines


def _package_entries(
    source_files: dict[str, bytes],
    spec: PackageSpec,
    sdk_files: dict[str, dict[str, bytes]],
) -> dict[str, bytes]:
    entries: dict[str, bytes] = {"LICENSE": source_files["LICENSE"]}
    prefix = f"{spec.source_dir}/"
    for source, content in source_files.items():
        if not source.startswith(prefix):
            continue
        relative = source[len(prefix) :]
        if relative.endswith(".py"):
            entries[f"src/{relative}"] = content
        elif relative in {
            "README.md",
            "PROVENANCE.md",
            "plugin.manifest.json",
            "requirements.lock",
        } or (relative.startswith("contracts/") and relative.endswith(".json")):
            entries[relative] = content
    for sdk in spec.vendored_sdks:
        sdk_prefix = f"{sdk.source_root}/"
        for source, content in sdk_files[sdk.distribution].items():
            if not source.startswith(sdk_prefix):
                continue
            relative = source[len(sdk_prefix) :]
            entries[f"{sdk.archive_root}/{relative}"] = content
    if "src/cyrene_plugin_runtime/bootstrap.py" not in entries:
        raise PackageBuildError("package archive lacks the Python runtime bootstrap")
    return entries


def _sdk_metadata(
    spec: PackageSpec,
    source_ref: str,
    source_sha: str,
    source_files: dict[str, bytes],
    sdk_files: dict[str, dict[str, bytes]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for sdk in spec.vendored_sdks:
        files = sdk_files[sdk.distribution]
        project_path = sdk.project_path
        raw_project = source_files[project_path]
        project = tomllib.loads(raw_project.decode("utf-8"))
        project_record = project.get("project", {})
        if project_record.get("name") != sdk.distribution:
            raise PackageBuildError(
                f"vendored SDK project name mismatch: {sdk.distribution}"
            )
        version = project_record.get("version")
        if not isinstance(version, str) or not version:
            raise PackageBuildError(
                f"vendored SDK version is missing: {sdk.distribution}"
            )
        all_inputs = {project_path: raw_project, **files}
        source_inputs, tree_digest = training_release._source_index(all_inputs)
        records.append(
            {
                "distribution": sdk.distribution,
                "version": version,
                "source": {
                    "repository": SOURCE_REPOSITORY_URL,
                    "ref": source_ref,
                    "commit": source_sha,
                    "input_tree_digest": tree_digest,
                    "inputs": source_inputs,
                },
                "delivery": "vendored-zip-src",
                "archive_root": sdk.archive_root,
            }
        )
    return records


def _sbom_document(
    package: dict[str, Any],
    spec: PackageSpec,
    source_ref: str,
    source_sha: str,
    source_files: dict[str, bytes],
    sdk_files: dict[str, dict[str, bytes]],
    pins: list[str],
) -> dict[str, Any]:
    package_ref = (
        f"pkg:pypi/{canonicalize_name(package['distribution'])}@{package['version']}"
    )
    components: list[dict[str, Any]] = []
    dependency_refs: list[str] = []
    for pin in pins:
        match = LOCK_PIN.fullmatch(pin)
        assert match is not None
        name, version = match.groups()
        reference = f"pkg:pypi/{canonicalize_name(name)}@{version}"
        dependency_refs.append(reference)
        components.append(
            {
                "type": "library",
                "name": canonicalize_name(name),
                "version": version,
                "purl": reference,
                "bom-ref": reference,
            }
        )
    for sdk in spec.vendored_sdks:
        version = tomllib.loads(source_files[sdk.project_path].decode("utf-8"))[
            "project"
        ]["version"]
        reference = f"pkg:pypi/{canonicalize_name(sdk.distribution)}@{version}"
        dependency_refs.append(reference)
        inputs = {
            sdk.project_path: source_files[sdk.project_path],
            **sdk_files[sdk.distribution],
        }
        records, tree_digest = training_release._source_index(inputs)
        properties = [
            {"name": "cyrene:source:repository", "value": SOURCE_REPOSITORY_URL},
            {"name": "cyrene:source:ref", "value": source_ref},
            {"name": "cyrene:source:commit", "value": source_sha},
            {"name": "cyrene:source:input-tree-digest", "value": tree_digest},
            {
                "name": "cyrene:source:inputs",
                "value": json.dumps(records, sort_keys=True, separators=(",", ":")),
            },
            {
                "name": "cyrene:delivery",
                "value": f"vendored-zip-src:{sdk.archive_root}",
            },
        ]
        components.append(
            {
                "type": "library",
                "name": sdk.distribution,
                "version": version,
                "purl": reference,
                "bom-ref": reference,
                "externalReferences": [{"type": "vcs", "url": SOURCE_REPOSITORY_URL}],
                "properties": properties,
            }
        )
    package_properties = [
        {"name": "cyrene:component-id", "value": spec.component_id},
        {"name": "cyrene:capability-id", "value": package["capability"]},
        {"name": "cyrene:source:repository", "value": SOURCE_REPOSITORY_URL},
        {"name": "cyrene:source:ref", "value": source_ref},
        {"name": "cyrene:source:commit", "value": source_sha},
        {
            "name": "cyrene:package:requirements-lock",
            "value": f"{spec.source_dir}/requirements.lock",
        },
    ]
    return {
        "bomFormat": SBOM_FORMAT,
        "specVersion": SBOM_SPEC_VERSION,
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": spec.package_id,
                "version": package["version"],
                "bom-ref": package_ref,
                "properties": package_properties,
                "externalReferences": [{"type": "vcs", "url": SOURCE_REPOSITORY_URL}],
            }
        },
        "components": sorted(components, key=lambda row: row["bom-ref"]),
        "dependencies": [
            {"ref": package_ref, "dependsOn": sorted(set(dependency_refs))}
        ],
        "properties": [
            {"name": "cyrene:target", "value": TARGET_ID},
            {
                "name": "cyrene:plugin-manifest-digest",
                "value": training_release.sha256_bytes(
                    source_files[f"{spec.source_dir}/plugin.manifest.json"]
                ),
            },
        ],
    }


def _component_manifest(
    *,
    package: dict[str, Any],
    source_sha: str,
    source_ref: str,
    channel: str,
    release_tag: str,
    run_id: str,
    run_attempt: int,
    archive_path: Path,
    archive_entries: dict[str, bytes],
    descriptor_path: Path,
    lock_path: Path,
    wheel_path: Path,
    release_metadata_path: Path,
    sbom_path: Path,
) -> dict[str, Any]:
    release_uri = f"{SOURCE_REPOSITORY_URL}/releases/download/{release_tag}"
    archive_digest = training_release.sha256_file(archive_path)
    archive_files = {
        name: training_release.sha256_bytes(content)
        for name, content in sorted(archive_entries.items())
    }
    manifest: dict[str, Any] = {
        "schemaVersion": 2,
        "releaseId": release_tag,
        "componentId": package["component_id"],
        "version": package["version"],
        "channel": channel,
        "target": TARGET,
        "protocolVersion": CAPABILITY_PROTOCOL,
        "contentDigest": archive_digest,
        "artifact": {
            "kind": "plugin-package",
            "packageId": package["package_id"],
            "capabilityId": package["capability"],
            "interfaceVersion": "1",
            "archive": {
                "format": "zip",
                "uri": f"{release_uri}/{archive_path.name}",
                "sha256": archive_digest,
                "sizeBytes": archive_path.stat().st_size,
                "files": archive_files,
                "maxEntries": len(archive_entries),
                "maxUncompressedBytes": sum(
                    len(value) for value in archive_entries.values()
                ),
            },
            "descriptor": {
                "uri": f"{release_uri}/{descriptor_path.name}",
                "sha256": training_release.sha256_file(descriptor_path),
                "sizeBytes": descriptor_path.stat().st_size,
            },
            "requirementsLock": {
                "uri": f"{release_uri}/{lock_path.name}",
                "sha256": training_release.sha256_file(lock_path),
                "sizeBytes": lock_path.stat().st_size,
            },
            "preparerWheels": [
                {
                    "uri": f"{release_uri}/{wheel_path.name}",
                    "sha256": training_release.sha256_file(wheel_path),
                    "sizeBytes": wheel_path.stat().st_size,
                }
            ],
            "packageReleaseMetadata": {
                "uri": f"{release_uri}/{release_metadata_path.name}",
                "sha256": training_release.sha256_file(release_metadata_path),
                "sizeBytes": release_metadata_path.stat().st_size,
            },
            "sbom": {
                "uri": f"{release_uri}/{sbom_path.name}",
                "sha256": training_release.sha256_file(sbom_path),
                "sizeBytes": sbom_path.stat().st_size,
                "format": "cyclonedx-json",
                "specVersion": SBOM_SPEC_VERSION,
            },
        },
        "dependencies": [
            {"componentId": "cy-package-runtime", "versionRange": ">=0.1.0, <0.2.0"}
        ],
        "restart": {"group": "none"},
        "source": {
            "repository": SOURCE_REPOSITORY_URL,
            "ref": source_ref,
            "commit": source_sha,
        },
        "provenance": {
            "attestation": {
                "kind": "github-artifact-attestation",
                "subjectName": archive_path.name,
                "repository": REPOSITORY,
                "workflow": f"{REPOSITORY}/{WORKFLOW_PATH}",
                "predicateType": "https://slsa.dev/provenance/v1",
                "run": {
                    "id": run_id,
                    "attempt": run_attempt,
                    "url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}",
                },
            }
        },
    }
    manifest["manifestDigest"] = _jcs_digest(manifest, "manifestDigest")
    return manifest


def _component_index(
    *,
    package: dict[str, Any],
    source_sha: str,
    source_ref: str,
    channel: str,
    release_tag: str,
    run_id: str,
    run_attempt: int,
    manifest_digest: str,
) -> dict[str, Any]:
    manifest_uri = f"{SOURCE_REPOSITORY_URL}/releases/download/{release_tag}/{COMPONENT_MANIFEST_NAME}"
    index: dict[str, Any] = {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "channel": channel,
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": {
            "repository": SOURCE_REPOSITORY_URL,
            "ref": source_ref,
            "commit": source_sha,
        },
        "provenance": {
            "attestation": {
                "kind": "github-artifact-attestation",
                "subjectName": RELEASE_INDEX_NAME,
                "repository": REPOSITORY,
                "workflow": f"{REPOSITORY}/{WORKFLOW_PATH}",
                "predicateType": "https://slsa.dev/provenance/v1",
                "run": {
                    "id": run_id,
                    "attempt": run_attempt,
                    "url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}",
                },
            }
        },
        "releases": [
            {
                "componentId": package["component_id"],
                "version": package["version"],
                "target": TARGET,
                "manifestUri": manifest_uri,
                "manifestDigest": manifest_digest,
            }
        ],
        "compatibilityGroups": [],
    }
    index["indexDigest"] = _jcs_digest(index, "indexDigest")
    return index


def _channel_identity(
    channel: str, source_ref: str, source_sha: str, component_id: str, version: str
) -> str | None:
    if channel == "candidate":
        return None
    if channel == "preview" and source_ref != "refs/heads/develop":
        raise PackageBuildError(
            "preview packages must be built from refs/heads/develop"
        )
    if channel == "stable" and source_ref not in {
        "refs/heads/main",
        "refs/heads/release",
    }:
        raise PackageBuildError("stable packages must be built from main or release")
    if not GIT_SHA.fullmatch(source_sha):
        raise PackageBuildError("source commit must be a 40-character lowercase SHA")
    return f"{channel}-{component_id}-{version}-{source_sha}"


def build_release_artifacts(
    *,
    root: Path,
    output_dir: Path,
    package_id: str,
    source_sha: str,
    source_ref: str,
    channel: str,
    run_id: str | None,
    run_attempt: int,
    preparer_wheel: Path | None = None,
) -> dict[str, Path]:
    """Build one explicit official plugin package and its release envelopes."""

    try:
        spec = PACKAGE_SPECS[package_id]
    except KeyError as error:
        raise PackageBuildError(
            f"unknown official package identity: {package_id}"
        ) from error
    if not GIT_SHA.fullmatch(source_sha):
        raise PackageBuildError("source commit must be a 40-character lowercase SHA")
    if not source_ref or "\n" in source_ref or "\r" in source_ref:
        raise PackageBuildError("source ref must be non-empty single-line text")
    if channel not in SUPPORTED_CHANNELS:
        raise PackageBuildError(f"unsupported release channel: {channel}")
    root = root.resolve(strict=True)
    _, package = _read_package_spec(root, spec)
    release_tag = _channel_identity(
        channel, source_ref, source_sha, spec.component_id, package["version"]
    )
    if channel != "candidate" and (
        not isinstance(run_id, str)
        or not run_id.isdigit()
        or int(run_id) < 1
        or run_attempt < 1
    ):
        raise PackageBuildError(
            "published component metadata requires a valid GitHub run identity"
        )

    source_files, sdk_files = _source_files(root, spec)
    pins = _verify_lock_inputs(spec, source_files)
    package_entries = _package_entries(source_files, spec, sdk_files)
    lock_bytes = source_files[f"{spec.source_dir}/requirements.lock"]

    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PackageBuildError(f"release output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    wheel_owner: tempfile.TemporaryDirectory[str] | None = None
    if preparer_wheel is None:
        wheel_path, wheel_owner = training_release.build_runtime_wheel(root, source_sha)
    else:
        wheel_path = preparer_wheel.resolve(strict=True)
    try:
        training_release._validate_preparer_wheel(wheel_path)
        archive_name = f"{package_id}-{package['version']}-{TARGET_ID}.zip"
        descriptor_name = f"{package_id}-{package['version']}-package-descriptor.json"
        lock_name = f"{package_id}-{package['version']}-requirements.lock"
        metadata_name = f"{package_id}-{package['version']}-package-release.json"
        sbom_name = f"{package_id}-{package['version']}-sbom.cdx.json"
        archive_path = output_dir / archive_name
        descriptor_path = output_dir / descriptor_name
        lock_path = output_dir / lock_name
        release_metadata_path = output_dir / metadata_name
        sbom_path = output_dir / sbom_name
        output_wheel = output_dir / wheel_path.name
        lock_path.write_bytes(lock_bytes)
        shutil.copyfile(wheel_path, output_wheel)

        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_STORED
        ) as archive:
            for name, content in sorted(package_entries.items()):
                if (
                    not name
                    or name.startswith("/")
                    or "\\" in name
                    or ".." in PurePosixPath(name).parts
                ):
                    raise PackageBuildError(f"unsafe package archive path: {name}")
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)

        archive_digest = training_release.sha256_file(archive_path)
        content_digest = training_release._package_content_digest(package_entries)
        lock_digest = training_release.sha256_bytes(lock_bytes)
        publication_status = "CANDIDATE" if channel == "candidate" else "PUBLISHED"
        package_uri = (
            None
            if release_tag is None
            else f"{SOURCE_REPOSITORY_URL}/releases/download/{release_tag}/{archive_name}"
        )
        descriptor_artifact: dict[str, Any] = {"status": "NOT_PUBLISHED"}
        if package_uri is not None:
            descriptor_artifact = {
                "status": "PUBLISHED",
                "uri": package_uri,
                "digest": content_digest,
                "format": "zip",
            }
        descriptor = {
            "record_type": "package_descriptor",
            "spec_version": "0.1",
            "package": {
                "manifest_ref": "plugin.manifest.json",
                "id": package_id,
                "version": package["version"],
            },
            "capability": {"id": package["capability"], "interface_version": "1"},
            "implementation": {
                "entrypoint": package["entrypoint"],
                "artifact": descriptor_artifact,
            },
            "dependencies": {
                "lock": {
                    "status": "LOCKED",
                    "format": "requirements.lock",
                    "ref": "requirements.lock",
                    "digest": lock_digest,
                }
            },
            "integrity": {
                "artifact_digest": content_digest,
                "archive_digest": archive_digest,
                "signature_ref": None,
            },
            "provenance": {
                "source_repository": SOURCE_REPOSITORY_URL,
                "source_revision": source_sha,
                "builder": f"{REPOSITORY}/{WORKFLOW_PATH}",
                "attestation_ref": None,
            },
            "publication_status": publication_status,
        }
        descriptor_path.write_bytes(_json_bytes(descriptor))

        source_inputs, source_tree_digest = training_release._source_index(source_files)
        sdk_records = _sdk_metadata(
            spec, source_ref, source_sha, source_files, sdk_files
        )
        sbom = _sbom_document(
            package, spec, source_ref, source_sha, source_files, sdk_files, pins
        )
        sbom_path.write_bytes(_json_bytes(sbom))
        archive_records = [
            {"name": path.name, "sha256": training_release.sha256_file(path)}
            for path in (
                archive_path,
                descriptor_path,
                lock_path,
                output_wheel,
                sbom_path,
            )
        ]
        release_metadata: dict[str, Any] = {
            "record_type": "cyrene.plugin.package.release.v1",
            "spec_version": "1",
            "publication_status": publication_status,
            "channel": channel,
            "source": {
                "repository": SOURCE_REPOSITORY_URL,
                "commit": source_sha,
                "ref": source_ref,
                "input_tree_digest": source_tree_digest,
                "inputs": source_inputs,
            },
            "package": {
                "id": package_id,
                "version": package["version"],
                "component_id": package["component_id"],
                "capability": package["capability"],
                "interface_version": "1",
                "entrypoint": package["entrypoint"],
                "distribution": package["distribution"],
                "supported_services": package["supported_services"],
            },
            "source_policy": {
                "authority": "plugin.manifest.json#supportedServices",
                "supported_product_sources": [
                    {
                        "service": service,
                        "component_id": PRODUCT_SERVICE_COMPONENTS[service],
                    }
                    for service in package["supported_services"]
                ],
            },
            "target": {
                "id": TARGET_ID,
                "os": "linux",
                "os_version": "24.04",
                "distribution": "ubuntu",
                "distribution_version": "24.04",
                "architecture": "x86_64",
                "python": "3.12",
            },
            "runtime": {
                "protocol": "cyrene.plugin.runtime.v1.DirectPluginRuntime",
                "launch_executable": "prepared-runtime",
                "preparer_command": "cyrene-plugin-python-preparer",
                "preparer_package": f"{PREPARER_DISTRIBUTION}=={PREPARER_VERSION}",
                "preparer_wheel_install_command": [
                    "<python 3.12 executable>",
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "<verified preparer wheel path>",
                ],
                "preparer_configuration": {
                    "host_api": "cy-package-runtime::CommandDependencyPreparer",
                    "arguments": [
                        "--uv",
                        "<uv executable>",
                        "--python",
                        "<python 3.12 executable>",
                    ],
                    "evidence_protocol": "cyrene.package-dependency-preparer.v1",
                    "runtime_executable_is_consumed_by": "ProcessPluginServiceSupervisor",
                },
                "python": "3.12",
                "dependency_lock": "requirements.lock",
            },
            "vendored_sdks": sdk_records,
            "release_tag": release_tag,
            "artifact_uri": package_uri,
            "assets": {
                "package": {
                    "name": archive_name,
                    "format": "zip",
                    "sha256": archive_digest,
                    "content_sha256": content_digest,
                },
                "descriptor": {
                    "name": descriptor_path.name,
                    "format": "json",
                    "sha256": training_release.sha256_file(descriptor_path),
                },
                "dependency_lock": {
                    "name": lock_name,
                    "format": "requirements.lock",
                    "sha256": lock_digest,
                    "package_ref": "requirements.lock",
                },
                "preparer_wheel": {
                    "name": output_wheel.name,
                    "package": PREPARER_DISTRIBUTION,
                    "version": PREPARER_VERSION,
                    "format": "wheel",
                    "target": "py3-none-any",
                    "entrypoint": "cyrene-plugin-python-preparer",
                    "sha256": training_release.sha256_file(output_wheel),
                },
                "sbom": {
                    "name": sbom_path.name,
                    "format": "cyclonedx-json",
                    "spec_version": SBOM_SPEC_VERSION,
                    "sha256": training_release.sha256_file(sbom_path),
                },
            },
            "attestation_policy": {
                "provider": "github-actions",
                "workflow": f"{REPOSITORY}/{WORKFLOW_PATH}",
                "source_commit": source_sha,
                "subject_asset_names": sorted(
                    [
                        archive_name,
                        descriptor_path.name,
                        lock_path.name,
                        output_wheel.name,
                        sbom_path.name,
                        release_metadata_path.name,
                        *(
                            [COMPONENT_MANIFEST_NAME, RELEASE_INDEX_NAME]
                            if release_tag is not None
                            else []
                        ),
                    ]
                ),
                "subject_assets": archive_records,
            },
        }
        release_metadata_path.write_bytes(_json_bytes(release_metadata))

        output: dict[str, Path] = {
            "package": archive_path,
            "descriptor": descriptor_path,
            "dependency_lock": lock_path,
            "preparer_wheel": output_wheel,
            "sbom": sbom_path,
            "release_metadata": release_metadata_path,
        }
        if release_tag is not None:
            assert isinstance(run_id, str)
            manifest_document = _component_manifest(
                package=package,
                source_sha=source_sha,
                source_ref=source_ref,
                channel=channel,
                release_tag=release_tag,
                run_id=run_id,
                run_attempt=run_attempt,
                archive_path=archive_path,
                archive_entries=package_entries,
                descriptor_path=descriptor_path,
                lock_path=lock_path,
                wheel_path=output_wheel,
                release_metadata_path=release_metadata_path,
                sbom_path=sbom_path,
            )
            component_manifest_path = output_dir / COMPONENT_MANIFEST_NAME
            component_manifest_path.write_bytes(_json_bytes(manifest_document))
            index_document = _component_index(
                package=package,
                source_sha=source_sha,
                source_ref=source_ref,
                channel=channel,
                release_tag=release_tag,
                run_id=run_id,
                run_attempt=run_attempt,
                manifest_digest=manifest_document["manifestDigest"],
            )
            component_index_path = output_dir / RELEASE_INDEX_NAME
            component_index_path.write_bytes(_json_bytes(index_document))
            output["component_manifest"] = component_manifest_path
            output["component_index"] = component_index_path
        _write_subject_checksums(output, output_dir)
        return output
    finally:
        if wheel_owner is not None:
            wheel_owner.cleanup()


def _write_subject_checksums(paths: dict[str, Path], output_dir: Path) -> Path:
    path = output_dir / "attestation-subjects.sha256"
    path.write_text(
        "".join(
            f"{training_release.sha256_file(paths[key]).removeprefix('sha256:')}  {paths[key].name}\n"
            for key in sorted(paths)
        ),
        encoding="utf-8",
    )
    return path


def verify_release_artifacts(
    *,
    root: Path,
    output_dir: Path,
    package_id: str,
    source_sha: str,
    source_ref: str,
    channel: str,
    run_id: str | None,
    run_attempt: int,
) -> dict[str, Path]:
    """Verify release asset identities, digests, source pins, and entrypoint layout."""

    spec = PACKAGE_SPECS.get(package_id)
    if spec is None:
        raise PackageBuildError(f"unknown official package identity: {package_id}")
    root = root.resolve(strict=True)
    output_dir = output_dir.resolve(strict=True)
    _, package = _read_package_spec(root, spec)
    source_files, sdk_files = _source_files(root, spec)
    pins = _verify_lock_inputs(spec, source_files)
    source_inputs, source_tree_digest = training_release._source_index(source_files)
    expected_names = {
        "package": f"{package_id}-{package['version']}-{TARGET_ID}.zip",
        "descriptor": f"{package_id}-{package['version']}-package-descriptor.json",
        "dependency_lock": f"{package_id}-{package['version']}-requirements.lock",
        "preparer_wheel": PREPARER_WHEEL,
        "sbom": f"{package_id}-{package['version']}-sbom.cdx.json",
        "release_metadata": f"{package_id}-{package['version']}-package-release.json",
    }
    paths = {key: output_dir / value for key, value in expected_names.items()}
    for path in paths.values():
        if path.is_symlink() or not path.is_file():
            raise PackageBuildError(f"release asset is missing or unsafe: {path.name}")
    if channel not in SUPPORTED_CHANNELS or not GIT_SHA.fullmatch(source_sha):
        raise PackageBuildError("release channel or source SHA is invalid")
    if (
        paths["dependency_lock"].read_bytes()
        != source_files[f"{spec.source_dir}/requirements.lock"]
    ):
        raise PackageBuildError("published lock differs from the committed exact lock")
    with zipfile.ZipFile(paths["package"]) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise PackageBuildError("package ZIP contains duplicate paths")
        entries = {name: archive.read(name) for name in names}
    if entries != _package_entries(source_files, spec, sdk_files):
        raise PackageBuildError(
            "package archive payload differs from the allowlisted source"
        )
    if entries.get("requirements.lock") != paths["dependency_lock"].read_bytes():
        raise PackageBuildError("package ZIP lock differs from the signed lock asset")
    entry_module = package["entrypoint"].split(":", 1)[0].replace(".", "/")
    if f"src/{entry_module}.py" not in entries or f"{entry_module}.py" in entries:
        raise PackageBuildError(
            "Plugin entrypoint is not under the bootstrap source root"
        )
    content_digest = training_release._package_content_digest(entries)
    descriptor = _read_json(paths["descriptor"])
    if (
        descriptor.get("package")
        != {
            "manifest_ref": "plugin.manifest.json",
            "id": package_id,
            "version": package["version"],
        }
        or descriptor.get("capability")
        != {"id": package["capability"], "interface_version": "1"}
        or descriptor.get("dependencies", {}).get("lock", {}).get("digest")
        != training_release.sha256_file(paths["dependency_lock"])
        or descriptor.get("integrity", {}).get("artifact_digest") != content_digest
        or descriptor.get("integrity", {}).get("archive_digest")
        != training_release.sha256_file(paths["package"])
        or descriptor.get("implementation", {}).get("entrypoint")
        != package["entrypoint"]
        or descriptor.get("implementation", {}).get("artifact", {}).get("uri")
        != (
            None
            if _channel_identity(
                channel, source_ref, source_sha, spec.component_id, package["version"]
            )
            is None
            else f"{SOURCE_REPOSITORY_URL}/releases/download/"
            f"{_channel_identity(channel, source_ref, source_sha, spec.component_id, package['version'])}/"
            f"{paths['package'].name}"
        )
        or descriptor.get("provenance", {}).get("source_revision") != source_sha
        or descriptor.get("provenance", {}).get("builder")
        != f"{REPOSITORY}/{WORKFLOW_PATH}"
        or descriptor.get("publication_status")
        != ("CANDIDATE" if channel == "candidate" else "PUBLISHED")
    ):
        raise PackageBuildError(
            "Package Spec identity, lock, or archive digest mismatch"
        )
    metadata = _read_json(paths["release_metadata"])
    release_tag = _channel_identity(
        channel, source_ref, source_sha, spec.component_id, package["version"]
    )
    if (
        metadata.get("record_type") != "cyrene.plugin.package.release.v1"
        or metadata.get("publication_status")
        != ("CANDIDATE" if channel == "candidate" else "PUBLISHED")
        or metadata.get("source", {}).get("commit") != source_sha
        or metadata.get("source", {}).get("ref") != source_ref
        or metadata.get("source", {}).get("input_tree_digest") != source_tree_digest
        or metadata.get("source", {}).get("inputs") != source_inputs
        or metadata.get("package", {}).get("id") != package_id
        or metadata.get("package", {}).get("version") != package["version"]
        or metadata.get("package", {}).get("capability") != package["capability"]
        or metadata.get("release_tag") != release_tag
        or metadata.get("artifact_uri")
        != (
            None
            if release_tag is None
            else f"{SOURCE_REPOSITORY_URL}/releases/download/{release_tag}/{paths['package'].name}"
        )
        or metadata.get("source_policy")
        != {
            "authority": "plugin.manifest.json#supportedServices",
            "supported_product_sources": [
                {
                    "service": service,
                    "component_id": PRODUCT_SERVICE_COMPONENTS[service],
                }
                for service in package["supported_services"]
            ],
        }
        or metadata.get("vendored_sdks")
        != _sdk_metadata(spec, source_ref, source_sha, source_files, sdk_files)
        or metadata.get("target", {}).get("id") != TARGET_ID
        or metadata.get("runtime", {}).get("protocol")
        != CAPABILITY_PROTOCOL + ".DirectPluginRuntime"
    ):
        raise PackageBuildError(
            "package release metadata is not bound to this source/package"
        )
    _validate_release_asset_metadata(metadata, paths, content_digest)
    sbom = _read_json(paths["sbom"])
    expected_sbom = _sbom_document(
        package, spec, source_ref, source_sha, source_files, sdk_files, pins
    )
    if sbom != expected_sbom:
        raise PackageBuildError(
            "release SBOM differs from the exact lock/source inputs"
        )
    _validate_attestation_policy(metadata, paths, source_sha)
    training_release._validate_preparer_wheel(paths["preparer_wheel"])
    if release_tag is not None:
        if not isinstance(run_id, str) or not run_id.isdigit() or run_attempt < 1:
            raise PackageBuildError(
                "published component release lacks GitHub run identity"
            )
        manifest_path = output_dir / COMPONENT_MANIFEST_NAME
        index_path = output_dir / RELEASE_INDEX_NAME
        if not manifest_path.is_file() or not index_path.is_file():
            raise PackageBuildError("component manifest or release index is missing")
        component_manifest = _read_json(manifest_path)
        component_index = _read_json(index_path)
        expected_manifest = _component_manifest(
            package=package,
            source_sha=source_sha,
            source_ref=source_ref,
            channel=channel,
            release_tag=release_tag,
            run_id=run_id,
            run_attempt=run_attempt,
            archive_path=paths["package"],
            archive_entries=entries,
            descriptor_path=paths["descriptor"],
            lock_path=paths["dependency_lock"],
            wheel_path=paths["preparer_wheel"],
            release_metadata_path=paths["release_metadata"],
            sbom_path=paths["sbom"],
        )
        if component_manifest != expected_manifest:
            raise PackageBuildError(
                "component manifest identity or asset binding mismatch"
            )
        expected_manifest_uri = (
            f"{SOURCE_REPOSITORY_URL}/releases/download/"
            f"{release_tag}/{COMPONENT_MANIFEST_NAME}"
        )
        expected_index_attestation = {
            "kind": "github-artifact-attestation",
            "subjectName": RELEASE_INDEX_NAME,
            "repository": REPOSITORY,
            "workflow": f"{REPOSITORY}/{WORKFLOW_PATH}",
            "predicateType": "https://slsa.dev/provenance/v1",
            "run": {
                "id": run_id,
                "attempt": run_attempt,
                "url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}",
            },
        }
        generated_at = component_index.get("generatedAt")
        try:
            parsed_generated_at = datetime.fromisoformat(
                generated_at.replace("Z", "+00:00")
            )
        except (AttributeError, TypeError, ValueError):
            parsed_generated_at = None
        if (
            component_index.get("schemaVersion") != 1
            or component_index.get("repository") != REPOSITORY
            or component_index.get("channel") != channel
            or component_index.get("source", {}).get("commit") != source_sha
            or component_index.get("source", {}).get("ref") != source_ref
            or component_index.get("source", {}).get("repository")
            != SOURCE_REPOSITORY_URL
            or component_index.get("provenance", {}).get("attestation")
            != expected_index_attestation
            or parsed_generated_at is None
            or parsed_generated_at.utcoffset() is None
            or parsed_generated_at.utcoffset().total_seconds() != 0
            or component_index.get("compatibilityGroups") != []
            or component_index.get("indexDigest")
            != _jcs_digest(component_index, "indexDigest")
            or len(component_index.get("releases", [])) != 1
            or component_index["releases"][0]
            != {
                "componentId": spec.component_id,
                "version": package["version"],
                "target": TARGET,
                "manifestUri": expected_manifest_uri,
                "manifestDigest": component_manifest["manifestDigest"],
            }
            or component_index["releases"][0].get("manifestDigest")
            != component_manifest["manifestDigest"]
        ):
            raise PackageBuildError(
                "component release index identity or digest mismatch"
            )
        paths["component_manifest"] = manifest_path
        paths["component_index"] = index_path
    _write_subject_checksums(paths, output_dir)
    return paths


def _validate_release_asset_metadata(
    metadata: dict[str, Any], paths: dict[str, Path], content_digest: str
) -> None:
    assets = metadata.get("assets")
    expected = {
        "package": {
            "name": paths["package"].name,
            "format": "zip",
            "sha256": training_release.sha256_file(paths["package"]),
            "content_sha256": content_digest,
        },
        "descriptor": {
            "name": paths["descriptor"].name,
            "format": "json",
            "sha256": training_release.sha256_file(paths["descriptor"]),
        },
        "dependency_lock": {
            "name": paths["dependency_lock"].name,
            "format": "requirements.lock",
            "sha256": training_release.sha256_file(paths["dependency_lock"]),
            "package_ref": "requirements.lock",
        },
        "preparer_wheel": {
            "name": paths["preparer_wheel"].name,
            "package": PREPARER_DISTRIBUTION,
            "version": PREPARER_VERSION,
            "format": "wheel",
            "target": "py3-none-any",
            "entrypoint": "cyrene-plugin-python-preparer",
            "sha256": training_release.sha256_file(paths["preparer_wheel"]),
        },
        "sbom": {
            "name": paths["sbom"].name,
            "format": "cyclonedx-json",
            "spec_version": SBOM_SPEC_VERSION,
            "sha256": training_release.sha256_file(paths["sbom"]),
        },
    }
    if assets != expected:
        raise PackageBuildError("package release metadata assets or digests mismatch")


def _validate_attestation_policy(
    metadata: dict[str, Any], paths: dict[str, Path], source_sha: str
) -> None:
    policy = metadata.get("attestation_policy")
    subjects = [
        {"name": paths[key].name, "sha256": training_release.sha256_file(paths[key])}
        for key in (
            "package",
            "descriptor",
            "dependency_lock",
            "preparer_wheel",
            "sbom",
        )
    ]
    expected = {
        "provider": "github-actions",
        "workflow": f"{REPOSITORY}/{WORKFLOW_PATH}",
        "source_commit": source_sha,
        "subject_asset_names": sorted(
            [
                paths["package"].name,
                paths["descriptor"].name,
                paths["dependency_lock"].name,
                paths["preparer_wheel"].name,
                paths["sbom"].name,
                paths["release_metadata"].name,
                *(
                    [COMPONENT_MANIFEST_NAME, RELEASE_INDEX_NAME]
                    if metadata.get("release_tag") is not None
                    else []
                ),
            ]
        ),
        "subject_assets": subjects,
    }
    if policy != expected:
        raise PackageBuildError(
            "release attestation policy does not bind every package asset"
        )


def verify_staged_release_readback(
    *,
    record: Any,
    asset_paths: list[Path],
    release_tag: str,
    package_id: str,
    package_version: str,
    source_ref: str,
    source_sha: str,
    channel: str,
) -> None:
    """Verify GitHub's draft-release identity and every uploaded asset byte.

    The GitHub CLI release JSON exposes ``isDraft``, ``isImmutable``,
    ``isPrerelease``, ``tagName``, ``targetCommitish``, and each asset's
    ``name``, ``size``, ``digest``, and ``state``. It does not expose an
    ``isLatest`` JSON field; draft state plus the create command's explicit
    ``--latest=false`` prevents this staging step from advancing latest.
    中文：按 CLI 实际返回字段核对 draft 身份及每个已上传资产的摘要与大小。
    """

    spec = PACKAGE_SPECS.get(package_id)
    if spec is None:
        raise PackageBuildError("staged release read-back has an unknown package ID")
    expected_tag = _channel_identity(
        channel, source_ref, source_sha, spec.component_id, package_version
    )
    if expected_tag is None or release_tag != expected_tag:
        raise PackageBuildError(
            "staged release read-back tag differs from the source identity"
        )
    if (
        not isinstance(record, dict)
        or record.get("isDraft") is not True
        or record.get("isImmutable") is not False
        or record.get("isPrerelease") is not (channel == "preview")
        or record.get("tagName") != release_tag
        or record.get("targetCommitish") != source_sha
    ):
        raise PackageBuildError(
            "staged release read-back identity or draft state differs"
        )

    expected_assets: dict[str, dict[str, Any]] = {}
    for path in asset_paths:
        if path.is_symlink() or not path.is_file():
            raise PackageBuildError(
                f"verified staged asset is missing or unsafe: {path}"
            )
        name = path.name
        if name in expected_assets:
            raise PackageBuildError(
                f"verified staged asset basename is duplicated: {name}"
            )
        expected_assets[name] = {
            "size": path.stat().st_size,
            "digest": training_release.sha256_file(path),
        }
    if not expected_assets:
        raise PackageBuildError("verified staged asset set is empty")

    rows = record.get("assets")
    if not isinstance(rows, list):
        raise PackageBuildError("staged release read-back assets are missing")
    actual_assets: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PackageBuildError(
                "staged release read-back contains a malformed asset"
            )
        name = row.get("name")
        size = row.get("size")
        digest = row.get("digest")
        if (
            not isinstance(name, str)
            or not name
            or name in actual_assets
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            or row.get("state") != "uploaded"
        ):
            raise PackageBuildError(
                "staged release read-back asset metadata is malformed"
            )
        actual_assets[name] = {"size": size, "digest": digest}
    if actual_assets != expected_assets:
        missing = sorted(set(expected_assets) - set(actual_assets))
        unexpected = sorted(set(actual_assets) - set(expected_assets))
        mismatched = sorted(
            name
            for name in set(expected_assets) & set(actual_assets)
            if expected_assets[name] != actual_assets[name]
        )
        raise PackageBuildError(
            "staged release read-back differs from verified asset identities "
            f"(missing={missing}, unexpected={unexpected}, mismatched={mismatched})"
        )


def _build_command(options: argparse.Namespace) -> None:
    paths = build_release_artifacts(
        root=options.root,
        output_dir=options.output_dir,
        package_id=options.package_id,
        source_sha=options.github_sha,
        source_ref=options.source_ref,
        channel=options.channel,
        run_id=options.run_id,
        run_attempt=options.run_attempt,
    )
    verify_release_artifacts(
        root=options.root,
        output_dir=options.output_dir,
        package_id=options.package_id,
        source_sha=options.github_sha,
        source_ref=options.source_ref,
        channel=options.channel,
        run_id=options.run_id,
        run_attempt=options.run_attempt,
    )
    print(json.dumps({key: str(path) for key, path in paths.items()}, sort_keys=True))


def _verify_command(options: argparse.Namespace) -> None:
    paths = verify_release_artifacts(
        root=options.root,
        output_dir=options.output_dir,
        package_id=options.package_id,
        source_sha=options.github_sha,
        source_ref=options.source_ref,
        channel=options.channel,
        run_id=options.run_id,
        run_attempt=options.run_attempt,
    )
    print(json.dumps({key: str(path) for key, path in paths.items()}, sort_keys=True))


def _arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("build", _build_command), ("verify", _verify_command)):
        command = subcommands.add_parser(name)
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument("--output-dir", type=Path, required=True)
        command.add_argument(
            "--package-id", choices=sorted(PACKAGE_SPECS), required=True
        )
        command.add_argument("--source-ref", required=True)
        command.add_argument(
            "--channel", choices=sorted(SUPPORTED_CHANNELS), required=True
        )
        command.add_argument("--target", choices=(TARGET_ID,), required=True)
        command.add_argument("--github-sha", required=True)
        command.add_argument("--run-id")
        command.add_argument("--run-attempt", type=int, default=1)
        command.set_defaults(handler=handler)
    return parser


def main() -> int:
    options = _arguments().parse_args()
    try:
        options.handler(options)
    except (OSError, UnicodeError, zipfile.BadZipFile, PackageBuildError) as error:
        raise SystemExit(f"official Plugin package release: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
