"""Tests for bounded argv execution, output redaction, timeout, and cancellation."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from cloud_management_provider.process import BoundedProcessRunner


def test_runner_uses_argv_and_bounds_redacted_output(tmp_path: Path) -> None:
    """The process runner retains only capped output and masks injected values."""

    secret = "host-injected-secret-12345"
    runner = BoundedProcessRunner()
    result = runner.run(
        sys.executable,
        ["-c", "import sys; print('prefix host-injected-secret-12345 ' + 'x' * 200)"],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=5,
        max_output_bytes=64,
        secrets_to_redact=[secret],
    )

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.stdout_truncated is True
    assert secret not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert len(result.stdout.encode("utf-8")) <= 64


def test_runner_kills_timed_out_process_group(tmp_path: Path) -> None:
    """A CLI timeout terminates the process instead of leaving it in the background."""

    result = BoundedProcessRunner().run(
        sys.executable,
        ["-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=0.1,
        max_output_bytes=128,
    )

    assert result.timed_out is True
    assert result.exit_code is not None


def test_runner_observes_external_cancellation(tmp_path: Path) -> None:
    """A host cancellation event kills the local process tree promptly."""

    cancellation = threading.Event()
    cancellation.set()
    result = BoundedProcessRunner().run(
        sys.executable,
        ["-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=5,
        max_output_bytes=128,
        cancel_event=cancellation,
    )

    assert result.cancelled is True
    assert result.exit_code is not None
