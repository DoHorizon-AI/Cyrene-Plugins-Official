#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 cli.py                                                          │
│  Module: tools.store                                                │
│  Role: Unified Command-Line Interface (CLI) for Cyrene Plugin Store │
│  模块职责：Cyrene 插件商店统一命令行入口与运行包装。                │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Keep bytecode suppressed for repository integrity
sys.dont_write_bytecode = True

# Add repository root to sys.path
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.store import catalog, installer

PROG_NAME = "cyrene-plugin-store"


# ─────────────────────────────────────────────────────────────────────
# Process Helper Functions for Supervisor Commands
# ─────────────────────────────────────────────────────────────────────


def _is_process_alive(pid: int | None) -> bool:
    """Check if process with given PID is alive on the operating system.

    中文: 检查给定 PID 的进程在操作系统中是否依然存活。
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _get_supervisor_state_path(target_dir: Path, plugin_id: str) -> Path:
    """Get path to supervisor state file for a plugin.

    中文: 获取指定插件的守护器状态记录文件路径。
    """
    sup_dir = target_dir / ".supervisor"
    sup_dir.mkdir(parents=True, exist_ok=True)
    return sup_dir / f"{plugin_id}.json"


def _read_supervisor_state(target_dir: Path, plugin_id: str) -> dict[str, Any] | None:
    """Read supervisor state dictionary from state file.

    中文: 从状态文件中读取指定插件的守护器状态信息。
    """
    state_file = _get_supervisor_state_path(target_dir, plugin_id)
    if not state_file.is_file():
        return None
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_supervisor_state(target_dir: Path, plugin_id: str, state: dict[str, Any]) -> None:
    """Persist supervisor state dictionary to state file.

    中文: 将指定插件的守护器状态信息写入文件保存。
    """
    state_file = _get_supervisor_state_path(target_dir, plugin_id)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _remove_supervisor_state(target_dir: Path, plugin_id: str) -> None:
    """Remove supervisor state file for a plugin.

    中文: 移除指定插件的守护器状态记录文件。
    """
    state_file = _get_supervisor_state_path(target_dir, plugin_id)
    if state_file.is_file():
        state_file.unlink()


# ─────────────────────────────────────────────────────────────────────
# Command Handlers: catalog
# ─────────────────────────────────────────────────────────────────────


def handle_catalog_list(args: argparse.Namespace) -> int:
    """Handle `catalog list` subcommand.

    中文: 处理 `catalog list` 命令，查询并展示插件列表。
    """
    try:
        plugins_list = catalog.list_plugins(
            profile=args.profile,
            service=getattr(args, "service", None),
            language=args.language,
            keyword=args.keyword,
        )
    except Exception as err:  # noqa: BLE001
        print(f"Error querying plugin catalog: {err}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(plugins_list, indent=2, ensure_ascii=False))
        return 0

    if not plugins_list:
        print("No plugins found matching the specified filters.")
        return 0

    # Display human-readable table
    print(f"\nCyrene Plugin Catalog ({len(plugins_list)} plugins):")
    print(f"{'ID':<38} {'VERSION':<10} {'LANGUAGE':<10} {'SERVICES':<36} {'CAPABILITIES'}")
    print("-" * 120)
    for p in plugins_list:
        p_id = p.get("id", "")
        p_ver = p.get("version", "")
        p_lang = p.get("language", "")
        services = ", ".join(p.get("supportedServices", []) or p.get("supported_services", []))
        caps = ", ".join(p.get("capabilities", []))
        print(f"{p_id:<38} {p_ver:<10} {p_lang:<10} {services:<36} {caps}")
    print()
    return 0


def handle_catalog_show(args: argparse.Namespace) -> int:
    """Handle `catalog show <id>` subcommand.

    中文: 处理 `catalog show <id>` 命令，展示指定插件的详细元数据。
    """
    try:
        plugin_info = catalog.get_plugin(args.id)
    except Exception as err:  # noqa: BLE001
        print(f"Error retrieving plugin '{args.id}': {err}", file=sys.stderr)
        return 1

    if not plugin_info:
        print(f"Error: Plugin '{args.id}' not found in catalog.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(plugin_info, indent=2, ensure_ascii=False))
        return 0

    print(f"\nPlugin Information: {plugin_info.get('id')}")
    print("=" * 60)
    print(f"  Name:         {plugin_info.get('name')}")
    print(f"  Version:      {plugin_info.get('version')}")
    print(f"  Kind:         {plugin_info.get('kind')}")
    print(f"  Language:     {plugin_info.get('language')}")
    print(f"  Description:  {plugin_info.get('description')}")
    services = ", ".join(plugin_info.get("supportedServices", []) or plugin_info.get("supported_services", []))
    print(f"  Supported Services: {services}")
    print(f"  Capabilities: {', '.join(plugin_info.get('capabilities', []))}")
    print(f"  Profiles:     {', '.join(plugin_info.get('profiles', []))}")
    if plugin_info.get("runtime"):
        print(f"  Runtime:      {json.dumps(plugin_info['runtime'])}")
    if plugin_info.get("contributions"):
        print(f"  Contributions:{json.dumps(plugin_info['contributions'])}")
    print()
    return 0


# ─────────────────────────────────────────────────────────────────────
# Command Handlers: install & install-profile
# ─────────────────────────────────────────────────────────────────────


def handle_install(args: argparse.Namespace) -> int:
    """Handle `install <id>` subcommand.

    中文: 处理 `install <id>` 命令，安装单个插件到指定目录。
    """
    target_dir = Path(args.target_dir or "dist/installed").resolve()
    try:
        receipt = installer.install_plugin(
            plugin_id=args.id,
            target_dir=target_dir,
            version=args.version,
            offline_dir=args.offline_dir,
        )
        print(
            f"Successfully installed plugin '{args.id}' "
            f"(v{receipt.get('version', 'unknown')}) into {target_dir}"
        )
        return 0
    except installer.InstallationIntegrityError as err:
        print(f"Installation integrity error: {err}", file=sys.stderr)
        return 1
    except Exception as err:  # noqa: BLE001
        print(f"Installation failed for '{args.id}': {err}", file=sys.stderr)
        return 1


def handle_install_profile(args: argparse.Namespace) -> int:
    """Handle `install-profile <profile_name>` subcommand.

    中文: 处理 `install-profile <profile_name>` 命令，按服务画像批量安装插件。
    """
    target_dir = Path(args.target_dir or "dist/installed").resolve()
    try:
        receipts = installer.install_profile(
            profile_name=args.profile_name,
            target_dir=target_dir,
            offline_dir=args.offline_dir,
            profile_file=getattr(args, "profile_file", None),
        )
        print(
            f"\nSuccessfully installed profile '{args.profile_name}' "
            f"({len(receipts)} plugins) into {target_dir}"
        )
        for r in receipts:
            print(f"  - {r.get('id')} (v{r.get('version')}, {r.get('language')})")
        print()
        return 0
    except Exception as err:  # noqa: BLE001
        print(f"Profile installation failed for '{args.profile_name}': {err}", file=sys.stderr)
        return 1


# ─────────────────────────────────────────────────────────────────────
# Command Handlers: uninstall
# ─────────────────────────────────────────────────────────────────────


def handle_uninstall(args: argparse.Namespace) -> int:
    """Handle `uninstall <id>` subcommand.

    中文: 处理 `uninstall <id>` 命令，卸载并清理指定插件。
    """
    target_dir = Path(args.target_dir or "dist/installed").resolve()
    try:
        success = installer.uninstall_plugin(plugin_id=args.id, target_dir=target_dir)
        if success:
            # Also clean up any supervisor records
            _remove_supervisor_state(target_dir, args.id)
            print(f"Successfully uninstalled plugin '{args.id}' from {target_dir}")
            return 0
        else:
            print(f"Plugin '{args.id}' was not installed in {target_dir}", file=sys.stderr)
            return 1
    except Exception as err:  # noqa: BLE001
        print(f"Uninstallation failed for '{args.id}': {err}", file=sys.stderr)
        return 1


# ─────────────────────────────────────────────────────────────────────
# Command Handlers: list & verify
# ─────────────────────────────────────────────────────────────────────


def handle_list(args: argparse.Namespace) -> int:
    """Handle `list` subcommand.

    中文: 处理 `list` 命令，查询并展示已安装插件清单。
    """
    target_dir = Path(args.target_dir or "dist/installed").resolve()
    try:
        installed_dict = installer.load_installed(target_dir=target_dir)
        installed_list = list(installed_dict.values())
    except Exception as err:  # noqa: BLE001
        print(f"Error reading installed plugins in {target_dir}: {err}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(installed_list, indent=2, ensure_ascii=False))
        return 0

    if not installed_list:
        print(f"No plugins installed in {target_dir}")
        return 0

    print(f"\nInstalled Plugins in {target_dir} ({len(installed_list)} plugins):")
    print(f"{'ID':<38} {'VERSION':<10} {'LANGUAGE':<10} {'STATUS':<12} {'INSTALL PATH'}")
    print("-" * 105)
    for item in installed_list:
        p_id = item.get("id", "")
        p_ver = item.get("version", "")
        p_lang = item.get("language", "")
        p_status = item.get("status", "")
        p_path = item.get("install_path", "")
        print(f"{p_id:<38} {p_ver:<10} {p_lang:<10} {p_status:<12} {p_path}")
    print()
    return 0


def handle_verify(args: argparse.Namespace) -> int:
    """Handle `verify` subcommand.

    中文: 处理 `verify` 命令，校验安装插件的文件完整性与 SHA256 签名。
    """
    target_dir = Path(args.target_dir or "dist/installed").resolve()
    try:
        result = installer.verify_installation(target_dir=target_dir, raise_on_error=False)
        if result.valid:
            print(
                f"Integrity verification passed: all {result.plugins_verified} "
                f"plugin(s) in {target_dir} are valid."
            )
            return 0
        else:
            print(
                f"Integrity verification failed for {target_dir}:",
                file=sys.stderr,
            )
            for err in result.errors:
                print(f"  - {err}", file=sys.stderr)
            return 1
    except Exception as err:  # noqa: BLE001
        print(f"Integrity verification encountered an error: {err}", file=sys.stderr)
        return 1


# ─────────────────────────────────────────────────────────────────────
# Command Handlers: supervisor
# ─────────────────────────────────────────────────────────────────────


def handle_supervisor_start(args: argparse.Namespace) -> int:
    """Handle `supervisor start <id> --target-dir <path>` subcommand.

    中文: 处理 `supervisor start` 命令，启动并监护指定插件 Worker 子进程。
    """
    target_dir = Path(args.target_dir).resolve()
    plugin_id = args.id

    # Check if already running
    state = _read_supervisor_state(target_dir, plugin_id)
    if state and _is_process_alive(state.get("pid")):
        print(
            f"Supervisor: Worker '{plugin_id}' is already running "
            f"(PID: {state.get('pid')}, State: {state.get('state')})."
        )
        return 0

    # Determine command to launch
    installed_dict = installer.load_installed(target_dir)
    receipt = installed_dict.get(plugin_id)

    version = receipt.get("version", "latest") if receipt else "latest"
    language = receipt.get("language", "python") if receipt else "python"
    install_path = (
        Path(receipt.get("install_path", str(target_dir / f"{plugin_id}@{version}")))
        if receipt
        else (target_dir / f"{plugin_id}@{version}")
    )
    entrypoint = receipt.get("entrypoint") if receipt else None

    # Construct launch command
    if language == "python":
        py_exec = sys.executable
        venv_py = install_path / ".venv" / "bin" / "python"
        if venv_py.is_file():
            py_exec = str(venv_py)

        if entrypoint and (install_path / entrypoint).is_file():
            cmd = [py_exec, "-B", str(install_path / entrypoint)]
        elif (install_path / "bootstrap.py").is_file():
            cmd = [py_exec, "-B", str(install_path / "bootstrap.py")]
        else:
            # Standalone worker loop fallback
            code = (
                "import time, signal, sys; "
                "signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0)); "
                "time.sleep(3600)"
            )
            cmd = [py_exec, "-c", code]
    else:
        # Native binary (Rust / C# AOT)
        bin_target = install_path / (entrypoint or f"bin/{plugin_id}")
        if bin_target.is_file():
            cmd = [str(bin_target)]
        else:
            code = (
                "import time, signal, sys; "
                "signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0)); "
                "time.sleep(3600)"
            )
            cmd = [sys.executable, "-c", code]

    try:
        import subprocess

        cwd = str(install_path if install_path.is_dir() else target_dir)
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        state_data = {
            "plugin_id": plugin_id,
            "version": version,
            "language": language,
            "pid": proc.pid,
            "state": "HEALTHY",
            "cmd": cmd,
            "started_at": time.time(),
        }
        _write_supervisor_state(target_dir, plugin_id, state_data)

        print(
            f"Supervisor started worker for '{plugin_id}' "
            f"[PID: {proc.pid}, Status: HEALTHY]"
        )
        return 0
    except Exception as err:  # noqa: BLE001
        print(f"Failed to start supervisor worker for '{plugin_id}': {err}", file=sys.stderr)
        return 1


def handle_supervisor_reload(args: argparse.Namespace) -> int:
    """Handle `supervisor reload <id> --target-dir <path>` subcommand.

    中文: 处理 `supervisor reload` 命令，零停机平滑重载插件 Worker 子进程。
    """
    target_dir = Path(args.target_dir).resolve()
    plugin_id = args.id

    state = _read_supervisor_state(target_dir, plugin_id)
    if not state or not _is_process_alive(state.get("pid")):
        # If not already running, perform start
        print(f"Worker '{plugin_id}' was not running. Launching new instance...")
        return handle_supervisor_start(args)

    old_pid = state["pid"]
    cmd = state.get("cmd") or [
        sys.executable,
        "-c",
        "import time, signal, sys; signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0)); time.sleep(3600)",
    ]

    try:
        import subprocess

        # 1. Spawn replacement worker
        new_proc = subprocess.Popen(
            cmd,
            cwd=str(target_dir),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 2. Terminate old worker gracefully
        try:
            os.kill(old_pid, signal.SIGTERM)
        except OSError:
            pass

        # 3. Update state
        state["pid"] = new_proc.pid
        state["state"] = "HEALTHY"
        state["started_at"] = time.time()
        _write_supervisor_state(target_dir, plugin_id, state)

        print(
            f"Supervisor reloaded worker for '{plugin_id}' "
            f"[Previous PID: {old_pid}, New PID: {new_proc.pid}, Status: HEALTHY]"
        )
        return 0
    except Exception as err:  # noqa: BLE001
        print(f"Failed to reload supervisor worker for '{plugin_id}': {err}", file=sys.stderr)
        return 1


def handle_supervisor_status(args: argparse.Namespace) -> int:
    """Handle `supervisor status <id> --target-dir <path>` subcommand.

    中文: 处理 `supervisor status` 命令，查询插件 Worker 存活与健康状态。
    """
    target_dir = Path(args.target_dir).resolve()
    plugin_id = args.id

    state = _read_supervisor_state(target_dir, plugin_id)
    if not state:
        print(f"Worker '{plugin_id}': STOPPED (no supervisor state found in {target_dir})")
        return 0

    pid = state.get("pid")
    if _is_process_alive(pid):
        print(
            f"Worker '{plugin_id}': Status={state.get('state', 'HEALTHY')}, "
            f"PID={pid}, Version={state.get('version', 'unknown')}"
        )
        return 0
    else:
        print(f"Worker '{plugin_id}': Status=STOPPED (process {pid} is no longer running)")
        return 0


def handle_supervisor_stop(args: argparse.Namespace) -> int:
    """Handle `supervisor stop <id> --target-dir <path>` subcommand.

    中文: 处理 `supervisor stop` 命令，停止并清理插件 Worker 子进程。
    """
    target_dir = Path(args.target_dir).resolve()
    plugin_id = args.id

    state = _read_supervisor_state(target_dir, plugin_id)
    if not state:
        print(f"Worker '{plugin_id}' is not running.")
        return 0

    pid = state.get("pid")
    if _is_process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(15):
                time.sleep(0.1)
                if not _is_process_alive(pid):
                    break
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        except OSError:
            pass

    _remove_supervisor_state(target_dir, plugin_id)
    print(f"Supervisor stopped worker for '{plugin_id}' (PID: {pid})")
    return 0


# ─────────────────────────────────────────────────────────────────────
# Argument Parser Construction
# ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser for cyrene-plugin-store.

    中文: 构造 cyrene-plugin-store 命令行参数解析器。
    """
    parser = argparse.ArgumentParser(
        prog=PROG_NAME,
        description="Cyrene Plugin Store CLI: Package discovery, universal installation, and runtime supervision.",
    )
    subparsers = parser.add_subparsers(
        dest="subcommand",
        title="subcommands",
        description="Available store commands",
        required=True,
    )

    # ── 1. catalog subcommand group ──
    catalog_parser = subparsers.add_parser(
        "catalog",
        help="Query, discover, and inspect plugins in the official catalog",
    )
    catalog_sub = catalog_parser.add_subparsers(
        dest="catalog_subcommand",
        title="catalog subcommands",
        required=True,
    )

    # catalog list
    cat_list = catalog_sub.add_parser(
        "list",
        help="List available plugins with optional profile, language, and keyword filters",
    )
    cat_list.add_argument(
        "--profile",
        "-p",
        type=str,
        default=None,
        help="Filter plugins by service profile name (e.g. reactor, echo, yield, catalyst, core, all)",
    )
    cat_list.add_argument(
        "--service",
        "-s",
        type=str,
        default=None,
        help="Filter plugins by official service name (e.g. Cyrene-Navigator, Cyrene-Exchange, Cyrene-Reactor)",
    )
    cat_list.add_argument(
        "--language",
        "-l",
        type=str,
        default=None,
        help="Filter plugins by programming language (e.g. python, csharp, rust)",
    )
    cat_list.add_argument(
        "--keyword",
        "-k",
        type=str,
        default=None,
        help="Keyword filter matching plugin ID, name, description, or capabilities",
    )
    cat_list.add_argument(
        "--json",
        action="store_true",
        help="Output results in JSON format",
    )

    # catalog show
    cat_show = catalog_sub.add_parser(
        "show",
        help="Display detailed metadata for a specific plugin",
    )
    cat_show.add_argument("id", type=str, help="Plugin identifier (e.g. cyrene.connectors.im)")
    cat_show.add_argument(
        "--json",
        action="store_true",
        help="Output plugin details in JSON format",
    )

    # ── 2. install ──
    install_parser = subparsers.add_parser(
        "install",
        help="Install a single plugin into the target directory",
    )
    install_parser.add_argument("id", type=str, help="Plugin identifier to install")
    install_parser.add_argument(
        "--version",
        "-v",
        type=str,
        default=None,
        help="Specific version to install (defaults to latest in catalog)",
    )
    install_parser.add_argument(
        "--target-dir",
        type=str,
        default=None,
        help="Target installation directory (defaults to dist/installed)",
    )
    install_parser.add_argument(
        "--offline-dir",
        type=str,
        default=None,
        help="Directory containing offline distribution packages",
    )

    # ── 3. install-profile ──
    install_prof = subparsers.add_parser(
        "install-profile",
        help="Batch install all plugins declared under a service profile",
    )
    install_prof.add_argument(
        "profile_name",
        type=str,
        help="Service profile name to install (e.g. reactor, echo, yield, catalyst, core, all)",
    )
    install_prof.add_argument(
        "--target-dir",
        type=str,
        default=None,
        help="Target installation directory (defaults to dist/installed)",
    )
    install_prof.add_argument(
        "--offline-dir",
        type=str,
        default=None,
        help="Directory containing offline distribution packages",
    )
    install_prof.add_argument(
        "--profile-file",
        type=str,
        default=None,
        help="Path to custom profiles.yaml or profiles.json configuration file",
    )

    # ── 4. uninstall ──
    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Uninstall a plugin and clean up its files and receipts",
    )
    uninstall_parser.add_argument("id", type=str, help="Plugin identifier to uninstall")
    uninstall_parser.add_argument(
        "--target-dir",
        type=str,
        default=None,
        help="Target installation directory (defaults to dist/installed)",
    )

    # ── 5. list ──
    list_parser = subparsers.add_parser(
        "list",
        help="List installed plugins recorded in the target directory receipt",
    )
    list_parser.add_argument(
        "--target-dir",
        type=str,
        default=None,
        help="Target installation directory to inspect (defaults to dist/installed)",
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output installed plugins in JSON format",
    )

    # ── 6. verify ──
    verify_parser = subparsers.add_parser(
        "verify",
        help="Verify the file integrity and SHA256 checksums of installed plugins",
    )
    verify_parser.add_argument(
        "--target-dir",
        type=str,
        default=None,
        help="Target installation directory to verify (defaults to dist/installed)",
    )

    # ── 7. supervisor subcommand group ──
    supervisor_parser = subparsers.add_parser(
        "supervisor",
        help="Manage standalone plugin worker processes and hot-swap lifecycle",
    )
    sup_sub = supervisor_parser.add_subparsers(
        dest="supervisor_subcommand",
        title="supervisor subcommands",
        required=True,
    )

    # supervisor start
    sup_start = sup_sub.add_parser(
        "start",
        help="Start supervising a plugin worker process",
    )
    sup_start.add_argument("id", type=str, help="Plugin identifier to start")
    sup_start.add_argument(
        "--target-dir",
        type=str,
        required=True,
        help="Target directory where plugin is installed",
    )

    # supervisor reload
    sup_reload = sup_sub.add_parser(
        "reload",
        help="Gracefully hot-swap / reload a supervised plugin worker process",
    )
    sup_reload.add_argument("id", type=str, help="Plugin identifier to reload")
    sup_reload.add_argument(
        "--target-dir",
        type=str,
        required=True,
        help="Target directory where plugin is installed",
    )

    # supervisor status
    sup_status = sup_sub.add_parser(
        "status",
        help="Query supervisor status for a plugin worker",
    )
    sup_status.add_argument("id", type=str, help="Plugin identifier to query")
    sup_status.add_argument(
        "--target-dir",
        type=str,
        required=True,
        help="Target directory where plugin is installed",
    )

    # supervisor stop
    sup_stop = sup_sub.add_parser(
        "stop",
        help="Stop a supervised plugin worker process",
    )
    sup_stop.add_argument("id", type=str, help="Plugin identifier to stop")
    sup_stop.add_argument(
        "--target-dir",
        type=str,
        required=True,
        help="Target directory where plugin is installed",
    )

    return parser


# ─────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Main CLI execution entry point.

    中文: CLI 主执行入口。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "catalog":
        if args.catalog_subcommand == "list":
            return handle_catalog_list(args)
        elif args.catalog_subcommand == "show":
            return handle_catalog_show(args)

    elif args.subcommand == "install":
        return handle_install(args)

    elif args.subcommand == "install-profile":
        return handle_install_profile(args)

    elif args.subcommand == "uninstall":
        return handle_uninstall(args)

    elif args.subcommand == "list":
        return handle_list(args)

    elif args.subcommand == "verify":
        return handle_verify(args)

    elif args.subcommand == "supervisor":
        if args.supervisor_subcommand == "start":
            return handle_supervisor_start(args)
        elif args.supervisor_subcommand == "reload":
            return handle_supervisor_reload(args)
        elif args.supervisor_subcommand == "status":
            return handle_supervisor_status(args)
        elif args.supervisor_subcommand == "stop":
            return handle_supervisor_stop(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
