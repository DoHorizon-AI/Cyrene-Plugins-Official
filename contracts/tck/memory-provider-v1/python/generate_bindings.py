"""
┌─────────────────────────────────────────────────────────────────────────┐
│ 📄 generate_bindings.py                                                 │
│ Module: memory.provider.v1 Python TCK                                    │
│ Role: Generate temporary canonical bindings and run the SDK TCK.         │
│                                                                         │
│ 模块职责：临时生成规范 Python binding 并运行 SDK 交叉一致性测试。        │
└─────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    """Generate canonical messages into a temporary directory and verify them."""

    repo = Path(__file__).resolve().parents[4]
    proto_root = repo / "contracts/proto"
    proto = proto_root / "cyrene/memory/provider/v1/memory_provider.proto"
    with tempfile.TemporaryDirectory(prefix="cyrene-memory-tck-") as temporary:
        generated = Path(temporary)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "grpc_tools.protoc",
                f"--proto_path={proto_root}",
                f"--python_out={generated}",
                str(proto),
            ],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("run_tck.py")),
                str(generated),
            ],
            cwd=repo,
            check=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
