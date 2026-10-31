"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_dependency_preparer.py                                     │
│  Module: cyrene_plugin_runtime tests                                │
│  Role: Validate the Plugins-owned Python preparation adapter.       │
│                                                                     │
│  模块职责：验证 Plugins 自有的 Python 依赖准备适配器。                    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from cyrene_plugin_runtime.dependency_preparer import (
    PROTOCOL,
    WHEELHOUSE_TARGET,
    _resolve_offline_wheelhouse,
    _validate_lock,
    _validate_wheelhouse,
)


def test_dependency_preparer_accepts_only_the_authorized_exact_lock(tmp_path) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    lock = package_root / "requirements.lock"
    payload = b"grpcio==1.62.3\nprotobuf==4.25.9\n"
    lock.write_bytes(payload)
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"

    assert _validate_lock(package_root, digest) == lock
    assert PROTOCOL == "cyrene.package-dependency-preparer.v1"

    with pytest.raises(ValueError, match="authorized digest"):
        _validate_lock(package_root, f"sha256:{'0' * 64}")

    lock.write_text("grpcio>=1.62\n", encoding="utf-8")
    ranged = f"sha256:{hashlib.sha256(lock.read_bytes()).hexdigest()}"
    with pytest.raises(ValueError, match="exact immutable pins"):
        _validate_lock(package_root, ranged)


def _write_wheelhouse(package_root: Path, lock_payload: bytes) -> tuple[Path, dict[str, Path]]:
    lock = package_root / "requirements.lock"
    lock.write_bytes(lock_payload)
    wheelhouse = package_root / "wheelhouse"
    wheelhouse.mkdir()
    wheels: dict[str, Path] = {}
    records = []
    for line in lock_payload.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==", 1)
        canonical = name.replace("_", "-").lower()
        filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        wheel = wheelhouse / filename
        dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
        with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(
                f"{dist_info}/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            )
            archive.writestr(
                f"{dist_info}/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n"
            )
        payload = wheel.read_bytes()
        records.append(
            {
                "filename": filename,
                "name": canonical,
                "version": version,
                "sha256": f"sha256:{hashlib.sha256(payload).hexdigest()}",
                "size": len(payload),
            }
        )
        wheels[canonical] = wheel
    digest = f"sha256:{hashlib.sha256(lock_payload).hexdigest()}"
    (wheelhouse / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "target": WHEELHOUSE_TARGET,
                "requirementsLockSha256": digest,
                "wheels": records,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return wheelhouse, wheels


def test_package_local_wheelhouse_must_match_lock_and_every_wheel_hash(tmp_path) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    lock_payload = b"grpcio==1.62.3\nprotobuf==4.25.9\n"
    wheelhouse, wheels = _write_wheelhouse(package_root, lock_payload)
    lock = package_root / "requirements.lock"
    digest = f"sha256:{hashlib.sha256(lock_payload).hexdigest()}"

    assert _validate_wheelhouse(wheelhouse, lock=lock, expected_digest=digest) == wheelhouse

    with pytest.raises(ValueError, match="not bound to this target and lock"):
        _validate_wheelhouse(wheelhouse, lock=lock, expected_digest=f"sha256:{'0' * 64}")

    wheels["grpcio"].write_bytes(wheels["grpcio"].read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash or size mismatch"):
        _validate_wheelhouse(wheelhouse, lock=lock, expected_digest=digest)


def test_package_local_wheelhouse_rejects_missing_locked_wheel(tmp_path) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    lock_payload = b"grpcio==1.62.3\nprotobuf==4.25.9\n"
    wheelhouse, wheels = _write_wheelhouse(package_root, lock_payload)
    wheels["protobuf"].unlink()
    digest = f"sha256:{hashlib.sha256(lock_payload).hexdigest()}"

    with pytest.raises(ValueError, match="missing or unsafe"):
        _validate_wheelhouse(wheelhouse, lock=package_root / "requirements.lock", expected_digest=digest)


def test_legacy_explicit_external_wheelhouse_does_not_require_manifest(tmp_path) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    lock = package_root / "requirements.lock"
    lock.write_text("grpcio==1.62.3\nprotobuf==4.25.9\n", encoding="utf-8")
    external = tmp_path / "legacy-wheelhouse"
    external.mkdir()
    (external / "grpcio-1.62.3-py3-none-any.whl").write_bytes(b"legacy")

    assert (
        _resolve_offline_wheelhouse(
            package_root,
            lock=lock,
            lock_digest="sha256:unused-for-legacy-explicit-override",
            explicit=external,
        )
        == external
    )
