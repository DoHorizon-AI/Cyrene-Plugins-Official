"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_qq_official_packages.py                                    │
│  Module: qq_connector.tests.test_qq_official_packages                │
│  Role: Offline tests for manifest-locked package staging.            │
│                                                                     │
│  模块职责：离线验证按可信清单暂存官方原包的流程。                      │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from qq_connector import QQOfficialPackageManager, QQPackageError, load_package_lock


class FakeResponse:
    """Small in-memory HTTPS response used without network access.

    中文：完全离线使用的内存 HTTPS 响应。
    """

    def __init__(self, payload: bytes, url: str) -> None:
        self._payload = payload
        self.url = url
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._payload)
        result, self._payload = self._payload[:size], self._payload[size:]
        return result


def _write_lock(
    path: Path,
    *,
    version: str = "9.9.0",
    target: str = "linux-x86_64",
    package_format: str = "deb",
    filename: str = "qq-client.deb",
    url: str = "https://dldir.qq.com/qq-client.deb",
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> None:
    payload = {
        "schema": "cyrene.qq.package-lock.v1",
        "publisher": "Tencent",
        "packages": [
            {
                "target": target,
                "client_version": version,
                "package_format": package_format,
                "filename": filename,
                "download_url": url,
                "source_page": "https://im.qq.com/download/",
                "sha256": sha256 or hashlib.sha256(b"official-fixture").hexdigest(),
                **({"size_bytes": size_bytes} if size_bytes is not None else {}),
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_package_lock_rejects_untrusted_sources_and_null_size(tmp_path: Path) -> None:
    lock_path = tmp_path / "qq-package-lock.json"
    _write_lock(lock_path, url="https://qq.com.attacker.example/client.deb")
    with pytest.raises(QQPackageError) as error:
        load_package_lock(lock_path)
    assert error.value.code == "UNTRUSTED_SOURCE"

    _write_lock(lock_path, url="http://dldir.qq.com/client.deb")
    with pytest.raises(QQPackageError) as error:
        load_package_lock(lock_path)
    assert error.value.code == "UNTRUSTED_SOURCE"

    _write_lock(lock_path)
    value = json.loads(lock_path.read_text(encoding="utf-8"))
    value["packages"][0]["size_bytes"] = None
    lock_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(QQPackageError) as error:
        load_package_lock(lock_path)
    assert error.value.code == "INVALID_REQUEST"


def test_package_manager_stages_exact_digest_and_rolls_back_atomically(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "qq-package-lock.json"
    store = tmp_path / "private-store"
    first_payload = b"exact original Tencent installer fixture v1"
    second_payload = b"exact original Tencent installer fixture v2"
    responses = {"v1": first_payload, "v2": second_payload}

    def opener(request: Any, timeout: int) -> FakeResponse:
        assert timeout > 0
        version = "v2" if "v2" in request.full_url else "v1"
        return FakeResponse(responses[version], request.full_url)

    _write_lock(
        lock_path,
        version="9.9.0",
        sha256=hashlib.sha256(first_payload).hexdigest(),
        size_bytes=len(first_payload),
    )
    manager = QQOfficialPackageManager(lock_path, store, opener=opener)
    first = manager.stage("linux-x86_64")
    assert first["status"] == "STAGED"
    assert first["runtime_ready"] is False
    assert Path(first["installer_path"]).read_bytes() == first_payload
    assert manager.status("linux-x86_64")["active_version"] == "9.9.0"

    _write_lock(
        lock_path,
        version="10.0.0",
        filename="qq-client-v2.deb",
        url="https://dldir.qq.com/qq-client-v2.deb",
        sha256=hashlib.sha256(second_payload).hexdigest(),
        size_bytes=len(second_payload),
    )
    second = manager.stage("linux-x86_64")
    assert second["client_version"] == "10.0.0"
    assert Path(second["installer_path"]).read_bytes() == second_payload
    assert manager.status("linux-x86_64")["previous_version"] == "9.9.0"

    rollback = manager.rollback("linux-x86_64")
    assert rollback["status"] == "STAGED_ROLLBACK"
    assert rollback["client_version"] == "9.9.0"
    assert Path(rollback["installer_path"]).read_bytes() == first_payload
    assert manager.status("linux-x86_64")["active_version"] == "9.9.0"
    assert not list(store.rglob("*.sh"))


def test_package_manager_fails_before_activation_on_digest_or_target_errors(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "qq-package-lock.json"
    store = tmp_path / "private-store"
    payload = b"package fixture"
    _write_lock(lock_path, sha256="0" * 64)

    manager = QQOfficialPackageManager(
        lock_path,
        store,
        opener=lambda request, timeout: FakeResponse(payload, request.full_url),
    )
    with pytest.raises(QQPackageError) as error:
        manager.stage("linux-x86_64")
    assert error.value.code == "CHECKSUM_MISMATCH"
    assert manager.status("linux-x86_64")["status"] == "NOT_CONFIGURED"
    assert not (store / "active" / "linux-x86_64.json").exists()

    with pytest.raises(QQPackageError) as error:
        manager.stage("macos-aarch64")
    assert error.value.code == "UNSUPPORTED_TARGET"


@pytest.mark.parametrize(
    ("target", "package_format", "filename"),
    [
        ("linux-aarch64", "deb", "qq-arm64.deb"),
        ("windows-x86_64", "msi", "qq-win64.msi"),
    ],
)
def test_package_manager_accepts_explicit_cross_target_locks_without_readiness_claim(
    tmp_path: Path,
    target: str,
    package_format: str,
    filename: str,
) -> None:
    lock_path = tmp_path / "qq-package-lock.json"
    payload = b"locked official package bytes"
    _write_lock(
        lock_path,
        target=target,
        package_format=package_format,
        filename=filename,
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    manager = QQOfficialPackageManager(
        lock_path,
        tmp_path / "store",
        opener=lambda request, timeout: FakeResponse(payload, request.full_url),
    )
    result = manager.stage(target)
    assert result["target"] == target
    assert result["runtime_ready"] is False
