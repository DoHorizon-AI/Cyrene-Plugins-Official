"""Regenerate the Python binding for the Plugins-owned direct runtime.

中文:重新生成 Plugins 持有的直连运行时 Python 绑定。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[2]
PROTO_ROOT = REPOSITORY_ROOT / "contracts/proto/cyrene/plugin/runtime/v1"
OUTPUT = PACKAGE_ROOT / "src/cyrene_plugin_runtime/_generated"


def main() -> int:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROTO_ROOT}",
            f"--python_out={OUTPUT}",
            f"--grpc_python_out={OUTPUT}",
            str(PROTO_ROOT / "direct_plugin_runtime.proto"),
        ],
        check=True,
    )
    grpc_binding = OUTPUT / "direct_plugin_runtime_pb2_grpc.py"
    source = grpc_binding.read_text(encoding="utf-8").replace(
        "import direct_plugin_runtime_pb2 as direct__plugin__runtime__pb2",
        "from . import direct_plugin_runtime_pb2 as direct__plugin__runtime__pb2",
    )
    grpc_binding.write_text(source, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
