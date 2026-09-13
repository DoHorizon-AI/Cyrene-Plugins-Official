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
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

main = import_module("cyrene_plugin_runtime.server").main


if __name__ == "__main__":
    raise SystemExit(main())
