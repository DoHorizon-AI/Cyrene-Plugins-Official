"""
┌─────────────────────────────────────────────────────────────────────┐
│  🧪 test_cli.py                                                     │
│  Module: tests.store                                                │
│  Role: Unit and integration tests for cyrene-plugin-store CLI       │
│  测试职责：验证统一命令行入口 cyrene-plugin-store 的参数解析、各子命令  │
│            调用逻辑、--json 输出与生命周期流程。                     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

# Ensure repository root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.store import installer
from tools.store.cli import build_parser, main

EXECUTABLE_PATH = _REPO_ROOT / "tools" / "store" / "cyrene_plugin_store.py"
DATA_TOOLS_PLUGIN_METADATA: dict[str, dict[str, object]] = {
    "cyrene.tools.document-parsing": {
        "name": "Document Parsing",
        "language": "python",
        "kind": "capability-plugin",
        "capabilities": ["document.parsing.v1"],
        "supportedServices": ["Cyrene-Catalyst"],
    },
    "cyrene.tools.knowledge-preparation": {
        "name": "Knowledge Preparation",
        "language": "python",
        "kind": "capability-plugin",
        "capabilities": ["dataset.knowledge.v1"],
        "supportedServices": ["Cyrene-Catalyst"],
    },
    "cyrene.tools.dataset-generation": {
        "name": "Dataset Generation",
        "language": "python",
        "kind": "capability-plugin",
        "capabilities": ["dataset.generation.v1"],
        "supportedServices": ["Cyrene-Catalyst"],
    },
}


def _assert_data_tools_plugin_metadata(plugins: list[dict[str, Any]]) -> None:
    """Check the trial plugins expose their frozen catalog metadata."""
    plugins_by_id = {plugin["id"]: plugin for plugin in plugins}
    for plugin_id, expected_metadata in DATA_TOOLS_PLUGIN_METADATA.items():
        assert plugin_id in plugins_by_id
        plugin = plugins_by_id[plugin_id]
        for field, expected_value in expected_metadata.items():
            assert plugin[field] == expected_value


# ─────────────────────────────────────────────────────────────────────
# Test Group 1: Parser and Argument Validation
# ─────────────────────────────────────────────────────────────────────


class TestParserAndHelp:
    """Tests CLI argument parsing, flags, and help messaging."""

    def test_parser_construction(self) -> None:
        """Verify build_parser returns configured ArgumentParser."""
        parser = build_parser()
        assert parser.prog == "cyrene-plugin-store"
        assert "catalog" in parser._subparsers._group_actions[0].choices
        assert "install" in parser._subparsers._group_actions[0].choices
        assert "install-profile" in parser._subparsers._group_actions[0].choices
        assert "uninstall" in parser._subparsers._group_actions[0].choices
        assert "list" in parser._subparsers._group_actions[0].choices
        assert "verify" in parser._subparsers._group_actions[0].choices
        assert "supervisor" in parser._subparsers._group_actions[0].choices

    def test_help_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify --help outputs usage text and exits cleanly."""
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "usage: cyrene-plugin-store" in captured.out
        assert "catalog" in captured.out
        assert "supervisor" in captured.out

    def test_catalog_subcommand_parsing(self) -> None:
        """Verify argument parsing for `catalog list` and `catalog show`."""
        parser = build_parser()

        # catalog list with full filters
        args = parser.parse_args([
            "catalog", "list",
            "--profile", "reactor",
            "--service", "Cyrene-Navigator",
            "--language", "python",
            "--keyword", "vllm",
            "--json",
        ])
        assert args.subcommand == "catalog"
        assert args.catalog_subcommand == "list"
        assert args.profile == "reactor"
        assert args.service == "Cyrene-Navigator"
        assert args.language == "python"
        assert args.keyword == "vllm"
        assert args.json is True

        # catalog show
        args_show = parser.parse_args(["catalog", "show", "cyrene.serving.vllm-runtime", "--json"])
        assert args_show.subcommand == "catalog"
        assert args_show.catalog_subcommand == "show"
        assert args_show.id == "cyrene.serving.vllm-runtime"
        assert args_show.json is True

    def test_install_subcommands_parsing(self) -> None:
        """Verify argument parsing for install, install-profile, uninstall."""
        parser = build_parser()

        # install
        args = parser.parse_args([
            "install", "cyrene.connectors.wecom",
            "--version", "0.1.0",
            "--target-dir", "/tmp/plugins",
            "--offline-dir", "/tmp/offline",
        ])
        assert args.subcommand == "install"
        assert args.id == "cyrene.connectors.wecom"
        assert args.version == "0.1.0"
        assert args.target_dir == "/tmp/plugins"
        assert args.offline_dir == "/tmp/offline"

        # install-profile
        args_prof = parser.parse_args([
            "install-profile", "echo",
            "--target-dir", "/tmp/plugins",
            "--offline-dir", "/tmp/offline",
        ])
        assert args_prof.subcommand == "install-profile"
        assert args_prof.profile_name == "echo"
        assert args_prof.target_dir == "/tmp/plugins"

        # uninstall
        args_uninst = parser.parse_args([
            "uninstall", "cyrene.connectors.wecom",
            "--target-dir", "/tmp/plugins",
        ])
        assert args_uninst.subcommand == "uninstall"
        assert args_uninst.id == "cyrene.connectors.wecom"
        assert args_uninst.target_dir == "/tmp/plugins"

    def test_list_and_verify_parsing(self) -> None:
        """Verify argument parsing for list and verify."""
        parser = build_parser()

        args_list = parser.parse_args(["list", "--target-dir", "/tmp/plugins", "--json"])
        assert args_list.subcommand == "list"
        assert args_list.target_dir == "/tmp/plugins"
        assert args_list.json is True

        args_verify = parser.parse_args(["verify", "--target-dir", "/tmp/plugins"])
        assert args_verify.subcommand == "verify"
        assert args_verify.target_dir == "/tmp/plugins"

    def test_supervisor_subcommands_parsing(self) -> None:
        """Verify argument parsing for supervisor start, reload, status, stop."""
        parser = build_parser()

        for action in ["start", "reload", "status", "stop"]:
            args = parser.parse_args([
                "supervisor", action, "cyrene.tools.dataset-validator",
                "--target-dir", "/tmp/plugins",
            ])
            assert args.subcommand == "supervisor"
            assert args.supervisor_subcommand == action
            assert args.id == "cyrene.tools.dataset-validator"
            assert args.target_dir == "/tmp/plugins"

    def test_missing_required_args_raises(self) -> None:
        """Verify parser raises error when required arguments are missing."""
        parser = build_parser()

        # Missing subcommand
        with pytest.raises(SystemExit):
            parser.parse_args([])

        # supervisor missing --target-dir
        with pytest.raises(SystemExit):
            parser.parse_args(["supervisor", "start", "my.plugin"])


# ─────────────────────────────────────────────────────────────────────
# Test Group 2: Catalog CLI Execution
# ─────────────────────────────────────────────────────────────────────


class TestCatalogCliExecution:
    """Tests `catalog list` and `catalog show` CLI commands."""

    def test_catalog_list_all(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test `catalog list` returns all 17 official plugins."""
        rc = main(["catalog", "list"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Cyrene Plugin Catalog (17 plugins):" in captured.out
        assert "cyrene.connectors.im" in captured.out
        assert "cyrene.tools.computer-runtime" in captured.out
        for plugin_id in DATA_TOOLS_PLUGIN_METADATA:
            assert plugin_id in captured.out

    def test_catalog_list_profile_filter(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test `catalog list --profile echo` filters to 3 plugins."""
        rc = main(["catalog", "list", "--profile", "echo"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Cyrene Plugin Catalog (3 plugins):" in captured.out
        assert "cyrene.connectors.im" in captured.out
        assert "cyrene.connectors.onebot-v11" in captured.out
        assert "cyrene.connectors.wecom" in captured.out

    def test_catalog_list_language_filter(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test `catalog list --language rust` filters to 1 plugin."""
        rc = main(["catalog", "list", "--language", "rust"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "cyrene.tools.computer-runtime" in captured.out
        assert "cyrene.connectors.im" not in captured.out

    def test_catalog_list_keyword_filter(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test `catalog list --keyword dataset` filters to dataset plugins."""
        rc = main(["catalog", "list", "--keyword", "dataset"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "cyrene.tools.dataset-preparation" in captured.out
        assert "cyrene.tools.dataset-validator" in captured.out
        assert "cyrene.tools.dataset-generation" in captured.out

    def test_catalog_list_service_filter(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test `catalog list --service Cyrene-Navigator` filters to 5 plugins."""
        rc = main(["catalog", "list", "--service", "Cyrene-Navigator"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Cyrene Plugin Catalog (5 plugins):" in captured.out
        assert "cyrene.connectors.im" in captured.out
        assert "cyrene.connectors.onebot-v11" in captured.out
        assert "cyrene.connectors.wecom" in captured.out
        assert "cyrene.providers.model-api-connector" in captured.out
        assert "cyrene.tools.computer-runtime" in captured.out

        # Short flag -s with Cyrene-Echo
        rc_echo = main(["catalog", "list", "-s", "Cyrene-Echo"])
        assert rc_echo == 0
        captured_echo = capsys.readouterr()
        assert "Cyrene Plugin Catalog (3 plugins):" in captured_echo.out
        assert "cyrene.evaluation.evaluator-pack" in captured_echo.out
        assert "cyrene.evaluation.exact-match" in captured_echo.out
        assert "cyrene.evaluation.llm-judge" in captured_echo.out

        # Catalyst includes the three trial plugins and the two existing dataset tools.
        rc_catalyst = main(
            ["catalog", "list", "--service", "Cyrene-Catalyst", "--json"]
        )
        assert rc_catalyst == 0
        catalyst_data = json.loads(capsys.readouterr().out)
        catalyst_ids = {plugin["id"] for plugin in catalyst_data}
        assert catalyst_ids == {
            "cyrene.tools.dataset-generation",
            "cyrene.tools.dataset-preparation",
            "cyrene.tools.dataset-validator",
            "cyrene.tools.document-parsing",
            "cyrene.tools.knowledge-preparation",
        }
        _assert_data_tools_plugin_metadata(catalyst_data)

    def test_catalog_list_json_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test `catalog list --json` outputs valid JSON array."""
        rc = main(["catalog", "list", "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 17
        plugin_ids = {p["id"] for p in data}
        assert "cyrene.serving.vllm-runtime" in plugin_ids
        _assert_data_tools_plugin_metadata(data)

    def test_catalog_show_existing(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test `catalog show <id>` prints structured plugin details."""
        rc = main(["catalog", "show", "cyrene.connectors.im"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Plugin Information: cyrene.connectors.im" in captured.out
        assert "QQ Connector" in captured.out
        assert "Language:     csharp" in captured.out
        assert "Supported Services: Cyrene-Exchange, Cyrene-Navigator" in captured.out

    def test_catalog_show_json_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test `catalog show <id> --json` outputs valid JSON plugin dictionary."""
        rc = main(["catalog", "show", "cyrene.training.llama-factory", "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["id"] == "cyrene.training.llama-factory"
        assert "capabilities" in data
        assert "profiles" in data
        assert "supportedServices" in data
        assert data["supportedServices"] == ["Cyrene-Yield"]

    def test_catalog_show_nonexistent(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test `catalog show <invalid_id>` returns exit code 1 with error."""
        rc = main(["catalog", "show", "nonexistent.fake.plugin"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "Error: Plugin 'nonexistent.fake.plugin' not found in catalog." in captured.err


# ─────────────────────────────────────────────────────────────────────
# Test Group 3: Install, Profile, List, Verify & Uninstall
# ─────────────────────────────────────────────────────────────────────


class TestInstallProfileAndReceiptLifecycle:
    """Tests install-profile, list, verify, and uninstall subcommands."""

    def test_list_empty_target_dir(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Test `list` on an empty directory reports no plugins installed."""
        rc = main(["list", "--target-dir", str(tmp_path)])
        assert rc == 0
        captured = capsys.readouterr()
        assert f"No plugins installed in {tmp_path}" in captured.out

    def test_list_empty_target_dir_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Test `list --json` on empty directory outputs empty JSON array []."""
        rc = main(["list", "--target-dir", str(tmp_path), "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data == []

    def test_list_and_verify_with_mock_receipt(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test `list`, `list --json`, and `verify` with a recorded mock plugin."""
        plugin_id = "test.mock.plugin"
        version = "1.0.0"
        install_dir = tmp_path / f"{plugin_id}@{version}"
        install_dir.mkdir(parents=True, exist_ok=True)
        bin_dir = install_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        bin_file = bin_dir / "server"
        bin_file.write_bytes(b"#!/bin/sh\necho mock\n")
        bin_file.chmod(0o755)

        bin_hash = installer.sha256_file(bin_file)

        receipt = {
            "id": plugin_id,
            "version": version,
            "language": "rust",
            "install_path": str(install_dir),
            "entrypoint": "bin/server",
            "installed_at": "2026-09-27T12:00:00Z",
            "status": "installed",
            "files": {
                "bin/server": bin_hash,
            },
        }
        installer.record_receipt(tmp_path, receipt)

        # 1. Test human-readable list
        rc_list = main(["list", "--target-dir", str(tmp_path)])
        assert rc_list == 0
        captured = capsys.readouterr()
        assert plugin_id in captured.out
        assert version in captured.out
        assert "installed" in captured.out

        # 2. Test list --json
        rc_json = main(["list", "--target-dir", str(tmp_path), "--json"])
        assert rc_json == 0
        captured_json = capsys.readouterr()
        data = json.loads(captured_json.out)
        assert len(data) == 1
        assert data[0]["id"] == plugin_id
        assert data[0]["version"] == version

        # 3. Test verify passes
        rc_verify = main(["verify", "--target-dir", str(tmp_path)])
        assert rc_verify == 0
        captured_verify = capsys.readouterr()
        assert "Integrity verification passed" in captured_verify.out

        # 4. Test verify fails when file is modified/tampered
        bin_file.write_bytes(b"tampered content")
        rc_verify_fail = main(["verify", "--target-dir", str(tmp_path)])
        assert rc_verify_fail == 1
        captured_fail = capsys.readouterr()
        assert "Integrity verification failed" in captured_fail.err

    def test_install_profile_batch(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Test `install-profile` batches all plugins declared in offline catalog."""
        offline_dir = tmp_path / "offline_repo"
        offline_dir.mkdir(parents=True, exist_ok=True)

        # Build mock native archive
        native_tar = offline_dir / "mock.provider-1.0.0-linux-x64.tar.gz"
        tmp_build = offline_dir / "tmp_build"
        tmp_build.mkdir(parents=True, exist_ok=True)
        bin_path = tmp_build / "bin"
        bin_path.mkdir(parents=True, exist_ok=True)
        (bin_path / "provider").write_bytes(b"#!/bin/sh\necho provider\n")

        with tarfile.open(native_tar, "w:gz") as tar:
            tar.add(bin_path, arcname="bin")

        tar_hash = installer.sha256_file(native_tar)

        # Mock catalog in offline directory
        catalog_manifest = {
            "version": "1.0",
            "plugins": [
                {
                    "id": "mock.provider",
                    "version": "1.0.0",
                    "language": "rust",
                    "entrypoint": "bin/provider",
                    "artifacts": [
                        {
                            "filename": native_tar.name,
                            "sha256": tar_hash,
                        }
                    ],
                }
            ],
        }
        (offline_dir / "plugins-manifest.json").write_text(json.dumps(catalog_manifest), encoding="utf-8")

        # Mock profiles.yaml in target directory
        import yaml
        profiles_file = tmp_path / "profiles.yaml"
        profiles_content = {
            "profiles": {
                "custom-profile": {
                    "description": "Test profile",
                    "plugins": ["mock.provider"],
                }
            }
        }
        profiles_file.write_text(yaml.safe_dump(profiles_content), encoding="utf-8")

        target_dir = tmp_path / "cluster_installed"

        # Execute install-profile CLI command
        rc = main([
            "install-profile", "custom-profile",
            "--target-dir", str(target_dir),
            "--offline-dir", str(offline_dir),
            "--profile-file", str(profiles_file),
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Successfully installed profile 'custom-profile'" in captured.out
        assert "mock.provider" in captured.out

        # Verify receipt was recorded
        installed = installer.load_installed(target_dir)
        assert "mock.provider" in installed

        # Test uninstall CLI command
        rc_uninst = main(["uninstall", "mock.provider", "--target-dir", str(target_dir)])
        assert rc_uninst == 0
        captured_uninst = capsys.readouterr()
        assert "Successfully uninstalled plugin 'mock.provider'" in captured_uninst.out

        # Confirm uninstalled
        installed_after = installer.load_installed(target_dir)
        assert "mock.provider" not in installed_after


# ─────────────────────────────────────────────────────────────────────
# Test Group 4: Supervisor CLI Commands
# ─────────────────────────────────────────────────────────────────────


class TestSupervisorCliCommands:
    """Tests supervisor start, status, reload, and stop subcommands."""

    def test_supervisor_full_lifecycle(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Test full supervisor CLI lifecycle: start -> status -> reload -> stop."""
        plugin_id = "test.supervised.worker"
        target_dir = tmp_path / "cluster"
        target_dir.mkdir(parents=True, exist_ok=True)

        # 1. Query status before start
        rc_stat0 = main(["supervisor", "status", plugin_id, "--target-dir", str(target_dir)])
        assert rc_stat0 == 0
        captured = capsys.readouterr()
        assert "STOPPED" in captured.out

        # 2. Start supervisor worker
        rc_start = main(["supervisor", "start", plugin_id, "--target-dir", str(target_dir)])
        assert rc_start == 0
        captured_start = capsys.readouterr()
        assert f"Supervisor started worker for '{plugin_id}'" in captured_start.out
        assert "HEALTHY" in captured_start.out

        # 3. Query status while running
        rc_stat1 = main(["supervisor", "status", plugin_id, "--target-dir", str(target_dir)])
        assert rc_stat1 == 0
        captured_stat1 = capsys.readouterr()
        assert "Status=HEALTHY" in captured_stat1.out

        # 4. Graceful reload (hot-swap)
        rc_reload = main(["supervisor", "reload", plugin_id, "--target-dir", str(target_dir)])
        assert rc_reload == 0
        captured_reload = capsys.readouterr()
        assert f"Supervisor reloaded worker for '{plugin_id}'" in captured_reload.out
        assert "New PID:" in captured_reload.out

        # 5. Stop worker
        rc_stop = main(["supervisor", "stop", plugin_id, "--target-dir", str(target_dir)])
        assert rc_stop == 0
        captured_stop = capsys.readouterr()
        assert f"Supervisor stopped worker for '{plugin_id}'" in captured_stop.out

        # 6. Query status after stop
        rc_stat2 = main(["supervisor", "status", plugin_id, "--target-dir", str(target_dir)])
        assert rc_stat2 == 0
        captured_stat2 = capsys.readouterr()
        assert "STOPPED" in captured_stat2.out


# ─────────────────────────────────────────────────────────────────────
# Test Group 5: Executable Entry Point (cyrene_plugin_store.py)
# ─────────────────────────────────────────────────────────────────────


class TestExecutableEntryPoint:
    """Tests invoking tools/store/cyrene_plugin_store.py as an executable."""

    def test_executable_permissions(self) -> None:
        """Verify cyrene_plugin_store.py is marked executable (chmod +x)."""
        assert EXECUTABLE_PATH.is_file()
        assert os.access(EXECUTABLE_PATH, os.X_OK)

    def test_executable_help(self) -> None:
        """Verify executable runs with --help and outputs usage."""
        proc = subprocess.run(
            [str(EXECUTABLE_PATH), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "usage: cyrene-plugin-store" in proc.stdout
        assert "catalog" in proc.stdout
        assert "supervisor" in proc.stdout

    def test_executable_catalog_list_json(self) -> None:
        """Verify executable runs catalog list --json and returns valid JSON."""
        proc = subprocess.run(
            [str(EXECUTABLE_PATH), "catalog", "list", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert isinstance(data, list)
        assert len(data) == 17
        _assert_data_tools_plugin_metadata(data)
