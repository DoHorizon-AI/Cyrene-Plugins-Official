"""
┌─────────────────────────────────────────────────────────────────────────┐
│ 📄 generate_python_contract_bindings.py                                  │
│ Module: tools.ci.generate_python_contract_bindings                        │
│ Role: Generate temporary Python projections from canonical Plugins proto.│
│                                                                         │
│ 模块职责：从规范 Plugins proto 生成临时 Python binding 用于 TCK。         │
└─────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROTO_ROOT = REPOSITORY_ROOT / "contracts" / "proto"
CONTRACTS = (
    "cyrene/memory/provider/v1/memory_provider.proto",
    "cyrene/model/provider/v1/model_provider.proto",
    "cyrene/knowledge/graph/v1/knowledge_graph.proto",
    "cyrene/speech/provider/v1/speech_provider.proto",
)


def main() -> int:
    """Generate Python protobuf classes beneath the required output folder."""

    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"--proto_path={PROTO_ROOT}",
        f"--python_out={args.output.resolve()}",
        *(str(PROTO_ROOT / proto) for proto in CONTRACTS),
    ]
    subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
