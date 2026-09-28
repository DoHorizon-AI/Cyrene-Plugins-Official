"""Pytest configuration for Cyrene-Plugins-Official tests.

中文: Cyrene 插件仓库测试通用配置。
"""

from __future__ import annotations

import sys
from pathlib import Path

# Prevent bytecode generation
sys.dont_write_bytecode = True

# Add repository root to sys.path so 'tools' can be imported
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
