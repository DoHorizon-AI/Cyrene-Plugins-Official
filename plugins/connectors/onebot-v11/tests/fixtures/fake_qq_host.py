"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 fake_qq_host.py                                                 │
│  Module: onebot_v11_connector.tests.fixtures.fake_qq_host           │
│  Role: Independently authored stdio counterpart for protocol tests.  │
│                                                                     │
│  模块职责：模拟授权 Host 的 stdio 行为，不代表真实 QQ 运行时。           │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _read_frame() -> dict[str, Any] | None:
    header = sys.stdin.buffer.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise RuntimeError("truncated header")
    size = int.from_bytes(header, "big")
    payload = sys.stdin.buffer.read(size)
    if len(payload) != size:
        raise RuntimeError("truncated payload")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("message is not an object")
    return value


def _write_frame(value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    sys.stdout.buffer.write(len(payload).to_bytes(4, "big") + payload)
    sys.stdout.buffer.flush()


def main() -> int:
    hello = _read_frame()
    if hello is None or hello.get("operation") != "hello":
        return 2
    params = hello.get("params", {})
    if not isinstance(params, dict):
        return 2
    binding_id = hello.get("binding_id")
    generation = hello.get("generation")
    client_version = params.get("required_client_version")
    report = {
        "protocol": params.get("protocol"),
        "protocol_version": params.get("protocol_version"),
        "binding_id": binding_id,
        "generation": generation,
        "platform": params.get("platform"),
        "client_version": client_version,
        "abi": "fake-qqnt-linux-x86_64",
    }
    _write_frame(
        {
            "type": "hello_ack",
            "request_id": hello.get("request_id"),
            "binding_id": binding_id,
            "generation": generation,
            "ok": True,
            "result": report,
        }
    )
    while True:
        message = _read_frame()
        if message is None:
            return 0
        message_type = message.get("type")
        if message_type == "shutdown":
            return 0
        if message_type == "cancel":
            continue
        if message_type != "request":
            return 2
        operation = message.get("operation")
        request_id = message.get("request_id")
        response = {
            "type": "response",
            "request_id": request_id,
            "binding_id": binding_id,
            "generation": generation,
            "ok": True,
            "result": {"operation": operation, "state": "ready", "account_id": "10001"},
        }
        if operation == "qq.message.send":
            response["result"] = {
                "message_id": f"{binding_id}-{generation}-message-1",
                "sequence": 7,
                "random": 11,
            }
        _write_frame(response)
        if operation == "qq.message.subscribe":
            _write_frame(
                {
                    "type": "event",
                    "event": "message.received",
                    "event_id": f"{binding_id}-{generation}-event-1",
                    "binding_id": binding_id,
                    "generation": generation,
                    "payload": {
                        "account_id": "10001",
                        "message_id": "native-message-1",
                        "peer": {
                            "kind": "group",
                            "peer_uid": "group-peer-1",
                            "group_code": "20001",
                        },
                        "sender": {
                            "uid": "uid-20002",
                            "uin": "20002",
                            "display_name": "member",
                        },
                        "sequence": 3,
                        "random": 5,
                        "timestamp": 1700000000,
                        "elements": [{"type": "text", "text": "hello from qq"}],
                    },
                }
            )


if __name__ == "__main__":
    raise SystemExit(main())
