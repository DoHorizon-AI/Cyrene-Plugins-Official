###############################################################################
# 📄 File: plugins/connectors/onebot-v11/tests/test_transport.py
# Module: Cyrene Plugins Official
# Role: Focused automated test coverage.
#
# This file documents an active plugin boundary; runtime behavior is unchanged.
#
# 模块：Cyrene Plugins Official
# 职责：聚焦的自动化测试覆盖。
# 本文件属于活动插件边界；运行时行为保持不变。
###############################################################################
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import queue
import socket
import threading
import time
from collections.abc import Mapping
from typing import Any

import pytest

from onebot_v11_connector import (
    CAPABILITY_ID,
    INBOUND_MESSAGE_EVENT_TYPE,
    INBOUND_MESSAGE_TYPE_URL,
    ConnectorError,
    OneBotV11Connector,
)
from onebot_v11_connector._generated import (
    message_connector_pb2 as message_contract,
)


class RecordingEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, bytes, str]] = []
        self._lock = threading.Lock()

    def emit(self, event_type: str, payload: bytes, type_url: str = "") -> bool:
        with self._lock:
            self.events.append((event_type, payload, type_url))
        return True


class Cancellation:
    def __init__(self) -> None:
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled


class FakeOneBotPeer:
    """A real TCP/WebSocket peer for transport tests, not a connector fake."""

    def __init__(self, access_token: str | None = "token") -> None:
        self.access_token = access_token
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self._server.settimeout(0.1)
        self.port = int(self._server.getsockname()[1])
        self._stop = threading.Event()
        self._connections: queue.Queue[socket.socket] = queue.Queue()
        self._current: socket.socket | None = None
        self._all_connections: list[socket.socket] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def wait_connection(self, timeout: float = 2.0) -> socket.socket:
        connection = self._connections.get(timeout=timeout)
        with self._lock:
            self._current = connection
        return connection

    def send_json(
        self, message: Mapping[str, Any], connection: socket.socket | None = None
    ) -> None:
        connection = connection or self._current_connection()
        _send_frame(connection, 0x1, json.dumps(dict(message)).encode())

    def send_ping(self, connection: socket.socket | None = None) -> None:
        connection = connection or self._current_connection()
        _send_frame(connection, 0x9, b"heartbeat")

    def receive_frame(
        self, connection: socket.socket | None = None, timeout: float = 2.0
    ) -> tuple[int, bytes]:
        connection = connection or self._current_connection()
        connection.settimeout(timeout)
        return _read_frame(connection)

    def drop(self, connection: socket.socket | None = None) -> None:
        connection = connection or self._current_connection()
        _close_socket(connection)
        with self._lock:
            if self._current is connection:
                self._current = None

    def close(self) -> None:
        self._stop.set()
        _close_socket(self._server)
        with self._lock:
            connections = list(self._all_connections)
            self._all_connections.clear()
            self._current = None
        for connection in connections:
            _close_socket(connection)
        self._thread.join(timeout=1.0)

    def _current_connection(self) -> socket.socket:
        with self._lock:
            connection = self._current
        if connection is None:
            raise AssertionError("fake OneBot peer has no active connection")
        return connection

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                connection.settimeout(2.0)
                request = _read_http_headers(connection)
                if (
                    self.access_token is not None
                    and request.get("authorization") != f"Bearer {self.access_token}"
                ):
                    connection.sendall(
                        b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n"
                    )
                    _close_socket(connection)
                    continue
                key = request.get("sec-websocket-key")
                if not key:
                    _close_socket(connection)
                    continue
                accept = base64.b64encode(
                    hashlib.sha1(
                        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                    ).digest()
                ).decode()
                connection.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode()
                )
                with self._lock:
                    self._all_connections.append(connection)
                self._connections.put(connection)
            except (OSError, AssertionError):
                _close_socket(connection)


def test_forward_websocket_connects_parses_events_and_correlates_actions() -> None:
    peer = FakeOneBotPeer()
    connector = OneBotV11Connector(
        _config(peer, "qq-main", "10001", timeout_seconds=1.0)
    )
    emitter = RecordingEmitter()
    try:
        assert connector.on_subscribe("main", CAPABILITY_ID, b"{}", emitter) is None
        connection = peer.wait_connection()
        transport = connector._transport  # noqa: SLF001 - transport acceptance proof
        assert _wait_until(lambda: transport.connected)

        peer.send_json(_inbound_event("10001", "same-message"), connection)
        assert _wait_until(
            lambda: (
                len(emitter.events) == 1
                and emitter.events[0][0] == INBOUND_MESSAGE_EVENT_TYPE
            )
        )
        assert emitter.events[0][2] == INBOUND_MESSAGE_TYPE_URL
        assert (
            message_contract.InboundMessagePayload.FromString(
                emitter.events[0][1]
            ).conversation.account_id
            == "10001"
        )

        peer.send_ping(connection)
        opcode, payload = peer.receive_frame(connection)
        assert opcode == 0xA
        assert payload == b"heartbeat"

        peer.send_json(
            {
                "post_type": "meta_event",
                "meta_event_type": "heartbeat",
                "self_id": "10001",
            },
            connection,
        )
        assert _wait_until(lambda: transport.heartbeat_count == 1)

        result: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                result.append(connector.send_message(_send_request()))
            except BaseException as error:  # pragma: no cover - assertion below
                errors.append(error)

        worker = threading.Thread(target=invoke)
        worker.start()
        opcode, payload = peer.receive_frame(connection)
        assert opcode == 0x1
        action = json.loads(payload)
        assert action["action"] == "send_group_msg"
        assert action["params"]["group_id"] == 20001
        peer.send_json(
            {
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": 9001},
                "echo": action["echo"],
            },
            connection,
        )
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert errors == []
        assert result == [{"status": "accepted", "vendor_message_id": "9001"}]

        peer.drop(connection)
        reconnected = peer.wait_connection()
        assert _wait_until(lambda: transport.connected)
        assert transport.reconnect_count >= 1
        peer.send_json(_inbound_event("10001", "after-reconnect"), reconnected)
        assert _wait_until(lambda: len(emitter.events) == 2)
        assert (
            message_contract.InboundMessagePayload.FromString(
                emitter.events[1][1]
            ).message_id
            == "after-reconnect"
        )
    finally:
        connector.close()
        peer.close()


def test_forward_websocket_auth_failure_is_deterministic() -> None:
    peer = FakeOneBotPeer(access_token="expected")
    config = _config(
        peer, "qq-main", "10001", access_token="wrong", timeout_seconds=0.5
    )
    connector = OneBotV11Connector(config)
    try:
        with pytest.raises(ConnectorError) as error:
            connector.send_message(_send_request())
        assert error.value.code == "AUTHENTICATION_FAILED"
    finally:
        connector.close()
        peer.close()


def test_forward_websocket_timeout_cancellation_and_shutdown() -> None:
    peer = FakeOneBotPeer()
    connector = OneBotV11Connector(
        _config(peer, "qq-main", "10001", timeout_seconds=0.2)
    )
    try:
        transport = connector._transport  # noqa: SLF001 - transport acceptance proof
        transport.start()
        connection = peer.wait_connection()
        with pytest.raises(ConnectorError) as timeout:
            connector.send_message(_send_request())
        assert timeout.value.code == "TIMEOUT"
        opcode, _ = peer.receive_frame(connection)
        assert opcode == 0x1

        cancellation = Cancellation()
        errors: list[ConnectorError] = []

        def invoke() -> None:
            try:
                connector.send_message(_send_request(), cancellation=cancellation)
            except ConnectorError as error:
                errors.append(error)

        worker = threading.Thread(target=invoke)
        worker.start()
        opcode, _ = peer.receive_frame(connection)
        assert opcode == 0x1
        cancellation.cancelled = True
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert [error.code for error in errors] == ["CANCELLED"]
    finally:
        connector.close()
        assert not connector._transport.connected  # noqa: SLF001
        peer.close()


def test_two_websocket_bindings_are_transport_and_event_isolated() -> None:
    main_peer = FakeOneBotPeer()
    secondary_peer = FakeOneBotPeer()
    main = OneBotV11Connector(
        _config(main_peer, "qq-main", "10001", timeout_seconds=1.0)
    )
    secondary = OneBotV11Connector(
        _config(secondary_peer, "qq-secondary", "10002", timeout_seconds=1.0)
    )
    main_emitter = RecordingEmitter()
    secondary_emitter = RecordingEmitter()
    try:
        assert main.on_subscribe("main", CAPABILITY_ID, b"{}", main_emitter) is None
        assert (
            secondary.on_subscribe("secondary", CAPABILITY_ID, b"{}", secondary_emitter)
            is None
        )
        main_connection = main_peer.wait_connection()
        secondary_connection = secondary_peer.wait_connection()

        main_peer.send_json(_inbound_event("10001", "same-message"), main_connection)
        secondary_peer.send_json(
            _inbound_event("10002", "same-message"), secondary_connection
        )
        assert _wait_until(lambda: len(main_emitter.events) == 1)
        assert _wait_until(lambda: len(secondary_emitter.events) == 1)
        assert main_emitter.events[0][2] == INBOUND_MESSAGE_TYPE_URL
        assert secondary_emitter.events[0][2] == INBOUND_MESSAGE_TYPE_URL
        main_payload = message_contract.InboundMessagePayload.FromString(
            main_emitter.events[0][1]
        )
        secondary_payload = message_contract.InboundMessagePayload.FromString(
            secondary_emitter.events[0][1]
        )
        assert main_payload.conversation.account_id == "10001"
        assert secondary_payload.conversation.account_id == "10002"

        _invoke_and_respond(main, main_peer, main_connection, "10001")
        _invoke_and_respond(secondary, secondary_peer, secondary_connection, "10002")
    finally:
        main.close()
        secondary.close()
        main_peer.close()
        secondary_peer.close()


def test_reverse_websocket_profile_accepts_a_binding_local_peer() -> None:
    connector = OneBotV11Connector(
        {
            "binding_id": "qq-main",
            "transport_profile": "reverse_websocket",
            "reverse_listen_host": "127.0.0.1",
            "reverse_listen_port": 0,
            "access_token": "token",
            "self_account_id": "10001",
            "timeout_seconds": 1.0,
        }
    )
    emitter = RecordingEmitter()
    client: socket.socket | None = None
    try:
        assert connector.reverse_listen_address is not None
        assert connector.on_subscribe("main", CAPABILITY_ID, b"{}", emitter) is None
        client = _connect_reverse_client(connector.reverse_listen_address, "token")
        transport = connector._transport  # noqa: SLF001 - transport acceptance proof
        assert _wait_until(lambda: transport.connected)

        _send_client_frame(
            client, 0x1, json.dumps(_inbound_event("10001", "reverse-event")).encode()
        )
        assert _wait_until(lambda: len(emitter.events) == 1)
        assert emitter.events[0][2] == INBOUND_MESSAGE_TYPE_URL
        assert (
            message_contract.InboundMessagePayload.FromString(
                emitter.events[0][1]
            ).message_id
            == "reverse-event"
        )

        result: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                result.append(connector.send_message(_send_request()))
            except BaseException as error:  # pragma: no cover - assertion below
                errors.append(error)

        worker = threading.Thread(target=invoke)
        worker.start()
        opcode, payload = _read_frame(client)
        assert opcode == 0x1
        action = json.loads(payload)
        assert action["action"] == "send_group_msg"
        _send_client_frame(
            client,
            0x1,
            json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": "reverse-reply"},
                    "echo": action["echo"],
                }
            ).encode(),
        )
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert errors == []
        assert result == [{"status": "accepted", "vendor_message_id": "reverse-reply"}]
    finally:
        connector.close()
        if client is not None:
            _close_socket(client)


def _config(
    peer: FakeOneBotPeer,
    binding_id: str,
    account_id: str,
    *,
    access_token: str = "token",
    timeout_seconds: float,
) -> dict[str, Any]:
    return {
        "binding_id": binding_id,
        "websocket_url": f"ws://127.0.0.1:{peer.port}/onebot",
        "transport_profile": "forward_websocket",
        "runtime_profile": "qq-client",
        "access_token": access_token,
        "self_account_id": account_id,
        "timeout_seconds": timeout_seconds,
    }


def _send_request(account_id: str = "10001") -> dict[str, Any]:
    return {
        "conversation": {
            "vendor": "onebot.v11",
            "account_id": account_id,
            "conversation_id": "20001",
            "kind": "group",
        },
        "content": [{"text": {"text": "hello"}}],
    }


def _inbound_event(account_id: str, message_id: str) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": account_id,
        "message_id": message_id,
        "group_id": "20001",
        "sender": {"user_id": "10002", "nickname": "member"},
        "message": [{"type": "text", "data": {"text": message_id}}],
    }


def _invoke_and_respond(
    connector: OneBotV11Connector,
    peer: FakeOneBotPeer,
    connection: socket.socket,
    account_id: str,
) -> None:
    result: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            result.append(connector.send_message(_send_request(account_id)))
        except BaseException as error:  # pragma: no cover - assertion below
            errors.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    opcode, payload = peer.receive_frame(connection)
    assert opcode == 0x1
    action = json.loads(payload)
    peer.send_json(
        {
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": f"{account_id}-reply"},
            "echo": action["echo"],
        },
        connection,
    )
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert errors == []
    assert result == [
        {"status": "accepted", "vendor_message_id": f"{account_id}-reply"}
    ]


def _wait_until(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _read_http_headers(connection: socket.socket) -> dict[str, str]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed during handshake")
        data.extend(chunk)
    lines = bytes(data).split(b"\r\n\r\n", 1)[0].decode().split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.lower()] = value.strip()
    return headers


def _read_frame(connection: socket.socket) -> tuple[int, bytes]:
    first, second = _read_exact(connection, 2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(_read_exact(connection, 2), "big")
    elif length == 127:
        length = int.from_bytes(_read_exact(connection, 8), "big")
    mask = _read_exact(connection, 4) if second & 0x80 else b""
    payload = bytearray(_read_exact(connection, length))
    if mask:
        for index in range(length):
            payload[index] ^= mask[index % 4]
    return opcode, bytes(payload)


def _connect_reverse_client(
    address: tuple[str, int] | None, access_token: str
) -> socket.socket:
    if address is None:
        raise AssertionError("reverse WebSocket listener did not bind")
    connection = socket.create_connection(address, timeout=2.0)
    key = base64.b64encode(b"0123456789abcdef").decode()
    connection.sendall(
        (
            "GET /onebot HTTP/1.1\r\n"
            f"Host: {address[0]}:{address[1]}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {access_token}\r\n\r\n"
        ).encode()
    )
    response = _read_http_response(connection)
    assert response[0].startswith("HTTP/1.1 101")
    connection.settimeout(2.0)
    return connection


def _read_http_response(connection: socket.socket) -> tuple[str, dict[str, str]]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed during handshake")
        data.extend(chunk)
    lines = bytes(data).split(b"\r\n\r\n", 1)[0].decode().split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.lower()] = value.strip()
    return lines[0], headers


def _send_client_frame(connection: socket.socket, opcode: int, payload: bytes) -> None:
    mask = b"test"
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | opcode, 0x80 | length))
    elif length < 65_536:
        header = bytes((0x80 | opcode, 0x80 | 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((0x80 | opcode, 0x80 | 127)) + length.to_bytes(8, "big")
    connection.sendall(header + mask + masked)


def _send_frame(connection: socket.socket, opcode: int, payload: bytes) -> None:
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | opcode, length))
    elif length < 65_536:
        header = bytes((0x80 | opcode, 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((0x80 | opcode, 127)) + length.to_bytes(8, "big")
    connection.sendall(header + payload)


def _read_exact(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError("peer closed")
        data.extend(chunk)
    return bytes(data)


def _close_socket(connection: socket.socket) -> None:
    with contextlib.suppress(OSError):
        connection.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        connection.close()
