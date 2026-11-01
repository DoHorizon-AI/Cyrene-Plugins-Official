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
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

import tomllib
from packaging.requirements import Requirement
from packaging.tags import parse_tag, sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

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
SOURCE_DEPENDENCY_BUILD_LOCK_PATH = "tooling/release/source-build-requirements.lock"
SOURCE_DEPENDENCY_BUILD_PINS = (
    "pip==25.2",
    "setuptools==77.0.3",
    "wheel==0.48.0",
)
JCS_LOCK_PATH = "tools/ci/connection-release-requirements.txt"
TARGETS = {
    "linux-ubuntu-24.04-x86_64-python-3.12": {
        "os": "linux",
        "osVersion": "24.04",
        "distribution": "ubuntu",
        "distributionVersion": "24.04",
        "architecture": "x86_64",
        "abi": "glibc-2.39",
        "runtime": "python:3.12",
    },
    "linux-ubuntu-24.04-aarch64-python-3.12": {
        "os": "linux",
        "osVersion": "24.04",
        "distribution": "ubuntu",
        "distributionVersion": "24.04",
        "architecture": "aarch64",
        "abi": "glibc-2.39",
        "runtime": "python:3.12",
    },
}
TARGET_ID_BY_MACHINE = {
    "x86_64": "linux-ubuntu-24.04-x86_64-python-3.12",
    "aarch64": "linux-ubuntu-24.04-aarch64-python-3.12",
    "arm64": "linux-ubuntu-24.04-aarch64-python-3.12",
}
DEFAULT_TARGET_ID = "linux-ubuntu-24.04-x86_64-python-3.12"


def target_id_for_machine(machine: str) -> str | None:
    """Map a native machine name to an explicitly supported release target.

    中文：仅将受支持的本机架构映射到固定发行目标。
    """

    return TARGET_ID_BY_MACHINE.get(machine.lower())


def _require_native_target(target_id: str, *, machine: str | None = None) -> None:
    """Reject cross-architecture builds and verifications before reading assets.

    中文：在读取或生成发行资产前拒绝跨架构目标。
    """

    native_machine = platform.machine() if machine is None else machine
    native_target = target_id_for_machine(native_machine)
    if native_target is None:
        raise PackageBuildError(
            f"unsupported native package builder architecture: {native_machine}"
        )
    if target_id != native_target:
        raise PackageBuildError(
            f"target {target_id} does not match native builder target {native_target}"
        )


TARGET_ID = target_id_for_machine(platform.machine()) or DEFAULT_TARGET_ID
TARGET = TARGETS[TARGET_ID]
SUPPORTED_CHANNELS = {"candidate", "preview", "stable"}
CAPABILITY_PROTOCOL = "cyrene.plugin.runtime.v1"
PREPARER_DISTRIBUTION = "cyrene-plugin-runtime"
PREPARER_VERSION = "0.2.2"
PREPARER_WHEEL = "cyrene_plugin_runtime-0.2.2-py3-none-any.whl"
WHEELHOUSE_TARGET = TARGET_ID
WHEELHOUSE_MANIFEST = "wheelhouse/manifest.json"
SBOM_SPEC_VERSION = "1.6"
SBOM_FORMAT = "CycloneDX"
RELEASE_INDEX_NAME = "component-release-index-v1.json"
COMPONENT_MANIFEST_NAME = "component-release-manifest-v2.json"
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
LOCK_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)$")
MODEL_METHOD_VERSIONS = {
    "embeddings": "1",
    "chat_completion": "1",
    "chat_completion_v2": "2",
    "chat_completion_v3": "3",
}


@dataclass(frozen=True)
class VendoredSdk:
    """Identify one internal Python distribution copied into a package ZIP."""

    distribution: str
    project_path: str
    source_root: str
    archive_root: str
    package_data: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceDependency:
    """Bind one public source-built wheel to an immutable upstream commit."""

    distribution: str
    version: str
    repository: str
    commit: str
    license_expression: str
    lock_path: str
    license_file_path: str


@dataclass(frozen=True)
class PackageSpec:
    """Bind a dotted package identity to its explicit repository paths."""

    package_id: str
    component_id: str
    primary_capability: str
    source_dir: str
    requirements_input: str
    vendored_sdks: tuple[VendoredSdk, ...]
    source_dependencies: tuple[SourceDependency, ...] = ()
    require_spdx_license: bool = False


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
MEMORY_PROVIDER_SDK = VendoredSdk(
    distribution="cyrene-memory-provider-contracts",
    project_path="sdk/python/cyrene_memory_provider_contracts/pyproject.toml",
    source_root="sdk/python/cyrene_memory_provider_contracts/src/cyrene_memory_provider_contracts",
    archive_root="src/cyrene_memory_provider_contracts",
)
KNOWLEDGE_GRAPH_SDK = VendoredSdk(
    distribution="cyrene-knowledge-graph-contracts",
    project_path="sdk/python/cyrene_knowledge_graph_contracts/pyproject.toml",
    source_root="sdk/python/cyrene_knowledge_graph_contracts/src/cyrene_knowledge_graph_contracts",
    archive_root="src/cyrene_knowledge_graph_contracts",
)
SPEECH_PROVIDER_SDK = VendoredSdk(
    distribution="cyrene-speech-provider-contracts",
    project_path="sdk/python/cyrene_speech_provider_contracts/pyproject.toml",
    source_root="sdk/python/cyrene_speech_provider_contracts/src/cyrene_speech_provider_contracts",
    archive_root="src/cyrene_speech_provider_contracts",
)
TOOL_PROVIDER_SDK = VendoredSdk(
    distribution="cyrene-tool-provider-contracts",
    project_path="sdk/python/cyrene_tool_provider_contracts/pyproject.toml",
    source_root="sdk/python/cyrene_tool_provider_contracts/src/cyrene_tool_provider_contracts",
    archive_root="src/cyrene_tool_provider_contracts",
)
ACCOUNT_CENTER_SDK = VendoredSdk(
    distribution="cyrene-account-center-contracts",
    project_path="sdk/python/cyrene_account_center_contracts/pyproject.toml",
    source_root="sdk/python/cyrene_account_center_contracts/src/cyrene_account_center_contracts",
    archive_root="src/cyrene_account_center_contracts",
)
INTEGRATION_SUPPORT_SDK = VendoredSdk(
    distribution="cyrene-integration-contracts",
    project_path="sdk/python/cyrene_integration_contracts/pyproject.toml",
    source_root="sdk/python/cyrene_integration_contracts/src/cyrene_integration_contracts",
    archive_root="src/cyrene_integration_contracts",
    package_data=("schema.json",),
)

WECOM_AIBOT_SDK = SourceDependency(
    distribution="wecom-aibot-python-sdk",
    version="1.0.1",
    repository="https://github.com/WecomTeam/wecom-aibot-python-sdk.git",
    commit="6bcb59a9a636c566f4c6ea5268b228e3def1611a",
    license_expression="MIT",
    lock_path="plugins/connectors/wecom/uv.lock",
    license_file_path=(
        "plugins/connectors/wecom/third_party/licenses/wecom-aibot-python-sdk/LICENSE"
    ),
)

# These mappings are explicit contract data. Never derive a package/component
# identity by replacing punctuation in the other identity.
PACKAGE_SPECS: dict[str, PackageSpec] = {
    "cyrene.tools.dataset-preparation": PackageSpec(
        package_id="cyrene.tools.dataset-preparation",
        component_id="cyrene-tools-dataset-preparation",
        primary_capability="dataset.preparation.v1",
        source_dir="plugins/tools/dataset-preparation",
        requirements_input="tooling/release/requirements/dataset-preparation.in",
        vendored_sdks=(RUNTIME_SDK,),
    ),
    "cyrene.tools.document-parsing": PackageSpec(
        package_id="cyrene.tools.document-parsing",
        component_id="cyrene-tools-document-parsing",
        primary_capability="document.parsing.v1",
        source_dir="plugins/tools/document-parsing",
        requirements_input="tooling/release/requirements/document-parsing.in",
        vendored_sdks=(RUNTIME_SDK,),
    ),
    "cyrene.tools.dataset-generation": PackageSpec(
        package_id="cyrene.tools.dataset-generation",
        component_id="cyrene-tools-dataset-generation",
        primary_capability="dataset.generation.v1",
        source_dir="plugins/tools/dataset-generation",
        requirements_input="tooling/release/requirements/dataset-generation.in",
        vendored_sdks=(RUNTIME_SDK, MODEL_PROVIDER_SDK),
    ),
    "cyrene.tools.knowledge-preparation": PackageSpec(
        package_id="cyrene.tools.knowledge-preparation",
        component_id="cyrene-tools-knowledge-preparation",
        primary_capability="dataset.knowledge.v1",
        source_dir="plugins/tools/knowledge-preparation",
        requirements_input="tooling/release/requirements/knowledge-preparation.in",
        vendored_sdks=(RUNTIME_SDK,),
    ),
    "cyrene.evaluation.exact-match": PackageSpec(
        package_id="cyrene.evaluation.exact-match",
        component_id="cyrene-evaluation-exact-match",
        primary_capability="evaluation.runner.v1",
        source_dir="plugins/evaluation/exact-match",
        requirements_input="tooling/release/requirements/exact-match.in",
        vendored_sdks=(RUNTIME_SDK,),
    ),
    "cyrene.accounts.account-center": PackageSpec(
        package_id="cyrene.accounts.account-center",
        component_id="cyrene-accounts-account-center",
        primary_capability="account.center.v1",
        source_dir="plugins/accounts/account-center",
        requirements_input="tooling/release/requirements/account-center.in",
        vendored_sdks=(RUNTIME_SDK, ACCOUNT_CENTER_SDK),
        require_spdx_license=True,
    ),
    "cyrene.providers.github-issues": PackageSpec(
        package_id="cyrene.providers.github-issues",
        component_id="cyrene-providers-github-issues",
        primary_capability="tool.provider.v1",
        source_dir="plugins/providers/github-issues",
        requirements_input="tooling/release/requirements/github-issues.in",
        vendored_sdks=(RUNTIME_SDK, ACCOUNT_CENTER_SDK),
        require_spdx_license=True,
    ),
    "cyrene.providers.microsoft-graph": PackageSpec(
        package_id="cyrene.providers.microsoft-graph",
        component_id="cyrene-providers-microsoft-graph",
        primary_capability="tool.provider.v1",
        source_dir="plugins/providers/microsoft-graph",
        requirements_input="tooling/release/requirements/microsoft-graph.in",
        vendored_sdks=(RUNTIME_SDK, ACCOUNT_CENTER_SDK),
        require_spdx_license=True,
    ),
    "cyrene.providers.azure-devops-observation": PackageSpec(
        package_id="cyrene.providers.azure-devops-observation",
        component_id="cyrene-providers-azure-devops-observation",
        primary_capability="tool.provider.v1",
        source_dir="plugins/providers/azure-devops-observation",
        requirements_input="tooling/release/requirements/azure-devops-observation.in",
        vendored_sdks=(RUNTIME_SDK, ACCOUNT_CENTER_SDK),
        require_spdx_license=True,
    ),
    "cyrene.providers.cloud-resources": PackageSpec(
        package_id="cyrene.providers.cloud-resources",
        component_id="cyrene-providers-cloud-resources",
        primary_capability="tool.provider.v1",
        source_dir="plugins/providers/cloud-resources",
        requirements_input="tooling/release/requirements/cloud-resources.in",
        vendored_sdks=(RUNTIME_SDK, INTEGRATION_SUPPORT_SDK),
        require_spdx_license=True,
    ),
    "cyrene.connectors.teams": PackageSpec(
        package_id="cyrene.connectors.teams",
        component_id="cyrene-connectors-teams",
        primary_capability="message.connector.v1",
        source_dir="plugins/connectors/teams",
        requirements_input="tooling/release/requirements/teams-connector.in",
        vendored_sdks=(RUNTIME_SDK,),
        require_spdx_license=True,
    ),
    "cyrene.providers.cloud-management": PackageSpec(
        package_id="cyrene.providers.cloud-management",
        component_id="cyrene-providers-cloud-management",
        primary_capability="tool.provider.v1",
        source_dir="plugins/providers/cloud-management",
        requirements_input="tooling/release/requirements/cloud-management.in",
        vendored_sdks=(RUNTIME_SDK, INTEGRATION_SUPPORT_SDK),
        require_spdx_license=True,
    ),
    "cyrene.providers.huggingface": PackageSpec(
        package_id="cyrene.providers.huggingface",
        component_id="cyrene-providers-huggingface",
        primary_capability="tool.provider.v1",
        source_dir="plugins/providers/huggingface",
        requirements_input="tooling/release/requirements/huggingface.in",
        vendored_sdks=(RUNTIME_SDK, ACCOUNT_CENTER_SDK),
        require_spdx_license=True,
    ),
    "cyrene.connectors.wecom": PackageSpec(
        package_id="cyrene.connectors.wecom",
        component_id="cyrene-connectors-wecom",
        primary_capability="message.connector.v1",
        source_dir="plugins/connectors/wecom",
        requirements_input="tooling/release/requirements/wecom-connector.in",
        vendored_sdks=(RUNTIME_SDK, INTEGRATION_SUPPORT_SDK),
        source_dependencies=(WECOM_AIBOT_SDK,),
        require_spdx_license=True,
    ),
    "cyrene.connectors.email-notification": PackageSpec(
        package_id="cyrene.connectors.email-notification",
        component_id="cyrene-connectors-email-notification",
        primary_capability="tool.provider.v1",
        source_dir="plugins/connectors/email-notification",
        requirements_input="tooling/release/requirements/email-notification.in",
        vendored_sdks=(RUNTIME_SDK, INTEGRATION_SUPPORT_SDK),
        require_spdx_license=True,
    ),
    "cyrene.providers.model-api-connector": PackageSpec(
        package_id="cyrene.providers.model-api-connector",
        component_id="cyrene-providers-model-api-connector",
        primary_capability="model.provider.v1",
        source_dir="plugins/providers/model-api-connector",
        requirements_input="tooling/release/requirements/model-api-connector.in",
        vendored_sdks=(RUNTIME_SDK, MODEL_PROVIDER_SDK),
        require_spdx_license=True,
    ),
    "cyrene.providers.memory-sqlite": PackageSpec(
        package_id="cyrene.providers.memory-sqlite",
        component_id="cyrene-providers-memory-sqlite",
        primary_capability="memory.provider.v1",
        source_dir="plugins/providers/memory-sqlite",
        requirements_input="tooling/release/requirements/memory-sqlite.in",
        vendored_sdks=(RUNTIME_SDK, MEMORY_PROVIDER_SDK, MODEL_PROVIDER_SDK),
        require_spdx_license=True,
    ),
    "cyrene.providers.memory-pgvector": PackageSpec(
        package_id="cyrene.providers.memory-pgvector",
        component_id="cyrene-providers-memory-pgvector",
        primary_capability="memory.provider.v1",
        source_dir="plugins/providers/memory-pgvector",
        requirements_input="tooling/release/requirements/memory-pgvector.in",
        vendored_sdks=(RUNTIME_SDK, MEMORY_PROVIDER_SDK, MODEL_PROVIDER_SDK),
        require_spdx_license=True,
    ),
    "cyrene.providers.knowledge-graph": PackageSpec(
        package_id="cyrene.providers.knowledge-graph",
        component_id="cyrene-providers-knowledge-graph",
        primary_capability="knowledge.graph.v1",
        source_dir="plugins/providers/knowledge-graph",
        requirements_input="tooling/release/requirements/knowledge-graph.in",
        vendored_sdks=(RUNTIME_SDK, KNOWLEDGE_GRAPH_SDK),
        require_spdx_license=True,
    ),
    "cyrene.providers.knowledge-retrieval": PackageSpec(
        package_id="cyrene.providers.knowledge-retrieval",
        component_id="cyrene-providers-knowledge-retrieval",
        primary_capability="tool.provider.v1",
        source_dir="plugins/providers/knowledge-retrieval",
        requirements_input="tooling/release/requirements/knowledge-retrieval.in",
        vendored_sdks=(RUNTIME_SDK, TOOL_PROVIDER_SDK, INTEGRATION_SUPPORT_SDK),
        require_spdx_license=True,
    ),
    "cyrene.providers.speech-sensevoice": PackageSpec(
        package_id="cyrene.providers.speech-sensevoice",
        component_id="cyrene-providers-speech-sensevoice",
        primary_capability="speech.provider.v1",
        source_dir="plugins/providers/speech-sensevoice",
        requirements_input="tooling/release/requirements/speech-sensevoice.in",
        vendored_sdks=(RUNTIME_SDK, SPEECH_PROVIDER_SDK),
        require_spdx_license=True,
    ),
    "cyrene.tools.media": PackageSpec(
        package_id="cyrene.tools.media",
        component_id="cyrene-tools-media",
        primary_capability="tool.provider.v1",
        source_dir="plugins/tools/media",
        requirements_input="tooling/release/requirements/media.in",
        vendored_sdks=(RUNTIME_SDK, TOOL_PROVIDER_SDK, INTEGRATION_SUPPORT_SDK),
        require_spdx_license=True,
    ),
}

PRODUCT_SERVICE_COMPONENTS = {
    "Cyrene-Catalyst": "cyrene-catalyst",
    "Cyrene-Echo": "cyrene-echo",
    "Cyrene-Exchange": "cyrene-exchange",
    "Cyrene-Navigator": "cyrene-navigator",
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
        or not capabilities
        or not all(isinstance(item, str) and item for item in capabilities)
        or len(capabilities) != len(set(capabilities))
    ):
        raise PackageBuildError(
            "each official package must declare unique capabilities"
        )
    if spec.primary_capability not in capabilities:
        raise PackageBuildError(
            "explicit primary capability is not declared by the Plugin manifest"
        )
    license_expression = manifest.get("licenseExpression") or manifest.get("license")
    if license_expression is not None and (
        not isinstance(license_expression, str)
        or not re.fullmatch(
            r"[A-Za-z0-9.+-]+(?:\s+(?:AND|OR|WITH)\s+[A-Za-z0-9.+-]+)*",
            license_expression,
        )
    ):
        raise PackageBuildError("Plugin license must be a valid SPDX expression")
    if spec.require_spdx_license and not license_expression:
        raise PackageBuildError(
            f"{spec.package_id} must declare an SPDX license before release packaging"
        )
    methods = manifest.get("methods")
    if not isinstance(methods, list) or not methods:
        raise PackageBuildError("each official package must expose versioned methods")
    for method in methods:
        if not isinstance(method, dict):
            raise PackageBuildError("each official package method must be an object")
        name = method.get("name")
        if not isinstance(name, str) or not name:
            raise PackageBuildError("each official package method must have a name")
        capability = method.get("capability", spec.primary_capability)
        if capability not in capabilities:
            raise PackageBuildError("method capability is absent from the Plugin manifest")
        expected_version = (
            MODEL_METHOD_VERSIONS.get(name)
            if capability == "model.provider.v1"
            else "1"
        )
        if expected_version is None or method.get("interfaceVersion") != expected_version:
            raise PackageBuildError("Plugin method differs from its supported interface version")
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
        "capability": spec.primary_capability,
        "capabilities": list(capabilities),
        "license_expression": license_expression,
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


def _walk_python_sources(
    path: Path, root: Path, *, package_data: tuple[str, ...] = ()
) -> dict[str, bytes]:
    """Collect Python source, typing markers, and explicitly declared package data."""

    if not path.is_dir() or path.is_symlink():
        raise PackageBuildError(
            f"internal SDK source directory is missing or unsafe: {path}"
        )
    parsed_data_paths = tuple(PurePosixPath(item) for item in package_data)
    if any(
        not item
        or not path.parts
        or path.as_posix() != item
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in item
        for item, path in zip(package_data, parsed_data_paths, strict=True)
    ):
        raise PackageBuildError("vendored SDK package data path is invalid")
    data_paths = {path.as_posix() for path in parsed_data_paths}
    files: dict[str, bytes] = {}
    found_data_paths: set[str] = set()
    for candidate in sorted(path.rglob("*")):
        if candidate.is_symlink():
            raise PackageBuildError(
                f"source tree may not contain a symlink: {candidate}"
            )
        relative = candidate.relative_to(path).as_posix()
        if candidate.is_file() and (
            candidate.suffix == ".py"
            or candidate.name == "py.typed"
            or relative in data_paths
        ):
            files[candidate.relative_to(root).as_posix()] = _regular_source(
                candidate, root
            )
            if relative in data_paths:
                found_data_paths.add(relative)
    missing_data_paths = data_paths.difference(found_data_paths)
    if missing_data_paths:
        raise PackageBuildError(
            f"vendored SDK package data is missing: {sorted(missing_data_paths)}"
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
        relative = path.relative_to(package_root)
        if any(
            part
            in {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"}
            for part in relative.parts
        ):
            continue
        if path.is_symlink():
            raise PackageBuildError(
                f"Plugin source tree may not contain a symlink: {path}"
            )
        if not path.is_file():
            continue
        if (
            (path.suffix == ".py" and "tests" not in relative.parts)
            or (path.suffix == ".json" and "contracts" in relative.parts)
            or path.name
            in {
                "LICENSE",
                "PROVENANCE.md",
                "configuration.schema.json",
                "THIRD_PARTY_NOTICES.md",
            }
        ):
            candidates.add(path)
    for dependency in spec.source_dependencies:
        candidates.add(root / dependency.lock_path)
        candidates.add(root / dependency.license_file_path)

    files = {
        path.relative_to(root).as_posix(): _regular_source(path, root)
        for path in sorted(candidates)
    }
    sdk_files: dict[str, dict[str, bytes]] = {}
    for sdk in spec.vendored_sdks:
        sdk_files[sdk.distribution] = _walk_python_sources(
            root / sdk.source_root, root, package_data=sdk.package_data
        )
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


def _wheel_metadata(payload: bytes, filename: str) -> tuple[str, str]:
    """Read identity and ensure WHEEL tags agree with the filename tags.

    中文：读取 wheel 身份，并确保 WHEEL 元数据与文件名标签一致。
    """

    try:
        _, _, _, filename_tags = parse_wheel_filename(filename)
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            metadata_names = [
                item
                for item in archive.namelist()
                if item.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise PackageBuildError(
                    f"wheel must contain one METADATA file: {filename}"
                )
            wheel_names = [
                item for item in archive.namelist() if item.endswith(".dist-info/WHEEL")
            ]
            if len(wheel_names) != 1:
                raise PackageBuildError(
                    f"wheel must contain one WHEEL metadata file: {filename}"
                )
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
            wheel_metadata = BytesParser().parsebytes(archive.read(wheel_names[0]))
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as error:
        raise PackageBuildError(f"invalid wheel archive: {filename}") from error
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise PackageBuildError(f"wheel has no distribution identity: {filename}")
    try:
        declared_tags = {
            tag
            for raw_tag in wheel_metadata.get_all("Tag", [])
            for tag in parse_tag(raw_tag)
        }
    except (TypeError, ValueError) as error:
        raise PackageBuildError(f"wheel has invalid WHEEL tags: {filename}") from error
    if not declared_tags or declared_tags != filename_tags:
        raise PackageBuildError(
            f"wheel WHEEL metadata tags do not match its filename: {filename}"
        )
    return canonicalize_name(name), version


def _wheelhouse_entries(lock_bytes: bytes, wheel_paths: list[Path]) -> dict[str, bytes]:
    """Build a lock-bound wheelhouse manifest from one exact wheel per pin."""

    pins = _lock_lines(lock_bytes, "requirements.lock")
    expected = {
        canonicalize_name(LOCK_PIN.fullmatch(pin).group(1)): LOCK_PIN.fullmatch(
            pin
        ).group(2)
        for pin in pins
    }
    records: list[dict[str, Any]] = []
    entries: dict[str, bytes] = {}
    found: dict[str, str] = {}
    for path in sorted(wheel_paths, key=lambda item: item.name):
        if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
            raise PackageBuildError(f"wheelhouse contains an unsafe file: {path}")
        filename = path.name
        if "/" in filename or "\\" in filename:
            raise PackageBuildError(f"wheelhouse filename is unsafe: {filename}")
        payload = path.read_bytes()
        try:
            distribution, version, _build, tags = parse_wheel_filename(filename)
        except ValueError as error:
            raise PackageBuildError(f"invalid wheel filename: {filename}") from error
        normalized = canonicalize_name(str(distribution))
        locked_version = expected.get(normalized)
        if locked_version != str(version) or normalized in found:
            raise PackageBuildError(
                f"wheelhouse wheel is extra, duplicated, or does not match the lock: {filename}"
            )
        metadata_name, metadata_version = _wheel_metadata(payload, filename)
        if metadata_name != normalized or metadata_version != locked_version:
            raise PackageBuildError(
                f"wheel metadata does not match the lock: {filename}"
            )
        if tags.isdisjoint(sys_tags()):
            raise PackageBuildError(
                f"wheel is incompatible with {TARGET_ID}: {filename}"
            )
        digest = training_release.sha256_bytes(payload)
        records.append(
            {
                "filename": filename,
                "name": normalized,
                "version": locked_version,
                "sha256": digest,
                "size": len(payload),
            }
        )
        entries[f"wheelhouse/{filename}"] = payload
        found[normalized] = filename
    if set(found) != set(expected):
        missing = sorted(set(expected).difference(found))
        raise PackageBuildError(f"wheelhouse is missing exact locked wheels: {missing}")
    manifest = {
        "schemaVersion": 1,
        "target": WHEELHOUSE_TARGET,
        "requirementsLockSha256": training_release.sha256_bytes(lock_bytes),
        "wheels": records,
    }
    entries[WHEELHOUSE_MANIFEST] = _json_bytes(manifest)
    _verify_wheelhouse_entries(entries, lock_bytes)
    return entries


def _verify_wheelhouse_entries(
    entries: dict[str, bytes], lock_bytes: bytes
) -> dict[str, Any]:
    """Fail closed unless package wheel files exactly satisfy its bound lock."""

    manifest_bytes = entries.get(WHEELHOUSE_MANIFEST)
    if manifest_bytes is None:
        raise PackageBuildError("package archive lacks wheelhouse/manifest.json")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PackageBuildError("wheelhouse manifest is invalid JSON") from error
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {"schemaVersion", "target", "requirementsLockSha256", "wheels"}
        or manifest.get("schemaVersion") != 1
        or manifest.get("target") != WHEELHOUSE_TARGET
        or manifest.get("requirementsLockSha256")
        != training_release.sha256_bytes(lock_bytes)
    ):
        raise PackageBuildError("wheelhouse manifest is not bound to this lock/target")
    pins = _lock_lines(lock_bytes, "requirements.lock")
    expected = {
        canonicalize_name(LOCK_PIN.fullmatch(pin).group(1)): LOCK_PIN.fullmatch(
            pin
        ).group(2)
        for pin in pins
    }
    wheels = manifest.get("wheels")
    if not isinstance(wheels, list) or len(wheels) != len(expected):
        raise PackageBuildError("wheelhouse manifest does not enumerate the full lock")
    seen: dict[str, str] = {}
    paths: set[str] = {WHEELHOUSE_MANIFEST}
    for record in wheels:
        if not isinstance(record, dict) or set(record) != {
            "filename",
            "name",
            "version",
            "sha256",
            "size",
        }:
            raise PackageBuildError("wheelhouse manifest has an invalid wheel record")
        filename = record.get("filename")
        name = record.get("name")
        version = record.get("version")
        digest = record.get("sha256")
        size = record.get("size")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
            or not filename.endswith(".whl")
            or not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
        ):
            raise PackageBuildError("wheelhouse manifest has an unsafe wheel record")
        normalized = canonicalize_name(name)
        if normalized in seen or expected.get(normalized) != version:
            raise PackageBuildError(
                "wheelhouse includes an unpinned or duplicate wheel"
            )
        payload = entries.get(f"wheelhouse/{filename}")
        if payload is None:
            raise PackageBuildError(f"wheelhouse wheel is missing: {filename}")
        wheel_digest = training_release.sha256_bytes(payload)
        if wheel_digest != digest or len(payload) != size:
            raise PackageBuildError(f"wheelhouse wheel digest mismatch: {filename}")
        try:
            distribution, wheel_version, _build, tags = parse_wheel_filename(filename)
        except ValueError as error:
            raise PackageBuildError(f"invalid wheel filename: {filename}") from error
        metadata_name, metadata_version = _wheel_metadata(payload, filename)
        if (
            canonicalize_name(str(distribution)) != normalized
            or str(wheel_version) != version
            or metadata_name != normalized
            or metadata_version != version
            or tags.isdisjoint(sys_tags())
        ):
            raise PackageBuildError(f"wheel identity mismatch: {filename}")
        seen[normalized] = filename
        paths.add(f"wheelhouse/{filename}")
    if set(seen) != set(expected):
        raise PackageBuildError("wheelhouse manifest omits one or more locked wheels")
    if set(entries) != paths:
        raise PackageBuildError("wheelhouse has missing or unlisted files")
    return manifest


def _download_wheelhouse(
    root: Path, spec: PackageSpec, lock_bytes: bytes, destination: Path
) -> dict[str, bytes]:
    """Resolve and download the complete binary closure for the frozen target."""

    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        raise PackageBuildError(
            f"wheelhouse generation requires the {TARGET_ID} builder environment"
        )
    try:
        os_release = platform.freedesktop_os_release()
    except (AttributeError, OSError):
        os_release = {}
    _require_native_target(TARGET_ID)
    if os_release.get("ID") != "ubuntu" or os_release.get("VERSION_ID") != "24.04":
        raise PackageBuildError(
            f"wheelhouse generation requires the {TARGET_ID} builder environment"
        )
    destination.mkdir(parents=True, exist_ok=True)
    lock = root / spec.source_dir / "requirements.lock"
    source_names = {
        canonicalize_name(dependency.distribution)
        for dependency in spec.source_dependencies
    }
    registry_pins = [
        line
        for line in _lock_lines(lock_bytes, str(lock))
        if canonicalize_name(LOCK_PIN.fullmatch(line).group(1)) not in source_names
    ]
    if not registry_pins:
        raise PackageBuildError("runtime lock has no registry wheels to download")
    with tempfile.TemporaryDirectory(prefix="cyrene-package-lock-filter-") as temp:
        registry_lock = Path(temp) / "registry-requirements.lock"
        registry_lock.write_text("\n".join(registry_pins) + "\n", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--no-deps",
                "--requirement",
                str(registry_lock),
                "--dest",
                str(destination),
            ],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise PackageBuildError(
            "could not resolve the locked binary wheel closure"
            + (f": {detail[-2000:]}" if detail else "")
        )
    _build_source_dependency_wheels(root, spec, destination)
    wheel_paths = sorted(destination.glob("*.whl"), key=lambda item: item.name)
    return _wheelhouse_entries(lock_bytes, wheel_paths)


def _build_source_dependency_wheels(
    root: Path, spec: PackageSpec, destination: Path
) -> None:
    """Build allowlisted source wheels after verifying exact upstream commits."""

    for dependency in spec.source_dependencies:
        with tempfile.TemporaryDirectory(prefix="cyrene-source-wheel-") as temp:
            checkout = Path(temp) / "source"
            commands = (
                ["git", "init", str(checkout)],
                [
                    "git",
                    "-C",
                    str(checkout),
                    "remote",
                    "add",
                    "origin",
                    dependency.repository,
                ],
                [
                    "git",
                    "-C",
                    str(checkout),
                    "fetch",
                    "--depth=1",
                    "origin",
                    dependency.commit,
                ],
                [
                    "git",
                    "-C",
                    str(checkout),
                    "checkout",
                    "--quiet",
                    "--detach",
                    "FETCH_HEAD",
                ],
            )
            for command in commands:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "").strip()
                    raise PackageBuildError(
                        "could not fetch the pinned source dependency"
                        + (f": {detail[-2000:]}" if detail else "")
                    )
            actual_commit = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if actual_commit != dependency.commit:
                raise PackageBuildError(
                    f"source dependency checkout differs from its pin: {dependency.distribution}"
                )
            try:
                project = tomllib.loads(
                    (checkout / "pyproject.toml").read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
                raise PackageBuildError(
                    f"source dependency project metadata is missing: {dependency.distribution}"
                ) from error
            project_record = project.get("project", {})
            if (
                project_record.get("name") != dependency.distribution
                or project_record.get("version") != dependency.version
                or project_record.get("license") != dependency.license_expression
            ):
                raise PackageBuildError(
                    f"source dependency identity or SPDX license changed: {dependency.distribution}"
                )
            upstream_license = _regular_source(checkout / "LICENSE", checkout)
            repository_license = _regular_source(
                root / dependency.license_file_path, root
            )
            if upstream_license != repository_license:
                raise PackageBuildError(
                    f"vendored third-party license differs from the pinned source: {dependency.distribution}"
                )
            build_environment = Path(temp) / "build-environment"
            environment_creation = subprocess.run(
                [sys.executable, "-m", "venv", str(build_environment)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if environment_creation.returncode != 0:
                detail = (
                    environment_creation.stderr or environment_creation.stdout or ""
                ).strip()
                raise PackageBuildError(
                    "could not create the isolated source build environment"
                    + (f": {detail[-2000:]}" if detail else "")
                )
            build_python = build_environment / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            source_build_lock = root / SOURCE_DEPENDENCY_BUILD_LOCK_PATH
            source_build_pins = tuple(
                line.strip()
                for line in source_build_lock.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
            if source_build_pins != SOURCE_DEPENDENCY_BUILD_PINS or any(
                LOCK_PIN.fullmatch(line) is None for line in source_build_pins
            ):
                raise PackageBuildError(
                    "source dependency build lock differs from the approved exact pins"
                )
            build_tools = subprocess.run(
                [
                    str(build_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--requirement",
                    str(source_build_lock),
                ],
                cwd=root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if build_tools.returncode != 0:
                detail = (build_tools.stderr or build_tools.stdout or "").strip()
                raise PackageBuildError(
                    "could not install the pinned source build tools"
                    + (f": {detail[-2000:]}" if detail else "")
                )
            result = subprocess.run(
                [
                    str(build_python),
                    "-m",
                    "pip",
                    "wheel",
                    "--disable-pip-version-check",
                    "--no-build-isolation",
                    "--no-deps",
                    "--wheel-dir",
                    str(destination),
                    str(checkout),
                ],
                cwd=root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise PackageBuildError(
                    f"could not build pinned source wheel {dependency.distribution}"
                    + (f": {detail[-2000:]}" if detail else "")
                )
            wheels = [
                path
                for path in destination.glob("*.whl")
                if _wheel_metadata(path.read_bytes(), path.name)
                == (
                    canonicalize_name(dependency.distribution),
                    dependency.version,
                )
            ]
            if len(wheels) != 1:
                raise PackageBuildError(
                    f"source wheel identity is ambiguous: {dependency.distribution}"
                )
            try:
                with zipfile.ZipFile(wheels[0]) as archive:
                    license_entries = [
                        name for name in archive.namelist() if name.endswith("/LICENSE")
                    ]
                    metadata_entries = [
                        name
                        for name in archive.namelist()
                        if name.endswith(".dist-info/METADATA")
                    ]
                    if len(metadata_entries) != 1:
                        raise PackageBuildError(
                            f"source dependency wheel metadata is ambiguous: {dependency.distribution}"
                        )
                    wheel_metadata = BytesParser().parsebytes(
                        archive.read(metadata_entries[0])
                    )
                    lock_path = root / spec.source_dir / "requirements.lock"
                    locked_versions = {
                        canonicalize_name(
                            LOCK_PIN.fullmatch(line).group(1)
                        ): LOCK_PIN.fullmatch(line).group(2)
                        for line in _lock_lines(lock_path.read_bytes(), str(lock_path))
                    }
                    for raw_requirement in wheel_metadata.get_all("Requires-Dist", []):
                        requirement = Requirement(raw_requirement)
                        if (
                            requirement.marker is not None
                            and not requirement.marker.evaluate()
                        ):
                            continue
                        locked_version = locked_versions.get(
                            canonicalize_name(requirement.name)
                        )
                        if locked_version is None or (
                            requirement.specifier
                            and not requirement.specifier.contains(
                                locked_version, prereleases=True
                            )
                        ):
                            raise PackageBuildError(
                                "runtime lock does not satisfy pinned source wheel dependency: "
                                f"{dependency.distribution}/{requirement.name}"
                            )
                    if (
                        wheel_metadata.get("License-Expression")
                        != dependency.license_expression
                    ):
                        raise PackageBuildError(
                            f"source dependency wheel SPDX expression differs from its pin: {dependency.distribution}"
                        )
                    wheel_licenses = [
                        archive.read(name)
                        for name in license_entries
                        if name.endswith("/licenses/LICENSE")
                    ]
            except zipfile.BadZipFile as error:
                raise PackageBuildError(
                    f"source dependency wheel is invalid: {dependency.distribution}"
                ) from error
            if not wheel_licenses:
                raise PackageBuildError(
                    f"source dependency wheel omits its license: {dependency.distribution}"
                )
            if repository_license not in wheel_licenses:
                raise PackageBuildError(
                    f"source dependency wheel license differs from its pinned source: {dependency.distribution}"
                )


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
    source_dependency_by_name = {
        canonicalize_name(dependency.distribution): dependency
        for dependency in spec.source_dependencies
    }
    input_by_name = {
        canonicalize_name(requirement.name): requirement
        for requirement in input_requirements
    }
    input_comments = input_bytes.decode("utf-8")
    matched_source_dependencies: set[str] = set()
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

        source_dependency = source_dependency_by_name.get(name)
        if source_dependency is not None:
            expected_url = (
                f"git+{source_dependency.repository}@{source_dependency.commit}"
            )
            if requirement.url != expected_url:
                raise PackageBuildError(
                    f"Plugin source dependency is not pinned to the approved commit: {name}"
                )
            lock_input = input_by_name.get(name)
            if (
                lock_input is None
                or str(lock_input.specifier) != f"=={source_dependency.version}"
                or pin_versions.get(name) != source_dependency.version
            ):
                raise PackageBuildError(
                    f"runtime lock does not bind the source dependency version: {name}"
                )
            uv_lock_path = source_dependency.lock_path
            try:
                uv_lock = tomllib.loads(source_files[uv_lock_path].decode("utf-8"))
            except (KeyError, UnicodeError, tomllib.TOMLDecodeError) as error:
                raise PackageBuildError(
                    f"source dependency lock is missing or invalid: {uv_lock_path}"
                ) from error
            locked_sources = [
                item
                for item in uv_lock.get("package", [])
                if isinstance(item, dict)
                and item.get("name") == source_dependency.distribution
            ]
            expected_locked_source = (
                f"{source_dependency.repository}?rev={source_dependency.commit}"
                f"#{source_dependency.commit}"
            )
            if len(locked_sources) != 1 or (
                locked_sources[0].get("version") != source_dependency.version
                or locked_sources[0].get("source", {}).get("git")
                != expected_locked_source
            ):
                raise PackageBuildError(
                    f"source dependency uv lock differs from the approved commit: {name}"
                )
            if name not in input_by_name:
                raise PackageBuildError(
                    f"runtime lock input omits the source dependency: {name}"
                )
            matched_source_dependencies.add(name)
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

    if matched_source_dependencies != set(source_dependency_by_name):
        raise PackageBuildError(
            "one or more declared source dependencies are not required by the Plugin"
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
    wheelhouse_entries: dict[str, bytes] | None = None,
) -> dict[str, bytes]:
    entries: dict[str, bytes] = {"LICENSE": source_files["LICENSE"]}
    prefix = f"{spec.source_dir}/"
    for source, content in source_files.items():
        if not source.startswith(prefix):
            continue
        relative = source[len(prefix) :]
        if relative.endswith(".py"):
            archive_path = (
                relative if relative.startswith("src/") else f"src/{relative}"
            )
            entries[archive_path] = content
        elif relative == "LICENSE":
            entries["licenses/plugin/LICENSE"] = content
        elif (
            relative
            in {
                "README.md",
                "PROVENANCE.md",
                "plugin.manifest.json",
                "requirements.lock",
                "configuration.schema.json",
            }
            or (relative.startswith("contracts/") and relative.endswith(".json"))
            or relative.startswith("third_party/licenses/")
        ):
            entries[relative] = content
    for sdk in spec.vendored_sdks:
        sdk_prefix = f"{sdk.source_root}/"
        for source, content in sdk_files[sdk.distribution].items():
            if not source.startswith(sdk_prefix):
                continue
            relative = source[len(sdk_prefix) :]
            entries[f"{sdk.archive_root}/{relative}"] = content
    if wheelhouse_entries:
        entries.update(wheelhouse_entries)
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


def _source_dependency_metadata(
    spec: PackageSpec, source_files: dict[str, bytes]
) -> list[dict[str, Any]]:
    """Describe pinned third-party wheels and their retained SPDX notices."""

    records: list[dict[str, Any]] = []
    for dependency in spec.source_dependencies:
        source_path = Path(dependency.license_file_path)
        try:
            archive_path = source_path.relative_to(Path(spec.source_dir)).as_posix()
        except ValueError as error:
            raise PackageBuildError(
                f"third-party license must be inside its Plugin source tree: {source_path}"
            ) from error
        license_bytes = source_files[dependency.license_file_path]
        records.append(
            {
                "distribution": dependency.distribution,
                "version": dependency.version,
                "source": {
                    "repository": dependency.repository,
                    "commit": dependency.commit,
                    "lock": dependency.lock_path,
                },
                "license_expression": dependency.license_expression,
                "license_file": {
                    "archive_path": archive_path,
                    "sha256": training_release.sha256_bytes(license_bytes),
                },
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
        component: dict[str, Any] = {
            "type": "library",
            "name": canonicalize_name(name),
            "version": version,
            "purl": reference,
            "bom-ref": reference,
        }
        source_dependency = next(
            (
                item
                for item in spec.source_dependencies
                if canonicalize_name(item.distribution) == canonicalize_name(name)
            ),
            None,
        )
        if source_dependency is not None:
            component["licenses"] = [
                {"expression": source_dependency.license_expression}
            ]
            component["externalReferences"] = [
                {"type": "vcs", "url": source_dependency.repository}
            ]
            component["properties"] = [
                {"name": "cyrene:source:commit", "value": source_dependency.commit},
                {"name": "cyrene:delivery", "value": "wheel-built-from-pinned-source"},
            ]
        components.append(component)
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
        {
            "name": "cyrene:capability-ids",
            "value": json.dumps(package["capabilities"], separators=(",", ":")),
        },
        {"name": "cyrene:source:repository", "value": SOURCE_REPOSITORY_URL},
        {"name": "cyrene:source:ref", "value": source_ref},
        {"name": "cyrene:source:commit", "value": source_sha},
        {
            "name": "cyrene:package:requirements-lock",
            "value": f"{spec.source_dir}/requirements.lock",
        },
    ]
    package_component: dict[str, Any] = {
        "type": "application",
        "name": spec.package_id,
        "version": package["version"],
        "bom-ref": package_ref,
        "properties": package_properties,
        "externalReferences": [{"type": "vcs", "url": SOURCE_REPOSITORY_URL}],
    }
    if package.get("license_expression"):
        package_component["licenses"] = [{"expression": package["license_expression"]}]
    return {
        "bomFormat": SBOM_FORMAT,
        "specVersion": SBOM_SPEC_VERSION,
        "version": 1,
        "metadata": {"component": package_component},
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
    wheelhouse_source: Path | None = None,
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
    lock_bytes = source_files[f"{spec.source_dir}/requirements.lock"]

    wheelhouse_owner = tempfile.TemporaryDirectory(prefix="cyrene-package-wheelhouse-")
    wheelhouse_path = Path(wheelhouse_owner.name)
    try:
        if wheelhouse_source is None:
            wheelhouse_entries = _download_wheelhouse(
                root, spec, lock_bytes, wheelhouse_path
            )
        else:
            if wheelhouse_source.is_symlink():
                raise PackageBuildError("wheelhouse source must not be a symlink")
            source_path = wheelhouse_source.resolve(strict=True)
            if source_path.is_symlink() or not source_path.is_dir():
                raise PackageBuildError("wheelhouse source must be a regular directory")
            wheelhouse_entries = _wheelhouse_entries(
                lock_bytes,
                [path for path in source_path.iterdir() if path.suffix == ".whl"],
            )
        package_entries = _package_entries(
            source_files, spec, sdk_files, wheelhouse_entries
        )
    except BaseException:
        wheelhouse_owner.cleanup()
        raise

    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        wheelhouse_owner.cleanup()
        raise PackageBuildError(f"release output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    wheel_owner: tempfile.TemporaryDirectory[str] | None = None
    try:
        if preparer_wheel is None:
            wheel_path, wheel_owner = training_release.build_runtime_wheel(
                root, source_sha
            )
        else:
            wheel_path = preparer_wheel.resolve(strict=True)
    except BaseException:
        if wheel_owner is not None:
            wheel_owner.cleanup()
        wheelhouse_owner.cleanup()
        raise
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
        source_dependency_records = _source_dependency_metadata(spec, source_files)
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
                "capabilities": package["capabilities"],
                "interface_version": "1",
                "license_expression": package["license_expression"],
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
                "architecture": TARGET["architecture"],
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
            "source_dependencies": source_dependency_records,
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
        wheelhouse_owner.cleanup()


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
    wheelhouse_entries = {
        name: content
        for name, content in entries.items()
        if name.startswith("wheelhouse/")
    }
    source_entries = {
        name: content
        for name, content in entries.items()
        if not name.startswith("wheelhouse/")
    }
    if source_entries != _package_entries(source_files, spec, sdk_files):
        raise PackageBuildError(
            "package archive payload differs from the allowlisted source"
        )
    _verify_wheelhouse_entries(
        wheelhouse_entries,
        source_files[f"{spec.source_dir}/requirements.lock"],
    )
    if entries.get("requirements.lock") != paths["dependency_lock"].read_bytes():
        raise PackageBuildError("package ZIP lock differs from the signed lock asset")
    entry_module = package["entrypoint"].split(":", 1)[0].replace(".", "/")
    module_source = f"src/{entry_module}.py"
    package_source = f"src/{entry_module}/__init__.py"
    if (module_source not in entries and package_source not in entries) or (
        f"{entry_module}.py" in entries
    ):
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
        or metadata.get("package", {}).get("capabilities") != package["capabilities"]
        or metadata.get("package", {}).get("license_expression")
        != package["license_expression"]
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
        or metadata.get("source_dependencies")
        != _source_dependency_metadata(spec, source_files)
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
            parsed_generated_at = datetime.fromisoformat(generated_at)
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


def verify_published_release_readback(
    *,
    record_by_id: Any,
    record_by_tag: Any,
    tag_ref: Any,
    asset_paths: list[Path],
    release_tag: str,
    package_id: str,
    package_version: str,
    source_ref: str,
    source_sha: str,
    channel: str,
) -> None:
    """Verify immutable release records, exact assets, and source tag target.

    中文：核验不可变 release、完整资产摘要以及 source tag 的精确 commit。
    """

    spec = PACKAGE_SPECS.get(package_id)
    if spec is None:
        raise PackageBuildError("published release read-back has an unknown package ID")
    expected_tag = _channel_identity(
        channel, source_ref, source_sha, spec.component_id, package_version
    )
    if expected_tag is None or release_tag != expected_tag:
        raise PackageBuildError(
            "published release read-back tag differs from the source identity"
        )

    expected_assets: dict[str, dict[str, Any]] = {}
    for path in asset_paths:
        if path.is_symlink() or not path.is_file():
            raise PackageBuildError(
                f"verified published asset is missing or unsafe: {path}"
            )
        name = path.name
        if name in expected_assets:
            raise PackageBuildError(
                f"verified published asset basename is duplicated: {name}"
            )
        expected_assets[name] = {
            "size": path.stat().st_size,
            "digest": training_release.sha256_file(path),
        }
    if not expected_assets:
        raise PackageBuildError("verified published asset set is empty")

    release_id: int | None = None
    for record in (record_by_id, record_by_tag):
        if not isinstance(record, dict):
            raise PackageBuildError("published release read-back is malformed")
        current_id = record.get("id")
        if type(current_id) is not int or current_id < 1:
            raise PackageBuildError("published release read-back lacks a database ID")
        if release_id is None:
            release_id = current_id
        if (
            current_id != release_id
            or record.get("tag_name") != release_tag
            or record.get("draft") is not False
            or record.get("immutable") is not True
            or record.get("prerelease") is not (channel == "preview")
        ):
            raise PackageBuildError(
                "published release read-back identity or immutable state differs"
            )
        rows = record.get("assets")
        if not isinstance(rows, list):
            raise PackageBuildError("published release read-back assets are missing")
        actual_assets: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise PackageBuildError(
                    "published release read-back contains a malformed asset"
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
                    "published release read-back asset metadata is malformed"
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
                "published release assets differ from verified inputs "
                f"(missing={missing}, unexpected={unexpected}, mismatched={mismatched})"
            )

    ref = f"refs/tags/{release_tag}"
    tag_object = tag_ref.get("object") if isinstance(tag_ref, dict) else None
    if (
        not isinstance(tag_ref, dict)
        or tag_ref.get("ref") != ref
        or not isinstance(tag_object, dict)
        or tag_object.get("type") != "commit"
        or tag_object.get("sha") != source_sha
    ):
        raise PackageBuildError(
            "published package tag does not resolve to the expected source commit"
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
        command.add_argument("--target", choices=tuple(TARGETS), required=True)
        command.add_argument("--github-sha", required=True)
        command.add_argument("--run-id")
        command.add_argument("--run-attempt", type=int, default=1)
        command.set_defaults(handler=handler)
    return parser


def main() -> int:
    options = _arguments().parse_args()
    try:
        _require_native_target(options.target)
        if options.target != TARGET_ID:
            raise PackageBuildError(
                f"selected target {options.target} does not match process target {TARGET_ID}"
            )
        options.handler(options)
    except (OSError, UnicodeError, zipfile.BadZipFile, PackageBuildError) as error:
        raise SystemExit(f"official Plugin package release: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
