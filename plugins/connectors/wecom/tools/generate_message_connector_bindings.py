#!/usr/bin/env python3
###############################################################################
# 📄 File: plugins/connectors/wecom/tools/generate_message_connector_bindings.py
# Module: Cyrene Plugins Official
# Role: Generate the WeCom connector's message.connector.v1 Python bindings.
#
# The capability schema and the generated consumer live in the same repository,
# so normal generation never checks out or reads Cyrene-Platform.
###############################################################################
# 中文:模块：Cyrene Plugins Official
# 职责：生成 WeCom 连接器的 message.connector.v1 Python 绑定。
# 能力架构与生成后的消费者位于同一仓库，常规生成不会检出 Cyrene-Platform。
"""Generate the WeCom connector binding from the Plugins-owned canonical proto.

中文:根据 Plugins 持有的规范 proto 生成 WeCom 连接器绑定。"""

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
    / "src/wecom_connector/_generated/message_connector_pb2.py"
)
TOOL_PROTO_RELATIVE_PATH = Path("proto/cyrene/tool/provider/v1/tool_provider.proto")
TOOL_OUTPUT_PATH = OUTPUT_PATH.with_name("tool_provider_pb2.py")
GENERATOR_VERSION = "1.62.3"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contract-root",
        type=Path,
        default=DEFAULT_CONTRACT_ROOT,
        help="Plugins contract root containing the canonical proto",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help="generated Python module destination",
    )
    parser.add_argument(
        "--tool-output",
        type=Path,
        default=TOOL_OUTPUT_PATH,
        help="canonical tool.provider.v1 generated module destination",
    )
    args = parser.parse_args()
    if importlib.metadata.version("grpcio-tools") != GENERATOR_VERSION:
        parser.error(f"binding generation requires grpcio-tools=={GENERATOR_VERSION}")
    # Match the declared protobuf 4.x runtime; a newer generator requires a
    # newer runtime and would make clean installations fail during import.
    # 中文：生成器与已声明的 protobuf 4.x 运行时保持兼容。
    for relative, destination in (
        (PROTO_RELATIVE_PATH, args.output),
        (TOOL_PROTO_RELATIVE_PATH, args.tool_output),
    ):
        proto = args.contract_root.resolve() / relative
        if not proto.is_file():
            parser.error(f"canonical proto does not exist: {proto}")
        output = destination.resolve()
        expected = proto.stem + "_pb2.py"
        if output.name != expected:
            parser.error(f"generated module must be named {expected}")
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            "-I",
            str(proto.parent),
            "--python_out",
            str(output.parent),
            str(proto),
        ]
        result = subprocess.run(command, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
