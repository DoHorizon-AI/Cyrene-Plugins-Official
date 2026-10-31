"""
┌──────────────────────────────────────────────────────────────────────┐
│  Module: cyrene_plugin_runtime.vendor_cli                             │
│  Role: Run fixed vendor CLIs with isolated argv-only process control.  │
│                                                                      │
│  模块职责：在隔离环境中以 argv 方式运行固定供应商 CLI。                 │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_ARGUMENTS = 128
_MAX_ARGUMENT_BYTES = 16_384
_MAX_TIMEOUT_SECONDS = 120.0
_ALLOWED_CREDENTIAL_ENV = frozenset({"GH_TOKEN", "HF_TOKEN"})
_SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"


class VendorCliConfigurationError(ValueError):
    """The fixed CLI request or host configuration is unsafe."""


@dataclass(frozen=True, slots=True)
class VendorCliResult:
    """Local process evidence without retaining or exposing CLI output."""

    exit_code: int | None
    duration_ms: int
    timed_out: bool
    cancelled: bool
    stdout_bytes: int
    stderr_bytes: int


class VendorCliRunner:
    """Run one fixed absolute executable with a fresh home and discarded output."""

    def run(
        self,
        executable: str,
        argv: Sequence[str],
        *,
        credential_environment: Mapping[str, str],
        timeout_seconds: float,
        cancellation: Any = None,
    ) -> VendorCliResult:
        """Start a fixed vendor CLI without a shell or inherited user settings.

        Args:
            executable: Host-configured absolute CLI path.
            argv: Exact validated command arguments, without an executable.
            credential_environment: Supported in-memory token variables only.
            timeout_seconds: Bounded command deadline.
            cancellation: Runtime cancellation object with an optional ``is_set``.
        Returns:
            Exit, timing, timeout, cancellation, and suppressed output byte counts.
        """

        _validate_request(executable, argv, credential_environment, timeout_seconds)
        with tempfile.TemporaryDirectory(
            prefix="cyrene-plugin-cli-", dir="/tmp"
        ) as root:
            root_path = Path(root)
            os.chmod(root_path, 0o700)
            home = root_path / "home"
            work = root_path / "work"
            temporary = root_path / "tmp"
            config = root_path / "config"
            cache = root_path / "cache"
            for directory in (home, work, temporary, config, cache):
                directory.mkdir(mode=0o700)

            environment = {
                "HOME": str(home),
                "TMPDIR": str(temporary),
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "XDG_CONFIG_HOME": str(config),
                "XDG_CACHE_HOME": str(cache),
                "GH_CONFIG_DIR": str(config / "gh"),
                "GH_HOST": "github.com",
                "HF_HOME": str(cache / "huggingface"),
                "HF_ENDPOINT": "https://huggingface.co",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "HF_HUB_DISABLE_UPDATE_CHECK": "1",
                "PATH": _SAFE_PATH,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "NO_COLOR": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GH_PROMPT_DISABLED": "1",
                "GH_NO_UPDATE_NOTIFIER": "1",
            }
            environment.update(credential_environment)
            return _run_process(
                executable,
                argv,
                cwd=work,
                environment=environment,
                timeout_seconds=timeout_seconds,
                cancellation=cancellation,
            )


class _DiscardCollector(threading.Thread):
    """Drain output pipes without retaining any bytes in memory."""

    def __init__(self, stream: Any) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self.byte_count = 0

    def run(self) -> None:
        """Drain the pipe so the child cannot block on a full output buffer."""

        if self._stream is None:
            return
        while True:
            chunk = self._stream.read(8192)
            if not chunk:
                return
            self.byte_count += len(chunk)


def _validate_request(
    executable: str,
    argv: Sequence[str],
    credential_environment: Mapping[str, str],
    timeout_seconds: float,
) -> None:
    """Reject shell input, ambient credentials, and unbounded invocations."""

    if not isinstance(executable, str) or not Path(executable).is_absolute():
        raise VendorCliConfigurationError("CLI executable must be an absolute path")
    if (
        not isinstance(argv, Sequence)
        or isinstance(argv, (str, bytes))
        or not 1 <= len(argv) <= _MAX_ARGUMENTS
        or not all(isinstance(item, str) and "\x00" not in item for item in argv)
        or sum(len(item.encode("utf-8")) for item in argv) > _MAX_ARGUMENT_BYTES
    ):
        raise VendorCliConfigurationError("CLI argv is invalid or exceeds its limit")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS
    ):
        raise VendorCliConfigurationError("CLI timeout is outside its allowed range")
    if (
        not isinstance(credential_environment, Mapping)
        or set(credential_environment) - _ALLOWED_CREDENTIAL_ENV
    ):
        raise VendorCliConfigurationError("CLI credential environment is not allowed")
    for value in credential_environment.values():
        if not isinstance(value, str) or not value or "\x00" in value or "\n" in value:
            raise VendorCliConfigurationError("CLI credential value is invalid")
        if any(value in argument for argument in argv):
            raise VendorCliConfigurationError("CLI credentials must not appear in argv")


def _run_process(
    executable: str,
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    cancellation: Any,
) -> VendorCliResult:
    """Run and stop one process group while recording only bounded metadata."""

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [executable, *argv],
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

    stdout = _DiscardCollector(process.stdout)
    stderr = _DiscardCollector(process.stderr)
    stdout.start()
    stderr.start()
    deadline = started + timeout_seconds
    timed_out = False
    cancelled = False
    while process.poll() is None:
        if _is_cancelled(cancellation):
            cancelled = True
            _terminate_group(process)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _terminate_group(process)
            break
        try:
            process.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            continue
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        _terminate_group(process)
        process.wait(timeout=1.0)
    stdout.join(timeout=2.0)
    stderr.join(timeout=2.0)
    return VendorCliResult(
        exit_code=process.returncode,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        timed_out=timed_out,
        cancelled=cancelled,
        stdout_bytes=stdout.byte_count,
        stderr_bytes=stderr.byte_count,
    )


def _is_cancelled(cancellation: Any) -> bool:
    """Read the optional runtime cancellation interface without assuming a type."""

    is_set = getattr(cancellation, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the process group so child commands cannot outlive the invocation."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
