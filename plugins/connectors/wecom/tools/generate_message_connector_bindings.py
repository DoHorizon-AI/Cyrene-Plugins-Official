#!/usr/bin/env python3
###############################################################################
# 📄 File: plugins/connectors/wecom/tools/generate_message_connector_bindings.py
# Module: Cyrene Plugins Official
# Role: Generate the WeCom connector's message.connector.v1 Python bindings.
#
# The capability schema and the generated consumer live in the same repository,
# so normal generation never checks out or reads Cyrene-Platform.
###############################################################################
"""Generate the WeCom connector binding from the Plugins-owned canonical proto."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROTO_RELATIVE_PATH = Path("proto/cyrene/message/connector/v1/message_connector.proto")
DEFAULT_CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "contracts"
OUTPUT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/wecom_connector/_generated/message_connector_pb2.py"
)


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
    args = parser.parse_args()

    proto = args.contract_root.resolve() / PROTO_RELATIVE_PATH
    if not proto.is_file():
        parser.error(f"canonical proto does not exist: {proto}")
    output = args.output.resolve()
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
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
