"""
┌─────────────────────────────────────────────────────────────────────────┐
│ 📄 generate_bindings.py                                                 │
│ Module: knowledge.graph.v1 Python TCK                                    │
│ Role: Generate independent bindings and run the graph SDK TCK.          │
│                                                                         │
│ 模块职责：生成独立 binding 并运行图谱 SDK TCK。                         │
└─────────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    """Generate temporary proto classes and run the cross-codec TCK."""

    root = Path(__file__).resolve().parents[4]
    with tempfile.TemporaryDirectory(prefix="cyrene-graph-tck-") as temporary:
        output = Path(temporary) / "generated"
        subprocess.run(
            [sys.executable, str(root / "tools/ci/generate_python_contract_bindings.py"), str(output)],
            cwd=root,
            check=True,
        )
        subprocess.run(
            [sys.executable, str(Path(__file__).with_name("run_tck.py")), str(output)],
            cwd=root,
            check=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
