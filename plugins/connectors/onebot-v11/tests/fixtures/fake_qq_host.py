"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 fake_qq_host.py                                                 │
│  Module: onebot_v11_connector.tests.fixtures.fake_qq_host           │
│  Role: Independently authored stdio counterpart for protocol TCKs.  │
│                                                                     │
│  模块职责：模拟授权 Host 的协议边界和故障模式，不代表真实 QQ 运行时。 │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

MAX_FRAME_BYTES = 8 * 1024 * 1024


def _mode() -> str:
    """Read a fixture-only mode from command-line arguments."""

    for argument in sys.argv[1:]:
        if argument.startswith("--mode="):
            return argument.partition("=")[2]
    return "normal"


def _operation_log() -> str | None:
    """Read an optional fixture-only operation log path."""

    for argument in sys.argv[1:]:
        if argument.startswith("--operation-log="):
            return argument.partition("=")[2]
    return os.environ.get("CYRENE_QQ_OPERATION_LOG")


def _read_frame() -> dict[str, Any] | None:
    """Read one bounded frame using only inherited stdin."""

    header = sys.stdin.buffer.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise RuntimeError("truncated header")
    size = int.from_bytes(header, "big")
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise RuntimeError("invalid frame length")
    payload = sys.stdin.buffer.read(size)
    if len(payload) != size:
        raise RuntimeError("truncated payload")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("message is not an object")
    return value


def _write_frame(value: dict[str, Any]) -> None:
    """Write one complete JSON frame to inherited stdout."""

    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(payload) > MAX_FRAME_BYTES:
        raise RuntimeError("response exceeds frame limit")
    sys.stdout.buffer.write(len(payload).to_bytes(4, "big") + payload)
    sys.stdout.buffer.flush()


def _response(
    request: dict[str, Any],
    binding_id: str,
    generation: int,
    *,
    mode: str,
) -> dict[str, Any]:
    """Build a deterministic response for one fixed operation."""

    operation = request.get("operation")
    params = request.get("params", {})
    result: dict[str, Any] = {
        "operation": operation,
        "state": "ready",
        "account_id": "10001",
    }
    if mode == "login_state" and operation == "qq.session.start_nt":
        result = {"operation": operation, "state": "login_required"}
    elif mode == "account_mismatch" and operation == "qq.session.start_nt":
        result = {"operation": operation, "state": "ready", "account_id": "10002"}
    elif operation == "qq.message.send":
        peer = params.get("peer", {}) if isinstance(params, dict) else {}
        result = {
            "message_id": f"{binding_id}-{generation}-message-1",
            "sequence": 7,
            "random": 11,
            "peer_uid": peer.get("peer_uid", "peer-1")
            if isinstance(peer, dict)
            else "peer-1",
        }
    elif mode == "media_file" and operation.startswith(("qq.media.", "qq.file.")):
        result = {
            "operation": operation,
            "state": "ready",
            "account_id": "10001",
            "element_id": "element-1",
            "file_id": "file-1",
            "media_id": "media-1",
            "local_result_reference": "qq://binding-local/media-1",
        }
    if mode == "login_state" and operation == "qq.login.quick":
        result = {
            "operation": operation,
            "state": "ready",
            "account_id": "10001",
        }
    if mode == "login_failed" and operation == "qq.login.quick":
        return {
            "type": "response",
            "request_id": request.get("request_id"),
            "binding_id": binding_id,
            "generation": generation,
            "ok": False,
            "error": {"code": "LOGIN_FAILED", "message": "fixture login failed"},
        }
    return {
        "type": "response",
        "request_id": request.get("request_id"),
        "binding_id": binding_id,
        "generation": generation,
        "ok": True,
        "result": result,
    }


def _message_event(binding_id: str, generation: int, event_id: str) -> dict[str, Any]:
    """Return one message event with distinct QQ identity values."""

    return {
        "type": "event",
        "event": "message.received",
        "event_id": event_id,
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


def _spawn_child() -> None:
    """Spawn a harmless long-lived helper to test process-group cleanup."""

    child = subprocess.Popen(["sleep", "60"])
    data_dir = os.environ.get("CYRENE_QQ_BINDING_DATA_DIR", ".")
    with open(
        os.path.join(data_dir, "fixture-child.pid"), "w", encoding="ascii"
    ) as file:
        file.write(str(child.pid))


def main() -> int:
    """Serve one fixture Host session until shutdown or a deliberate fault."""

    mode = _mode()
    hello = _read_frame()
    if hello is None or hello.get("operation") != "hello":
        return 2
    params = hello.get("params", {})
    if not isinstance(params, dict):
        return 2
    binding_id = hello.get("binding_id")
    generation = hello.get("generation")
    if not isinstance(binding_id, str) or not isinstance(generation, int):
        return 2
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
    if mode == "wrong_version":
        report["client_version"] = "unexpected-qq-build"
    elif mode == "wrong_abi":
        report["abi"] = "unexpected-qq-abi"
    elif mode == "missing_abi":
        report.pop("abi")
    elif mode == "wrong_binding":
        report["binding_id"] = f"{binding_id}-other"
    elif mode == "wrong_generation":
        report["generation"] = generation + 1
    listener: socket.socket | None = None
    if mode == "tcp_listener":
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
    if mode == "malformed_hello":
        _write_frame({"type": "hello_ack", "ok": True})
    else:
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
    if mode == "stderr_secret":
        sys.stderr.write("password=fixture-password token=fixture-token\n")
        sys.stderr.flush()
    if mode == "crash_once":
        marker = Path(os.environ.get("CYRENE_QQ_BINDING_DATA_DIR", ".")) / (
            "fixture-crash-once.marker"
        )
        if not marker.exists():
            marker.write_text("crashed\n", encoding="ascii")
            return 7
    if mode in {
        "crash_after_hello",
        "wrong_version",
        "wrong_binding",
        "wrong_generation",
        "malformed_hello",
    }:
        return 0 if mode != "crash_after_hello" else 7
    if mode == "spawn_child":
        _spawn_child()

    buffered: list[dict[str, Any]] = []
    while True:
        message = _read_frame()
        if message is None:
            return 0
        message_type = message.get("type")
        if message_type == "shutdown":
            if listener is not None:
                listener.close()
            return 0
        if message_type == "cancel":
            if mode in {"timeout", "cancel"}:
                # Deliberately late response: the parent must have removed the
                # request before sending cancel and ignore this frame.
                _write_frame(_response(message, binding_id, generation, mode=mode))
            continue
        if message_type != "request":
            return 2
        operation = message.get("operation")
        operation_log = _operation_log()
        if operation_log:
            with open(operation_log, "a", encoding="utf-8") as log:
                log.write(f"{operation}\n")
        if mode in {"timeout", "cancel"} and operation == "qq.group.detail":
            continue
        if mode == "out_of_order" and operation in {
            "qq.group.list",
            "qq.group.detail",
        }:
            buffered.append(message)
            if len(buffered) == 2:
                for pending in reversed(buffered):
                    _write_frame(_response(pending, binding_id, generation, mode=mode))
                buffered.clear()
            continue
        if mode == "subscribe_failed" and operation == "qq.message.subscribe":
            _write_frame(
                {
                    "type": "response",
                    "request_id": message.get("request_id"),
                    "binding_id": binding_id,
                    "generation": generation,
                    "ok": False,
                    "error": {
                        "code": "CAPABILITY_UNAVAILABLE",
                        "message": "fixture subscription failed",
                    },
                }
            )
            continue
        response = _response(message, binding_id, generation, mode=mode)
        _write_frame(response)
        if mode == "late_tcp_listener" and operation == "qq.group.list":
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
        if mode == "duplicate_response":
            _write_frame(response)
        if mode == "callbacks" and operation == "qq.message.send":
            send_result = response.get("result", {})
            _write_frame(
                {
                    "type": "event",
                    "event": "message.send_completion",
                    "event_id": f"{message.get('request_id')}-completion",
                    "request_id": message.get("request_id"),
                    "binding_id": binding_id,
                    "generation": generation,
                    "payload": {
                        "message_id": send_result.get("message_id"),
                        "sequence": send_result.get("sequence"),
                        "random": send_result.get("random"),
                        "peer_uid": send_result.get("peer_uid"),
                        "status": "completed",
                    },
                }
            )
        if mode == "media_file" and operation == "qq.media.download":
            media_result = response.get("result", {})
            _write_frame(
                {
                    "type": "event",
                    "event": "media.download_complete",
                    "event_id": f"{message.get('request_id')}-download-complete",
                    "request_id": message.get("request_id"),
                    "binding_id": binding_id,
                    "generation": generation,
                    "payload": {
                        "media_id": media_result.get("media_id"),
                        "file_id": media_result.get("file_id"),
                        "element_id": media_result.get("element_id"),
                        "local_result_reference": media_result.get(
                            "local_result_reference"
                        ),
                        "status": "completed",
                        "progress": 1.0,
                    },
                }
            )
        if operation == "qq.message.subscribe":
            event = _message_event(
                binding_id,
                generation,
                f"{binding_id}-{generation}-event-1",
            )
            _write_frame(event)
            if mode == "duplicate_event":
                _write_frame(event)
        if mode == "malformed_event" and operation == "qq.message.subscribe":
            _write_frame(
                {
                    "type": "event",
                    "event": "message.received",
                    "binding_id": binding_id,
                    "generation": generation,
                    "payload": {},
                }
            )


if __name__ == "__main__":
    raise SystemExit(main())
