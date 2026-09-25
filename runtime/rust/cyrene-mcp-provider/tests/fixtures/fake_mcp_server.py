#!/usr/bin/env python3
"""Test/dev fixture: minimal MCP stdio server over newline-delimited JSON-RPC.

Not a product: it exists so the Rust provider tests (and the plugin-server
integration tests) can exercise a real child process, real stdio framing, and
real tool payloads without external dependencies.

中文：测试/开发夹具：基于按行分隔 JSON-RPC 的最小 MCP 标准输入输出服务器。

中文：这不是产品代码；它用于让 Rust 提供方测试和 plugin-server 集成测试在没有外部依赖的情况下，执行真实子进程、真实标准输入输出分帧和真实工具载荷。
"""

from __future__ import annotations

import json
import os
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "slow",
        "description": "Sleep for the requested milliseconds, then answer",
        "inputSchema": {
            "type": "object",
            "properties": {"ms": {"type": "integer"}},
            "required": ["ms"],
        },
    },
    {
        "name": "fail",
        "description": "Report a tool-level failure",
        "inputSchema": {"type": "object"},
    },
]


def _result(request_id: object, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def handle(request: dict) -> dict | None:
    """Answer one JSON-RPC message; return None when no response is expected.

        中文：响应一条 JSON-RPC 消息；若无需响应则返回 None。"""

    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp-server", "version": "0.1.0"},
            },
        )
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(arguments.get("text", ""))}],
                    "isError": False,
                },
            )
        if name == "slow":
            time.sleep(float(arguments.get("ms", 0)) / 1000.0)
            return _result(
                request_id,
                {"content": [{"type": "text", "text": "slow done"}], "isError": False},
            )
        if name == "fail":
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": "tool failed on purpose"}],
                    "isError": True,
                },
            )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": f"unknown tool '{name}'"},
        }
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"unknown method '{method}'"},
    }


def main() -> int:
    """Serve newline-delimited JSON-RPC until stdin closes.

        中文：持续处理按行分隔的 JSON-RPC，直到标准输入关闭。"""

    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--pid-file" and len(arguments) > 1:
        with open(arguments[1], "w", encoding="utf-8") as handle_file:
            handle_file.write(str(os.getpid()))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response = handle(json.loads(line))
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
