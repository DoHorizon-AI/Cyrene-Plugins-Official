from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from cyrene_public_web.plugin import PublicWebError, _wait_for_parser_process


def _sleeping_process() -> subprocess.Popen[bytes]:
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **options,
    )


def test_parser_worker_timeout_kills_the_child_process_group() -> None:
    process = _sleeping_process()
    started = time.monotonic()
    with pytest.raises(PublicWebError, match="PARSER_TIMEOUT"):
        _wait_for_parser_process(process, timeout_seconds=0.2, cancellation=None)
    assert process.poll() is not None
    assert time.monotonic() - started < 1.5


def test_parser_worker_cancellation_kills_the_child_process_group() -> None:
    process = _sleeping_process()
    cancellation = threading.Event()
    timer = threading.Timer(0.1, cancellation.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(InterruptedError):
            _wait_for_parser_process(
                process,
                timeout_seconds=3,
                cancellation=cancellation,
            )
    finally:
        timer.cancel()
    assert process.poll() is not None
    assert time.monotonic() - started < 1.5
