"""
┌──────────────────────────────────────────────────────────────────────┐
│  Module: cyrene_plugin_runtime.tests.test_vendor_cli                 │
│  Role: Verify bounded isolated vendor CLI process behavior.          │
│                                                                      │
│  模块职责：验证CLI隔离环境、输出抑制和执行期限。                        │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import sys
import threading

import pytest
from cyrene_plugin_runtime.vendor_cli import (
    VendorCliConfigurationError,
    VendorCliRunner,
)


def test_runner_uses_clean_home_and_only_explicit_provider_token(monkeypatch) -> None:
    monkeypatch.setenv("CYRENE_CLI_TEST_AMBIENT", "must-not-inherit")
    script = (
        "import os,sys; sys.exit(0 if len(os.getenv('HF_TOKEN', '')) == 13 "
        "and 'CYRENE_CLI_TEST_AMBIENT' not in os.environ "
        "and os.path.basename(os.environ['HOME']) == 'home' "
        "and os.getcwd().endswith('/work') else 41)"
    )
    result = VendorCliRunner().run(
        sys.executable,
        ["-c", script],
        credential_environment={"HF_TOKEN": "fixture-token"},
        timeout_seconds=2,
    )

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.cancelled is False
    assert not hasattr(result, "stdout")
    assert not hasattr(result, "stderr")


def test_runner_discards_large_stdout_and_stderr_without_retaining_content() -> None:
    result = VendorCliRunner().run(
        sys.executable,
        [
            "-c",
            "import sys; sys.stdout.write('x' * 100000); sys.stderr.write('y' * 20000)",
        ],
        credential_environment={},
        timeout_seconds=2,
    )

    assert result.exit_code == 0
    assert result.stdout_bytes == 100000
    assert result.stderr_bytes == 20000
    assert not hasattr(result, "stdout")
    assert not hasattr(result, "stderr")


def test_runner_passes_shell_metacharacters_as_plain_argv(tmp_path) -> None:
    marker = tmp_path / "must-not-exist"
    injected = f"; touch {marker}"
    script = "import sys; sys.exit(0 if sys.argv[1] == sys.argv[2] else 1)"

    result = VendorCliRunner().run(
        sys.executable,
        ["-c", script, injected, injected],
        credential_environment={},
        timeout_seconds=2,
    )

    assert result.exit_code == 0
    assert marker.exists() is False


def test_runner_stops_a_timed_out_cli() -> None:
    result = VendorCliRunner().run(
        sys.executable,
        ["-c", "import time; time.sleep(5)"],
        credential_environment={},
        timeout_seconds=0.1,
    )

    assert result.timed_out is True
    assert result.exit_code is not None


def test_runner_stops_a_cancelled_cli() -> None:
    cancellation = threading.Event()
    threading.Timer(0.1, cancellation.set).start()
    result = VendorCliRunner().run(
        sys.executable,
        ["-c", "import time; time.sleep(5)"],
        credential_environment={},
        timeout_seconds=2,
        cancellation=cancellation,
    )

    assert result.cancelled is True
    assert result.exit_code is not None


@pytest.mark.parametrize(
    ("executable", "argv", "credential_environment", "timeout_seconds"),
    [
        ("gh", ["repo", "list"], {}, 1),
        ("/usr/local/bin/gh", "repo list", {}, 1),
        ("/usr/local/bin/gh", ["repo", "list"], {"HTTP_PROXY": "https://bad"}, 1),
        ("/usr/local/bin/gh", ["repo", "list"], {}, 121),
        ("/usr/local/bin/gh", ["repo\x00list"], {}, 1),
        ("/usr/local/bin/hf", ["repo", "info", "fixture-secret"], {"HF_TOKEN": "fixture-secret"}, 1),
    ],
)
def test_runner_rejects_nonfixed_or_unbounded_requests(
    executable, argv, credential_environment, timeout_seconds
) -> None:
    with pytest.raises(VendorCliConfigurationError):
        VendorCliRunner().run(
            executable,
            argv,
            credential_environment=credential_environment,
            timeout_seconds=timeout_seconds,
        )
