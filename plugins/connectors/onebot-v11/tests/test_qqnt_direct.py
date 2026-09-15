"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_qqnt_direct.py                                             │
│  Module: onebot_v11_connector.tests.test_qqnt_direct                │
│  Role: Protocol, mapping, isolation, and subprocess lifecycle tests. │
│                                                                     │
│  模块职责：验证 QQNT direct 的协议、映射、隔离与进程生命周期。           │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from onebot_v11_connector import (
    QQ_CAPABILITY_ID,
    QQ_OPERATION_NAMES,
    QQ_REQUEST_TYPE_URL,
    QQ_RESPONSE_TYPE_URL,
    ConnectorError,
    ConnectorPlugin,
    QQHostProtocolError,
    QQNTDirectConnector,
    encode_frame,
    read_frame,
)
from onebot_v11_connector._generated import message_connector_pb2 as message_contract


class RecordingEmitter:
    """Small direct-runtime emitter that records typed events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, bytes, str]] = []

    def emit(self, event_type: str, payload: bytes, type_url: str = "") -> bool:
        self.events.append((event_type, payload, type_url))
        return True


class CancelledToken:
    def is_cancelled(self) -> bool:
        return True


def _config(
    tmp_path: Path,
    binding_id: str,
    *,
    mode: str = "normal",
    timeout_seconds: float = 1.0,
) -> dict[str, Any]:
    fake_host = Path(__file__).parent / "fixtures" / "fake_qq_host.py"
    return {
        "runtime_profile": "qqnt-direct",
        "binding_id": binding_id,
        "host_executable": sys.executable,
        "host_args": ["-B", str(fake_host), f"--mode={mode}"],
        "data_dir": str(tmp_path / binding_id),
        "required_client_version": "qq-test-1",
        "account_id": "10001",
        "timeout_seconds": timeout_seconds,
        "secret_refs": ["secret://test/qq-password"],
    }


def _send_request() -> dict[str, Any]:
    return {
        "conversation": {
            "vendor": "qq",
            "account_id": "10001",
            "conversation_id": "20001",
            "kind": "group",
        },
        "content": [{"text": {"text": "hello"}}],
    }


def test_stdio_frame_is_big_endian_and_rejects_malformed_payloads() -> None:
    frame = encode_frame({"type": "hello", "value": "中文"})
    assert frame[:4] == (len(frame) - 4).to_bytes(4, "big")
    assert read_frame(BytesIO(frame)) == {"type": "hello", "value": "中文"}
    with pytest.raises(QQHostProtocolError):
        read_frame(BytesIO((3).to_bytes(4, "big") + b"bad"[:-1]))
    with pytest.raises(QQHostProtocolError):
        read_frame(BytesIO((1).to_bytes(4, "big") + b"["))


def test_api_matrix_covers_every_fixed_operation() -> None:
    matrix = (Path(__file__).parents[1] / "QQNT_DIRECT_API_MATRIX.md").read_text(
        encoding="utf-8"
    )
    documented = set(re.findall(r"`(qq\.[a-z0-9_.]+)`", matrix))
    assert set(QQ_OPERATION_NAMES).issubset(documented)


def test_direct_config_rejects_onebot_transport_fields(tmp_path: Path) -> None:
    config = _config(tmp_path, "qq-main")
    config["http_base_url"] = "http://127.0.0.1:8080"
    with pytest.raises(Exception, match="unknown fields|OneBot endpoint"):
        QQNTDirectConnector(config)


def test_direct_extension_rejects_undeclared_parameter_names(tmp_path: Path) -> None:
    connector = QQNTDirectConnector(_config(tmp_path, "qq-closed-params"))
    try:
        with pytest.raises(ConnectorError, match="undeclared fields"):
            connector.invoke_extension(
                "qq.group.list", {"account_id": "10001", "not_declared": True}
            )
        assert connector.generation == 0
    finally:
        connector.close()


@pytest.mark.parametrize(
    "mode",
    ["wrong_version", "wrong_binding", "wrong_generation", "malformed_hello"],
)
def test_hello_incompatibility_fails_closed(tmp_path: Path, mode: str) -> None:
    connector = QQNTDirectConnector(_config(tmp_path, f"qq-{mode}", mode=mode))
    try:
        error = connector.on_subscribe(
            "sub", "message.connector.v1", b"{}", RecordingEmitter()
        )
        assert error is not None
        assert any(
            code in error for code in ("PROTOCOL_MISMATCH", "UNSUPPORTED_VERSION")
        )
        assert connector.state == "FAILED"
    finally:
        connector.close()


def test_protocol_failure_marks_host_unavailable(tmp_path: Path) -> None:
    connector = QQNTDirectConnector(
        _config(tmp_path, "qq-malformed-event", mode="malformed_event")
    )
    emitter = RecordingEmitter()
    try:
        assert (
            connector.on_subscribe("sub", "message.connector.v1", b"{}", emitter)
            is None
        )
        deadline = time.monotonic() + 1.0
        while connector.state != "FAILED" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert connector.state == "FAILED"
        with pytest.raises(ConnectorError) as error:
            connector.invoke_extension("qq.group.list", {"account_id": "10001"})
        assert error.value.code == "CAPABILITY_UNAVAILABLE"
    finally:
        connector.close()


def test_host_diagnostics_redact_credential_like_values(tmp_path: Path) -> None:
    connector = QQNTDirectConnector(
        _config(tmp_path, "qq-redacted-diagnostics", mode="stderr_secret")
    )
    try:
        connector.invoke_extension("qq.group.list", {"account_id": "10001"})
        deadline = time.monotonic() + 1.0
        diagnostics = ""
        while not diagnostics and time.monotonic() < deadline:
            diagnostics = "\n".join(connector._host.diagnostics)  # noqa: SLF001
            if not diagnostics:
                time.sleep(0.01)
        assert "fixture-password" not in diagnostics
        assert "fixture-token" not in diagnostics
        assert "<redacted>" in diagnostics
    finally:
        connector.close()


def test_binding_data_directory_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real-data"
    target.mkdir()
    link = tmp_path / "linked-data"
    link.symlink_to(target, target_is_directory=True)
    config = _config(tmp_path, "qq-symlink")
    config["data_dir"] = str(link)
    connector = QQNTDirectConnector(config)
    try:
        with pytest.raises(ConnectorError) as error:
            connector.invoke_extension("qq.group.list", {"account_id": "10001"})
        assert error.value.code == "CAPABILITY_UNAVAILABLE"
        assert connector.generation == 1
    finally:
        connector.close()


def test_login_state_machine_requires_login_then_becomes_ready(tmp_path: Path) -> None:
    connector = QQNTDirectConnector(
        _config(tmp_path, "qq-login-state", mode="login_state")
    )
    try:
        ok, result = connector.on_invoke(
            QQ_CAPABILITY_ID,
            "qq.login.quick",
            json.dumps({"params": {"uin": "10001"}}).encode(),
            request_type_url=QQ_REQUEST_TYPE_URL,
        )
        assert ok, result
        assert connector.state == "READY"
    finally:
        connector.close()


def test_login_failure_and_account_mismatch_fail_closed(tmp_path: Path) -> None:
    failed_login = QQNTDirectConnector(
        _config(tmp_path, "qq-login-failed", mode="login_failed")
    )
    mismatched = QQNTDirectConnector(
        _config(tmp_path, "qq-account-mismatch", mode="account_mismatch")
    )
    try:
        ok, error = failed_login.on_invoke(
            QQ_CAPABILITY_ID,
            "qq.login.quick",
            json.dumps({"params": {"uin": "10001"}}).encode(),
            request_type_url=QQ_REQUEST_TYPE_URL,
        )
        assert not ok
        assert "login failed" in error.lower()
        assert failed_login.state == "FAILED"

        ok, error = mismatched.on_invoke(
            QQ_CAPABILITY_ID,
            "qq.login.self_status",
            json.dumps({"params": {"account_id": "10001"}}).encode(),
            request_type_url=QQ_REQUEST_TYPE_URL,
        )
        assert not ok
        assert "ACCOUNT_MISMATCH" in error
        assert mismatched.state == "FAILED"
    finally:
        failed_login.close()
        mismatched.close()


def test_fake_host_start_send_receive_and_shutdown_without_tcp_listener(
    tmp_path: Path,
) -> None:
    connector = QQNTDirectConnector(_config(tmp_path, "qq-main"))
    emitter = RecordingEmitter()
    try:
        assert (
            connector.on_subscribe("sub-main", "message.connector.v1", b"{}", emitter)
            is None
        )
        result = connector.send_message(_send_request())
        assert result["status"] == "accepted"
        assert result["vendor_message_id"].startswith("qq-main-1-")
        assert len(emitter.events) == 1
        event_type, raw_payload, type_url = emitter.events[0]
        assert event_type == "inbound_message"
        assert type_url.endswith("InboundMessagePayload")
        inbound = message_contract.InboundMessagePayload.FromString(raw_payload)
        assert inbound.conversation.vendor == "qq"
        assert inbound.conversation.account_id == "10001"
        assert inbound.conversation.conversation_id == "20001"
        assert inbound.content[0].text.text == "hello from qq"
        assert connector.compatibility["client_version"] == "qq-test-1"
        assert connector.generation == 1
    finally:
        connector.close()
    assert connector.state == "STOPPED"


def test_plugin_entrypoint_routes_qqnt_direct_profile_to_direct_adapter(
    tmp_path: Path,
) -> None:
    plugin = ConnectorPlugin(_config(tmp_path, "qq-plugin-entrypoint"))
    try:
        ok, result = plugin.on_invoke(
            QQ_CAPABILITY_ID,
            "qq.group.list",
            json.dumps({"params": {"account_id": "10001"}}).encode("utf-8"),
            request_type_url=QQ_REQUEST_TYPE_URL,
        )
        assert ok
        body = json.loads(result.value.decode("utf-8"))
        assert body["operation"] == "qq.group.list"
        assert plugin.runtime_profile == "qqnt-direct"
    finally:
        plugin.close()


def test_extension_is_fixed_and_uses_explicit_qq_client_contract(
    tmp_path: Path,
) -> None:
    connector = QQNTDirectConnector(_config(tmp_path, "qq-extension"))
    try:
        ok, result = connector.on_invoke(
            QQ_CAPABILITY_ID,
            "qq.group.list",
            json.dumps({"params": {"account_id": "10001"}}).encode(),
            request_type_url=QQ_REQUEST_TYPE_URL,
        )
        assert ok
        assert result.type_url == QQ_RESPONSE_TYPE_URL
        body = json.loads(result.value.decode("utf-8"))
        assert body["operation"] == "qq.group.list"
        assert body["mapping"] == {
            "service": "NodeIKernelGroupService",
            "method": "getGroupList",
        }

        bad, error = connector.on_invoke(
            QQ_CAPABILITY_ID,
            "qq.group.list",
            json.dumps({"params": {"service": "bad"}}).encode(),
            request_type_url=QQ_REQUEST_TYPE_URL,
        )
        assert not bad
        assert "reserved fields" in error
    finally:
        connector.close()


def test_group_invite_approval_uses_fixed_native_notification_operation(
    tmp_path: Path,
) -> None:
    connector = QQNTDirectConnector(_config(tmp_path, "qq-approval"))
    try:
        result = connector.respond_request(
            {
                "request_id": "notify-1",
                "request_kind": "group_invite",
                "decision": "approve",
                "vendor_request": {"group_code": "20001"},
            }
        )
        assert result["status"] == "accepted"
        assert result["request_kind"] == "group_invite"
    finally:
        connector.close()


def test_every_registered_qq_operation_has_a_fixed_fake_host_dispatch(
    tmp_path: Path,
) -> None:
    connector = QQNTDirectConnector(_config(tmp_path, "qq-operations"))
    try:
        for operation in QQ_OPERATION_NAMES:
            params: dict[str, Any] = {"account_id": "10001"}
            if operation == "qq.login.password":
                params = {"secret_ref": "secret://test/qq-password"}
            ok, result = connector.on_invoke(
                QQ_CAPABILITY_ID,
                operation,
                json.dumps({"params": params}).encode("utf-8"),
                request_type_url=QQ_REQUEST_TYPE_URL,
            )
            assert ok, (operation, result)
            body = json.loads(result.value.decode("utf-8"))
            assert body["operation"] == operation
            assert body["status"] == "accepted"
    finally:
        connector.close()


def test_canonical_protobuf_send_maps_directly_to_native_message(
    tmp_path: Path,
) -> None:
    connector = QQNTDirectConnector(_config(tmp_path, "qq-proto"))
    request = message_contract.SendMessageRequest(
        conversation=message_contract.ConversationScope(
            vendor="qq",
            account_id="10001",
            conversation_id="20001",
            kind=message_contract.CONVERSATION_KIND_GROUP,
        ),
        content=[
            message_contract.MessageContentPart(
                text=message_contract.TextContent(text="proto")
            )
        ],
    )
    try:
        ok, result = connector.on_invoke(
            "message.connector.v1",
            "send_message",
            request.SerializeToString(),
            request_type_url="type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest",
        )
        assert ok
        response = message_contract.DeliveryResult.FromString(result.value)
        assert response.status == message_contract.DELIVERY_STATUS_ACCEPTED
        assert response.vendor_message_id.startswith("qq-proto-1-")
    finally:
        connector.close()


def test_two_bindings_and_restart_keep_generation_and_events_isolated(
    tmp_path: Path,
) -> None:
    first = QQNTDirectConnector(_config(tmp_path, "qq-first"))
    second = QQNTDirectConnector(_config(tmp_path, "qq-second"))
    first_emitter = RecordingEmitter()
    second_emitter = RecordingEmitter()
    try:
        assert (
            first.on_subscribe(
                "sub-first", "message.connector.v1", b"{}", first_emitter
            )
            is None
        )
        assert (
            second.on_subscribe(
                "sub-second", "message.connector.v1", b"{}", second_emitter
            )
            is None
        )
        first.send_message(_send_request())
        second.send_message(_send_request())
        assert len(first_emitter.events) == 1
        assert len(second_emitter.events) == 1
        assert b"qq-main" not in first_emitter.events[0][1]
        assert first.generation == 1
        first._host.restart()  # noqa: SLF001 - lifecycle isolation assertion
        assert first.generation == 2
        assert second.generation == 1
        assert first.send_message(_send_request())["vendor_message_id"].startswith(
            "qq-first-2-"
        )
    finally:
        first.close()
        second.close()


def test_timeout_cancellation_and_late_response_are_generation_safe(
    tmp_path: Path,
) -> None:
    connector = QQNTDirectConnector(
        _config(tmp_path, "qq-timeout", mode="timeout", timeout_seconds=0.15)
    )
    try:
        with pytest.raises(ConnectorError) as error:
            connector.invoke_extension("qq.group.detail", {"account_id": "10001"})
        assert error.value.code == "TIMEOUT"
        time.sleep(0.05)
        assert connector.generation == 1
        result = connector.invoke_extension("qq.group.list", {"account_id": "10001"})
        assert result["status"] == "accepted"
    finally:
        connector.close()


def test_cancellation_sends_cancel_and_ignores_late_response(tmp_path: Path) -> None:
    class DelayedCancellation:
        def __init__(self) -> None:
            self.deadline = time.monotonic() + 0.15

        def is_cancelled(self) -> bool:
            return time.monotonic() >= self.deadline

    connector = QQNTDirectConnector(
        _config(tmp_path, "qq-cancel-frame", mode="cancel", timeout_seconds=1.0)
    )
    try:
        with pytest.raises(ConnectorError) as error:
            connector.invoke_extension(
                "qq.group.detail",
                {"account_id": "10001"},
                cancellation=DelayedCancellation(),
            )
        assert error.value.code == "CANCELLED"
        assert connector.generation == 1
    finally:
        connector.close()


def test_out_of_order_and_duplicate_responses_are_correlated(tmp_path: Path) -> None:
    connector = QQNTDirectConnector(
        _config(tmp_path, "qq-order", mode="out_of_order")
    )
    try:
        connector.on_subscribe("sub", "message.connector.v1", b"{}", RecordingEmitter())
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    connector.invoke_extension,
                    operation,
                    {"account_id": "10001"},
                )
                for operation in ("qq.group.list", "qq.group.detail")
            ]
            results = [future.result() for future in futures]
        assert {result["operation"] for result in results} == {
            "qq.group.list",
            "qq.group.detail",
        }

        duplicate = QQNTDirectConnector(
            _config(tmp_path, "qq-duplicate", mode="duplicate_response")
        )
        try:
            result = duplicate.invoke_extension(
                "qq.group.list", {"account_id": "10001"}
            )
            assert result["operation"] == "qq.group.list"
        finally:
            duplicate.close()
    finally:
        connector.close()


def test_duplicate_events_are_emitted_once(tmp_path: Path) -> None:
    connector = QQNTDirectConnector(
        _config(tmp_path, "qq-duplicate-event", mode="duplicate_event")
    )
    emitter = RecordingEmitter()
    try:
        assert (
            connector.on_subscribe("sub", "message.connector.v1", b"{}", emitter)
            is None
        )
        deadline = time.monotonic() + 1.0
        while len(emitter.events) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(emitter.events) == 1
    finally:
        connector.close()


def test_media_and_file_references_remain_bounded_and_typed(tmp_path: Path) -> None:
    connector = QQNTDirectConnector(
        _config(tmp_path, "qq-media", mode="media_file")
    )
    emitter = RecordingEmitter()
    try:
        assert (
            connector.on_subscribe("sub", "message.connector.v1", b"{}", emitter)
            is None
        )
        result = connector.invoke_extension(
            "qq.media.download", {"media_id": "media-1"}
        )
        assert result["result"]["local_result_reference"].startswith("qq://")
        connector.publish_inbound_event(
            {
                "event": "message.received",
                "event_id": "manual-media-event",
                "payload": {
                    "account_id": "10001",
                    "message_id": "native-media-message",
                    "peer": {
                        "kind": "private",
                        "peer_uid": "peer-1",
                        "user_uid": "20001",
                    },
                    "sender": {"uid": "uid-20001", "uin": "20001"},
                    "elements": [
                        {"type": "image", "remote_uri": "https://example.invalid/a"},
                        {"type": "file", "media_id": "file-1", "file_name": "a.bin"},
                    ],
                },
            }
        )
        payload = message_contract.InboundMessagePayload.FromString(
            emitter.events[-1][1]
        )
        assert payload.content[0].image.reference.remote_uri == "https://example.invalid/a"
        assert payload.content[1].file.reference.vendor_media.media_id == "file-1"
    finally:
        connector.close()


def test_shutdown_reaps_binding_process_group(tmp_path: Path) -> None:
    data_dir = tmp_path / "qq-process-group"
    connector = QQNTDirectConnector(
        _config(tmp_path, "qq-process-group", mode="spawn_child")
    )
    pid_file = data_dir / "fixture-child.pid"
    child_pid: int | None = None
    try:
        assert (
            connector.on_subscribe(
                "sub", "message.connector.v1", b"{}", RecordingEmitter()
            )
            is None
        )
        deadline = time.monotonic() + 1.0
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = int(pid_file.read_text(encoding="ascii"))
    finally:
        connector.close()
    assert child_pid is not None
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail(f"fixture child {child_pid} survived Host shutdown")
    pid_file.unlink(missing_ok=True)


def test_cancellation_is_rejected_before_host_dispatch(tmp_path: Path) -> None:
    connector = QQNTDirectConnector(_config(tmp_path, "qq-cancel"))
    try:
        with pytest.raises(Exception, match="cancel|CANCELLED|LOGIN_REQUIRED"):
            connector.send_message(_send_request(), cancellation=CancelledToken())
        assert connector.generation == 0
    finally:
        connector.close()


def test_direct_source_does_not_create_tcp_listener() -> None:
    source_root = Path(__file__).parents[1] / "src" / "onebot_v11_connector"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in source_root.glob("qqnt_direct*.py")
    )
    assert "import socket" not in source
    assert "socket.socket" not in source
    assert "localhost" not in source
