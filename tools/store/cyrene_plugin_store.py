#!/usr/bin/env python3
"""Cyrene Plugin Store CLI Executable Entry Point.

中文: Cyrene 插件商店统一命令行可执行入口。
"""

from __future__ import annotations

import sys
from pathlib import Path

# Suppress bytecode generation
sys.dont_write_bytecode = True

# Ensure repository root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.store.cli import main

if __name__ == "__main__":
    sys.exit(main())
