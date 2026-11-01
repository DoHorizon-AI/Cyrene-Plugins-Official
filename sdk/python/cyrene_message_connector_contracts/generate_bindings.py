"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 generate_bindings.py                                             │
│  Module: cyrene_message_connector_contracts                          │
│  Role: Generate consumer bindings from the frozen connector proto.   │
│                                                                      │
│  模块职责：从冻结的 connector proto 生成消费者绑定。                 │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _PACKAGE_ROOT.parents[2]
_DEFAULT_PROTO = (
    _REPOSITORY_ROOT
    / "contracts/proto/cyrene/message/connector/v1/message_connector.proto"
)
_DEFAULT_OUTPUT = (
    _PACKAGE_ROOT
    / "src/cyrene_message_connector_contracts/_generated/message_connector_pb2.py"
)


def generate(proto: Path = _DEFAULT_PROTO, output: Path = _DEFAULT_OUTPUT) -> int:
    """Generate one Python binding with the canonical descriptor filename.

    中文：使用协议所在目录作为 include root，保持 descriptor 文件名稳定。
    """

    canonical_proto = proto.resolve(strict=True)
    destination = output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        "-I",
        str(canonical_proto.parent),
        "--python_out",
        str(destination.parent),
        str(canonical_proto),
    ]
    result = subprocess.run(command, check=False)
    generated = destination.parent / f"{canonical_proto.stem}_pb2.py"
    if result.returncode == 0 and generated != destination:
        os.replace(generated, destination)
    return result.returncode


def main(arguments: list[str] | None = None) -> int:
    """Parse optional source/output paths and run protoc."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proto", type=Path, default=_DEFAULT_PROTO)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    options = parser.parse_args(arguments)
    return generate(options.proto, options.output)


if __name__ == "__main__":
    raise SystemExit(main())
