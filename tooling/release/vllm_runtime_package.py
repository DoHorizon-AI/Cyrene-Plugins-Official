"""Build and verify the immutable HTTP control/import package for vLLM.

This bundle owns model import through the existing Artifact SDK. It does not
bundle vLLM, PyTorch, CUDA, or an inference server environment.

中文：构建并验证 vLLM HTTP 控制/导入 package。该 bundle 通过现有 Artifact SDK
导入模型；不包含 vLLM、PyTorch、CUDA 或推理服务环境。
"""

from __future__ import annotations

import argparse
import email.message
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import tomllib

REPOSITORY = "DoHorizon-AI/Cyrene-Plugins-Official"
SOURCE_REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
WORKFLOW_PATH = ".github/workflows/vllm-runtime-package-release.yml"
PRODUCER_PATH = "tooling/release/vllm_runtime_package.py"
PACKAGE_SOURCE = "plugins/serving/vllm-runtime"
LOCK_SOURCE = "tooling/release/vllm-runtime-requirements.lock"
BUILD_LOCK_SOURCE = "tooling/release/build-requirements.lock"
TEST_SOURCE = "tests/release/test_vllm_runtime_package.py"
PACKAGE_ID = "cyrene.serving.vllm-runtime"
PACKAGE_VERSION = "0.1.0"
CAPABILITY_ID = "execution.engine.v1"
INTERFACE_VERSION = "1"
ENTRYPOINT = "package_service_adapter:main"
PLATFORM_REPOSITORY = "DoHorizon-AI/Cyrene-Platform"
PLATFORM_SOURCE_SHA = "c59be6f2bd82489fbe933dadff84fc589e00afd9"
PLATFORM_BUILDER_DEPENDENCY = "PyYAML==6.0.1"
ARTIFACT_DISTRIBUTION = "cyrene-artifacts"
ARTIFACT_VERSION = "0.1.0"
ARTIFACT_WHEEL = "cyrene_artifacts-0.1.0-py3-none-any.whl"
ARTIFACT_PROJECT = (
    "cyrene-artifacts @ git+https://github.com/DoHorizon-AI/Cyrene-Platform.git@"
    f"{PLATFORM_SOURCE_SHA}#subdirectory=sdk/python/cyrene_artifacts"
)
TARGET_ID = "linux-x86_64"
SUPPORTED_CHANNELS = {"candidate", "preview", "stable"}
EXPECTED_LOCK_LINES = ("cyrene-artifacts==0.1.0",)
BUILD_LOCK_LINES = (
    "build==1.6.1",
    "packaging==26.3",
    "pyproject-hooks==1.3.3",
    "setuptools==68.1.2",
    "wheel==0.48.0",
)
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
PIN_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+==[A-Za-z0-9][A-Za-z0-9_.+!-]*$")
EXPECTED_PACKAGE_PATHS = (
    "README.md",
    "PROVENANCE.md",
    "plugin.manifest.json",
    "package_service_adapter.py",
    "vllm_runtime.py",
    "contracts/v1/schema.json",
)


class PackageBuildError(ValueError):
    """Raised when package inputs or release outputs break the frozen contract.

    中文：package 输入或发布产物违反固定合约时抛出。
    """


def sha256_bytes(payload: bytes) -> str:
    """Return a lower-case SHA-256 digest in Package Spec form."""

    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def sha256_file(path: Path) -> str:
    """Hash a file without loading the complete asset into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PackageBuildError(f"cannot read JSON metadata {path}: {error}") from error
    if not isinstance(value, dict):
        raise PackageBuildError(f"JSON metadata must be an object: {path}")
    return value


def _canonical_digest(entries: dict[str, bytes]) -> str:
    """Match cy-package-runtime's length-prefixed, sorted-entry digest."""

    digest = hashlib.sha256()
    for name, content in sorted(entries.items()):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _source_files(root: Path) -> dict[str, bytes]:
    """Read the explicit source closure that determines this release."""

    root = root.resolve(strict=True)
    fixed = (
        "LICENSE",
        *(f"{PACKAGE_SOURCE}/{name}" for name in EXPECTED_PACKAGE_PATHS),
        f"{PACKAGE_SOURCE}/pyproject.toml",
        PRODUCER_PATH,
        LOCK_SOURCE,
        BUILD_LOCK_SOURCE,
        TEST_SOURCE,
    )
    files: dict[str, bytes] = {}
    for relative in sorted(fixed):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise PackageBuildError(f"required source must be a regular file: {relative}")
        if not path.resolve(strict=True).is_relative_to(root):
            raise PackageBuildError(f"source resolves outside the repository: {relative}")
        try:
            files[relative] = path.read_bytes()
        except OSError as error:
            raise PackageBuildError(f"cannot read source {relative}: {error}") from error

    lock_lines = tuple(
        line.strip()
        for line in files[LOCK_SOURCE].decode("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if lock_lines != EXPECTED_LOCK_LINES or any(
        not PIN_PATTERN.fullmatch(line) for line in lock_lines
    ):
        raise PackageBuildError("runtime lock must contain only cyrene-artifacts==0.1.0")
    build_lines = tuple(
        line.strip()
        for line in files[BUILD_LOCK_SOURCE].decode("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if build_lines != BUILD_LOCK_LINES:
        raise PackageBuildError("pinned Python wheel build tools changed")
    try:
        project = tomllib.loads(files[f"{PACKAGE_SOURCE}/pyproject.toml"].decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise PackageBuildError("vLLM runtime pyproject.toml is invalid") from error
    if (project.get("project") or {}).get("dependencies") != [ARTIFACT_PROJECT]:
        raise PackageBuildError("Artifact SDK dependency must retain the exact Platform source pin")
    return files


def _manifest_contract(manifest: dict[str, Any]) -> tuple[str, str]:
    """Fail closed unless the package manifest retains the HTTP service contract."""

    if manifest.get("id") != PACKAGE_ID or manifest.get("version") != PACKAGE_VERSION:
        raise PackageBuildError("vLLM package identity or version changed")
    if manifest.get("capabilities") != [CAPABILITY_ID]:
        raise PackageBuildError("vLLM package must own only execution.engine.v1")
    methods = manifest.get("methods")
    if not isinstance(methods, list) or not methods or any(
        not isinstance(method, dict)
        or method.get("interfaceVersion") != INTERFACE_VERSION
        or method.get("executionMode") != "service"
        for method in methods
    ):
        raise PackageBuildError("vLLM service methods must retain interface version 1")
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or (
        runtime.get("language") != "python"
        or runtime.get("entrypoint") != ENTRYPOINT
        or runtime.get("protocol") != "cyrene.serving.runtime.http.v1"
        or runtime.get("transport") != "http"
        or runtime.get("connectionRefScheme") != "cyrene-http-v1"
        or runtime.get("launch")
        != {"executable": "prepared-runtime", "args": ["-B", "package_service_adapter.py"]}
    ):
        raise PackageBuildError("vLLM package must use the HTTP lifecycle adapter launch")
    return PACKAGE_ID, PACKAGE_VERSION


def _package_entries(source_files: dict[str, bytes]) -> dict[str, bytes]:
    entries = {"LICENSE": source_files["LICENSE"], "requirements.lock": source_files[LOCK_SOURCE]}
    for path in EXPECTED_PACKAGE_PATHS:
        entries[path] = source_files[f"{PACKAGE_SOURCE}/{path}"]
    return entries


def _source_index(source_files: dict[str, bytes]) -> tuple[list[dict[str, str]], str]:
    inputs = [
        {"path": path, "sha256": sha256_bytes(content)}
        for path, content in sorted(source_files.items())
    ]
    canonical = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return inputs, sha256_bytes(canonical.encode("utf-8"))


def _platform_source_files(root: Path) -> dict[str, bytes]:
    """Read the exact Platform SDK and official wheel-builder source closure."""

    root = root.resolve(strict=True)
    fixed = (
        "repository-policy.yaml",
        "tooling/release/build_python_sdks.py",
        "sdk/python/cyrene_artifacts/README.md",
        "sdk/python/cyrene_artifacts/pyproject.toml",
    )
    candidates = {root / path for path in fixed}
    module_root = root / "sdk/python/cyrene_artifacts/src/cy_artifacts"
    if not module_root.is_dir():
        raise PackageBuildError("pinned Platform Artifact SDK source is missing")
    for path in module_root.rglob("*"):
        if path.is_symlink():
            raise PackageBuildError("Platform Artifact SDK source may not use symlinks")
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed"):
            candidates.add(path)
    files: dict[str, bytes] = {}
    for path in sorted(candidates):
        if path.is_symlink() or not path.is_file() or not path.resolve(strict=True).is_relative_to(root):
            raise PackageBuildError(f"required Platform source is unavailable: {path}")
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    try:
        project = tomllib.loads(
            files["sdk/python/cyrene_artifacts/pyproject.toml"].decode("utf-8")
        )
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise PackageBuildError("pinned Platform Artifact SDK project is invalid") from error
    if (project.get("project") or {}).get("name") != ARTIFACT_DISTRIBUTION or (
        project.get("project") or {}
    ).get("version") != ARTIFACT_VERSION:
        raise PackageBuildError("pinned Platform Artifact SDK name or version mismatch")
    return files


def _validate_wheel(path: Path, platform_files: dict[str, bytes]) -> None:
    """Require the exact pure-Python Platform wheel built from its pinned source."""

    if path.name != ARTIFACT_WHEEL or path.is_symlink() or not path.is_file():
        raise PackageBuildError(f"expected the official SDK wheel {ARTIFACT_WHEEL}")
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
            if len(metadata_names) != 1 or len(wheel_names) != 1:
                raise PackageBuildError("Artifact SDK wheel metadata is incomplete")
            metadata = email.message_from_bytes(archive.read(metadata_names[0]))
            wheel_text = archive.read(wheel_names[0]).decode("utf-8")
            wheel_sources = {
                name: archive.read(name)
                for name in names
                if name.startswith("cy_artifacts/")
                and name.endswith((".py", "/py.typed"))
            }
    except (OSError, UnicodeError, zipfile.BadZipFile) as error:
        raise PackageBuildError("Artifact SDK wheel is unreadable") from error
    if metadata.get("Name", "").lower().replace("_", "-") != ARTIFACT_DISTRIBUTION:
        raise PackageBuildError("Artifact SDK wheel distribution name mismatch")
    if metadata.get("Version") != ARTIFACT_VERSION or "Tag: py3-none-any" not in wheel_text.splitlines():
        raise PackageBuildError("Artifact SDK wheel version or ABI tag mismatch")
    expected_sources = {
        path.removeprefix("sdk/python/cyrene_artifacts/src/"): content
        for path, content in platform_files.items()
        if path.startswith("sdk/python/cyrene_artifacts/src/cy_artifacts/")
        and path.endswith((".py", "/py.typed"))
    }
    if wheel_sources != expected_sources:
        raise PackageBuildError("Artifact SDK wheel sources differ from the pinned Platform commit")
    if metadata.get_all("Requires-Dist"):
        raise PackageBuildError("Artifact SDK wheel unexpectedly declares external dependencies")


def _descriptor(
    *, source_sha: str, channel: str, archive_digest: str, content_digest: str, lock_digest: str
) -> dict[str, Any]:
    publication_status = "CANDIDATE" if channel == "candidate" else "PUBLISHED"
    artifact: dict[str, Any] = {"status": "NOT_PUBLISHED"}
    if publication_status == "PUBLISHED":
        tag = f"{channel}-{PACKAGE_ID}-{PACKAGE_VERSION}-{source_sha}"
        archive_name = f"{PACKAGE_ID}-{PACKAGE_VERSION}-{TARGET_ID}.zip"
        artifact = {
            "status": "PUBLISHED",
            "uri": f"{SOURCE_REPOSITORY_URL}/releases/download/{tag}/{archive_name}",
            "digest": content_digest,
            "format": "zip",
        }
    return {
        "record_type": "package_descriptor",
        "spec_version": "0.1",
        "package": {"manifest_ref": "plugin.manifest.json", "id": PACKAGE_ID, "version": PACKAGE_VERSION},
        "capability": {"id": CAPABILITY_ID, "interface_version": INTERFACE_VERSION},
        "implementation": {"entrypoint": ENTRYPOINT, "artifact": artifact},
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


def _release_metadata(
    *,
    source_sha: str,
    source_ref: str,
    channel: str,
    source_files: dict[str, bytes],
    descriptor_bytes: bytes,
    package_path: Path,
    lock_path: Path,
    wheel_path: Path,
    platform_files: dict[str, bytes],
) -> dict[str, Any]:
    source_inputs, input_tree_digest = _source_index(source_files)
    publication_status = "CANDIDATE" if channel == "candidate" else "PUBLISHED"
    release_tag = (
        None
        if channel == "candidate"
        else f"{channel}-{PACKAGE_ID}-{PACKAGE_VERSION}-{source_sha}"
    )
    package_uri = (
        None
        if release_tag is None
        else f"{SOURCE_REPOSITORY_URL}/releases/download/{release_tag}/{package_path.name}"
    )
    package_digest = sha256_file(package_path)
    lock_digest = sha256_file(lock_path)
    wheel_digest = sha256_file(wheel_path)
    descriptor_digest = sha256_bytes(descriptor_bytes)
    subjects = [
        {"name": path.name, "sha256": sha256_file(path)}
        for path in (package_path, lock_path, wheel_path)
    ]
    subjects.append(
        {
            "name": f"{PACKAGE_ID}-{PACKAGE_VERSION}-package-descriptor.json",
            "sha256": descriptor_digest,
        }
    )
    platform_inputs, platform_tree_digest = _source_index(platform_files)
    return {
        "record_type": "cyrene.plugin.package.release.v1",
        "spec_version": "1",
        "publication_status": publication_status,
        "channel": channel,
        "source": {
            "repository": SOURCE_REPOSITORY_URL,
            "commit": source_sha,
            "ref": source_ref,
            "input_tree_digest": input_tree_digest,
            "inputs": source_inputs,
        },
        "package": {
            "id": PACKAGE_ID,
            "version": PACKAGE_VERSION,
            "capability": CAPABILITY_ID,
            "interface_version": INTERFACE_VERSION,
            "entrypoint": ENTRYPOINT,
            "transport": "http",
            "connection_ref_scheme": "cyrene-http-v1",
            "metadata_endpoint": {"method": "GET", "path": "/metadata", "auth": "bearer"},
        },
        "target": {"id": TARGET_ID, "os": "linux", "architecture": "x86_64", "python": ">=3.11"},
        "runtime": {
            "protocol": "cyrene.serving.runtime.http.v1",
            "activation_supervisor": "cy-package-runtime::ProcessPluginServiceSupervisor",
            "readiness_envelope": "direct_plugin_ready (lifecycle handshake only)",
            "readiness_transport_claim": "HTTP; DirectPluginRuntime gRPC is not implemented",
            "launch_executable": "prepared-runtime",
            "launch_args": ["-B", "package_service_adapter.py"],
            "owner_configuration": [
                "CYRENE_VLLM_RUNTIME_HOME",
                "CYRENE_ARTIFACT_ROOT",
                "CYRENE_SERVING_CREDENTIAL_FILE",
                "CYRENE_SERVING_CONTROL_URL",
            ],
            "dependency_preparer": {
                "host_api": "cy-package-runtime::CommandDependencyPreparer",
                "package_lock": "requirements.lock",
                "offline_mode": True,
                "wheelhouse_asset": ARTIFACT_WHEEL,
            },
            "serving_inference_dependencies": "future; not included or accepted by this import-only package",
        },
        "external_dependencies": {
            "artifact_sdk": {
                "distribution": ARTIFACT_DISTRIBUTION,
                "version": ARTIFACT_VERSION,
                "source_repository": f"https://github.com/{PLATFORM_REPOSITORY}",
                "source_commit": PLATFORM_SOURCE_SHA,
                "source_tree_digest": platform_tree_digest,
                "source_inputs": platform_inputs,
                "build_producer": {
                    "path": "tooling/release/build_python_sdks.py",
                    "sha256": sha256_bytes(platform_files["tooling/release/build_python_sdks.py"]),
                },
                "builder_support_dependency": {
                    "pin": PLATFORM_BUILDER_DEPENDENCY,
                    "scope": "build-only; required by the official Platform builder import",
                },
                "wheel": ARTIFACT_WHEEL,
                "wheel_sha256": wheel_digest,
            }
        },
        "release_tag": release_tag,
        "artifact_uri": package_uri,
        "assets": {
            "package": {"name": package_path.name, "format": "zip", "sha256": package_digest},
            "descriptor": {
                "name": f"{PACKAGE_ID}-{PACKAGE_VERSION}-package-descriptor.json",
                "format": "json",
                "sha256": descriptor_digest,
            },
            "dependency_lock": {
                "name": lock_path.name,
                "format": "requirements.lock",
                "sha256": lock_digest,
                "package_ref": "requirements.lock",
            },
            "artifact_sdk_wheel": {
                "name": wheel_path.name,
                "package": ARTIFACT_DISTRIBUTION,
                "version": ARTIFACT_VERSION,
                "format": "wheel",
                "target": "py3-none-any",
                "sha256": wheel_digest,
            },
        },
        "attestation_policy": {
            "provider": "github-artifact-attestation",
            "workflow": f"{REPOSITORY}/{WORKFLOW_PATH}",
            "source_commit": source_sha,
            "subject_assets": subjects,
        },
    }


def _check_source(source_sha: str, source_ref: str, channel: str, target: str) -> None:
    if not SHA_PATTERN.fullmatch(source_sha):
        raise PackageBuildError("source commit must be a 40-character lower-case SHA")
    if not source_ref or "\n" in source_ref or "\r" in source_ref:
        raise PackageBuildError("source ref must be non-empty single-line text")
    if channel not in SUPPORTED_CHANNELS or target != TARGET_ID:
        raise PackageBuildError("release channel or target is unsupported")
    if (channel == "preview" and source_ref != "refs/heads/develop") or (
        channel == "stable" and source_ref not in {"refs/heads/main", "refs/heads/release"}
    ):
        raise PackageBuildError("release channel does not match the canonical source branch")


def build_release_artifacts(
    *,
    root: Path,
    output_dir: Path,
    source_sha: str,
    source_ref: str,
    channel: str,
    target: str,
    artifact_wheel: Path,
    platform_source_root: Path,
) -> dict[str, Path]:
    """Build a deterministic Plugin ZIP, Package Descriptor, lock, and release index."""

    _check_source(source_sha, source_ref, channel, target)
    root = root.resolve(strict=True)
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PackageBuildError(f"release output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    source_files = _source_files(root)
    platform_files = _platform_source_files(platform_source_root)
    _validate_wheel(artifact_wheel, platform_files)
    manifest = _read_json(root / f"{PACKAGE_SOURCE}/plugin.manifest.json")
    _, version = _manifest_contract(manifest)
    entries = _package_entries(source_files)
    package_path = output_dir / f"{PACKAGE_ID}-{version}-{target}.zip"
    descriptor_path = output_dir / f"{PACKAGE_ID}-{version}-package-descriptor.json"
    lock_path = output_dir / f"{PACKAGE_ID}-{version}-requirements.lock"
    wheel_path = output_dir / ARTIFACT_WHEEL
    release_path = output_dir / f"{PACKAGE_ID}-{version}-package-release.json"
    lock_path.write_bytes(source_files[LOCK_SOURCE])
    shutil.copyfile(artifact_wheel, wheel_path)

    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(entries.items()):
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

    content_digest = _canonical_digest(entries)
    archive_digest = sha256_file(package_path)
    lock_digest = sha256_bytes(source_files[LOCK_SOURCE])
    descriptor = _descriptor(
        source_sha=source_sha,
        channel=channel,
        archive_digest=archive_digest,
        content_digest=content_digest,
        lock_digest=lock_digest,
    )
    descriptor_bytes = _json_bytes(descriptor)
    descriptor_path.write_bytes(descriptor_bytes)
    metadata = _release_metadata(
        source_sha=source_sha,
        source_ref=source_ref,
        channel=channel,
        source_files=source_files,
        descriptor_bytes=descriptor_bytes,
        package_path=package_path,
        lock_path=lock_path,
        wheel_path=wheel_path,
        platform_files=platform_files,
    )
    release_path.write_bytes(_json_bytes(metadata))
    return {
        "package": package_path,
        "descriptor": descriptor_path,
        "dependency_lock": lock_path,
        "artifact_sdk_wheel": wheel_path,
        "release_metadata": release_path,
    }


def verify_release_artifacts(
    *,
    root: Path,
    output_dir: Path,
    source_sha: str,
    source_ref: str,
    channel: str,
    target: str,
    platform_source_root: Path,
) -> dict[str, Path]:
    """Verify package entries, Package Spec refs, source pins, and every asset digest."""

    _check_source(source_sha, source_ref, channel, target)
    root = root.resolve(strict=True)
    output_dir = output_dir.resolve(strict=True)
    source_files = _source_files(root)
    platform_files = _platform_source_files(platform_source_root)
    _validate_wheel(output_dir / ARTIFACT_WHEEL, platform_files)
    manifest = _read_json(root / f"{PACKAGE_SOURCE}/plugin.manifest.json")
    _, version = _manifest_contract(manifest)
    archive_name = f"{PACKAGE_ID}-{version}-{target}.zip"
    descriptor_name = f"{PACKAGE_ID}-{version}-package-descriptor.json"
    lock_name = f"{PACKAGE_ID}-{version}-requirements.lock"
    release_name = f"{PACKAGE_ID}-{version}-package-release.json"
    expected_names = {archive_name, descriptor_name, lock_name, release_name, ARTIFACT_WHEEL}
    output_entries = list(output_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in output_entries):
        raise PackageBuildError("release output may contain only regular files")
    if {path.name for path in output_entries} != expected_names:
        raise PackageBuildError("release output contains missing or unexpected assets")

    package_path = output_dir / archive_name
    descriptor_path = output_dir / descriptor_name
    lock_path = output_dir / lock_name
    wheel_path = output_dir / ARTIFACT_WHEEL
    release_path = output_dir / release_name
    descriptor = _read_json(descriptor_path)
    metadata = _read_json(release_path)
    entries: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(package_path) as archive:
            seen: set[str] = set()
            for item in archive.infolist():
                name = item.filename
                if (
                    not name
                    or name.startswith("/")
                    or "\\" in name
                    or ".." in PurePosixPath(name).parts
                    or name in seen
                    or item.is_dir()
                    or ((item.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise PackageBuildError(f"unsafe or duplicate package archive entry: {name}")
                seen.add(name)
                entries[name] = archive.read(item)
    except (OSError, zipfile.BadZipFile) as error:
        raise PackageBuildError("package archive is unreadable") from error
    if entries != _package_entries(source_files):
        raise PackageBuildError("package archive differs from its allowlisted source inputs")
    if lock_path.read_bytes() != source_files[LOCK_SOURCE]:
        raise PackageBuildError("published dependency lock differs from source lock")

    content_digest = _canonical_digest(entries)
    archive_digest = sha256_file(package_path)
    lock_digest = sha256_bytes(source_files[LOCK_SOURCE])
    publication_status = "CANDIDATE" if channel == "candidate" else "PUBLISHED"
    if descriptor != _descriptor(
        source_sha=source_sha,
        channel=channel,
        archive_digest=archive_digest,
        content_digest=content_digest,
        lock_digest=lock_digest,
    ):
        raise PackageBuildError("Package Descriptor identity or digest mismatch")
    if metadata.get("record_type") != "cyrene.plugin.package.release.v1":
        raise PackageBuildError("release metadata has an unsupported record type")
    if metadata.get("publication_status") != publication_status or metadata.get("channel") != channel:
        raise PackageBuildError("release metadata status or channel mismatch")
    source_inputs, input_tree_digest = _source_index(source_files)
    expected_tag = (
        None
        if channel == "candidate"
        else f"{channel}-{PACKAGE_ID}-{PACKAGE_VERSION}-{source_sha}"
    )
    expected_uri = (
        None
        if expected_tag is None
        else f"{SOURCE_REPOSITORY_URL}/releases/download/{expected_tag}/{archive_name}"
    )
    if metadata.get("source") != {
        "repository": SOURCE_REPOSITORY_URL,
        "commit": source_sha,
        "ref": source_ref,
        "input_tree_digest": input_tree_digest,
        "inputs": source_inputs,
    }:
        raise PackageBuildError("release metadata source or input-tree digest mismatch")
    if metadata.get("release_tag") != expected_tag or metadata.get("artifact_uri") != expected_uri:
        raise PackageBuildError("release tag or immutable artifact URI mismatch")
    sdk_wheel_digest = sha256_file(wheel_path)
    platform_inputs, platform_tree_digest = _source_index(platform_files)
    expected_dependencies = {
        "artifact_sdk": {
            "distribution": ARTIFACT_DISTRIBUTION,
            "version": ARTIFACT_VERSION,
            "source_repository": f"https://github.com/{PLATFORM_REPOSITORY}",
            "source_commit": PLATFORM_SOURCE_SHA,
            "source_tree_digest": platform_tree_digest,
            "source_inputs": platform_inputs,
            "build_producer": {
                "path": "tooling/release/build_python_sdks.py",
                "sha256": sha256_bytes(platform_files["tooling/release/build_python_sdks.py"]),
            },
            "builder_support_dependency": {
                "pin": PLATFORM_BUILDER_DEPENDENCY,
                "scope": "build-only; required by the official Platform builder import",
            },
            "wheel": ARTIFACT_WHEEL,
            "wheel_sha256": sdk_wheel_digest,
        }
    }
    if metadata.get("external_dependencies") != expected_dependencies:
        raise PackageBuildError("external Artifact SDK source or wheel digest mismatch")
    expected_package_metadata = {
        "id": PACKAGE_ID,
        "version": PACKAGE_VERSION,
        "capability": CAPABILITY_ID,
        "interface_version": INTERFACE_VERSION,
        "entrypoint": ENTRYPOINT,
        "transport": "http",
        "connection_ref_scheme": "cyrene-http-v1",
        "metadata_endpoint": {"method": "GET", "path": "/metadata", "auth": "bearer"},
    }
    if metadata.get("package") != expected_package_metadata:
        raise PackageBuildError("release package transport or capability metadata mismatch")
    expected_assets = {
        "package": {"name": archive_name, "format": "zip", "sha256": archive_digest},
        "descriptor": {"name": descriptor_name, "format": "json", "sha256": sha256_file(descriptor_path)},
        "dependency_lock": {
            "name": lock_name,
            "format": "requirements.lock",
            "sha256": lock_digest,
            "package_ref": "requirements.lock",
        },
        "artifact_sdk_wheel": {
            "name": ARTIFACT_WHEEL,
            "package": ARTIFACT_DISTRIBUTION,
            "version": ARTIFACT_VERSION,
            "format": "wheel",
            "target": "py3-none-any",
            "sha256": sdk_wheel_digest,
        },
    }
    if metadata.get("assets") != expected_assets:
        raise PackageBuildError("release asset names or digests mismatch")
    expected_runtime = {
        "protocol": "cyrene.serving.runtime.http.v1",
        "activation_supervisor": "cy-package-runtime::ProcessPluginServiceSupervisor",
        "readiness_envelope": "direct_plugin_ready (lifecycle handshake only)",
        "readiness_transport_claim": "HTTP; DirectPluginRuntime gRPC is not implemented",
        "launch_executable": "prepared-runtime",
        "launch_args": ["-B", "package_service_adapter.py"],
        "owner_configuration": [
            "CYRENE_VLLM_RUNTIME_HOME",
            "CYRENE_ARTIFACT_ROOT",
            "CYRENE_SERVING_CREDENTIAL_FILE",
            "CYRENE_SERVING_CONTROL_URL",
        ],
        "dependency_preparer": {
            "host_api": "cy-package-runtime::CommandDependencyPreparer",
            "package_lock": "requirements.lock",
            "offline_mode": True,
            "wheelhouse_asset": ARTIFACT_WHEEL,
        },
        "serving_inference_dependencies": "future; not included or accepted by this import-only package",
    }
    if metadata.get("runtime") != expected_runtime:
        raise PackageBuildError("runtime lifecycle or transport metadata mismatch")
    if metadata.get("target") != {
        "id": TARGET_ID,
        "os": "linux",
        "architecture": "x86_64",
        "python": ">=3.11",
    }:
        raise PackageBuildError("release target mismatch")
    if metadata.get("attestation_policy", {}).get("source_commit") != source_sha:
        raise PackageBuildError("attestation source commit mismatch")
    expected_subjects = [
        {"name": path.name, "sha256": sha256_file(path)}
        for path in (package_path, lock_path, wheel_path)
    ]
    expected_subjects.append({"name": descriptor_name, "sha256": sha256_file(descriptor_path)})
    expected_policy = {
        "provider": "github-artifact-attestation",
        "workflow": f"{REPOSITORY}/{WORKFLOW_PATH}",
        "source_commit": source_sha,
        "subject_assets": expected_subjects,
    }
    if metadata.get("attestation_policy") != expected_policy:
        raise PackageBuildError("attestation subject identities mismatch")

    return {
        "package": package_path,
        "descriptor": descriptor_path,
        "dependency_lock": lock_path,
        "artifact_sdk_wheel": wheel_path,
        "release_metadata": release_path,
    }


def _write_subject_checksums(artifacts: dict[str, Path], output_dir: Path) -> Path:
    """Write the checksums input consumed by GitHub artifact attestations."""

    subject_path = output_dir / "attestation-subjects.sha256"
    subjects = [artifacts[name] for name in ("package", "descriptor", "dependency_lock", "artifact_sdk_wheel", "release_metadata")]
    subject_path.write_text(
        "".join(f"{sha256_file(path).removeprefix('sha256:')}  {path.name}\n" for path in subjects),
        encoding="utf-8",
    )
    return subject_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("build", "verify"):
        command = subparsers.add_parser(action)
        command.add_argument("--output-dir", type=Path, required=True)
        command.add_argument("--source-ref", required=True)
        command.add_argument("--channel", choices=sorted(SUPPORTED_CHANNELS), required=True)
        command.add_argument("--target", required=True)
        command.add_argument("--github-sha", required=True)
        command.add_argument("--platform-source-root", type=Path, required=True)
        if action == "build":
            command.add_argument("--artifact-wheel", type=Path, required=True)
        else:
            command.add_argument("--subject-checksums", action="store_true")
    return parser


def main() -> int:
    options = _parser().parse_args()
    try:
        if options.action == "build":
            artifacts = build_release_artifacts(
                root=Path(__file__).resolve().parents[2],
                output_dir=options.output_dir,
                source_sha=options.github_sha,
                source_ref=options.source_ref,
                channel=options.channel,
                target=options.target,
                artifact_wheel=options.artifact_wheel,
                platform_source_root=options.platform_source_root,
            )
        else:
            artifacts = verify_release_artifacts(
                root=Path(__file__).resolve().parents[2],
                output_dir=options.output_dir,
                source_sha=options.github_sha,
                source_ref=options.source_ref,
                channel=options.channel,
                target=options.target,
                platform_source_root=options.platform_source_root,
            )
            if options.subject_checksums:
                _write_subject_checksums(artifacts, options.output_dir.resolve(strict=True))
    except (OSError, PackageBuildError, subprocess.SubprocessError) as error:
        print(f"vllm-runtime-package: {error}", file=sys.stderr)
        return 1
    for kind, path in artifacts.items():
        print(f"{kind}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
