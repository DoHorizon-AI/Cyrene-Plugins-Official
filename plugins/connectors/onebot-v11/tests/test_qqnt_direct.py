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
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from onebot_v11_connector import (
    QQ_CAPABILITY_ID,
    QQ_OPERATION_NAMES,
    QQ_REQUEST_TYPE_URL,
    QQ_RESPONSE_TYPE_URL,
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


def _config(tmp_path: Path, binding_id: str) -> dict[str, Any]:
    fake_host = Path(__file__).parent / "fixtures" / "fake_qq_host.py"
    return {
        "runtime_profile": "qqnt-direct",
        "binding_id": binding_id,
        "host_executable": sys.executable,
        "host_args": ["-B", str(fake_host)],
        "data_dir": str(tmp_path / binding_id),
        "required_client_version": "qq-test-1",
        "account_id": "10001",
        "timeout_seconds": 1.0,
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


def test_direct_config_rejects_onebot_transport_fields(tmp_path: Path) -> None:
    config = _config(tmp_path, "qq-main")
    config["http_base_url"] = "http://127.0.0.1:8080"
    with pytest.raises(Exception, match="unknown fields|OneBot endpoint"):
        QQNTDirectConnector(config)


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
