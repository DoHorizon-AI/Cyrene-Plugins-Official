"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 qqnt_direct_discovery.py                                        │
│  Module: onebot_v11_connector.qqnt_direct_discovery                  │
│  Role: Deterministic Linux x64 QQ installation discovery.            │
│                                                                     │
│  模块职责：校验操作员提供的 QQ Host/数据路径，并拒绝模糊安装选择。       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import os
import platform as host_platform
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

SUPPORTED_PLATFORM = "linux-x86_64"
INSTALLATION_MANIFEST_SCHEMA = "cyrene.qq.installation.v1"
MAX_MANIFEST_BYTES = 64 * 1024


class QQInstallationError(RuntimeError):
    """Structured error raised when an installation cannot be selected safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class QQInstallation:
    """One canonical operator-selected QQ Host installation.

    ``client_version`` is the exact build allow-list supplied by the operator.
    The native Host hello remains the authoritative observed version check.
    "client_version" 是操作员提供的精确 build 白名单；原生 Host hello 仍负责实测校验。
    """

    host_executable: Path
    data_dir: Path
    client_version: str
    platform: str
    architecture: str
    source: str
    installation_id: str | None = None


def discover_explicit(
    host_executable: str | Path,
    data_dir: str | Path,
    client_version: str,
    *,
    expected_platform: str = SUPPORTED_PLATFORM,
    source: str = "operator-path",
) -> QQInstallation:
    """Validate one explicitly selected installation without modifying it.

    The first supported target is Linux x86_64.  The executable must be one
    regular executable file, while the binding data directory may be absent so
    the caller can create it with binding-private permissions.  No directory
    scanning or heuristic fallback occurs in this path.

    Raises:
        QQInstallationError: If the host, platform, data path, or build is not
            an exact supported selection.
    """

    if expected_platform != SUPPORTED_PLATFORM:
        raise QQInstallationError(
            "UNSUPPORTED_VERSION",
            f"only {SUPPORTED_PLATFORM} installation discovery is supported",
        )
    if not isinstance(client_version, str) or not client_version.strip():
        raise QQInstallationError(
            "INVALID_REQUEST", "client_version must be non-empty text"
        )
    _require_runtime_platform()
    executable = _canonical_executable(host_executable)
    canonical_data_dir = _canonical_data_dir(data_dir)
    return QQInstallation(
        host_executable=executable,
        data_dir=canonical_data_dir,
        client_version=client_version.strip(),
        platform=SUPPORTED_PLATFORM,
        architecture="x86_64",
        source=source,
    )


def discover_manifest(
    path: str | Path, *, required_client_version: str
) -> QQInstallation:
    """Load one operator-authored installation manifest deterministically.

    The manifest is a path and metadata declaration, not a session or secret
    store.  Unknown fields are rejected so an unreviewed installation layout
    cannot silently become supported.
    """

    manifest_path = _canonical_manifest_path(path)
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise QQInstallationError(
            "NO_INSTALLATION", "QQ installation manifest cannot be read"
        ) from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise QQInstallationError(
            "INVALID_REQUEST", "QQ installation manifest exceeds the size limit"
        )
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QQInstallationError(
            "INVALID_REQUEST", "QQ installation manifest is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise QQInstallationError(
            "INVALID_REQUEST", "QQ installation manifest must be an object"
        )
    expected_fields = {
        "schema",
        "installation_id",
        "host_executable",
        "data_dir",
        "client_version",
        "platform",
        "architecture",
    }
    unknown = set(decoded).difference(expected_fields)
    if unknown:
        raise QQInstallationError(
            "INVALID_REQUEST",
            f"QQ installation manifest has unknown fields: {sorted(unknown)}",
        )
    if decoded.get("schema") != INSTALLATION_MANIFEST_SCHEMA:
        raise QQInstallationError(
            "UNSUPPORTED_VERSION", "QQ installation manifest schema is unsupported"
        )
    manifest_version = _required_text(decoded.get("client_version"), "client_version")
    if manifest_version != required_client_version:
        raise QQInstallationError(
            "UNSUPPORTED_VERSION",
            "QQ installation manifest build does not match the allow-list",
        )
    if decoded.get("platform") != SUPPORTED_PLATFORM:
        raise QQInstallationError(
            "UNSUPPORTED_VERSION", "QQ installation platform is unsupported"
        )
    if _normalize_architecture(decoded.get("architecture")) != "x86_64":
        raise QQInstallationError(
            "UNSUPPORTED_VERSION", "QQ installation architecture is unsupported"
        )
    installation = discover_explicit(
        decoded.get("host_executable"),
        decoded.get("data_dir"),
        manifest_version,
        source=f"manifest:{manifest_path}",
    )
    installation_id = decoded.get("installation_id")
    if installation_id is not None:
        installation_id = _required_text(installation_id, "installation_id")
    return replace(installation, installation_id=installation_id)


def discover_manifests(
    paths: Sequence[str | Path], *, required_client_version: str
) -> QQInstallation:
    """Select exactly one manifest and reject zero or multiple candidates."""

    if isinstance(paths, (str, bytes)):
        raise QQInstallationError(
            "INVALID_REQUEST", "installation manifests must be an ordered list"
        )
    candidates = tuple(paths)
    if not candidates:
        raise QQInstallationError(
            "NO_INSTALLATION", "no QQ installation manifest was provided"
        )
    if len(candidates) != 1:
        raise QQInstallationError(
            "AMBIGUOUS_INSTALLATION",
            f"exactly one QQ installation is required, found {len(candidates)}",
        )
    return discover_manifest(
        candidates[0], required_client_version=required_client_version
    )


def _require_runtime_platform() -> None:
    """Reject execution outside the first approved Linux x86_64 target."""

    architecture = _normalize_architecture(host_platform.machine())
    if sys.platform != "linux" or architecture != "x86_64":
        raise QQInstallationError(
            "UNSUPPORTED_VERSION",
            "QQNT direct discovery requires Linux x86_64",
        )


def _canonical_executable(value: Any) -> Path:
    """Resolve one regular executable path and reject zero candidates."""

    path = _required_absolute_path(value, "host_executable")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise QQInstallationError(
            "NO_INSTALLATION", "QQ Host executable does not exist"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise QQInstallationError(
            "INVALID_REQUEST", "QQ Host executable must be a regular executable file"
        )
    return resolved


def _canonical_data_dir(value: Any) -> Path:
    """Canonicalize a binding data path while allowing first-use creation."""

    path = _required_absolute_path(value, "data_dir")
    if path.is_symlink():
        raise QQInstallationError(
            "CAPABILITY_UNAVAILABLE", "data_dir must not be a symlink"
        )
    resolved = path.resolve(strict=False)
    if resolved.exists() and not resolved.is_dir():
        raise QQInstallationError(
            "CAPABILITY_UNAVAILABLE", "data_dir must be a directory"
        )
    return resolved


def _canonical_manifest_path(value: Any) -> Path:
    """Resolve one regular manifest path without following ambiguous roots."""

    path = _required_absolute_path(value, "installation_manifest")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise QQInstallationError(
            "NO_INSTALLATION", "QQ installation manifest does not exist"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise QQInstallationError(
            "INVALID_REQUEST", "QQ installation manifest must be a regular file"
        )
    return resolved


def _required_absolute_path(value: Any, field: str) -> Path:
    """Validate an absolute path value before canonicalization."""

    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise QQInstallationError("INVALID_REQUEST", f"{field} must be a path")
    path = Path(value)
    if not path.is_absolute():
        raise QQInstallationError("INVALID_REQUEST", f"{field} must be absolute")
    return path


def _required_text(value: Any, field: str) -> str:
    """Validate one bounded manifest text field."""

    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise QQInstallationError("INVALID_REQUEST", f"{field} must be bounded text")
    return value.strip()


def _normalize_architecture(value: Any) -> str:
    """Normalize the only accepted architecture aliases."""

    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower()
    return {"amd64": "x86_64", "x86-64": "x86_64"}.get(normalized, normalized)
