#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 exercise_onebot_native_transports.py                             │
│  Module: tools.ci                                                   │
│  Role: Exercise packaged Native AOT OneBot transport profiles.       │
│                                                                     │
│  模块职责：验收已打包 Native AOT OneBot 的三种传输 profile 与事件订阅     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import select
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAPABILITY = "message.connector.v1"
SEND_METHOD = "send_message"
SEND_REQUEST_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest"
)
DELIVERY_RESULT_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult"
)
FILTER_TYPE_URL = "type.cyrene.io/cyrene.message.connector.v1.Filter"
MESSAGE_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.InboundMessagePayload"
)


def _parse_args() -> argparse.Namespace:
    """Parse a packaged binary and evidence destination.

        中文：解析已打包二进制文件和证据输出位置。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--rid", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    return parser.parse_args()


def _read_exact(connection: socket.socket, size: int) -> bytes:
    """Read exactly one bounded WebSocket field.

        中文：读取一个有界的 WebSocket 字段。
    """

    output = bytearray()
    while len(output) < size:
        chunk = connection.recv(size - len(output))
        if not chunk:
            raise ConnectionError("WebSocket peer closed during frame read")
        output.extend(chunk)
    return bytes(output)


def _read_headers(connection: socket.socket) -> bytes:
    """Read one bounded HTTP upgrade header block.

        中文：读取一段有界的 HTTP upgrade header。
    """

    output = bytearray()
    while b"\r\n\r\n" not in output:
        chunk = connection.recv(4096)
        if not chunk:
            raise ConnectionError("WebSocket peer closed during handshake")
        output.extend(chunk)
        if len(output) > 64 * 1024:
            raise ValueError("WebSocket handshake exceeds the 64 KiB limit")
    return bytes(output)


def _send_frame(
    connection: socket.socket,
    opcode: int,
    payload: bytes,
    *,
    mask: bool,
) -> None:
    """Send one small or extended WebSocket frame.

        中文：发送一个短帧或扩展长度的 WebSocket 帧。
    """

    if len(payload) > 16 * 1024 * 1024:
        raise ValueError("test WebSocket payload exceeds the 16 MiB limit")
    first = 0x80 | opcode
    if len(payload) < 126:
        header = bytes((first, (0x80 if mask else 0) | len(payload)))
    elif len(payload) <= 0xFFFF:
        header = bytes((first, (0x80 if mask else 0) | 126)) + len(payload).to_bytes(
            2, "big"
        )
    else:
        header = bytes((first, (0x80 if mask else 0) | 127)) + len(payload).to_bytes(
            8, "big"
        )
    if not mask:
        connection.sendall(header + payload)
        return
    mask_key = os.urandom(4)
    encoded = bytes(value ^ mask_key[index % 4] for index, value in enumerate(payload))
    connection.sendall(header + mask_key + encoded)


def _read_frame(connection: socket.socket) -> tuple[int, bytes]:
    """Read one WebSocket frame and unmask it when necessary.

        中文：读取一帧 WebSocket 数据，并在需要时解除掩码。
    """

    first, second = _read_exact(connection, 2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(_read_exact(connection, 2), "big")
    elif length == 127:
        length = int.from_bytes(_read_exact(connection, 8), "big")
    if length > 16 * 1024 * 1024:
        raise ValueError("test WebSocket frame exceeds the 16 MiB limit")
    masked = bool(second & 0x80)
    mask_key = _read_exact(connection, 4) if masked else b""
    payload = _read_exact(connection, length)
    if masked:
        payload = bytes(value ^ mask_key[index % 4] for index, value in enumerate(payload))
    return opcode, payload


class FakeOneBotWebSocketPeer:
    """Minimal real TCP/WebSocket peer for packaged transport acceptance.

        中文：用于已打包传输验收的精简真实 TCP／WebSocket 对端。
    """

    def __init__(self, *, server: bool) -> None:
        self._server_mode = server
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._connection: socket.socket | None = None
        self._server: socket.socket | None = None
        self._state_lock = threading.Lock()
        self._actions: queue.Queue[dict[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None
        if server:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(0.1)
            self._server = listener
            self.port = int(listener.getsockname()[1])
        else:
            self.port = 0

    def start(self) -> None:
        """Start the forward-WebSocket server peer.

            中文：启动 forward-WebSocket 服务端对端。
        """

        if not self._server_mode:
            raise RuntimeError("client peer must use connect")
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    @property
    def server_mode(self) -> bool:
        """Return whether this peer accepts a forward-WebSocket connection.

            中文：返回此对端是否接受 forward-WebSocket 连接。
        """

        return self._server_mode

    def connect(self, host: str, port: int) -> None:
        """Connect as a reverse-WebSocket client peer.

            中文：作为 reverse-WebSocket 客户端对端连接。
        """

        if self._server_mode:
            raise RuntimeError("server peer must use start")
        connection = socket.create_connection((host, port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET /onebot HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        connection.sendall(request)
        response = _read_headers(connection)
        if not response.startswith(b"HTTP/1.1 101 "):
            raise RuntimeError(f"reverse WebSocket handshake failed: {response!r}")
        self._activate(connection)

    def wait_ready(self, timeout: float = 5.0) -> None:
        """Wait until the protocol handshake and reader are active.

            中文：等待协议握手完成且 reader 已开始工作。
        """

        if not self._ready.wait(timeout):
            raise TimeoutError("fake OneBot WebSocket peer did not become ready")

    def send_event(self, payload: dict[str, Any]) -> None:
        """Send one OneBot inbound event to the Native AOT transport.

            中文：向 Native AOT 传输发送一条 OneBot 入站事件。
        """

        self.wait_ready()
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        with self._state_lock:
            connection = self._connection
        if connection is None:
            raise RuntimeError("fake WebSocket peer has no active connection")
        _send_frame(connection, 0x1, encoded, mask=not self._server_mode)

    def wait_action(self, timeout: float = 5.0) -> dict[str, Any]:
        """Wait for one correlated OneBot action from the Native AOT transport.

            中文：等待 Native AOT 传输发出一个有关联的 OneBot action。
        """

        try:
            return self._actions.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError("Native AOT transport did not send a WebSocket action") from error

    def close(self) -> None:
        """Stop the peer and close all owned sockets.

            中文：停止对端并关闭其拥有的所有 socket。
        """

        self._stop.set()
        for connection in (self._connection, self._server):
            if connection is None:
                continue
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                self._server_handshake(connection)
                self._activate(connection)
                return
            except (ConnectionError, OSError, RuntimeError, ValueError):
                connection.close()
                return

    def _server_handshake(self, connection: socket.socket) -> None:
        """Accept a RFC 6455 upgrade from a forward client.

            中文：接受来自 forward client 的 RFC 6455 upgrade。
        """

        headers = _read_headers(connection).decode("ascii", errors="replace")
        values: dict[str, str] = {}
        for line in headers.split("\r\n")[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            values[name.strip().lower()] = value.strip()
        key = values.get("sec-websocket-key")
        if key is None or values.get("upgrade", "").lower() != "websocket":
            raise RuntimeError("invalid forward WebSocket handshake")
        accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        connection.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )

    def _activate(self, connection: socket.socket) -> None:
        with self._state_lock:
            self._connection = connection
        self._ready.set()
        self._thread = threading.Thread(
            target=self._reader_loop,
            args=(connection,),
            daemon=True,
        )
        self._thread.start()

    def _reader_loop(self, connection: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                opcode, payload = _read_frame(connection)
            except (ConnectionError, OSError, ValueError):
                return
            if opcode == 0x9:
                _send_frame(connection, 0xA, payload, mask=not self._server_mode)
                continue
            if opcode == 0x8:
                return
            if opcode != 0x1:
                continue
            frame = json.loads(payload)
            if not isinstance(frame, dict) or "action" not in frame:
                continue
            self._actions.put(frame)
            response = {
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": "native-ws-1"},
                "echo": frame.get("echo"),
            }
            _send_frame(
                connection,
                0x1,
                json.dumps(response, separators=(",", ":")).encode("utf-8"),
                mask=not self._server_mode,
            )


def _read_ready(process: subprocess.Popen[str]) -> dict[str, Any]:
    """Read the bounded Native AOT readiness announcement.

        中文：读取有界的 Native AOT readiness 通告。
    """

    if process.stdout is None:
        raise RuntimeError("Native AOT stdout is not captured")
    stdout_fd = process.stdout.fileno()
    deadline = time.monotonic() + 15
    diagnostics: list[str] = []
    pending = b""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([stdout_fd], [], [], 0.1)
        if not ready:
            if process.poll() is not None:
                break
            continue
        chunk = os.read(stdout_fd, 4096)
        if not chunk:
            if process.poll() is not None:
                break
            continue
        pending += chunk
        while b"\n" in pending:
            raw_line, pending = pending.split(b"\n", 1)
            text = raw_line.decode("utf-8", errors="replace").strip()
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                diagnostics.append(text)
                continue
            if isinstance(value, dict) and value.get("event") == "direct_plugin_ready":
                return value
            diagnostics.append(text)
    stderr = ""
    if process.poll() is None:
        process.terminate()
    try:
        _, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        _, stderr = process.communicate(timeout=5)
    raise RuntimeError(
        "Native AOT host did not announce readiness: "
        f"stdout={diagnostics!r} stderr={stderr!r} returncode={process.returncode}"
    )


def _canonical_send_request(message_wire: Any) -> Any:
    """Build one canonical message shared by both WebSocket profiles.

        中文：构造一条由两种 WebSocket profile 共用的规范消息。
    """

    return message_wire.SendMessageRequest(
        conversation=message_wire.ConversationScope(
            vendor="onebot.v11",
            account_id="10001",
            conversation_id="20001",
            kind=message_wire.CONVERSATION_KIND_GROUP,
        ),
        content=[
            message_wire.MessageContentPart(
                text=message_wire.TextContent(text="native-ws")
            )
        ],
    )


def _message_event() -> dict[str, Any]:
    """Build one OneBot event for the subscription stream.

        中文：为订阅流构造一条 OneBot 事件。
    """

    return {
        "time": 1,
        "self_id": "10001",
        "post_type": "message",
        "message_type": "group",
        "message_id": "native-ws-event-1",
        "group_id": "20001",
        "sender": {"user_id": "10002", "nickname": "native-peer"},
        "message": [{"type": "text", "data": {"text": "from-native-ws"}}],
    }


def _subscription_request(runtime_wire: Any, request_id: str) -> Any:
    """Build one canonical direct-runtime subscription request.

        中文：构造一个规范 direct-runtime 订阅请求。
    """

    return runtime_wire.DirectInvocationRequest(
        capability=CAPABILITY,
        interface_version="1",
        method="events",
        payload_type_url=FILTER_TYPE_URL,
        payload=b"{}",
        request_id=request_id,
        stream_mode=runtime_wire.DIRECT_STREAM_MODE_SUBSCRIPTION,
    )


def _exercise_profile(
    binary: Path,
    profile_name: str,
    config: dict[str, Any],
    peer: FakeOneBotWebSocketPeer,
    runtime_wire: Any,
    runtime_wire_grpc: Any,
    message_wire: Any,
    grpc_module: Any,
) -> dict[str, Any]:
    """Exercise Health, subscription, event normalization, and action invoke.

        中文：依次验证 Health、订阅、事件规范化和 action 调用。
    """

    if peer.server_mode:
        peer.start()
    environment = os.environ.copy()
    environment["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)
    environment["CYRENE_CAPABILITY_BINDING_ID"] = config["binding_id"]
    process = subprocess.Popen(
        [str(binary), "--listen", "127.0.0.1:0"],
        cwd=binary.parent.parent,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    channel = None
    stream = None
    reader: threading.Thread | None = None
    try:
        announcement = _read_ready(process)
        connection_ref = announcement.get("connection_ref")
        if not isinstance(connection_ref, str) or not connection_ref.startswith("grpc://"):
            raise RuntimeError(f"invalid readiness announcement: {announcement}")
        channel = grpc_module.insecure_channel(connection_ref.removeprefix("grpc://"))
        grpc_module.channel_ready_future(channel).result(timeout=10)
        client = runtime_wire_grpc.DirectPluginRuntimeStub(channel)
        health = client.Health(runtime_wire.HealthRequest(), timeout=5)
        if health.status != runtime_wire.HealthResponse.STATUS_SERVING:
            raise RuntimeError(f"Native AOT host is not serving: {health}")

        if not peer.server_mode:
            peer.connect("127.0.0.1", int(config["reverse_listen_port"]))
        stream = client.InvokeStream(
            _subscription_request(runtime_wire, f"{profile_name}-subscription"),
            timeout=10,
        )
        events: queue.Queue[tuple[str, Any]] = queue.Queue()

        def consume() -> None:
            try:
                events.put(("item", next(stream)))
            except (
                grpc_module.RpcError,
                OSError,
                RuntimeError,
                StopIteration,
            ) as error:
                events.put(("error", error))

        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        peer.wait_ready()
        time.sleep(0.1)
        peer.send_event(_message_event())
        event_kind, event_item = events.get(timeout=5)
        if event_kind == "error":
            raise RuntimeError(f"subscription stream failed: {event_item}")
        if event_item.WhichOneof("event") != "payload":
            raise RuntimeError(f"subscription returned a non-payload item: {event_item}")
        if event_item.payload.type_url != MESSAGE_TYPE_URL:
            raise RuntimeError(f"unexpected event type URL: {event_item.payload.type_url}")
        inbound = message_wire.InboundMessagePayload.FromString(event_item.payload.value)
        if inbound.message_id != "native-ws-event-1":
            raise RuntimeError(f"unexpected normalized event: {inbound}")

        canonical = _canonical_send_request(message_wire)
        response = client.Invoke(
            runtime_wire.DirectInvocationRequest(
                capability=CAPABILITY,
                interface_version="1",
                method=SEND_METHOD,
                payload_type_url=SEND_REQUEST_TYPE_URL,
                payload=canonical.SerializeToString(),
                request_id=f"{profile_name}-send",
            ),
            timeout=10,
        )
        if response.WhichOneof("result") != "payload":
            raise RuntimeError(f"send_message failed: {response}")
        if response.payload.type_url != DELIVERY_RESULT_TYPE_URL:
            raise RuntimeError(f"unexpected delivery type URL: {response.payload.type_url}")
        delivery = message_wire.DeliveryResult.FromString(response.payload.value)
        if delivery.vendor_message_id != "native-ws-1":
            raise RuntimeError(f"unexpected delivery result: {delivery}")
        action = peer.wait_action()
        if action.get("action") != "send_group_msg":
            raise RuntimeError(f"unexpected OneBot action: {action}")
        if action.get("params", {}).get("group_id") != 20001:
            raise RuntimeError(f"unexpected action parameters: {action}")
        stream.cancel()
        return {
            "profile": profile_name,
            "connection_ref": connection_ref,
            "health": runtime_wire.HealthResponse.Status.Name(health.status),
            "event": {
                "type_url": event_item.payload.type_url,
                "message_id": inbound.message_id,
            },
            "invoke": {
                "method": SEND_METHOD,
                "action": action["action"],
                "result_type_url": response.payload.type_url,
                "vendor_message_id": delivery.vendor_message_id,
            },
        }
    finally:
        if stream is not None:
            stream.cancel()
        if reader is not None:
            reader.join(timeout=2)
        if channel is not None:
            channel.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        peer.close()


def main() -> int:
    """Run both WebSocket profiles against one packaged binary.

        中文：使用同一个已打包二进制运行两种 WebSocket profile。
    """

    args = _parse_args()
    binary = args.binary.resolve(strict=True)
    sys.path.insert(0, str(REPOSITORY_ROOT / "sdk/python/cyrene_plugin_runtime/src"))
    sys.path.insert(0, str(REPOSITORY_ROOT / "plugins/connectors/onebot-v11/src"))
    import grpc
    from cyrene_plugin_runtime._generated import (
        direct_plugin_runtime_pb2 as runtime_wire,
    )
    from cyrene_plugin_runtime._generated import (
        direct_plugin_runtime_pb2_grpc as runtime_wire_grpc,
    )
    from onebot_v11_connector._generated import message_connector_pb2 as message_wire

    forward_peer = FakeOneBotWebSocketPeer(server=True)
    forward = _exercise_profile(
        binary,
        "forward_websocket",
        {
            "binding_id": f"ci-{args.rid}-forward",
            "runtime_profile": "onebot-v11",
            "transport_profile": "forward_websocket",
            "websocket_url": f"ws://127.0.0.1:{forward_peer.port}/onebot",
            "self_account_id": "10001",
            "timeout_seconds": 5,
        },
        forward_peer,
        runtime_wire,
        runtime_wire_grpc,
        message_wire,
        grpc,
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        reverse_port = int(probe.getsockname()[1])
    reverse = _exercise_profile(
        binary,
        "reverse_websocket",
        {
            "binding_id": f"ci-{args.rid}-reverse",
            "runtime_profile": "onebot-v11",
            "transport_profile": "reverse_websocket",
            "reverse_listen_host": "127.0.0.1",
            "reverse_listen_port": reverse_port,
            "self_account_id": "10001",
            "timeout_seconds": 5,
        },
        FakeOneBotWebSocketPeer(server=False),
        runtime_wire,
        runtime_wire_grpc,
        message_wire,
        grpc,
    )
    evidence = {"rid": args.rid, "profiles": [forward, reverse]}
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
