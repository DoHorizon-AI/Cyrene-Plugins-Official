"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 qqnt_direct_host.py                                             │
│  Module: onebot_v11_connector.qqnt_direct_host                      │
│  Role: One-binding QQ Host subprocess lifecycle over inherited stdio. │
│                                                                     │
│  模块职责：通过继承 stdio 启停 QQ Host，并完成请求、回调与取消关联。     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .qqnt_direct_protocol import QQHostProtocolError, read_frame, write_frame

QQ_HOST_PROTOCOL = "cyrene.qq.host.v1"
QQ_HOST_PROTOCOL_VERSION = "1"
_REDACTED_DIAGNOSTIC = re.compile(
    r"(?i)(password|token|secret|ticket|cookie)(\s*[:=]\s*)\S+"
)


class QQHostError(RuntimeError):
    """Structured error produced by the QQ Host process boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class QQHostLaunchConfig:
    """Immutable launch inputs for exactly one binding and one process tree."""

    binding_id: str
    command: tuple[str, ...]
    data_dir: Path
    required_client_version: str
    platform: str
    timeout_seconds: float
    shutdown_timeout_seconds: float


@dataclass(slots=True)
class _PendingRequest:
    """One generation-scoped request waiting for a Host response."""

    completed: threading.Event
    result: Any = None
    error: QQHostError | None = None


class QQHostClient:
    """Supervise one authorized QQ Host child over pipes only.

    The child receives no listening address and the client never imports or
    creates a network transport.  Every request identifier contains the
    binding and current worker generation, so a late callback cannot cross a
    restart or another binding.
    """

    def __init__(
        self,
        config: QQHostLaunchConfig,
        *,
        event_handler: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._config = config
        self._event_handler = event_handler
        self._state_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._pending: dict[str, _PendingRequest] = {}
        self._request_counter = 0
        self._generation = 0
        self._state = "CREATED"
        self._compatibility: dict[str, Any] = {}
        self._diagnostics: list[str] = []

    @property
    def binding_id(self) -> str:
        """Return the immutable binding identity assigned to this client."""

        return self._config.binding_id

    @property
    def generation(self) -> int:
        """Return the current process generation, or zero before first start."""

        with self._state_lock:
            return self._generation

    @property
    def state(self) -> str:
        """Return the current lifecycle state."""

        with self._state_lock:
            return self._state

    @property
    def compatibility(self) -> Mapping[str, Any]:
        """Return the bounded Host hello report without credentials."""

        with self._state_lock:
            return dict(self._compatibility)

    @property
    def diagnostics(self) -> tuple[str, ...]:
        """Return bounded, redacted diagnostics useful for operator evidence."""

        with self._state_lock:
            return tuple(self._diagnostics)

    def start(self) -> Mapping[str, Any]:
        """Start the child, negotiate protocol compatibility, and return its report.

        Raises:
            QQHostError: If the child cannot start or fails the exact protocol,
                binding, platform, or QQ client-version checks.
        """

        with self._state_lock:
            current = self._process
            if current is not None and current.poll() is None:
                return dict(self._compatibility)
            self._generation += 1
            generation = self._generation
            self._state = "STARTING"
            self._config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            environment = os.environ.copy()
            environment.update(
                {
                    "CYRENE_QQ_BINDING_ID": self.binding_id,
                    "CYRENE_QQ_BINDING_GENERATION": str(generation),
                    "CYRENE_QQ_BINDING_DATA_DIR": str(self._config.data_dir),
                }
            )
            try:
                process = subprocess.Popen(
                    list(self._config.command),
                    cwd=self._config.data_dir,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    close_fds=True,
                )
            except (OSError, ValueError) as exc:
                self._state = "FAILED"
                raise QQHostError(
                    "CAPABILITY_UNAVAILABLE", "QQ Host could not be started"
                ) from exc
            self._process = process
            self._compatibility = {}
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                name=f"qq-host-reader-{self.binding_id}-{generation}",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._stderr_loop,
                name=f"qq-host-stderr-{self.binding_id}-{generation}",
                daemon=True,
            )
            self._reader_thread.start()
            self._stderr_thread.start()

        try:
            report = self.request(
                "hello",
                {
                    "protocol": QQ_HOST_PROTOCOL,
                    "protocol_version": QQ_HOST_PROTOCOL_VERSION,
                    "platform": self._config.platform,
                    "required_client_version": self._config.required_client_version,
                },
            )
            self._validate_hello(report, generation)
        except QQHostError:
            self.close()
            raise
        with self._state_lock:
            self._state = "NATIVE_READY"
        return dict(self._compatibility)

    def restart(self) -> Mapping[str, Any]:
        """Drain the current child and start a new generation for this binding."""

        self.close()
        return self.start()

    def request(
        self,
        operation: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
        cancellation: Any | None = None,
    ) -> Any:
        """Send one fixed operation and await its generation-scoped response."""

        if not isinstance(operation, str) or not operation.strip():
            raise QQHostError("INVALID_REQUEST", "Host operation must be text")
        if not isinstance(params, Mapping):
            raise QQHostError(
                "INVALID_REQUEST", "Host operation params must be an object"
            )
        if cancellation is not None and cancellation.is_cancelled():
            raise QQHostError("CANCELLED", "QQ Host operation was cancelled")
        forbidden = {"service", "method", "raw_payload", "binding_id", "generation"}
        if forbidden.intersection(params):
            raise QQHostError(
                "INVALID_REQUEST", "Host operation params contain reserved fields"
            )
        with self._state_lock:
            process = self._process
            generation = self._generation
        if process is None or process.poll() is not None:
            raise QQHostError("CAPABILITY_UNAVAILABLE", "QQ Host is not running")
        with self._pending_lock:
            self._request_counter += 1
            request_id = f"{self.binding_id}:{generation}:{self._request_counter}"
            pending = _PendingRequest(threading.Event())
            self._pending[request_id] = pending
        message = {
            "type": "request",
            "request_id": request_id,
            "binding_id": self.binding_id,
            "generation": generation,
            "operation": operation,
            "params": dict(params),
        }
        try:
            self._send(message)
        except (BrokenPipeError, OSError, QQHostProtocolError) as exc:
            self._remove_pending(request_id)
            raise QQHostError(
                "CAPABILITY_UNAVAILABLE", "QQ Host stdio write failed"
            ) from exc

        timeout = timeout_seconds or self._config.timeout_seconds
        deadline = time.monotonic() + timeout
        while True:
            if pending.completed.wait(
                timeout=min(0.05, max(0.0, deadline - time.monotonic()))
            ):
                break
            if cancellation is not None and cancellation.is_cancelled():
                self._remove_pending(request_id)
                self._send_cancel(request_id, generation)
                raise QQHostError("CANCELLED", "QQ Host operation was cancelled")
            if time.monotonic() >= deadline:
                self._remove_pending(request_id)
                self._send_cancel(request_id, generation)
                raise QQHostError("TIMEOUT", f"QQ Host operation {operation} timed out")
        if pending.error is not None:
            raise pending.error
        return pending.result

    def close(self) -> None:
        """Request shutdown, then boundedly reap the entire child process."""

        with self._state_lock:
            process = self._process
            if process is None:
                self._state = "STOPPED"
                return
            self._state = "DRAINING"
        if process.poll() is None:
            try:
                self._send(
                    {
                        "type": "shutdown",
                        "binding_id": self.binding_id,
                        "generation": self.generation,
                    }
                )
            except (BrokenPipeError, OSError, QQHostProtocolError):
                self._record_diagnostic("QQ Host shutdown pipe was already closed")
            try:
                process.wait(timeout=self._config.shutdown_timeout_seconds)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=self._config.shutdown_timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=self._config.shutdown_timeout_seconds)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=self._config.shutdown_timeout_seconds)
        self._fail_pending(QQHostError("CAPABILITY_UNAVAILABLE", "QQ Host stopped"))
        with self._state_lock:
            self._process = None
            self._reader_thread = None
            self._stderr_thread = None
            self._state = "STOPPED"

    def _validate_hello(self, report: Any, generation: int) -> None:
        if not isinstance(report, Mapping):
            raise QQHostError(
                "PROTOCOL_MISMATCH", "QQ Host hello report is not an object"
            )
        expected = {
            "protocol": QQ_HOST_PROTOCOL,
            "protocol_version": QQ_HOST_PROTOCOL_VERSION,
            "binding_id": self.binding_id,
            "generation": generation,
            "platform": self._config.platform,
        }
        for key, value in expected.items():
            if report.get(key) != value:
                raise QQHostError(
                    "PROTOCOL_MISMATCH", f"QQ Host hello field {key} mismatched"
                )
        client_version = report.get("client_version")
        if client_version != self._config.required_client_version:
            raise QQHostError(
                "UNSUPPORTED_VERSION", "QQ Host client version is not allow-listed"
            )
        with self._state_lock:
            self._compatibility = {
                key: report[key]
                for key in (
                    "protocol",
                    "protocol_version",
                    "binding_id",
                    "generation",
                    "platform",
                    "client_version",
                    "abi",
                )
                if key in report
            }

    def _send(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            process = self._process
            if process is None or process.stdin is None:
                raise QQHostProtocolError("QQ Host stdin is unavailable")
            write_frame(process.stdin, message)

    def _send_cancel(self, request_id: str, generation: int) -> None:
        try:
            self._send(
                {
                    "type": "cancel",
                    "request_id": request_id,
                    "binding_id": self.binding_id,
                    "generation": generation,
                }
            )
        except (BrokenPipeError, OSError, QQHostProtocolError):
            self._record_diagnostic("QQ Host cancellation pipe was already closed")

    def _reader_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                message = read_frame(process.stdout)
                if message is None:
                    break
                message_type = message.get("type")
                if message_type == "event":
                    self._handle_event(message)
                elif message_type in {"response", "hello_ack"}:
                    self._handle_response(message)
                else:
                    raise QQHostProtocolError("QQ Host sent an unknown message type")
        except (QQHostProtocolError, OSError) as exc:
            self._fail_pending(QQHostError("PROTOCOL_MISMATCH", str(exc)))
        finally:
            self._fail_pending(
                QQHostError("CAPABILITY_UNAVAILABLE", "QQ Host closed stdio")
            )

    def _handle_response(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("request_id")
        if not isinstance(request_id, str):
            raise QQHostProtocolError("QQ Host response has no request_id")
        if (
            message.get("binding_id") != self.binding_id
            or message.get("generation") != self.generation
        ):
            raise QQHostProtocolError("QQ Host response crossed binding or generation")
        with self._pending_lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        if message.get("ok") is not True:
            error = message.get("error")
            if not isinstance(error, Mapping):
                error = {}
            pending.error = QQHostError(
                str(error.get("code", "EXECUTION_FAILED")),
                str(error.get("message", "QQ Host rejected the operation")),
            )
        else:
            pending.result = message.get("result", {})
        pending.completed.set()

    def _handle_event(self, message: Mapping[str, Any]) -> None:
        if (
            message.get("binding_id") != self.binding_id
            or message.get("generation") != self.generation
        ):
            raise QQHostProtocolError("QQ Host event crossed binding or generation")
        event = message.get("event")
        if not isinstance(event, str) or not event:
            raise QQHostProtocolError("QQ Host event has no event name")
        handler = self._event_handler
        if handler is None:
            return
        try:
            handler(message)
        except Exception as exc:  # noqa: BLE001 - isolate malformed vendor events from the reader.
            self._record_diagnostic(f"QQ Host event handler rejected {event}: {exc}")

    def _stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw_line in iter(process.stderr.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line:
                self._record_diagnostic(line)

    def _record_diagnostic(self, value: str) -> None:
        redacted = _REDACTED_DIAGNOSTIC.sub(r"\1\2<redacted>", value)[:512]
        with self._state_lock:
            self._diagnostics.append(redacted)
            del self._diagnostics[:-64]

    def _remove_pending(self, request_id: str) -> None:
        with self._pending_lock:
            self._pending.pop(request_id, None)

    def _fail_pending(self, error: QQHostError) -> None:
        with self._pending_lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = error
            item.completed.set()
