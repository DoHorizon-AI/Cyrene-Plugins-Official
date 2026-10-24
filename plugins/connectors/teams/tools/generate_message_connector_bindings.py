"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 generate_message_connector_bindings.py                           │
│  Module: tools.generate_message_connector_bindings                   │
│  Role: Regenerate the Teams package's canonical message binding.     │
│                                                                      │
│  模块职责：重新生成 Teams 包的规范 message binding                      │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import importlib.metadata
import subprocess
import sys
from pathlib import Path

PROTO_RELATIVE_PATH = Path("proto/cyrene/message/connector/v1/message_connector.proto")
DEFAULT_CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "contracts"
OUTPUT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/teams_connector/_generated/message_connector_pb2.py"
)
GENERATOR_VERSION = "1.62.3"


def main() -> int:
    """Regenerate the binding from the repository-owned canonical proto.

    中文：从仓库持有的规范 proto 重新生成绑定。
    """

    parser = argparse.ArgumentParser()
    parser.add_argument("--contract-root", type=Path, default=DEFAULT_CONTRACT_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    if importlib.metadata.version("grpcio-tools") != GENERATOR_VERSION:
        parser.error(f"generation requires grpcio-tools=={GENERATOR_VERSION}")

    proto = args.contract_root.resolve() / PROTO_RELATIVE_PATH
    if not proto.is_file():
        parser.error("canonical message connector proto is missing")
    output = args.output.resolve()
    if output.name != "message_connector_pb2.py":
        parser.error("output filename must be message_connector_pb2.py")
    output.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            "-I",
            str(proto.parent),
            "--python_out",
            str(output.parent),
            str(proto),
        ],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
