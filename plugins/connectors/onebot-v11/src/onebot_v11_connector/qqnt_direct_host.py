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

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .qqnt_direct_discovery import (
    QQInstallationError,
    discover_explicit,
    discover_manifest,
)
from .qqnt_direct_operations import (
    QQOperationValidationError,
    get_qq_operation,
    validate_qq_parameters,
)
from .qqnt_direct_protocol import QQHostProtocolError, read_frame, write_frame

QQ_HOST_PROTOCOL = "cyrene.qq.host.v1"
QQ_HOST_PROTOCOL_VERSION = "1"
_REDACTED_DIAGNOSTIC = re.compile(
    r"(?i)(password|token|secret|ticket|cookie)(\s*[:=]\s*)\S+"
)
_BINDING_OWNER_FILE = ".cyrene-binding-owner.json"


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
    required_host_abi: str
    platform: str
    timeout_seconds: float
    startup_timeout_seconds: float
    shutdown_timeout_seconds: float
    max_restart_attempts: int = 2
    restart_window_seconds: float = 60.0
    restart_backoff_seconds: float = 0.25
    restart_backoff_max_seconds: float = 5.0
    crash_circuit_cooldown_seconds: float = 60.0
    installation_manifest: Path | None = None


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
        self._failure_code: str | None = None
        self._failure_message = ""
        self._restart_history: list[float] = []
        self._circuit_open_until = 0.0
        self._binding_lock_handle: Any | None = None

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

    @property
    def supervision(self) -> Mapping[str, Any]:
        """Return bounded restart and crash-circuit state for diagnostics."""

        with self._state_lock:
            self._prune_restart_history(time.monotonic())
            now = time.monotonic()
            return {
                "state": self._state,
                "failure_code": self._failure_code,
                "restart_attempts_in_window": len(self._restart_history),
                "max_restart_attempts": self._config.max_restart_attempts,
                "circuit_open": now < self._circuit_open_until,
            }

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
            try:
                if self._config.installation_manifest is not None:
                    installation = discover_manifest(
                        self._config.installation_manifest,
                        required_client_version=self._config.required_client_version,
                    )
                    explicit = discover_explicit(
                        self._config.command[0],
                        self._config.data_dir,
                        self._config.required_client_version,
                        expected_platform=self._config.platform,
                    )
                    if (
                        installation.host_executable != explicit.host_executable
                        or installation.data_dir != explicit.data_dir
                    ):
                        raise QQInstallationError(
                            "INVALID_REQUEST",
                            "QQ installation manifest does not match configured paths",
                        )
                else:
                    installation = discover_explicit(
                        self._config.command[0],
                        self._config.data_dir,
                        self._config.required_client_version,
                        expected_platform=self._config.platform,
                    )
                data_dir = installation.data_dir
                data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not data_dir.is_dir():
                    raise OSError("binding data path is not a directory")
                if os.name == "posix":
                    os.chmod(data_dir, 0o700)
                self._binding_lock_handle = _claim_binding_data_dir(
                    data_dir, self.binding_id
                )
            except QQInstallationError as exc:
                self._state = "FAILED"
                self._failure_code = exc.code
                self._failure_message = exc.message
                raise QQHostError(exc.code, exc.message) from exc
            except OSError as exc:
                self._state = "FAILED"
                self._failure_code = "CAPABILITY_UNAVAILABLE"
                self._failure_message = "QQ Host data directory is unusable"
                raise QQHostError(
                    "CAPABILITY_UNAVAILABLE", "QQ Host data directory is unusable"
                ) from exc
            environment = os.environ.copy()
            environment.update(
                {
                    "CYRENE_QQ_BINDING_ID": self.binding_id,
                    "CYRENE_QQ_BINDING_GENERATION": str(generation),
                    "CYRENE_QQ_BINDING_DATA_DIR": str(data_dir),
                }
            )
            try:
                popen_kwargs: dict[str, Any] = {
                    "cwd": data_dir,
                    "env": environment,
                    "stdin": subprocess.PIPE,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "close_fds": True,
                }
                if os.name == "posix":
                    popen_kwargs["start_new_session"] = True
                elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                command = (str(installation.host_executable), *self._config.command[1:])
                process = subprocess.Popen(list(command), **popen_kwargs)
            except (OSError, ValueError) as exc:
                self._state = "FAILED"
                self._failure_code = "CAPABILITY_UNAVAILABLE"
                self._failure_message = "QQ Host could not be started"
                self._release_binding_lock()
                raise QQHostError(
                    "CAPABILITY_UNAVAILABLE", "QQ Host could not be started"
                ) from exc
            self._process = process
            self._compatibility = {}
            self._failure_code = None
            self._failure_message = ""
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
                    "required_host_abi": self._config.required_host_abi,
                },
                timeout_seconds=self._config.startup_timeout_seconds,
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

    def recover(self) -> Mapping[str, Any]:
        """Perform one bounded crash recovery without retrying a QQ operation.

        Only an unexpected process/stdio exit is restartable.  The operation
        that observed the failure is never replayed; the caller may retry a
        later operation after the new generation has initialized its session.
        """

        with self._state_lock:
            if self._state != "FAILED":
                raise QQHostError(
                    "CAPABILITY_UNAVAILABLE", "QQ Host is not awaiting recovery"
                )
            if self._failure_code not in {"PROCESS_EXITED", "STDIO_CLOSED"}:
                raise QQHostError(
                    "CAPABILITY_UNAVAILABLE",
                    "QQ Host failure is not eligible for automatic recovery",
                )
            now = time.monotonic()
            self._prune_restart_history(now)
            if now < self._circuit_open_until:
                raise QQHostError(
                    "CAPABILITY_UNAVAILABLE", "QQ Host crash circuit is open"
                )
            if len(self._restart_history) >= self._config.max_restart_attempts:
                self._circuit_open_until = (
                    now + self._config.crash_circuit_cooldown_seconds
                )
                raise QQHostError(
                    "CAPABILITY_UNAVAILABLE", "QQ Host crash restart budget exhausted"
                )
            attempt = len(self._restart_history)
            self._restart_history.append(now)
            delay = min(
                self._config.restart_backoff_seconds * (2**attempt),
                self._config.restart_backoff_max_seconds,
            )
        if delay > 0:
            time.sleep(delay)
        self.close()
        return self.start()

    def request(
        self,
        operation: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
        cancellation: Any | None = None,
        request_id_sink: Callable[[str], None] | None = None,
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
        spec = get_qq_operation(operation) if operation != "hello" else None
        if operation != "hello" and spec is None:
            raise QQHostError(
                "UNKNOWN_OPERATION", f"unsupported QQ operation {operation}"
            )
        if spec is not None and not spec.requestable:
            raise QQHostError(
                "UNSUPPORTED_OPERATION",
                f"QQ operation {operation} is callback-only",
            )
        if operation != "hello":
            try:
                checked_params = validate_qq_parameters(operation, params)
            except QQOperationValidationError as exc:
                raise QQHostError(
                    "INVALID_REQUEST",
                    str(exc),
                ) from exc
        else:
            checked_params = dict(params)
        with self._state_lock:
            process = self._process
            generation = self._generation
            state = self._state
        if state in {"FAILED", "DRAINING", "STOPPED"}:
            raise QQHostError("CAPABILITY_UNAVAILABLE", "QQ Host is unavailable")
        if process is None or process.poll() is not None:
            self._mark_failed("PROCESS_EXITED", "QQ Host is not running", process)
            raise QQHostError("CAPABILITY_UNAVAILABLE", "QQ Host is not running")
        with self._pending_lock:
            self._request_counter += 1
            request_id = f"{self.binding_id}:{generation}:{self._request_counter}"
            pending = _PendingRequest(threading.Event())
            self._pending[request_id] = pending
        if request_id_sink is not None:
            request_id_sink(request_id)
        message = {
            "type": "request",
            "request_id": request_id,
            "binding_id": self.binding_id,
            "generation": generation,
            "operation": operation,
            "params": checked_params,
        }
        try:
            self._send(message)
        except (BrokenPipeError, OSError, QQHostProtocolError) as exc:
            self._remove_pending(request_id)
            if process.poll() is not None:
                self._mark_failed(
                    "PROCESS_EXITED", "QQ Host stdio write failed", process
                )
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
                self._release_binding_lock()
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
                _terminate_process_tree(process, force=False)
                try:
                    process.wait(timeout=self._config.shutdown_timeout_seconds)
                except subprocess.TimeoutExpired:
                    _terminate_process_tree(process, force=True)
                    process.wait(timeout=self._config.shutdown_timeout_seconds)
        # A well-behaved Host exits on shutdown, but it may have spawned
        # binding-local helpers.  Reap that process group even after the
        # leader has already exited.
        _terminate_process_tree(process, force=True, include_exited=True)
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
        self._release_binding_lock()

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
        host_abi = report.get("abi")
        if host_abi != self._config.required_host_abi:
            raise QQHostError("UNSUPPORTED_VERSION", "QQ Host ABI is not allow-listed")
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
            self._mark_failed("PROTOCOL_MISMATCH", str(exc), process)
        finally:
            self._fail_pending(
                QQHostError("CAPABILITY_UNAVAILABLE", "QQ Host closed stdio")
            )
            with self._state_lock:
                if self._process is process and self._state not in {
                    "DRAINING",
                    "STOPPED",
                }:
                    self._state = "FAILED"
                    if self._failure_code is None:
                        self._failure_code = (
                            "PROCESS_EXITED"
                            if process.poll() is not None
                            else "STDIO_CLOSED"
                        )
                        self._failure_message = "QQ Host closed stdio"

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
        if not isinstance(message.get("event_id"), str) or not message["event_id"]:
            raise QQHostProtocolError("QQ Host event has no event_id")
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

    def _mark_failed(
        self, code: str, message: str, process: subprocess.Popen[bytes] | None
    ) -> None:
        """Mark one still-current Host generation failed without touching others."""

        with self._state_lock:
            if process is not None and self._process is not process:
                return
            if self._state in {"DRAINING", "STOPPED"}:
                return
            self._state = "FAILED"
            self._failure_code = code
            self._failure_message = message

    def _prune_restart_history(self, now: float) -> None:
        """Discard recovery attempts outside the configured rolling window."""

        cutoff = now - self._config.restart_window_seconds
        self._restart_history[:] = [
            timestamp for timestamp in self._restart_history if timestamp >= cutoff
        ]

    def _release_binding_lock(self) -> None:
        """Unlock and close this binding's active owner-marker handle."""

        handle = self._binding_lock_handle
        self._binding_lock_handle = None
        if handle is None:
            return
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            self._record_diagnostic("QQ Host binding lock unlock failed")
        finally:
            with suppress(OSError):
                handle.close()

    def _fail_pending(self, error: QQHostError) -> None:
        with self._pending_lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = error
            item.completed.set()


def _terminate_process_tree(
    process: subprocess.Popen[bytes], *, force: bool, include_exited: bool = False
) -> None:
    """Terminate the binding-local process group without touching other bindings."""

    if process.poll() is not None and not include_exited:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            return
        except ProcessLookupError:
            return
        except OSError:
            # Fall back to the child handle if the process group disappeared.
            pass
    try:
        (process.kill if force else process.terminate)()
    except ProcessLookupError:
        return


def _claim_binding_data_dir(data_dir: Path, binding_id: str) -> Any:
    """Claim one data directory for a single binding identity.

    The marker is deliberately small and contains no account secret or QQ
    session material.  Existing QQ data may remain in the directory, but a
    second binding cannot silently reuse the same root after the first binding
    has claimed it.  The open marker handle also carries the active lock for
    the lifetime of the worker, so no separate lock file is left behind.
    """

    marker = data_dir / _BINDING_OWNER_FILE
    if marker.is_symlink() or marker.exists() and not marker.is_file():
        raise OSError("binding owner marker is not a regular file")
    metadata = {
        "schema": "cyrene.qq.binding-owner.v1",
        "binding_id": binding_id,
    }
    encoded = (
        json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(
            marker,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OSError("binding owner marker is invalid") from exc
        if not isinstance(existing, dict) or existing.get("binding_id") != binding_id:
            raise OSError("binding data directory belongs to another binding") from None
        if os.name == "posix":
            os.chmod(marker, 0o600)
    else:
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
        except OSError:
            with suppress(OSError):
                marker.unlink()
            raise
        if os.name == "posix":
            os.chmod(marker, 0o600)
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(marker, flags)
    except OSError as exc:
        raise OSError("binding owner marker cannot be locked") from exc
    handle = os.fdopen(descriptor, "a+b", closefd=True)
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            raise OSError("binding locks require the supported Linux target")
    except (BlockingIOError, OSError) as exc:
        with suppress(OSError):
            handle.close()
        raise OSError("binding data directory is already active") from exc
    return handle
