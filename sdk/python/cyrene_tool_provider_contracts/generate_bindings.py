"""
┌─────────────────────────────────────────────────────────────────────────┐
│ 📄 generate_bindings.py                                                 │
│ Module: cyrene_tool_provider_contracts.generate_bindings                 │
│ Role: Regenerate the checked-in tool.provider.v1 Python binding.        │
│                                                                         │
│ 模块职责：按规范生成 tool.provider.v1 Python binding。                  │
└─────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    """Regenerate protobuf code with the shared runtime compiler version."""

    root = Path(__file__).resolve().parents[3]
    proto = root / "contracts/proto/cyrene/tool/provider/v1/tool_provider.proto"
    output = (
        root
        / "sdk/python/cyrene_tool_provider_contracts/src/cyrene_tool_provider_contracts/_generated/tool_provider_pb2.py"
    )
    with tempfile.TemporaryDirectory(prefix="cyrene-tool-provider-pb2-") as temporary:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "grpc_tools.protoc",
                f"--proto_path={proto.parent}",
                f"--python_out={temporary}",
                proto.name,
            ],
            cwd=proto.parent,
            check=True,
        )
        shutil.copyfile(Path(temporary) / "tool_provider_pb2.py", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
