"""
┌─────────────────────────────────────────────────────────────────────┐
│  🧪 test_installer.py                                               │
│  Module: tests.store                                                │
│  Role: Unit tests for multi-language universal plugin installer     │
│  测试职责：多语言通用安装器、收据维护与一致性引擎的隔离单元测试。    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

# Ensure tools/store can be imported
STORE_DIR = Path(__file__).resolve().parents[2] / "tools" / "store"
if str(STORE_DIR) not in sys.path:
    sys.path.insert(0, str(STORE_DIR))

from installer import (
    InstallationIntegrityError,
    install_plugin,
    install_profile,
    load_installed,
    sha256_file,
    uninstall_plugin,
    verify_installation,
)


def create_mock_native_tar(output_path: Path, binary_content: bytes = b"#!/bin/sh\necho native\n") -> str:
    """Helper to create a mock native plugin .tar.gz archive."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = output_path.parent / f"tmp_{output_path.stem}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    bin_dir = temp_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    bin_file = bin_dir / "mock-server"
    bin_file.write_bytes(binary_content)

    manifest_file = temp_dir / "plugin.manifest.json"
    manifest_data = {
        "id": "mock.native",
        "name": "Mock Native Plugin",
        "version": "1.0.0",
        "runtime": {"language": "rust", "entrypoint": "bin/mock-server"},
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    with tarfile.open(output_path, "w:gz") as tar:
        tar.add(bin_file, arcname="bin/mock-server")
        tar.add(manifest_file, arcname="plugin.manifest.json")

    return sha256_file(output_path)


def create_mock_wheel(output_path: Path, pkg_name: str = "mock_python", version: str = "0.2.0") -> str:
    """Helper to create a valid minimal Python wheel."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dist_info = f"{pkg_name}-{version}.dist-info"

    metadata = (
        f"Metadata-Version: 2.1\n"
        f"Name: {pkg_name.replace('_', '-')}\n"
        f"Version: {version}\n"
        f"Summary: Mock Python plugin\n"
    )
    wheel_info = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    init_code = "def start():\n    return 'started'\n"
    record_content = (
        f"{pkg_name}/__init__.py,,\n"
        f"{dist_info}/METADATA,,\n"
        f"{dist_info}/WHEEL,,\n"
        f"{dist_info}/RECORD,,\n"
    )

    with zipfile.ZipFile(output_path, "w") as zf:
        zf.writestr(f"{pkg_name}/__init__.py", init_code)
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/WHEEL", wheel_info)
        zf.writestr(f"{dist_info}/RECORD", record_content)

    return sha256_file(output_path)


def create_mock_nodejs_tar(output_path: Path) -> str:
    """Helper to create a mock Node.js plugin .tar.gz archive."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = output_path.parent / f"tmp_{output_path.stem}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    pkg_json = temp_dir / "package.json"
    pkg_data = {
        "name": "mock-node-plugin",
        "version": "1.0.0",
        "description": "Mock node plugin",
        "bin": {"mock-node": "./bin/run.js"},
    }
    pkg_json.write_text(json.dumps(pkg_data, indent=2), encoding="utf-8")

    bin_dir = temp_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    run_js = bin_dir / "run.js"
    run_js.write_text("#!/usr/bin/env node\nconsole.log('node running');\n", encoding="utf-8")

    with tarfile.open(output_path, "w:gz") as tar:
        tar.add(pkg_json, arcname="package.json")
        tar.add(run_js, arcname="bin/run.js")

    return sha256_file(output_path)


# ─────────────────────────────────────────────────────────────────────
# Test Cases
# ─────────────────────────────────────────────────────────────────────


def test_mock_native_plugin_install_and_permissions(tmp_path: Path):
    """Test native plugin extraction, chmod +x execution rights, and SHA256 validation.

    测试 mock native 插件的解压、赋权 chmod +x、SHA256 校验。
    """
    pkg_dir = tmp_path / "artifacts"
    tar_path = pkg_dir / "mock.native-1.0.0-linux-x64.tar.gz"
    expected_hash = create_mock_native_tar(tar_path)

    target_dir = tmp_path / "installed_plugins"

    # 1. Normal installation with matching expected hash
    receipt = install_plugin(
        plugin_id="mock.native",
        target_dir=target_dir,
        version="1.0.0",
        language="rust",
        package_path=tar_path,
        expected_sha256=expected_hash,
    )
    assert receipt["id"] == "mock.native"
    assert receipt["status"] == "installed"

    dest_dir = target_dir / "mock.native@1.0.0"
    assert dest_dir.exists()
    assert dest_dir.is_dir()

    # Verify binary executable permission
    bin_file = dest_dir / "bin" / "mock-server"
    assert bin_file.exists()
    mode = bin_file.stat().st_mode
    assert bool(mode & stat.S_IXUSR), "Binary must have user execute permission"
    assert bool(mode & stat.S_IXOTH), "Binary must have other execute permission"

    # Verify receipt fields in installed.json
    installed = load_installed(target_dir)
    assert "mock.native" in installed
    record = installed["mock.native"]
    assert record["id"] == "mock.native"
    assert record["version"] == "1.0.0"
    assert record["language"] == "rust"
    assert record["status"] == "installed"
    assert record["entrypoint"] == "bin/mock-server"
    assert record["sha256"] == expected_hash
    assert "files" in record
    assert "bin/mock-server" in record["files"]

    # Verify integrity passes
    result = verify_installation(target_dir)
    assert result.valid
    assert result.plugins_verified == 1

    # 2. Test hash mismatch rejection
    wrong_hash = "0" * 64
    with pytest.raises(InstallationIntegrityError, match="hash mismatch"):
        install_plugin(
            plugin_id="mock.native.bad",
            target_dir=target_dir,
            version="1.0.0",
            language="rust",
            package_path=tar_path,
            expected_sha256=wrong_hash,
        )


def test_mock_python_plugin_install_workflow(tmp_path: Path):
    """Test python plugin wheel installation into an isolated .venv environment.

    测试 mock python 插件的安装流程与 .venv 隔离环境初始化。
    """
    pkg_dir = tmp_path / "artifacts"
    whl_path = pkg_dir / "mock_python-0.2.0-py3-none-any.whl"
    expected_hash = create_mock_wheel(whl_path, pkg_name="mock_python", version="0.2.0")

    target_dir = tmp_path / "installed_plugins"

    receipt = install_plugin(
        plugin_id="mock.python",
        target_dir=target_dir,
        version="0.2.0",
        language="python",
        package_path=whl_path,
        expected_sha256=expected_hash,
    )
    assert receipt["id"] == "mock.python"
    assert receipt["version"] == "0.2.0"

    dest_dir = target_dir / "mock.python@0.2.0"
    assert dest_dir.exists()

    # Check .venv structure
    venv_dir = dest_dir / ".venv"
    assert venv_dir.exists()
    venv_python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    assert venv_python.exists()
    assert os.access(venv_python, os.X_OK)

    # Check receipt
    installed = load_installed(target_dir)
    assert "mock.python" in installed
    record = installed["mock.python"]
    assert record["id"] == "mock.python"
    assert record["version"] == "0.2.0"
    assert record["language"] == "python"
    assert record["status"] == "installed"
    assert record["sha256"] == expected_hash

    # Verify installation integrity
    res = verify_installation(target_dir)
    assert res.valid


def test_mock_nodejs_plugin_install(tmp_path: Path):
    """Test Node.js plugin installation, package.json parsing and script verification.

    测试 Node.js 插件解压、package.json 校验与可执行脚本赋权。
    """
    pkg_dir = tmp_path / "artifacts"
    tar_path = pkg_dir / "mock.nodejs-1.0.0.tar.gz"
    create_mock_nodejs_tar(tar_path)

    target_dir = tmp_path / "installed_plugins"
    receipt = install_plugin(
        plugin_id="mock.nodejs",
        target_dir=target_dir,
        version="1.0.0",
        language="nodejs",
        package_path=tar_path,
    )
    assert receipt["id"] == "mock.nodejs"
    assert receipt["language"] == "nodejs"

    dest_dir = target_dir / "mock.nodejs@1.0.0"
    assert (dest_dir / "package.json").exists()
    run_script = dest_dir / "bin" / "run.js"
    assert run_script.exists()
    assert os.access(run_script, os.X_OK)

    installed = load_installed(target_dir)
    assert "mock.nodejs" in installed
    assert installed["mock.nodejs"]["entrypoint"] == "bin/run.js"
    assert verify_installation(target_dir).valid


def test_install_profile_batch(tmp_path: Path):
    """Test batch installation of plugins declared under a profile in profiles.yaml.

    测试 install_profile 依据 profiles.yaml 批量安装及 installed.json 收据记录。
    """
    offline_dir = tmp_path / "offline_packages"
    offline_dir.mkdir(parents=True, exist_ok=True)

    native_tar = offline_dir / "plug.native-1.0.0-linux-x64.tar.gz"
    native_hash = create_mock_native_tar(native_tar)

    py_whl = offline_dir / "plug_python-0.1.0-py3-none-any.whl"
    py_hash = create_mock_wheel(py_whl, pkg_name="plug_python", version="0.1.0")

    # Create mock profiles.yaml
    profiles_yaml = tmp_path / "profiles.yaml"
    profiles_content = {
        "schema_version": "cyrene.store-profiles.v1",
        "profiles": {
            "test-stack": {
                "description": "Integration test stack profile",
                "plugins": [
                    "plug.native",
                    "plug.python",
                ],
            }
        },
    }
    profiles_yaml.write_text(yaml.safe_dump(profiles_content), encoding="utf-8")

    # Create mock catalog manifest
    catalog_file = offline_dir / "plugins-manifest.json"
    catalog_data = {
        "version": "1.0",
        "plugins": [
            {
                "id": "plug.native",
                "version": "1.0.0",
                "language": "rust",
                "entrypoint": "bin/mock-server",
                "artifacts": [
                    {
                        "filename": native_tar.name,
                        "sha256": native_hash,
                    }
                ],
            },
            {
                "id": "plug.python",
                "version": "0.1.0",
                "language": "python",
                "artifacts": [
                    {
                        "filename": py_whl.name,
                        "sha256": py_hash,
                    }
                ],
            },
        ],
    }
    catalog_file.write_text(json.dumps(catalog_data), encoding="utf-8")

    target_dir = tmp_path / "installed_cluster"

    # Run batch installation
    receipts = install_profile(
        profile_name="test-stack",
        target_dir=target_dir,
        offline_dir=offline_dir,
        profile_file=profiles_yaml,
    )

    assert len(receipts) == 2
    installed = load_installed(target_dir)
    assert len(installed) == 2
    assert "plug.native" in installed
    assert "plug.python" in installed

    assert installed["plug.native"]["status"] == "installed"
    assert installed["plug.python"]["status"] == "installed"
    assert (target_dir / "plug.native@1.0.0").exists()
    assert (target_dir / "plug.python@0.1.0").exists()

    # Verify overall integrity
    verify_res = verify_installation(target_dir)
    assert verify_res.valid
    assert verify_res.plugins_verified == 2


def test_verify_installation_catches_corruption_and_mismatch(tmp_path: Path):
    """Test that corrupted files, hash mismatches, or missing files are intercepted by verify_installation.

    测试损坏文件、哈希不匹配或文件缺失时被 verify_installation 拦截。
    """
    pkg_dir = tmp_path / "artifacts"
    tar_path = pkg_dir / "mock.native-1.0.0.tar.gz"
    create_mock_native_tar(tar_path)

    target_dir = tmp_path / "installed_plugins"
    install_plugin(
        plugin_id="mock.native",
        target_dir=target_dir,
        version="1.0.0",
        language="rust",
        package_path=tar_path,
    )

    # Initial state must be completely valid
    assert verify_installation(target_dir).valid

    # Case 1: Corrupt a tracked binary file
    bin_file = target_dir / "mock.native@1.0.0" / "bin" / "mock-server"
    bin_file.write_bytes(b"corrupted binary payload 12345")

    # Strict mode should raise InstallationIntegrityError
    with pytest.raises(InstallationIntegrityError, match="SHA-256 mismatch"):
        verify_installation(target_dir, raise_on_error=True)

    # Non-strict mode should return report with valid=False
    report = verify_installation(target_dir, raise_on_error=False)
    assert not report
    assert not report.valid
    assert any("SHA-256 mismatch" in err for err in report.errors)

    # Case 2: Delete a tracked file
    bin_file.unlink()
    with pytest.raises(InstallationIntegrityError, match="missing tracked file"):
        verify_installation(target_dir, raise_on_error=True)

    report_missing = verify_installation(target_dir, raise_on_error=False)
    assert not report_missing
    assert any("missing tracked file" in err for err in report_missing.errors)


def test_uninstall_and_cleanup(tmp_path: Path):
    """Test clean uninstallation of a plugin and receipt updates in installed.json.

    测试卸载清理：干净移除插件目录并同步更新 installed.json。
    """
    pkg_dir = tmp_path / "artifacts"
    tar_path = pkg_dir / "mock.native-1.0.0.tar.gz"
    create_mock_native_tar(tar_path)

    whl_path = pkg_dir / "mock_python-0.2.0-py3-none-any.whl"
    create_mock_wheel(whl_path, pkg_name="mock_python", version="0.2.0")

    target_dir = tmp_path / "installed_plugins"
    install_plugin("mock.native", target_dir, version="1.0.0", language="rust", package_path=tar_path)
    install_plugin("mock.python", target_dir, version="0.2.0", language="python", package_path=whl_path)

    installed_before = load_installed(target_dir)
    assert len(installed_before) == 2
    native_folder = target_dir / "mock.native@1.0.0"
    python_folder = target_dir / "mock.python@0.2.0"
    assert native_folder.exists()
    assert python_folder.exists()

    # Uninstall mock.native
    removed = uninstall_plugin("mock.native", target_dir)
    assert removed is True

    # Check filesystem: native folder removed, python folder untouched
    assert not native_folder.exists()
    assert python_folder.exists()

    # Check receipt in installed.json
    installed_after = load_installed(target_dir)
    assert "mock.native" not in installed_after
    assert "mock.python" in installed_after
    assert len(installed_after) == 1

    # Verify remaining installation
    verify_res = verify_installation(target_dir)
    assert verify_res.valid
    assert verify_res.plugins_verified == 1

    # Uninstall non-existent plugin returns False gracefully
    assert uninstall_plugin("non.existent.plugin", target_dir) is False


def test_cli_subcommands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Test installer.py CLI subcommands (install, list, verify, uninstall).

    测试 installer.py 命令行子命令（install、list、verify、uninstall）。
    """
    from installer import main

    pkg_dir = tmp_path / "artifacts"
    tar_path = pkg_dir / "cli.test-1.0.0.tar.gz"
    create_mock_native_tar(tar_path)

    target_dir = tmp_path / "cli_installed"

    # 1. CLI install
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "installer.py",
            "install",
            "cli.test",
            "--target-dir",
            str(target_dir),
            "--package",
            str(tar_path),
            "--version",
            "1.0.0",
            "--language",
            "rust",
        ],
    )
    rc = main()
    assert rc == 0

    # 2. CLI list
    monkeypatch.setattr(
        sys,
        "argv",
        ["installer.py", "list", "--target-dir", str(target_dir)],
    )
    rc = main()
    assert rc == 0

    # 3. CLI verify
    monkeypatch.setattr(
        sys,
        "argv",
        ["installer.py", "verify", "--target-dir", str(target_dir)],
    )
    rc = main()
    assert rc == 0

    # 4. CLI uninstall
    monkeypatch.setattr(
        sys,
        "argv",
        ["installer.py", "uninstall", "cli.test", "--target-dir", str(target_dir)],
    )
    rc = main()
    assert rc == 0


def test_real_packages_offline_smoke(tmp_path: Path):
    """Smoke test using actual pre-built packages from dist/plugins if available.

    测试真实预编译离线包的安装、收据写入与完整性校验。
    """
    real_dist = Path(__file__).resolve().parents[3] / "dist" / "plugins" / "cyrene-plugins-packages"
    if not real_dist.exists():
        pytest.skip(f"Real dist directory {real_dist} not found, skipping real package test.")

    target_dir = tmp_path / "real_installed"
    tar_artifact = real_dist / "cyrene.tools.computer-runtime-0.1.0-linux-x64.tar.gz"
    whl_artifact = real_dist / "cyrene_exact_match_evaluator-0.1.0-py3-none-any.whl"

    if tar_artifact.exists():
        receipt = install_plugin(
            "cyrene.tools.computer-runtime",
            target_dir=target_dir,
            version="0.1.0",
            language="rust",
            package_path=tar_artifact,
        )
        assert receipt["status"] == "installed"
        assert (target_dir / "cyrene.tools.computer-runtime@0.1.0").exists()

    if whl_artifact.exists():
        receipt_py = install_plugin(
            "cyrene.evaluation.exact-match",
            target_dir=target_dir,
            version="0.1.0",
            language="python",
            package_path=whl_artifact,
        )
        assert receipt_py["status"] == "installed"
        assert (target_dir / "cyrene.evaluation.exact-match@0.1.0").exists()

    # Verify real package installation integrity
    res = verify_installation(target_dir)
    assert res.valid

