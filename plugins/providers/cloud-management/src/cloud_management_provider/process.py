"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud_management_provider.process                          │
│  Role: Run fixed vendor CLIs with bounded argv-only process control.│
│                                                                      │
│  模块职责：以有界 argv 子进程安全运行固定供应商 CLI。                 │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_MAX_ARGUMENTS = 128
_MAX_ARGUMENT_BYTES = 16_384
_MAX_OUTPUT_BYTES = 64 * 1024
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")
_TOKEN_PATTERNS = (
    re.compile(r"\bhf_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9._-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)


class ProcessConfigurationError(ValueError):
    """The fixed process request is malformed or exceeds its limits."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded process evidence returned without command-line secrets."""

    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    cancelled: bool
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_bytes: int
    stderr_bytes: int


class BoundedProcessRunner:
    """Run a profile-pinned executable with isolated environment and bounded output."""

    def run(
        self,
        executable: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
        max_output_bytes: int = 16 * 1024,
        cancel_event: threading.Event | None = None,
        secrets_to_redact: Sequence[str] = (),
    ) -> ProcessResult:
        """Start one fixed program without a shell and cap execution evidence."""

        _validate_process_request(
            executable, argv, cwd, environment, timeout_seconds, max_output_bytes
        )
        command = [executable, *argv]
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=dict(environment),
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            raise FileNotFoundError("fixed vendor CLI is unavailable") from error

        out = _BoundedCollector(process.stdout, max_output_bytes)
        err = _BoundedCollector(process.stderr, max_output_bytes)
        out.start()
        err.start()
        timed_out = False
        cancelled = False
        deadline = started + timeout_seconds
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _terminate_process_group(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_group(process)
                break
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                continue
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            process.wait(timeout=1.0)
        out.join(timeout=2.0)
        err.join(timeout=2.0)
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        return ProcessResult(
            exit_code=process.returncode,
            stdout=_redact(out.value(), secrets_to_redact),
            stderr=_redact(err.value(), secrets_to_redact),
            duration_ms=duration_ms,
            timed_out=timed_out,
            cancelled=cancelled,
            stdout_truncated=out.truncated,
            stderr_truncated=err.truncated,
            stdout_bytes=out.byte_count,
            stderr_bytes=err.byte_count,
        )


class _BoundedCollector(threading.Thread):
    """Drain one pipe while retaining no more than its configured byte cap."""

    def __init__(self, stream: object, limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._chunks: list[bytes] = []
        self._stored = 0
        self._seen = 0

    @property
    def truncated(self) -> bool:
        """Whether the child emitted bytes beyond the retained limit."""

        return self._seen > self._stored

    @property
    def byte_count(self) -> int:
        """Return the total emitted bytes without retaining the full output."""

        return self._seen

    def value(self) -> str:
        """Decode retained bytes without surfacing invalid terminal encodings."""

        return b"".join(self._chunks).decode("utf-8", errors="replace")

    def run(self) -> None:
        """Continuously drain the pipe to avoid a child blocked on full buffers."""

        if self._stream is None:
            return
        while True:
            chunk = self._stream.read(8192)
            if not chunk:
                return
            self._seen += len(chunk)
            remaining = self._limit - self._stored
            if remaining > 0:
                retained = chunk[:remaining]
                self._chunks.append(retained)
                self._stored += len(retained)


def _validate_process_request(
    executable: str,
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    max_output_bytes: int,
) -> None:
    """Reject implicit shell input, unsafe paths, and unbounded resource requests."""

    if not isinstance(executable, str) or not Path(executable).is_absolute():
        raise ProcessConfigurationError("CLI executable must be an absolute path")
    if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)):
        raise ProcessConfigurationError("CLI argv must be a sequence of strings")
    if not 1 <= len(argv) <= _MAX_ARGUMENTS or not all(
        isinstance(item, str) and "\x00" not in item for item in argv
    ):
        raise ProcessConfigurationError("CLI argv shape is invalid")
    if sum(len(item.encode("utf-8")) for item in argv) > _MAX_ARGUMENT_BYTES:
        raise ProcessConfigurationError("CLI argv exceeds its byte limit")
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ProcessConfigurationError(
            "CLI working directory must be an existing absolute directory"
        )
    if not 0 < timeout_seconds <= 120:
        raise ProcessConfigurationError("CLI timeout is outside its allowed range")
    if not 1 <= max_output_bytes <= _MAX_OUTPUT_BYTES:
        raise ProcessConfigurationError("CLI output limit is outside its allowed range")
    for key, value in environment.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key) or not isinstance(value, str):
            raise ProcessConfigurationError("CLI environment contains an invalid entry")
        if "\x00" in value:
            raise ProcessConfigurationError("CLI environment contains a NUL byte")


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the pinned process group so helper children cannot outlive the tool call."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _redact(value: str, secrets_to_redact: Sequence[str]) -> str:
    """Remove injected credentials and common token shapes from retained output."""

    result = value
    for secret in secrets_to_redact:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = _BEARER_PATTERN.sub("Bearer [REDACTED]", result)
    for pattern in _TOKEN_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result
