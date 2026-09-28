"""Cyrene Plugin Store tooling package.

中文: Cyrene 插件商店工具包。
"""

from __future__ import annotations

import sys

# Suppress bytecode generation
sys.dont_write_bytecode = True

from .catalog import (
    build_catalog,
    get_plugin,
    get_profile_plugins,
    list_plugins,
    load_catalog,
)
from .cli import build_parser, main
from .installer import (
    InstallationIntegrityError,
    install_plugin,
    install_profile,
    load_installed,
    uninstall_plugin,
    verify_installation,
)
from .profiles import get_plugins_for_profile, get_profiles_for_plugin, load_profiles
from .supervisor import (
    CrashReport,
    HealthCheckConfig,
    HealthCheckKind,
    HotSwapError,
    ProcessSupervisor,
    SupervisorError,
    WorkerProcess,
    WorkerSpec,
    WorkerState,
)

__all__ = [
    "CrashReport",
    "HealthCheckConfig",
    "HealthCheckKind",
    "HotSwapError",
    "InstallationIntegrityError",
    "ProcessSupervisor",
    "SupervisorError",
    "WorkerProcess",
    "WorkerSpec",
    "WorkerState",
    "build_catalog",
    "build_parser",
    "get_plugin",
    "get_plugins_for_profile",
    "get_profile_plugins",
    "get_profiles_for_plugin",
    "install_plugin",
    "install_profile",
    "list_plugins",
    "load_catalog",
    "load_installed",
    "load_profiles",
    "main",
    "uninstall_plugin",
    "verify_installation",
]
