"""
Build and verify the immutable training.llama-factory.v1 package assets.

This producer keeps package contents, Package Spec digests, source identity,
and the Python dependency-preparer wheel tied to one Plugins commit.
中文：将 package 内容、Package Spec 摘要、源码身份和 Python 依赖准备器 wheel
绑定到同一个 Plugins commit。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import tomllib

REPOSITORY = "DoHorizon-AI/Cyrene-Plugins-Official"
SOURCE_REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
WORKFLOW_PATH = ".github/workflows/training-plugin-package-release.yml"
PRODUCER_PATH = "tooling/release/training_llama_factory_package.py"
PACKAGE_SOURCE = "plugins/training/llama-factory"
RUNTIME_SOURCE = "sdk/python/cyrene_plugin_runtime/src/cyrene_plugin_runtime"
RUNTIME_PROJECT = "sdk/python/cyrene_plugin_runtime"
LOCK_SOURCE = "tooling/release/training-llama-factory-requirements.lock"
BUILD_LOCK_SOURCE = "tooling/release/build-requirements.lock"
PACKAGE_ID = "cyrene.training.llama-factory"
CAPABILITY_ID = "training.llama-factory.v1"
INTERFACE_VERSION = "1"
RUNTIME_DISTRIBUTION = "cyrene-plugin-runtime"
RUNTIME_VERSION = "0.2.0"
RUNTIME_WHEEL_NAME = "cyrene_plugin_runtime-0.2.0-py3-none-any.whl"
TARGET_ID = "linux-x86_64"
SUPPORTED_CHANNELS = {"candidate", "preview", "stable"}
EXPECTED_LOCK_LINES = ("grpcio==1.62.3", "protobuf==4.25.9")
EXPECTED_BUILD_LOCK_LINES = (
    "build==1.6.1",
    "packaging==26.3",
    "pyproject-hooks==1.3.3",
    "setuptools==68.1.2",
    "wheel==0.48.0",
)
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
LOCK_PIN_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+==[A-Za-z0-9][A-Za-z0-9_.+!-]*$")


class PackageBuildError(ValueError):
    """Raised when package inputs or release outputs violate the frozen contract.

    中文：package 输入或发布产物违反固定合约时抛出。
    """


def sha256_bytes(payload: bytes) -> str:
    """Return a Package Spec-compatible lower-case SHA-256 digest."""

    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def sha256_file(path: Path) -> str:
    """Hash one file without loading the complete payload into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PackageBuildError(f"cannot read JSON metadata {path}: {error}") from error
    if not isinstance(value, dict):
        raise PackageBuildError(f"JSON metadata must be an object: {path}")
    return value


def _source_files(root: Path) -> dict[str, bytes]:
    """Return the complete, allowlisted source input set for this package.

    Args:
        root: Plugins repository root.

    Returns:
        Repository-relative source paths mapped to their exact bytes.
    """

    root = root.resolve(strict=True)
    fixed_paths = (
        "LICENSE",
        f"{PACKAGE_SOURCE}/README.md",
        f"{PACKAGE_SOURCE}/PROVENANCE.md",
        f"{PACKAGE_SOURCE}/plugin.manifest.json",
        f"{PACKAGE_SOURCE}/llama_factory.py",
        f"{PACKAGE_SOURCE}/contracts/v1/schema.json",
        f"{RUNTIME_PROJECT}/pyproject.toml",
        PRODUCER_PATH,
        LOCK_SOURCE,
        BUILD_LOCK_SOURCE,
    )
    candidates = {root / relative for relative in fixed_paths}
    runtime_root = root / RUNTIME_SOURCE
    if not runtime_root.is_dir():
        raise PackageBuildError(
            f"Python runtime source directory is missing: {runtime_root}"
        )
    for path in runtime_root.rglob("*"):
        if path.is_symlink():
            raise PackageBuildError(
                f"runtime source may not be a symbolic link: {path}"
            )
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed"):
            candidates.add(path)

    files: dict[str, bytes] = {}
    for path in sorted(candidates):
        if path.is_symlink():
            raise PackageBuildError(
                f"package source may not be a symbolic link: {path}"
            )
        if not path.is_file():
            raise PackageBuildError(f"required package source is missing: {path}")
        if not path.resolve(strict=True).is_relative_to(root):
            raise PackageBuildError(
                f"package source resolves outside the repository: {path}"
            )
        relative = path.relative_to(root).as_posix()
        try:
            files[relative] = path.read_bytes()
        except OSError as error:
            raise PackageBuildError(
                f"cannot read package source {path}: {error}"
            ) from error

    lock = files[LOCK_SOURCE].decode("utf-8")
    lock_lines = tuple(
        line.strip()
        for line in lock.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if lock_lines != EXPECTED_LOCK_LINES or any(
        not LOCK_PIN_PATTERN.fullmatch(line) for line in lock_lines
    ):
        raise PackageBuildError(
            "runtime dependency lock must retain the exact approved pins"
        )
    return files


def _package_entries(source_files: dict[str, bytes]) -> dict[str, bytes]:
    """Project allowlisted repository inputs into the installed package layout."""

    entries: dict[str, bytes] = {"LICENSE": source_files["LICENSE"]}
    projected = {
        f"{PACKAGE_SOURCE}/README.md": "README.md",
        f"{PACKAGE_SOURCE}/PROVENANCE.md": "PROVENANCE.md",
        f"{PACKAGE_SOURCE}/plugin.manifest.json": "plugin.manifest.json",
        f"{PACKAGE_SOURCE}/llama_factory.py": "llama_factory.py",
        f"{PACKAGE_SOURCE}/contracts/v1/schema.json": "contracts/v1/schema.json",
        LOCK_SOURCE: "requirements.lock",
    }
    for source, package_path in projected.items():
        entries[package_path] = source_files[source]
    runtime_prefix = f"{RUNTIME_SOURCE}/"
    for source, content in source_files.items():
        if source.startswith(runtime_prefix):
            relative = source[len(runtime_prefix) :]
            entries[f"src/cyrene_plugin_runtime/{relative}"] = content
    return entries


def _validate_manifest(manifest: dict[str, Any]) -> tuple[str, str]:
    """Fail closed unless the input manifest is the frozen Python capability."""

    package_id = manifest.get("id")
    version = manifest.get("version")
    if package_id != PACKAGE_ID or not isinstance(version, str) or not version.strip():
        raise PackageBuildError(
            "training Plugin manifest identity is not the expected package"
        )
    if manifest.get("capabilities") != [CAPABILITY_ID]:
        raise PackageBuildError(
            "training Plugin must own only training.llama-factory.v1 here"
        )
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        raise PackageBuildError("training Plugin manifest runtime is missing")
    entrypoint = runtime.get("entrypoint")
    if (
        runtime.get("language") != "python"
        or runtime.get("protocol") != "cyrene.plugin.runtime.v1.DirectPluginRuntime"
        or entrypoint != "llama_factory:LlamaFactoryTrainingPlugin"
    ):
        raise PackageBuildError(
            "training Plugin runtime identity differs from DirectPluginRuntime"
        )
    launch = runtime.get("launch")
    if not isinstance(launch, dict) or launch.get("executable") != "prepared-runtime":
        raise PackageBuildError(
            "training Plugin must use the existing prepared-runtime launch"
        )
    if launch.get("args") != [
        "-B",
        "src/cyrene_plugin_runtime/bootstrap.py",
        "--entrypoint",
        entrypoint,
    ]:
        raise PackageBuildError(
            "training Plugin launch arguments differ from the packaged SDK layout"
        )
    methods = manifest.get("methods")
    if (
        not isinstance(methods, list)
        or not methods
        or any(
            not isinstance(method, dict)
            or method.get("interfaceVersion") != INTERFACE_VERSION
            for method in methods
        )
    ):
        raise PackageBuildError(
            "training Plugin methods must retain interface version 1"
        )
    return package_id, version


def _package_content_digest(entries: dict[str, bytes]) -> str:
    """Match cy-package-runtime's length-prefixed sorted-entry digest algorithm."""

    digest = hashlib.sha256()
    for name, content in sorted(entries.items()):
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _source_index(source_files: dict[str, bytes]) -> tuple[list[dict[str, str]], str]:
    records = [
        {"path": path, "sha256": sha256_bytes(content)}
        for path, content in sorted(source_files.items())
    ]
    canonical = json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return records, sha256_bytes(canonical.encode("utf-8"))


def _validate_preparer_wheel(path: Path) -> None:
    """Ensure the companion SDK wheel exposes the configured preparer command."""

    if path.name != RUNTIME_WHEEL_NAME or not path.is_file() or path.is_symlink():
        raise PackageBuildError(
            f"expected the official preparer wheel {RUNTIME_WHEEL_NAME}"
        )
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            metadata_names = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
            entrypoint_names = [
                name for name in names if name.endswith(".dist-info/entry_points.txt")
            ]
            if (
                len(metadata_names) != 1
                or len(wheel_names) != 1
                or len(entrypoint_names) != 1
            ):
                raise PackageBuildError("preparer wheel has invalid dist-info metadata")
            metadata = archive.read(metadata_names[0]).decode("utf-8")
            wheel = archive.read(wheel_names[0]).decode("utf-8")
            entrypoints = archive.read(entrypoint_names[0]).decode("utf-8")
            if (
                "Name: cyrene-plugin-runtime\n" not in metadata
                or "Version: 0.2.0\n" not in metadata
            ):
                raise PackageBuildError(
                    "preparer wheel package identity is not cyrene-plugin-runtime 0.2.0"
                )
            if "Tag: py3-none-any\n" not in wheel:
                raise PackageBuildError("preparer wheel target must be py3-none-any")
            if (
                "cyrene-plugin-python-preparer = "
                "cyrene_plugin_runtime.dependency_preparer:main" not in entrypoints
            ):
                raise PackageBuildError(
                    "preparer wheel is missing cyrene-plugin-python-preparer"
                )
            if "cyrene_plugin_runtime/dependency_preparer.py" not in names:
                raise PackageBuildError(
                    "preparer wheel is missing its command implementation"
                )
    except (OSError, UnicodeError, zipfile.BadZipFile) as error:
        raise PackageBuildError(
            f"cannot read preparer wheel {path}: {error}"
        ) from error


def build_runtime_wheel(
    root: Path, source_sha: str, python: str = sys.executable
) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    """Build the SDK wheel reproducibly with the repository's pinned build lock.

    Args:
        root: Plugins repository root.
        source_sha: Checked-out commit that supplies all wheel inputs.
        python: Python interpreter whose pinned build frontend is installed.

    Returns:
        A validated wheel path and its temporary directory owner.
    """

    if not SHA_PATTERN.fullmatch(source_sha):
        raise PackageBuildError("source commit must be a 40-character lower-case SHA")
    root = root.resolve(strict=True)
    runtime_project = root / RUNTIME_PROJECT
    try:
        project = tomllib.loads(
            (runtime_project / "pyproject.toml").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise PackageBuildError(
            f"cannot read Python runtime project: {error}"
        ) from error
    metadata = project.get("project", {})
    if (
        metadata.get("name") != RUNTIME_DISTRIBUTION
        or metadata.get("version") != RUNTIME_VERSION
    ):
        raise PackageBuildError(
            "Python runtime project identity differs from the pinned preparer asset"
        )

    build_lock = root / BUILD_LOCK_SOURCE
    build_lines = tuple(
        line.strip()
        for line in build_lock.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if build_lines != EXPECTED_BUILD_LOCK_LINES or any(
        not LOCK_PIN_PATTERN.fullmatch(line) for line in build_lines
    ):
        raise PackageBuildError("wheel build lock must retain the exact approved pins")

    epoch_result = subprocess.run(
        ["git", "show", "-s", "--format=%ct", source_sha],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if epoch_result.returncode != 0 or not epoch_result.stdout.strip().isdigit():
        raise PackageBuildError(
            "could not resolve the source commit timestamp for wheel reproducibility"
        )

    temporary = tempfile.TemporaryDirectory(prefix="cyrene-training-runtime-wheel-")
    output = Path(temporary.name)
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = epoch_result.stdout.strip()
    result = subprocess.run(
        [
            python,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output),
            str(runtime_project),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        temporary.cleanup()
        raise PackageBuildError(
            f"could not build pinned Python runtime wheel: {result.stderr.strip() or result.stdout.strip()}"
        )
    wheel = output / RUNTIME_WHEEL_NAME
    try:
        _validate_preparer_wheel(wheel)
    except PackageBuildError:
        temporary.cleanup()
        raise
    return wheel, temporary


def _package_descriptor(
    *,
    package_id: str,
    version: str,
    entrypoint: str,
    channel: str,
    source_sha: str,
    archive_digest: str,
    content_digest: str,
    lock_digest: str,
) -> dict[str, Any]:
    publication_status = "CANDIDATE" if channel == "candidate" else "PUBLISHED"
    descriptor: dict[str, Any] = {
        "record_type": "package_descriptor",
        "spec_version": "0.1",
        "package": {
            "manifest_ref": "plugin.manifest.json",
            "id": package_id,
            "version": version,
        },
        "capability": {"id": CAPABILITY_ID, "interface_version": INTERFACE_VERSION},
        "implementation": {"entrypoint": entrypoint},
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
    artifact: dict[str, Any] = {"status": "NOT_PUBLISHED"}
    if publication_status == "PUBLISHED":
        release_tag = f"{channel}-{package_id}-{version}-{source_sha}"
        archive_name = f"{package_id}-{version}-{TARGET_ID}.zip"
        artifact = {
            "status": "PUBLISHED",
            "uri": f"{SOURCE_REPOSITORY_URL}/releases/download/{release_tag}/{archive_name}",
            "digest": content_digest,
            "format": "zip",
        }
    descriptor["implementation"]["artifact"] = artifact
    return descriptor


def build_release_artifacts(
    *,
    root: Path,
    output_dir: Path,
    source_sha: str,
    source_ref: str,
    channel: str,
    target: str,
    preparer_wheel: Path,
) -> dict[str, Path]:
    """Build one LLaMA Factory package, generic descriptor, lock, and release index.

    Args:
        root: Plugins repository root.
        output_dir: Empty output directory for release assets.
        source_sha: Exact source commit from the checked-out repository.
        source_ref: Source ref passed by GitHub Actions.
        channel: ``candidate``, ``preview``, or ``stable``.
        target: Supported deployment target; only ``linux-x86_64`` is accepted.
        preparer_wheel: Prebuilt ``cyrene-plugin-runtime`` SDK wheel.

    Returns:
        Paths for every producer-created file.
    """

    if not SHA_PATTERN.fullmatch(source_sha):
        raise PackageBuildError("source commit must be a 40-character lower-case SHA")
    if not source_ref or "\n" in source_ref or "\r" in source_ref:
        raise PackageBuildError("source ref must be non-empty single-line text")
    if channel not in SUPPORTED_CHANNELS:
        raise PackageBuildError(f"unsupported release channel: {channel}")
    if (channel == "preview" and source_ref != "refs/heads/develop") or (
        channel == "stable"
        and source_ref not in {"refs/heads/main", "refs/heads/release"}
    ):
        raise PackageBuildError(
            "release channel does not match the canonical source branch"
        )
    if target != TARGET_ID:
        raise PackageBuildError(f"unsupported deployment target: {target}")
    _validate_preparer_wheel(preparer_wheel)

    root = root.resolve(strict=True)
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PackageBuildError(f"release output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    source_files = _source_files(root)
    manifest = _read_json(root / f"{PACKAGE_SOURCE}/plugin.manifest.json")
    package_id, version = _validate_manifest(manifest)
    runtime = manifest["runtime"]
    entrypoint = runtime["entrypoint"]
    package_entries = _package_entries(source_files)
    archive_name = f"{package_id}-{version}-{target}.zip"
    descriptor_name = f"{package_id}-{version}-package-descriptor.json"
    lock_name = f"{package_id}-{version}-requirements.lock"
    release_name = f"{package_id}-{version}-package-release.json"
    wheel_path = output_dir / preparer_wheel.name
    lock_path = output_dir / lock_name
    archive_path = output_dir / archive_name
    descriptor_path = output_dir / descriptor_name
    release_path = output_dir / release_name

    lock_bytes = source_files[LOCK_SOURCE]
    lock_path.write_bytes(lock_bytes)
    shutil.copyfile(preparer_wheel, wheel_path)

    with zipfile.ZipFile(
        archive_path, mode="w", compression=zipfile.ZIP_STORED
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

    content_digest = _package_content_digest(package_entries)
    archive_digest = sha256_file(archive_path)
    lock_digest = sha256_bytes(lock_bytes)
    descriptor = _package_descriptor(
        package_id=package_id,
        version=version,
        entrypoint=entrypoint,
        channel=channel,
        source_sha=source_sha,
        archive_digest=archive_digest,
        content_digest=content_digest,
        lock_digest=lock_digest,
    )
    descriptor_bytes = _json_bytes(descriptor)
    descriptor_path.write_bytes(descriptor_bytes)

    source_records, source_tree_digest = _source_index(source_files)
    publication_status = "CANDIDATE" if channel == "candidate" else "PUBLISHED"
    release_tag = (
        None
        if channel == "candidate"
        else f"{channel}-{package_id}-{version}-{source_sha}"
    )
    package_uri = descriptor["implementation"]["artifact"].get("uri")
    wheel_digest = sha256_file(wheel_path)
    package_attestation_subjects = [
        {"name": archive_name, "sha256": archive_digest},
        {"name": descriptor_name, "sha256": sha256_bytes(descriptor_bytes)},
        {"name": lock_name, "sha256": lock_digest},
        {"name": wheel_path.name, "sha256": wheel_digest},
    ]
    release_metadata = {
        "record_type": "cyrene.plugin.package.release.v1",
        "spec_version": "1",
        "publication_status": publication_status,
        "channel": channel,
        "source": {
            "repository": SOURCE_REPOSITORY_URL,
            "commit": source_sha,
            "ref": source_ref,
            "input_tree_digest": source_tree_digest,
            "inputs": source_records,
        },
        "package": {
            "id": package_id,
            "version": version,
            "capability": CAPABILITY_ID,
            "interface_version": INTERFACE_VERSION,
            "entrypoint": entrypoint,
        },
        "target": {
            "id": target,
            "os": "linux",
            "architecture": "x86_64",
            "python": ">=3.11",
        },
        "runtime": {
            "protocol": "cyrene.plugin.runtime.v1.DirectPluginRuntime",
            "launch_executable": "prepared-runtime",
            "preparer_command": "cyrene-plugin-python-preparer",
            "preparer_package": f"{RUNTIME_DISTRIBUTION}=={RUNTIME_VERSION}",
            "preparer_wheel_install_command": [
                "<python >=3.11 executable>",
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
                    "<python >=3.11 executable>",
                ],
                "evidence_protocol": "cyrene.package-dependency-preparer.v1",
                "runtime_executable_is_consumed_by": "ProcessPluginServiceSupervisor",
            },
        },
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
                "name": descriptor_name,
                "format": "json",
                "sha256": sha256_bytes(descriptor_bytes),
            },
            "dependency_lock": {
                "name": lock_name,
                "format": "requirements.lock",
                "sha256": lock_digest,
                "package_ref": "requirements.lock",
            },
            "preparer_wheel": {
                "name": wheel_path.name,
                "package": RUNTIME_DISTRIBUTION,
                "version": RUNTIME_VERSION,
                "format": "wheel",
                "target": "py3-none-any",
                "entrypoint": "cyrene-plugin-python-preparer",
                "sha256": wheel_digest,
            },
        },
        "attestation_policy": {
            "provider": "github-artifact-attestation",
            "workflow": f"{REPOSITORY}/{WORKFLOW_PATH}",
            "source_commit": source_sha,
            "subject_assets": package_attestation_subjects,
        },
    }
    release_path.write_bytes(_json_bytes(release_metadata))

    return {
        "package": archive_path,
        "descriptor": descriptor_path,
        "dependency_lock": lock_path,
        "preparer_wheel": wheel_path,
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
) -> dict[str, Path]:
    """Verify an output bundle against current source, descriptor, and digest records."""

    root = root.resolve(strict=True)
    output_dir = output_dir.resolve(strict=True)
    expected_files = _source_files(root)
    manifest = _read_json(root / f"{PACKAGE_SOURCE}/plugin.manifest.json")
    package_id, version = _validate_manifest(manifest)
    if not SHA_PATTERN.fullmatch(source_sha):
        raise PackageBuildError("source commit must be a 40-character lower-case SHA")
    if channel not in SUPPORTED_CHANNELS or target != TARGET_ID:
        raise PackageBuildError("release channel or deployment target is unsupported")
    if (channel == "preview" and source_ref != "refs/heads/develop") or (
        channel == "stable"
        and source_ref not in {"refs/heads/main", "refs/heads/release"}
    ):
        raise PackageBuildError(
            "release channel does not match the canonical source branch"
        )

    archive_name = f"{package_id}-{version}-{target}.zip"
    descriptor_name = f"{package_id}-{version}-package-descriptor.json"
    lock_name = f"{package_id}-{version}-requirements.lock"
    release_name = f"{package_id}-{version}-package-release.json"
    expected_names = {
        archive_name,
        descriptor_name,
        lock_name,
        release_name,
        RUNTIME_WHEEL_NAME,
    }
    output_entries = list(output_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in output_entries):
        raise PackageBuildError("release output may contain only regular files")
    actual_names = {path.name for path in output_entries}
    if actual_names != expected_names:
        raise PackageBuildError("release output contains missing or unexpected assets")

    archive_path = output_dir / archive_name
    descriptor_path = output_dir / descriptor_name
    lock_path = output_dir / lock_name
    wheel_path = output_dir / RUNTIME_WHEEL_NAME
    release_path = output_dir / release_name
    descriptor = _read_json(descriptor_path)
    release = _read_json(release_path)
    if descriptor.get("package") != {
        "manifest_ref": "plugin.manifest.json",
        "id": package_id,
        "version": version,
    }:
        raise PackageBuildError(
            "Package Spec identity differs from plugin.manifest.json"
        )
    if descriptor.get("capability") != {
        "id": CAPABILITY_ID,
        "interface_version": INTERFACE_VERSION,
    }:
        raise PackageBuildError(
            "Package Spec capability differs from plugin.manifest.json"
        )
    if descriptor.get("provenance", {}).get("source_revision") != source_sha:
        raise PackageBuildError("Package Spec provenance source commit mismatch")
    if descriptor.get("publication_status") != (
        "CANDIDATE" if channel == "candidate" else "PUBLISHED"
    ):
        raise PackageBuildError(
            "Package Spec publication status does not match channel"
        )

    package_entries: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            seen: set[str] = set()
            for info in archive.infolist():
                name = info.filename
                if (
                    not name
                    or name.startswith("/")
                    or "\\" in name
                    or ".." in PurePosixPath(name).parts
                    or name in seen
                    or info.is_dir()
                    or (info.external_attr >> 16) & 0o170000 == 0o120000
                ):
                    raise PackageBuildError(
                        f"unsafe or duplicate package archive entry: {name}"
                    )
                seen.add(name)
                package_entries[name] = archive.read(info)
    except (OSError, zipfile.BadZipFile) as error:
        raise PackageBuildError(
            f"cannot read package archive {archive_path}: {error}"
        ) from error
    if package_entries != _package_entries(expected_files):
        raise PackageBuildError(
            "package archive entries do not match the allowlisted source inputs"
        )
    if lock_path.read_bytes() != expected_files[LOCK_SOURCE]:
        raise PackageBuildError("published lock asset differs from source lock")

    content_digest = _package_content_digest(package_entries)
    archive_digest = sha256_file(archive_path)
    lock_digest = sha256_bytes(expected_files[LOCK_SOURCE])
    if descriptor.get("dependencies", {}).get("lock") != {
        "status": "LOCKED",
        "format": "requirements.lock",
        "ref": "requirements.lock",
        "digest": lock_digest,
    }:
        raise PackageBuildError("Package Spec lock reference or digest mismatch")
    if (
        descriptor.get("integrity", {}).get("artifact_digest") != content_digest
        or descriptor.get("integrity", {}).get("archive_digest") != archive_digest
    ):
        raise PackageBuildError("Package Spec archive or content digest mismatch")
    implementation = descriptor.get("implementation", {})
    expected_entrypoint = manifest["runtime"]["entrypoint"]
    if implementation.get("entrypoint") != expected_entrypoint:
        raise PackageBuildError(
            "Package Spec entrypoint differs from plugin.manifest.json"
        )
    artifact = implementation.get("artifact")
    if not isinstance(artifact, dict):
        raise PackageBuildError("Package Spec implementation artifact is missing")
    if channel == "candidate":
        if artifact != {"status": "NOT_PUBLISHED"}:
            raise PackageBuildError(
                "candidate Package Spec must not claim a published URI"
            )
        expected_tag = None
    else:
        expected_tag = f"{channel}-{package_id}-{version}-{source_sha}"
        expected_uri = (
            f"{SOURCE_REPOSITORY_URL}/releases/download/{expected_tag}/{archive_name}"
        )
        if artifact != {
            "status": "PUBLISHED",
            "uri": expected_uri,
            "digest": content_digest,
            "format": "zip",
        }:
            raise PackageBuildError(
                "published Package Spec artifact does not match its immutable URI"
            )

    source_records, source_tree_digest = _source_index(expected_files)
    if release.get("record_type") != "cyrene.plugin.package.release.v1":
        raise PackageBuildError("package release metadata has an unknown record type")
    if release.get("publication_status") != descriptor.get("publication_status"):
        raise PackageBuildError("release metadata and Package Spec status differ")
    if release.get("channel") != channel or release.get("release_tag") != expected_tag:
        raise PackageBuildError(
            "release metadata channel or tag differs from source identity"
        )
    if release.get("source") != {
        "repository": SOURCE_REPOSITORY_URL,
        "commit": source_sha,
        "ref": source_ref,
        "input_tree_digest": source_tree_digest,
        "inputs": source_records,
    }:
        raise PackageBuildError(
            "release metadata source identity or input digest mismatch"
        )
    assets = release.get("assets", {})
    expected_assets = {
        "package": {
            "name": archive_name,
            "format": "zip",
            "sha256": archive_digest,
            "content_sha256": content_digest,
        },
        "descriptor": {
            "name": descriptor_path.name,
            "format": "json",
            "sha256": sha256_file(descriptor_path),
        },
        "dependency_lock": {
            "name": lock_name,
            "format": "requirements.lock",
            "sha256": lock_digest,
            "package_ref": "requirements.lock",
        },
        "preparer_wheel": {
            "name": wheel_path.name,
            "package": RUNTIME_DISTRIBUTION,
            "version": RUNTIME_VERSION,
            "format": "wheel",
            "target": "py3-none-any",
            "entrypoint": "cyrene-plugin-python-preparer",
            "sha256": sha256_file(wheel_path),
        },
    }
    if assets != expected_assets:
        raise PackageBuildError("package release asset identities or digests mismatch")
    if release.get("target") != {
        "id": TARGET_ID,
        "os": "linux",
        "architecture": "x86_64",
        "python": ">=3.11",
    }:
        raise PackageBuildError(
            "package release deployment target differs from the supported target"
        )
    if release.get("runtime") != {
        "protocol": "cyrene.plugin.runtime.v1.DirectPluginRuntime",
        "launch_executable": "prepared-runtime",
        "preparer_command": "cyrene-plugin-python-preparer",
        "preparer_package": f"{RUNTIME_DISTRIBUTION}=={RUNTIME_VERSION}",
        "preparer_wheel_install_command": [
            "<python >=3.11 executable>",
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
                "<python >=3.11 executable>",
            ],
            "evidence_protocol": "cyrene.package-dependency-preparer.v1",
            "runtime_executable_is_consumed_by": "ProcessPluginServiceSupervisor",
        },
    }:
        raise PackageBuildError(
            "package runtime or dependency-preparer configuration metadata mismatch"
        )
    policy = release.get("attestation_policy", {})
    expected_subjects = [
        {"name": archive_name, "sha256": archive_digest},
        {"name": descriptor_path.name, "sha256": sha256_file(descriptor_path)},
        {"name": lock_name, "sha256": lock_digest},
        {"name": wheel_path.name, "sha256": sha256_file(wheel_path)},
    ]
    if policy != {
        "provider": "github-artifact-attestation",
        "workflow": f"{REPOSITORY}/{WORKFLOW_PATH}",
        "source_commit": source_sha,
        "subject_assets": expected_subjects,
    }:
        raise PackageBuildError(
            "release metadata attestation subjects do not match payload digests"
        )
    _validate_preparer_wheel(wheel_path)
    if sha256_file(descriptor_path) != assets["descriptor"]["sha256"]:
        raise PackageBuildError("descriptor digest differs from the release metadata")
    return {
        "package": archive_path,
        "descriptor": descriptor_path,
        "dependency_lock": lock_path,
        "preparer_wheel": wheel_path,
        "release_metadata": release_path,
    }


def _git_value(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise PackageBuildError(
            f"could not resolve source identity from Git: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _require_committed_inputs(root: Path) -> None:
    """Require release inputs and their publisher policy to be committed."""

    paths = [
        "LICENSE",
        PACKAGE_SOURCE,
        RUNTIME_PROJECT,
        PRODUCER_PATH,
        LOCK_SOURCE,
        BUILD_LOCK_SOURCE,
        WORKFLOW_PATH,
        "tests/release/test_training_llama_factory_package.py",
    ]
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *paths],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PackageBuildError("could not verify that release inputs are committed")
    if result.stdout.strip():
        raise PackageBuildError(
            "release inputs or publisher policy contain uncommitted changes"
        )


def _write_subject_checksums(paths: dict[str, Path], output_dir: Path) -> Path:
    checksum_path = output_dir / "attestation-subjects.sha256"
    subjects = sorted(
        paths[key]
        for key in (
            "package",
            "descriptor",
            "dependency_lock",
            "preparer_wheel",
            "release_metadata",
        )
    )
    checksum_path.write_text(
        "".join(
            f"{sha256_file(path).removeprefix('sha256:')}  {path.name}\n"
            for path in subjects
        ),
        encoding="utf-8",
    )
    return checksum_path


def _build_command(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    _require_committed_inputs(root)
    source_sha = _git_value(root, "rev-parse", "HEAD")
    source_ref = args.source_ref
    if args.github_sha and args.github_sha != source_sha:
        raise PackageBuildError("checked-out commit differs from GITHUB_SHA")
    temporary_wheel, wheel_directory = build_runtime_wheel(root, source_sha)
    try:
        paths = build_release_artifacts(
            root=root,
            output_dir=args.output_dir,
            source_sha=source_sha,
            source_ref=source_ref,
            channel=args.channel,
            target=args.target,
            preparer_wheel=temporary_wheel,
        )
    finally:
        wheel_directory.cleanup()
    print(json.dumps({key: str(path) for key, path in paths.items()}, sort_keys=True))


def _verify_command(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    _require_committed_inputs(root)
    source_sha = _git_value(root, "rev-parse", "HEAD")
    if args.github_sha and args.github_sha != source_sha:
        raise PackageBuildError("checked-out commit differs from GITHUB_SHA")
    paths = verify_release_artifacts(
        root=root,
        output_dir=args.output_dir,
        source_sha=source_sha,
        source_ref=args.source_ref,
        channel=args.channel,
        target=args.target,
    )
    if args.subject_checksums:
        _write_subject_checksums(paths, args.output_dir.resolve(strict=True))
    print(json.dumps({key: str(path) for key, path in paths.items()}, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the immutable training Plugin release bundle"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument(
            "--root", type=Path, default=Path(__file__).resolve().parents[2]
        )
        command.add_argument("--output-dir", type=Path, required=True)
        command.add_argument("--source-ref", required=True)
        command.add_argument(
            "--channel", choices=sorted(SUPPORTED_CHANNELS), required=True
        )
        command.add_argument("--target", choices=[TARGET_ID], default=TARGET_ID)
        command.add_argument("--github-sha", default=os.environ.get("GITHUB_SHA", ""))
        if name == "verify":
            command.add_argument("--subject-checksums", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "build":
            _build_command(args)
        else:
            _verify_command(args)
    except (OSError, PackageBuildError, subprocess.SubprocessError) as error:
        print(f"training-plugin-release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
