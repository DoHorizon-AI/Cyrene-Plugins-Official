###############################################################################
# 📄 File: plugins/connectors/onebot-v11/src/onebot_v11_connector/transport.py
# Module: Cyrene Plugins Official
# Role: Protocol or message connector implementation.
#
# This file documents an active plugin boundary; runtime behavior is unchanged.
#
# 模块：Cyrene Plugins Official
# 职责：协议或消息连接器实现。
# 本文件属于活动插件边界；运行时行为保持不变。
###############################################################################
"""Dependency-free OneBot v11 forward-WebSocket transport.

The transport is deliberately scoped to one :class:`OneBotInstanceConfig`.
It owns the protocol connection and never selects another configured binding.
The external OneBot process is the peer; Product/session policy remains above
the ``message.connector.v1`` boundary.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from .connector import CancellationToken, ConnectorError

_MAX_HANDSHAKE_BYTES = 64 * 1024
_MAX_FRAME_BYTES = 16 * 1024 * 1024
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
_READ_POLL_SECONDS = 0.25
_CLOSE_WAIT_SECONDS = 1.0


@dataclass(slots=True)
class _PendingCall:
    completed: threading.Event
    response: Mapping[str, Any] | None = None
    error: ConnectorError | None = None


class OneBotWebSocketTransport:
    """OneBot v11 forward-WebSocket client for one configured binding.

    The reader is a single daemon thread per transport.  It dispatches inbound
    events to the connector callback and correlates action responses by the
    OneBot ``echo`` field.  A broken connection fails in-flight actions and is
    retried with bounded exponential backoff; no action is replayed
    implicitly, because replay could duplicate a side effect.
    """

    def __init__(
        self,
        config: Any,
        *,
        event_handler: Callable[[Mapping[str, Any]], int] | None = None,
        reconnect_initial_seconds: float = 0.05,
        reconnect_max_seconds: float = 1.0,
    ) -> None:
        websocket_url = getattr(config, "websocket_url", None)
        parsed = urlparse(websocket_url) if websocket_url else None
        if parsed is not None and (
            parsed.scheme not in {"ws", "wss"} or not parsed.hostname
        ):
            raise ConnectorError(
                "INVALID_REQUEST", "websocket_url must be an absolute ws(s) URL"
            )
        self._config = config
        self._url = parsed
        self._event_handler = event_handler
        self._reconnect_initial_seconds = max(0.01, reconnect_initial_seconds)
        self._reconnect_max_seconds = max(
            self._reconnect_initial_seconds, reconnect_max_seconds
        )
        self._state_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()
        self._connected = threading.Event()
        self._wake = threading.Event()
        self._next_echo = 0
        self._pending: dict[str, _PendingCall] = {}
        self._receive_buffer = bytearray()
        self._last_error: ConnectorError | None = None
        self._last_event_error: ConnectorError | None = None
        self._reconnect_count = 0
        self._heartbeat_count = 0

    @property
    def connected(self) -> bool:
        """Whether the transport currently owns a completed WebSocket session."""

        return self._connected.is_set()

    @property
    def reconnect_count(self) -> int:
        """Number of reconnect attempts after an established session failed."""

        with self._state_lock:
            return self._reconnect_count

    @property
    def heartbeat_count(self) -> int:
        """Number of OneBot heartbeat events observed on this binding."""

        with self._state_lock:
            return self._heartbeat_count

    @property
    def last_error(self) -> ConnectorError | None:
        """Most recent connection/protocol error, for deterministic diagnostics."""

        with self._state_lock:
            return self._last_error

    @property
    def last_event_error(self) -> ConnectorError | None:
        """Most recent rejected inbound event; the connection remains usable."""

        with self._state_lock:
            return self._last_event_error

    def set_event_handler(
        self, event_handler: Callable[[Mapping[str, Any]], int] | None
    ) -> None:
        """Attach the connector's binding-local application-event callback."""

        with self._state_lock:
            self._event_handler = event_handler

    def start(self) -> None:
        """Start connection management without blocking subscription setup."""

        if self._url is None:
            return
        with self._state_lock:
            if self._closed.is_set():
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name=f"onebot-ws-{self._config.binding_id}",
                daemon=True,
            )
            self._thread.start()

    def attach_socket(self, sock: socket.socket, initial_bytes: bytes = b"") -> None:
        """Attach an already-handshaken reverse-WebSocket peer."""

        if self._closed.is_set():
            _close_socket(sock)
            return
        with self._state_lock:
            previous = self._socket
            self._socket = sock
            self._receive_buffer.clear()
            self._receive_buffer.extend(initial_bytes)
            self._last_error = None
            self._thread = threading.Thread(
                target=self._receive_attached_loop,
                name=f"onebot-ws-{self._config.binding_id}",
                daemon=True,
            )
            thread = self._thread
        if previous is not None and previous is not sock:
            _close_socket(previous)
        self._connected.set()
        thread.start()

    def call(
        self,
        action: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float,
        cancellation: CancellationToken | None,
    ) -> Mapping[str, Any]:
        """Send an action and wait for the matching OneBot ``echo`` response."""

        _raise_if_cancelled(cancellation)
        self.start()
        deadline = time.monotonic() + timeout_seconds
        self._wait_until_connected(deadline, cancellation)

        with self._state_lock:
            self._next_echo += 1
            echo = f"{self._config.binding_id}:{self._next_echo}"
        pending = _PendingCall(threading.Event())
        with self._pending_lock:
            self._pending[echo] = pending

        try:
            self._send_json(
                {
                    "action": action,
                    "params": dict(params),
                    "echo": echo,
                }
            )
        except OSError as exc:
            self._remove_pending(echo)
            raise ConnectorError(
                "CAPABILITY_UNAVAILABLE",
                f"OneBot action {action} could not reach its runtime",
            ) from exc

        while True:
            _raise_if_cancelled(cancellation)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._remove_pending(echo)
                raise ConnectorError("TIMEOUT", f"OneBot action {action} timed out")
            if pending.completed.wait(min(0.05, remaining)):
                break

        if pending.error is not None:
            raise pending.error
        response = pending.response
        if response is None:
            raise ConnectorError(
                "PROTOCOL_MISMATCH", f"OneBot action {action} had no response"
            )
        if response.get("status") != "ok" or response.get("retcode") != 0:
            raise ConnectorError(
                "EXECUTION_FAILED",
                f"OneBot action {action} was rejected by the runtime",
            )
        data = response.get("data", {})
        if not isinstance(data, Mapping):
            raise ConnectorError(
                "PROTOCOL_MISMATCH",
                f"OneBot action {action} returned non-object data",
            )
        return dict(data)

    def close(self) -> None:
        """Stop reconnecting, close the socket, and fail pending actions."""

        self._closed.set()
        self._wake.set()
        self._connected.clear()
        self._fail_pending(
            ConnectorError(
                "CAPABILITY_UNAVAILABLE", "OneBot WebSocket transport is closed"
            )
        )
        with self._state_lock:
            sock = self._socket
            self._socket = None
        if sock is not None:
            _close_socket(sock)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(_CLOSE_WAIT_SECONDS)

    def _run(self) -> None:
        backoff = self._reconnect_initial_seconds
        while not self._closed.is_set():
            sock: socket.socket | None = None
            try:
                sock = self._connect_socket()
                with self._state_lock:
                    self._socket = sock
                    self._last_error = None
                self._connected.set()
                backoff = self._reconnect_initial_seconds
                self._receive_loop(sock)
            except ConnectorError as exc:
                self._set_last_error(exc)
                if exc.code == "AUTHENTICATION_FAILED":
                    break
            except (ConnectionError, OSError) as exc:
                self._set_last_error(
                    ConnectorError(
                        "CAPABILITY_UNAVAILABLE",
                        "OneBot WebSocket runtime disconnected",
                    )
                )
                del exc
            finally:
                self._disconnect_socket(sock)

            if self._closed.is_set():
                break
            with self._state_lock:
                self._reconnect_count += 1
            self._wake.wait(backoff)
            self._wake.clear()
            backoff = min(self._reconnect_max_seconds, backoff * 2)

    def _receive_attached_loop(self) -> None:
        with self._state_lock:
            sock = self._socket
        if sock is None:
            return
        try:
            self._receive_loop(sock)
        except ConnectorError as exc:
            self._set_last_error(exc)
        except (ConnectionError, OSError):
            self._set_last_error(
                ConnectorError(
                    "CAPABILITY_UNAVAILABLE",
                    "OneBot WebSocket runtime disconnected",
                )
            )
        finally:
            self._disconnect_socket(sock)

    def _connect_socket(self) -> socket.socket:
        if self._url is None:
            raise ConnectorError(
                "CAPABILITY_UNAVAILABLE",
                "OneBot reverse WebSocket is waiting for a peer",
            )
        host = self._url.hostname
        if host is None:
            raise ConnectorError("INVALID_REQUEST", "websocket_url host is missing")
        port = self._url.port or (443 if self._url.scheme == "wss" else 80)
        timeout = float(self._config.timeout_seconds)
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            if self._url.scheme == "wss":
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=host)
            sock.settimeout(_READ_POLL_SECONDS)
            self._websocket_handshake(sock, host, port)
            return sock
        except ConnectorError:
            if sock is not None:
                _close_socket(sock)
            raise
        except (OSError, ssl.SSLError) as exc:
            if sock is not None:
                _close_socket(sock)
            raise ConnectorError(
                "CAPABILITY_UNAVAILABLE",
                "OneBot WebSocket runtime could not be reached",
            ) from exc

    def _websocket_handshake(self, sock: socket.socket, host: str, port: int) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        host_header = host
        if ":" in host and not host.startswith("["):
            host_header = f"[{host}]"
        default_port = 443 if self._url.scheme == "wss" else 80
        if port != default_port:
            host_header = f"{host_header}:{port}"
        path = self._url.path or "/"
        if self._url.query:
            path = f"{path}?{self._url.query}"
        headers = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        access_token = self._config.access_token
        if access_token:
            headers.append(f"Authorization: Bearer {access_token}")
        request = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")
        sock.sendall(request)
        status_line, response_headers, remainder = self._read_handshake_response(sock)
        self._receive_buffer.clear()
        self._receive_buffer.extend(remainder)
        pieces = status_line.split(" ", 2)
        if len(pieces) < 2 or not pieces[1].isdigit():
            raise ConnectorError(
                "PROTOCOL_MISMATCH", "OneBot WebSocket handshake response is invalid"
            )
        status = int(pieces[1])
        if status in {401, 403}:
            raise ConnectorError(
                "AUTHENTICATION_FAILED", "OneBot WebSocket authentication failed"
            )
        if status != 101:
            raise ConnectorError(
                "PROTOCOL_MISMATCH",
                f"OneBot WebSocket handshake returned HTTP {status}",
            )
        expected_accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        if response_headers.get("sec-websocket-accept") != expected_accept:
            raise ConnectorError(
                "PROTOCOL_MISMATCH",
                "OneBot WebSocket handshake accept key is invalid",
            )

    def _read_handshake_response(
        self, sock: socket.socket
    ) -> tuple[str, dict[str, str], bytes]:
        data = bytearray()
        deadline = time.monotonic() + float(self._config.timeout_seconds)
        while b"\r\n\r\n" not in data:
            if len(data) > _MAX_HANDSHAKE_BYTES:
                raise ConnectorError(
                    "PROTOCOL_MISMATCH", "OneBot WebSocket handshake is too large"
                )
            if time.monotonic() >= deadline:
                raise ConnectorError(
                    "TIMEOUT", "OneBot WebSocket handshake timed out"
                )
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                raise ConnectorError(
                    "CAPABILITY_UNAVAILABLE",
                    "OneBot WebSocket peer closed during handshake",
                )
            data.extend(chunk)
        head, remainder = bytes(data).split(b"\r\n\r\n", 1)
        try:
            lines = head.decode("iso-8859-1").split("\r\n")
        except UnicodeDecodeError as exc:
            raise ConnectorError(
                "PROTOCOL_MISMATCH", "OneBot WebSocket handshake is not valid HTTP"
            ) from exc
        if not lines:
            raise ConnectorError(
                "PROTOCOL_MISMATCH", "OneBot WebSocket handshake is empty"
            )
        response_headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if not separator:
                continue
            response_headers[name.strip().lower()] = value.strip()
        return lines[0], response_headers, remainder

    def _receive_loop(self, sock: socket.socket) -> None:
        while not self._closed.is_set():
            opcode, payload = _read_message(sock, self)
            if opcode is None:
                return
            if opcode != 0x1:
                raise ConnectorError(
                    "PROTOCOL_MISMATCH",
                    "OneBot WebSocket peer sent a non-text data message",
                )
            try:
                message = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ConnectorError(
                    "PROTOCOL_MISMATCH",
                    "OneBot WebSocket message is not valid JSON",
                ) from exc
            if not isinstance(message, Mapping):
                raise ConnectorError(
                    "PROTOCOL_MISMATCH", "OneBot WebSocket message is not an object"
                )
            self._handle_message(dict(message))

    def _handle_message(self, message: Mapping[str, Any]) -> None:
        if "echo" in message:
            echo = str(message["echo"])
            with self._pending_lock:
                pending = self._pending.pop(echo, None)
            if pending is not None:
                pending.response = dict(message)
                pending.completed.set()
                return

        if (
            message.get("post_type") == "meta_event"
            and message.get("meta_event_type") == "heartbeat"
        ):
            with self._state_lock:
                self._heartbeat_count += 1
            return

        if "post_type" not in message:
            return
        with self._state_lock:
            event_handler = self._event_handler
        if event_handler is None:
            return
        try:
            event_handler(message)
        except ConnectorError as exc:
            with self._state_lock:
                self._last_event_error = exc

    def _send_json(self, message: Mapping[str, Any]) -> None:
        payload = json.dumps(
            dict(message), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(payload) > _MAX_MESSAGE_BYTES:
            raise OSError("OneBot WebSocket message exceeds the size limit")
        with self._state_lock:
            sock = self._socket
        if sock is None or not self._connected.is_set():
            raise OSError("OneBot WebSocket is disconnected")
        with self._send_lock:
            _send_frame(sock, 0x1, payload, mask=True)

    def _disconnect_socket(self, sock: socket.socket | None) -> None:
        with self._state_lock:
            current = self._socket
            if sock is not None and current is not None and current is not sock:
                return
            self._socket = None
        self._connected.clear()
        if sock is not None:
            _close_socket(sock)
        self._fail_pending(
            ConnectorError(
                "CAPABILITY_UNAVAILABLE", "OneBot WebSocket runtime is unavailable"
            )
        )

    def _wait_until_connected(
        self,
        deadline: float,
        cancellation: CancellationToken | None,
    ) -> None:
        while not self._connected.is_set():
            _raise_if_cancelled(cancellation)
            if self._closed.is_set():
                raise ConnectorError(
                    "CAPABILITY_UNAVAILABLE", "OneBot WebSocket transport is closed"
                )
            if time.monotonic() >= deadline:
                with self._state_lock:
                    error = self._last_error
                if error is not None and error.code in {
                    "AUTHENTICATION_FAILED",
                    "PROTOCOL_MISMATCH",
                }:
                    raise error
                raise ConnectorError(
                    "CAPABILITY_UNAVAILABLE",
                    "OneBot WebSocket runtime is unavailable",
                )
            time.sleep(0.01)

    def _fail_pending(self, error: ConnectorError) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = error
            item.completed.set()

    def _remove_pending(self, echo: str) -> None:
        with self._pending_lock:
            self._pending.pop(echo, None)

    def _set_last_error(self, error: ConnectorError) -> None:
        with self._state_lock:
            self._last_error = error


class OneBotReverseWebSocketServer:
    """Accept OneBot v11 reverse-WebSocket connections for one binding."""

    def __init__(self, config: Any, transport: OneBotWebSocketTransport) -> None:
        self._config = config
        self._transport = transport
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._address: tuple[str, int] | None = None

    @property
    def listen_address(self) -> tuple[str, int] | None:
        """Return the actual bound address, including an ephemeral port."""

        return self._address

    def start(self) -> None:
        """Bind the reverse endpoint and accept peers in the background."""

        if self._thread is not None and self._thread.is_alive():
            return
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(
                (self._config.reverse_listen_host, self._config.reverse_listen_port)
            )
            server.listen(8)
            server.settimeout(_READ_POLL_SECONDS)
        except OSError as exc:
            if "server" in locals():
                _close_socket(server)
            raise ConnectorError(
                "CAPABILITY_UNAVAILABLE",
                "OneBot reverse WebSocket listener could not start",
            ) from exc
        self._server = server
        host, port = server.getsockname()[:2]
        self._address = (str(host), int(port))
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._accept_loop,
            name=f"onebot-reverse-ws-{self._config.binding_id}",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """Stop accepting peers and close the binding-local transport."""

        self._stop.set()
        server = self._server
        self._server = None
        if server is not None:
            _close_socket(server)
        self._transport.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(_CLOSE_WAIT_SECONDS)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            server = self._server
            if server is None:
                return
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                connection.settimeout(_READ_POLL_SECONDS)
                request_line, headers, remainder = _read_http_request(
                    connection, float(self._config.timeout_seconds)
                )
                method, target, *_ = request_line.split(" ", 2)
                if method != "GET" or headers.get("upgrade", "").lower() != "websocket":
                    _send_http_error(connection, 400, "Bad Request")
                    _close_socket(connection)
                    continue
                if not self._authorized(headers, target):
                    _send_http_error(connection, 401, "Unauthorized")
                    _close_socket(connection)
                    continue
                key = headers.get("sec-websocket-key")
                if not key:
                    _send_http_error(connection, 400, "Bad Request")
                    _close_socket(connection)
                    continue
                accept = base64.b64encode(
                    hashlib.sha1(
                        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(
                            "ascii"
                        )
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
                connection.settimeout(_READ_POLL_SECONDS)
                self._transport.attach_socket(connection, remainder)
            except (ConnectorError, ConnectionError, OSError):
                _close_socket(connection)

    def _authorized(self, headers: Mapping[str, str], target: str) -> bool:
        expected = self._config.access_token
        if not expected:
            return True
        provided = headers.get("authorization", "")
        if provided == f"Bearer {expected}":
            return True
        query_token = parse_qs(urlparse(target).query).get("access_token", [""])[0]
        return secrets.compare_digest(query_token, expected)


def _read_message(
    sock: socket.socket, transport: OneBotWebSocketTransport
) -> tuple[int | None, bytes]:
    fragments: bytearray | None = None
    fragment_opcode: int | None = None
    while True:
        fin, opcode, payload = _read_frame(sock, transport)
        if opcode in {0x8, 0x9, 0xA}:
            if opcode == 0x9:
                with transport._send_lock:  # noqa: SLF001 - frame-level protocol hook
                    _send_frame(sock, 0xA, payload, mask=True)
            elif opcode == 0x8 and fin:
                try:
                    with transport._send_lock:  # noqa: SLF001
                        _send_frame(sock, 0x8, payload[:125], mask=True)
                except OSError:
                    pass
                return None, b""
            continue

        if opcode == 0x1 or opcode == 0x2:
            if fragments is not None:
                raise ConnectorError(
                    "PROTOCOL_MISMATCH", "nested fragmented WebSocket message"
                )
            if fin:
                return opcode, payload
            fragments = bytearray(payload)
            fragment_opcode = opcode
            continue
        if opcode == 0x0:
            if fragments is None:
                raise ConnectorError(
                    "PROTOCOL_MISMATCH", "orphan WebSocket continuation frame"
                )
            fragments.extend(payload)
            if len(fragments) > _MAX_MESSAGE_BYTES:
                raise ConnectorError(
                    "PROTOCOL_MISMATCH", "OneBot WebSocket message is too large"
                )
            if fin:
                return fragment_opcode or 0x1, bytes(fragments)
            continue
        raise ConnectorError(
            "PROTOCOL_MISMATCH", f"unsupported WebSocket opcode {opcode}"
        )


def _read_frame(
    sock: socket.socket, transport: OneBotWebSocketTransport
) -> tuple[bool, int, bytes]:
    first, second = _receive_exact(sock, 2, transport)
    fin = bool(first & 0x80)
    if first & 0x70:
        raise ConnectorError("PROTOCOL_MISMATCH", "WebSocket RSV bits are unsupported")
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(_receive_exact(sock, 2, transport), "big")
    elif length == 127:
        length = int.from_bytes(_receive_exact(sock, 8, transport), "big")
        if length & (1 << 63):
            raise ConnectorError("PROTOCOL_MISMATCH", "invalid WebSocket frame length")
    if length > _MAX_FRAME_BYTES:
        raise ConnectorError("PROTOCOL_MISMATCH", "WebSocket frame is too large")
    if opcode >= 0x8 and (not fin or length > 125):
        raise ConnectorError("PROTOCOL_MISMATCH", "invalid WebSocket control frame")
    mask_key = _receive_exact(sock, 4, transport) if masked else b""
    payload = bytearray(_receive_exact(sock, length, transport))
    if masked:
        for index in range(length):
            payload[index] ^= mask_key[index % 4]
    return fin, opcode, bytes(payload)


def _read_http_request(
    sock: socket.socket, timeout_seconds: float
) -> tuple[str, dict[str, str], bytes]:
    data = bytearray()
    deadline = time.monotonic() + timeout_seconds
    while b"\r\n\r\n" not in data:
        if len(data) > _MAX_HANDSHAKE_BYTES:
            raise ConnectorError(
                "PROTOCOL_MISMATCH", "OneBot WebSocket request is too large"
            )
        if time.monotonic() >= deadline:
            raise ConnectorError("TIMEOUT", "OneBot WebSocket request timed out")
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            continue
        if not chunk:
            raise ConnectionError("peer closed during handshake")
        data.extend(chunk)
    head, remainder = bytes(data).split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").split("\r\n")
    if not lines:
        raise ConnectorError("PROTOCOL_MISMATCH", "OneBot WebSocket request is empty")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return lines[0], headers, remainder


def _send_http_error(sock: socket.socket, status: int, reason: str) -> None:
    with contextlib.suppress(OSError):
        sock.sendall(
            f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n\r\n".encode(
                "ascii"
            )
        )


def _receive_exact(
    sock: socket.socket, size: int, transport: OneBotWebSocketTransport
) -> bytes:
    data = bytearray()
    buffered = transport._receive_buffer  # noqa: SLF001 - handshake/read boundary
    if buffered:
        take = min(size, len(buffered))
        data.extend(buffered[:take])
        del buffered[:take]
    while len(data) < size:
        if transport._closed.is_set():  # noqa: SLF001 - close interrupts reader
            raise OSError("transport closed")
        try:
            chunk = sock.recv(size - len(data))
        except TimeoutError:
            continue
        if not chunk:
            raise ConnectionError("WebSocket peer closed")
        data.extend(chunk)
    return bytes(data)


def _send_frame(
    sock: socket.socket, opcode: int, payload: bytes, *, mask: bool
) -> None:
    if len(payload) > _MAX_FRAME_BYTES:
        raise OSError("WebSocket frame is too large")
    first = 0x80 | (opcode & 0x0F)
    mask_bit = 0x80 if mask else 0
    length = len(payload)
    if length < 126:
        header = bytes((first, mask_bit | length))
    elif length < 65_536:
        header = bytes((first, mask_bit | 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((first, mask_bit | 127)) + length.to_bytes(8, "big")
    if mask:
        mask_key = secrets.token_bytes(4)
        body = bytes(value ^ mask_key[index % 4] for index, value in enumerate(payload))
        sock.sendall(header + mask_key + body)
    else:
        sock.sendall(header + payload)


def _close_socket(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


def _raise_if_cancelled(cancellation: CancellationToken | None) -> None:
    if cancellation is not None and cancellation.is_cancelled():
        raise ConnectorError("CANCELLED", "OneBot operation was cancelled")
