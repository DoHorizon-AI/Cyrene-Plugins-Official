"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 bootstrap.py                                                    │
│  Module: cyrene_plugin_runtime                                      │
│  Role: Start the vendored direct runtime from an unpacked package.  │
│                                                                     │
│  模块职责：从已解包的 Plugin 包启动随包分发的直连运行时。                 │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

if __package__ in {None, ""}:
    # Executed by path, which puts this package directory on sys.path[0] and
    # would let a module inside the package shadow a standard-library module of
    # the same name (``cyrene_plugin_runtime/logging.py`` shadowing ``logging``).
    # Replace that entry with the source root the runtime expects.
    # 中文:按路径执行时,此软件包目录会进入 sys.path[0],使包内模块可能遮蔽同名标准库模块(例如 ``cyrene_plugin_runtime/logging.py`` 遮蔽 ``logging``)。# 中文:# 中文:将该路径项替换为运行时所需的源码根目录。
    package_dir = Path(__file__).resolve().parent
    source_root = str(package_dir.parent)
    normalized: list[str] = []
    for entry in sys.path:
        try:
            if Path(entry or ".").resolve() == package_dir:
                continue
        except OSError:
            pass
        normalized.append(entry)
    sys.path[:] = [source_root, *normalized]

main = import_module("cyrene_plugin_runtime.server").main


if __name__ == "__main__":
    raise SystemExit(main())
