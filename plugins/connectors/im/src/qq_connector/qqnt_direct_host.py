"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 qqnt_direct_host.py                                             │
│  Module: qq_connector.qqnt_direct_host                               │
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
import sys
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
    """Structured error produced by the QQ Host process boundary.

        中文:QQ Host 进程边界产生的结构化错误。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class QQHostLaunchConfig:
    """Immutable launch inputs for exactly one binding and one process tree.

        中文:仅供一个 binding 和一个进程树使用的不可变启动输入。
    """

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
    """One generation-scoped request waiting for a Host response.

        中文:一条按代次关联、正在等待 Host 响应的请求。
    """

    completed: threading.Event
    result: Any = None
    error: QQHostError | None = None


class QQHostClient:
    """Supervise one authorized QQ Host child over pipes only.

    The child receives no listening address and the client never imports or
    creates a network transport.  Every request identifier contains the
    binding and current worker generation, so a late callback cannot cross a
    restart or another binding.

        中文：仅通过管道监督一个已授权的 QQ Host 子进程。子进程不会收到任何监听地址,
        客户端也不会导入或创建网络传输。每个请求标识都包含 binding 和当前 Worker 代次,
        因此迟到的回调不会跨越进程重启或落入其他 binding。
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
        self._listener_watch_thread: threading.Thread | None = None
        self._listener_watch_stop = threading.Event()
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
        """Return the immutable binding identity assigned to this client.

            中文:返回分配给此客户端的不可变 binding 身份。
        """

        return self._config.binding_id

    @property
    def generation(self) -> int:
        """Return the current process generation, or zero before first start.

            中文:返回当前进程代次;首次启动之前返回 0。
        """

        with self._state_lock:
            return self._generation

    @property
    def state(self) -> str:
        """Return the current lifecycle state.

            中文:返回当前生命周期状态。
        """

        with self._state_lock:
            return self._state

    @property
    def compatibility(self) -> Mapping[str, Any]:
        """Return the bounded Host hello report without credentials.

            中文:返回不含凭据的有界 Host hello 报告。
        """

        with self._state_lock:
            return dict(self._compatibility)

    @property
    def diagnostics(self) -> tuple[str, ...]:
        """Return bounded, redacted diagnostics useful for operator evidence.

            中文:返回有界且已脱敏、可供操作人员留存证据的诊断信息。
        """

        with self._state_lock:
            return tuple(self._diagnostics)

    @property
    def supervision(self) -> Mapping[str, Any]:
        """Return bounded restart and crash-circuit state for diagnostics.

            中文:返回用于诊断的有界重启与崩溃熔断状态。
        """

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

            中文:启动子进程、协商协议兼容性并返回协商报告。

Raises:如果子进程无法启动,或未通过针对协议、binding、平台或 QQ 客户端版本的精确校验,
则抛出 QQHostError。
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
            self._listener_watch_stop.clear()
            self._listener_watch_thread = threading.Thread(
                target=self._listener_watch_loop,
                name=f"qq-host-portless-{self.binding_id}-{generation}",
                daemon=True,
            )
            self._reader_thread.start()
            self._stderr_thread.start()
            self._listener_watch_thread.start()

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
            _assert_no_tcp_listener(process)
        except QQHostError:
            self.close()
            raise
        with self._state_lock:
            self._state = "NATIVE_READY"
        return dict(self._compatibility)

    def restart(self) -> Mapping[str, Any]:
        """Drain the current child and start a new generation for this binding.

            中文:排空当前子进程,并为此 binding 启动一个新的代次。
        """

        self.close()
        return self.start()

    def recover(self) -> Mapping[str, Any]:
        """Perform one bounded crash recovery without retrying a QQ operation.

        Only an unexpected process/stdio exit is restartable.  The operation
        that observed the failure is never replayed; the caller may retry a
        later operation after the new generation has initialized its session.

            中文：执行一次有界崩溃恢复,但不重试 QQ 操作。
            只有意外的进程／标准输入输出退出才允许重启。观察到故障的操作绝不会重放;
            新代次完成 session 初始化后,调用方可以重试后续操作。
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
        """Send one fixed operation and await its generation-scoped response.

            中文:发送一个固定操作,并等待与当前代次关联的响应。
        """

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
        """Request shutdown, then boundedly reap the entire child process.

            中文:请求关闭,然后在有界时间内回收整个子进程。
        """

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
        # 中文：正常的 Host 会响应关闭请求并退出,但它可能已经派生了属于当前 binding
        # 的辅助进程。因此,即使 leader 已退出,也要回收整个进程组。
        _terminate_process_tree(process, force=True, include_exited=True)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (BrokenPipeError, OSError):
                    self._record_diagnostic(
                        "QQ Host process stream was already closed"
                    )
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=self._config.shutdown_timeout_seconds)
        self._listener_watch_stop.set()
        listener_watch_thread = self._listener_watch_thread
        if (
            listener_watch_thread is not None
            and listener_watch_thread is not threading.current_thread()
        ):
            listener_watch_thread.join(timeout=self._config.shutdown_timeout_seconds)
        self._fail_pending(QQHostError("CAPABILITY_UNAVAILABLE", "QQ Host stopped"))
        with self._state_lock:
            self._process = None
            self._reader_thread = None
            self._stderr_thread = None
            self._listener_watch_thread = None
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

    def _listener_watch_loop(self) -> None:
        """Fail closed if the binding-local Host process tree opens TCP LISTEN.

            中文:如果 binding 本地的 Host 进程树打开了 TCP LISTEN listener,则失败关闭。
        """

        process = self._process
        if process is None:
            return
        while not self._listener_watch_stop.wait(0.25):
            try:
                _assert_no_tcp_listener(process)
            except QQHostError as exc:
                self._fail_pending(exc)
                self._mark_failed(exc.code, exc.message, process)
                _terminate_process_tree(process, force=True)
                return

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
        """Mark one still-current Host generation failed without touching others.

            中文:仅将一个仍为当前代次的 Host 标记为失败,不触碰其他代次。
        """

        with self._state_lock:
            if process is not None and self._process is not process:
                return
            if self._state in {"DRAINING", "STOPPED"}:
                return
            self._state = "FAILED"
            self._failure_code = code
            self._failure_message = message

    def _prune_restart_history(self, now: float) -> None:
        """Discard recovery attempts outside the configured rolling window.

            中文:丢弃落在已配置滚动时间窗之外的恢复尝试。
        """

        cutoff = now - self._config.restart_window_seconds
        self._restart_history[:] = [
            timestamp for timestamp in self._restart_history if timestamp >= cutoff
        ]

    def _release_binding_lock(self) -> None:
        """Unlock and close this binding's active owner-marker handle.

            中文:解锁并关闭此 binding 当前持有的 owner-marker 句柄。
        """

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
    """Terminate the binding-local process group without touching other bindings.

        中文:终止 binding 本地的进程组,不触碰其他 binding。
    """

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
            # 中文：如果进程组已消失，则回退使用子进程句柄。
            pass
    try:
        (process.kill if force else process.terminate)()
    except ProcessLookupError:
        return


def _assert_no_tcp_listener(process: subprocess.Popen[bytes]) -> None:
    """Reject a Host process tree that owns an IPv4/IPv6 TCP listener.

    The direct profile may use QQ's outbound network connections, but the
    connector/Host IPC boundary is inherited stdio and must remain portless.
    This Linux-only probe maps socket inodes held by the child and descendants
    to the kernel's LISTEN tables.  Missing or already-exited /proc entries are
    treated as a race with process shutdown, not as a listener.

        中文：拒绝拥有 IPv4／IPv6 TCP listener 的 Host 进程树。
        direct profile 可以使用 QQ 的出站网络连接,
        但 connector 与 Host 之间的 IPC 必须通过继承的标准输入输出进行,
        并且不能开放端口。这个仅限 Linux 的探测会把子进程及其后代持有的 socket inode
        映射到内核 LISTEN 表。缺失或已退出的 `/proc` 条目视为进程关闭期间的竞态,
        而不是 listener。
    """

    if sys.platform != "linux" or process.poll() is not None:
        return
    listening_inodes = _linux_tcp_listening_inodes()
    if not listening_inodes:
        return
    for pid in _linux_process_tree(process.pid):
        fd_root = Path(f"/proc/{pid}/fd")
        try:
            entries = tuple(fd_root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            if not target.startswith("socket:[") or not target.endswith("]"):
                continue
            inode = target[8:-1]
            if inode in listening_inodes:
                raise QQHostError(
                    "PROTOCOL_MISMATCH",
                    "QQ Host process tree opened a TCP listener",
                )


def _linux_tcp_listening_inodes() -> set[str]:
    """Return socket inodes in the Linux IPv4/IPv6 TCP LISTEN state.

        中文:返回处于 Linux IPv4／IPv6 TCP LISTEN 状态的 socket inode。
    """

    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            # /proc/net/tcp: sl local_address rem_address st ... uid timeout inode
            # 中文：/proc/net/tcp 字段依次为 sl、local_address、
            # rem_address、st、...、uid、timeout、inode。
            if len(fields) > 9 and fields[3].upper() == "0A":
                inodes.add(fields[9])
    return inodes


def _linux_process_tree(root_pid: int) -> tuple[int, ...]:
    """Return a best-effort snapshot of one process and its descendants.

        中文:尽力返回一个进程及其后代进程的快照。
    """

    parents: dict[int, int] = {}
    proc_root = Path("/proc")
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return (root_pid,)
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat_line = (entry / "stat").read_text(encoding="ascii")
            closing = stat_line.rfind(")")
            fields = stat_line[closing + 2 :].split()
            # After the comm field: state, ppid, pgrp, ...
            # 中文：comm 字段之后依次是 state、ppid、pgrp 等字段。
            if len(fields) > 2:
                parents[int(entry.name)] = int(fields[1])
        except (OSError, ValueError):
            continue
    result = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in result and pid not in result:
                result.add(pid)
                changed = True
    return tuple(result)


def _claim_binding_data_dir(data_dir: Path, binding_id: str) -> Any:
    """Claim one data directory for a single binding identity.

    The marker is deliberately small and contains no account secret or QQ
    session material.  Existing QQ data may remain in the directory, but a
    second binding cannot silently reuse the same root after the first binding
    has claimed it.  The open marker handle also carries the active lock for
    the lifetime of the worker, so no separate lock file is left behind.

        中文：为单一 binding 身份独占一个数据目录。标记文件有意保持精简,
        不包含账户密钥或 QQ session 材料。目录中可以保留已有 QQ 数据,
        但某个 binding 认领根目录后,第二个 binding 不能静默复用它。
        打开的标记文件句柄会在 Worker 的整个生命周期内持有活动锁,
        因此无需留下单独的锁文件。
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
