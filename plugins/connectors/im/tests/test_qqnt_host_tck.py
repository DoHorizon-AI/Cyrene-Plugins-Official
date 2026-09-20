"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_qqnt_host_tck.py                                           │
│  Module: qq_connector.tests.test_qqnt_host_tck                       │
│  Role: Named QQ Host protocol contract test kit.                    │
│                                                                     │
│  模块职责：以独立 TCK 入口验证 QQ Host 边界，不声称已连接真实 QQ。      │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from qq_connector import (
    CALLBACK_ONLY_OPERATION_NAMES,
    QQ_OPERATION_NAMES,
    QQHostClient,
    QQHostError,
    QQHostLaunchConfig,
)


def _launch_config(
    tmp_path: Path,
    binding_id: str,
    *,
    mode: str = "normal",
    timeout_seconds: float = 1.0,
    operation_log: Path | None = None,
) -> QQHostLaunchConfig:
    """Build one fixture-backed launch configuration for the TCK."""

    fake_host = Path(__file__).parent / "fixtures" / "fake_qq_host.py"
    command = [sys.executable, "-B", str(fake_host), f"--mode={mode}"]
    if operation_log is not None:
        command.append(f"--operation-log={operation_log}")
    return QQHostLaunchConfig(
        binding_id=binding_id,
        command=tuple(command),
        data_dir=tmp_path / binding_id,
        required_client_version="qq-test-1",
        required_host_abi="fake-qqnt-linux-x86_64",
        platform="linux-x86_64",
        timeout_seconds=timeout_seconds,
        startup_timeout_seconds=1.0,
        shutdown_timeout_seconds=1.0,
    )


def _request_params(operation: str) -> dict[str, Any]:
    """Return the smallest declared payload that exercises one operation."""

    if operation == "qq.message.send":
        return {
            "account_id": "10001",
            "peer": {"peer_uid": "peer-1"},
            "elements": [{"type": "text", "text": "TCK"}],
        }
    return {"account_id": "10001"}


def test_qq_host_tck_negotiates_exact_hello_and_dispatches_all_fixed_operations(
    tmp_path: Path,
) -> None:
    """Verify exact compatibility negotiation and the complete allow-list."""

    binding_id = "qq-host-tck-all-operations"
    operation_log = tmp_path / "operations.log"
    client = QQHostClient(
        _launch_config(tmp_path, binding_id, operation_log=operation_log)
    )
    try:
        compatibility = client.start()
        assert compatibility == {
            "protocol": "cyrene.qq.host.v1",
            "protocol_version": "1",
            "binding_id": binding_id,
            "generation": 1,
            "platform": "linux-x86_64",
            "client_version": "qq-test-1",
            "abi": "fake-qqnt-linux-x86_64",
        }
        assert client.state == "NATIVE_READY"

        requestable = set(QQ_OPERATION_NAMES).difference(
            CALLBACK_ONLY_OPERATION_NAMES
        )
        for operation in requestable:
            result = client.request(operation, _request_params(operation))
            assert isinstance(result, Mapping)
            if "operation" in result:
                assert result["operation"] == operation
        assert (
            set(operation_log.read_text(encoding="utf-8").splitlines())
            == requestable
        )
    finally:
        client.close()


def test_qq_host_tck_rejects_passthrough_and_callback_only_requests(
    tmp_path: Path,
) -> None:
    """Verify that only registered operation names cross the Host boundary."""

    client = QQHostClient(_launch_config(tmp_path, "qq-host-tck-boundary"))
    try:
        client.start()
        invalid_requests = (
            (
                "qq.group.list",
                {"service": "NodeIKernelGroupService"},
                "INVALID_REQUEST",
            ),
            ("qq.group.list", {"method": "getGroupList"}, "INVALID_REQUEST"),
            ("qq.group.list", {"raw_payload": {}}, "INVALID_REQUEST"),
            ("qq.kernel.raw", {}, "UNKNOWN_OPERATION"),
            ("qq.message.send_completion", {}, "UNSUPPORTED_OPERATION"),
            ("qq.group.list", {"not_declared": True}, "INVALID_REQUEST"),
            ("qq.group.list", {"account_id": True}, "INVALID_REQUEST"),
            (
                "qq.group.list",
                {"account_id": "10001", "count": -1},
                "INVALID_REQUEST",
            ),
        )
        for operation, params, code in invalid_requests:
            with pytest.raises(QQHostError) as error:
                client.request(operation, params)
            assert error.value.code == code
    finally:
        client.close()


def test_qq_host_tck_correlates_callbacks_to_the_current_request(
    tmp_path: Path,
) -> None:
    """Verify typed callback delivery retains binding and generation identity."""

    events: list[Mapping[str, Any]] = []
    event_received = threading.Event()

    def handle_event(event: Mapping[str, Any]) -> None:
        events.append(event)
        event_received.set()

    binding_id = "qq-host-tck-callbacks"
    client = QQHostClient(
        _launch_config(tmp_path, binding_id, mode="callbacks"),
        event_handler=handle_event,
    )
    request_ids: list[str] = []
    try:
        client.start()
        result = client.request(
            "qq.message.send",
            _request_params("qq.message.send"),
            request_id_sink=request_ids.append,
        )
        assert result["message_id"].startswith(f"{binding_id}-1-")
        assert event_received.wait(timeout=1.0)
        assert len(events) == 1
        event = events[0]
        assert event["event"] == "message.send_completion"
        assert len(request_ids) == 1
        assert event["request_id"] == request_ids[0]
        assert event["binding_id"] == binding_id
        assert event["generation"] == 1
    finally:
        client.close()


def test_qq_host_tck_cancellation_ignores_late_response(
    tmp_path: Path,
) -> None:
    """Verify cancellation remains local and a deliberately late response is ignored."""

    client = QQHostClient(
        _launch_config(
            tmp_path,
            "qq-host-tck-cancel",
            mode="cancel",
            timeout_seconds=1.0,
        )
    )

    class DelayedCancellation:
        def __init__(self) -> None:
            self.deadline = time.monotonic() + 0.1

        def is_cancelled(self) -> bool:
            return time.monotonic() >= self.deadline

    try:
        client.start()
        with pytest.raises(QQHostError) as error:
            client.request(
                "qq.group.detail",
                {"account_id": "10001"},
                cancellation=DelayedCancellation(),
            )
        assert error.value.code == "CANCELLED"
        assert client.state == "NATIVE_READY"
        assert client.request("qq.group.list", {"account_id": "10001"})[
            "operation"
        ] == "qq.group.list"
    finally:
        client.close()


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    (
        ("wrong_version", "UNSUPPORTED_VERSION"),
        ("wrong_abi", "UNSUPPORTED_VERSION"),
        ("wrong_binding", "PROTOCOL_MISMATCH"),
        ("wrong_generation", "PROTOCOL_MISMATCH"),
        ("malformed_hello", "PROTOCOL_MISMATCH"),
    ),
)
def test_qq_host_tck_fails_closed_on_incompatible_host_hello(
    tmp_path: Path,
    mode: str,
    expected_code: str,
) -> None:
    """Verify mismatched QQ build or protocol identity never becomes ready."""

    client = QQHostClient(_launch_config(tmp_path, f"qq-host-tck-{mode}", mode=mode))
    try:
        with pytest.raises(QQHostError) as error:
            client.start()
        assert error.value.code == expected_code
        assert client.state == "STOPPED"
    finally:
        client.close()


def test_qq_host_tck_assigns_a_new_generation_after_restart(tmp_path: Path) -> None:
    """Verify restart changes generation while preserving the binding identity."""

    binding_id = "qq-host-tck-generation"
    client = QQHostClient(_launch_config(tmp_path, binding_id))
    try:
        first = client.start()
        assert first["generation"] == 1
        client.close()
        second = client.start()
        assert second["binding_id"] == binding_id
        assert second["generation"] == 2
        assert client.generation == 2
    finally:
        client.close()


def test_qq_host_tck_rejects_a_child_tcp_listener(tmp_path: Path) -> None:
    """The inherited-stdio Host boundary must remain portless at runtime."""

    client = QQHostClient(
        _launch_config(tmp_path, "qq-host-tck-portless", mode="tcp_listener")
    )
    try:
        with pytest.raises(QQHostError) as error:
            client.start()
        assert error.value.code == "PROTOCOL_MISMATCH"
        assert "listener" in error.value.message
        assert client.state == "STOPPED"
    finally:
        client.close()


def test_qq_host_tck_stops_a_listener_opened_after_startup(tmp_path: Path) -> None:
    """The watchdog continues enforcing portless operation after hello."""

    client = QQHostClient(
        _launch_config(tmp_path, "qq-host-tck-late-portless", mode="late_tcp_listener")
    )
    try:
        client.start()
        assert client.request("qq.group.list", {"account_id": "10001"})[
            "operation"
        ] == "qq.group.list"
        deadline = time.monotonic() + 2.0
        while client.state != "FAILED" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.state == "FAILED"
        assert client.supervision["failure_code"] == "PROTOCOL_MISMATCH"
    finally:
        client.close()
