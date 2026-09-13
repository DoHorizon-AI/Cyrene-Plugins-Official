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

import pytest
from cyrene_plugin_runtime.dependency_preparer import PROTOCOL, _validate_lock


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
