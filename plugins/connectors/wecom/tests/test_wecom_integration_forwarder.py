"""Fixed Exchange admission and local callback-context tests for WeCom.

These cases exercise the actual connector normalization path and synthetic
transport only; they never connect to a real WeCom tenant or Exchange service.
模块职责：验证规范入站、签名重试、持久化回调上下文与零 Navigator 转发。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

import pytest

from wecom_connector._generated import message_connector_pb2 as message_contract
from wecom_connector.bot_client import (
    APP_CMD_RESPOND,
    APP_CMD_SUBSCRIBE,
    InboundMessage,
    ScriptedWsTransport,
)
from wecom_connector.connector import (
    ConnectorError,
    WeComConnector,
    WeComInstanceConfig,
)
from wecom_connector.integration_forwarder import (
    HttpTransport,
    WeComIntegrationConfig,
    WeComIntegrationForwarder,
    load_hmac_key,
    validate_exchange_base_url,
)
from wecom_connector.navigator import NavigatorIntegrationConfig, TrustedConversation

EXCHANGE_BINDING_ID = "98f17b77-54f6-4976-998e-071581a373d5"
SOURCE_KEY = b"synthetic-wecom-source-key-32-bytes-long"


class CapturingHttpTransport(HttpTransport):
    """Return scripted Exchange admission responses while recording raw bytes."""

    def __init__(self, responses: list[tuple[int, bytes]] | None = None) -> None:
        durable_ack = b'{"accepted":true,"persisted":true,"eventId":"synthetic"}'
        self.responses = list(responses or [(202, durable_ack)])
        self.requests: list[tuple[str, bytes, Mapping[str, str], float]] = []

    def post(
        self, url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.requests.append((url, body, dict(headers), timeout))
        if self.responses:
            return self.responses.pop(0)
        return 202, b'{"accepted":true,"persisted":true,"eventId":"synthetic"}'


def integration_config(
    path: Path, *, binding_id: str = EXCHANGE_BINDING_ID
) -> WeComIntegrationConfig:
    """Build a fake host-resolved sink without embedding secrets in JSON."""

    return WeComIntegrationConfig(
        binding_id=binding_id,
        exchange_base_url="https://exchange.example.test",
        organization_id="org-test",
        workspace_id="workspace-test",
        source_authority="wecom.bot",
        bot_namespace="corp:test-bot",
        account_id="bot:test-bot",
        group_conversation_id="group-42",
        source_hmac_key=SOURCE_KEY,
        outbox_path=path,
    )


def connector_config(
    path: Path, *, navigator: NavigatorIntegrationConfig | None = None
) -> WeComInstanceConfig:
    """Create one bot activation with its optional fixed Exchange sink."""

    return WeComInstanceConfig(
        binding_id="activation-wecom-test",
        mode="bot",
        bot_id="test-bot",
        bot_secret="synthetic-bot-secret",
        navigator=navigator,
        integration=integration_config(path),
    )


def inbound_message(
    *,
    message_id: str = "message-1",
    sender_id: str = "member-7",
    conversation_id: str = "group-42",
    chat_type: str = "group",
    content: str = "/issue query 12",
    callback_req_id: str = "callback-req-7",
    from_self: bool = False,
) -> InboundMessage:
    """Return the normalized official-SDK callback DTO used by the connector."""

    return InboundMessage(
        msg_id=message_id,
        chat_id=conversation_id,
        chat_type=chat_type,
        sender_id=sender_id,
        sender_name="Untrusted display name",
        msg_type="text",
        content=content,
        raw_payload={"body": {"from": {"is_self": from_self}}},
        callback_req_id=callback_req_id,
    )


def test_connector_posts_actual_normalized_proto_and_exact_hmac(tmp_path: Path) -> None:
    transport = CapturingHttpTransport()
    config = connector_config(tmp_path / "wecom-outbox.sqlite3")
    connector = WeComConnector(config, integration_http_transport=transport)
    try:
        connector.publish_inbound_message(inbound_message())
        forwarder = connector._integration_forwarder
        assert forwarder is not None
        assert forwarder.drain_once() is True

        url, body, headers, _timeout = transport.requests[0]
        envelope = json.loads(body)
        assert url == config.integration.endpoint
        assert envelope["bindingId"] == EXCHANGE_BINDING_ID
        assert envelope["organizationId"] == "org-test"
        assert envelope["workspaceId"] == "workspace-test"
        assert envelope["botNamespace"] == "corp:test-bot"
        assert envelope["accountId"] == "bot:test-bot"
        assert envelope["conversationId"] == "group-42"
        assert envelope["senderId"] == "member-7"
        assert envelope["fromSelf"] is False
        assert envelope["sourceTransportRequestId"] == "callback-req-7"
        proto = message_contract.InboundMessagePayload()
        proto.ParseFromString(
            __import__("base64").b64decode(envelope["messageProtoB64"])
        )
        assert proto.message_id == "message-1"
        assert proto.sender_id == "member-7"
        assert proto.conversation.vendor == "wecom.bot"
        assert proto.conversation.account_id == "bot:test-bot"
        assert proto.conversation.conversation_id == "group-42"
        assert proto.content[0].text.text == "/issue query 12"
        assert [
            fact.value
            for fact in proto.vendor_extension.facts
            if fact.name == "transport_request_id"
        ] == ["callback-req-7"]

        path = urllib.parse.urlsplit(url).path
        body_hash = hashlib.sha256(body).hexdigest()
        assert headers["X-Cyrene-WeCom-Body-SHA256"] == body_hash
        assert headers["X-Cyrene-WeCom-Source-Binding-Id"] == EXCHANGE_BINDING_ID
        signing_input = (
            f"v1\nPOST\n{path}\n{headers['X-Cyrene-WeCom-Timestamp']}\n"
            f"{headers['X-Cyrene-WeCom-Request-Id']}\n{EXCHANGE_BINDING_ID}\n{body_hash}"
        ).encode()
        expected = hmac.new(SOURCE_KEY, signing_input, hashlib.sha256).hexdigest()
        assert headers["X-Cyrene-WeCom-Signature"] == f"sha256={expected}"
        assert SOURCE_KEY.decode() not in body.decode()
        assert forwarder.queue_status() == {"PENDING": 0, "ACKED": 1, "RETAINED": 0}
    finally:
        connector.close()


def test_outbox_survives_restart_and_restores_only_matching_reply_context(
    tmp_path: Path,
) -> None:
    database = tmp_path / "wecom-outbox.sqlite3"
    config = connector_config(database)
    first = WeComConnector(config, integration_http_transport=CapturingHttpTransport())
    first.publish_inbound_message(inbound_message())
    first.close()

    transport = CapturingHttpTransport()
    scripted = ScriptedWsTransport()

    def acknowledge(frame: dict[str, object]) -> None:
        if frame.get("cmd") in {APP_CMD_SUBSCRIBE, APP_CMD_RESPOND}:
            headers = frame.get("headers")
            request_id = headers.get("req_id", "") if isinstance(headers, dict) else ""
            scripted.push_inbound(
                {"headers": {"req_id": request_id}, "errcode": 0, "errmsg": "ok"}
            )

    scripted.send_hook = acknowledge
    restarted = WeComConnector(
        config,
        ws_transport=scripted,
        integration_http_transport=transport,
    )
    try:
        forwarder = restarted._integration_forwarder
        assert forwarder is not None
        assert forwarder.queue_status()["PENDING"] == 1
        assert forwarder.drain_once() is True
        restarted.publish_inbound_message(inbound_message())
        assert forwarder.queue_status()["ACKED"] == 1
        assert len(transport.requests) == 1
        assert (
            forwarder.resolve_reply_context(
                message_id="message-1",
                account_id="bot:test-bot",
                conversation_id="group-42",
                conversation_kind="group",
            )
            == "callback-req-7"
        )
        assert (
            forwarder.resolve_reply_context(
                message_id="message-1",
                account_id="bot:test-bot",
                conversation_id="other-group",
                conversation_kind="group",
            )
            is None
        )

        with pytest.raises(ConnectorError, match="trusted callback context"):
            restarted.send_message(
                {
                    "conversation": {
                        "vendor": "wecom.bot",
                        "account_id": "bot:test-bot",
                        "conversation_id": "other-group",
                        "kind": "group",
                    },
                    "content": [{"kind": "text", "text": "no"}],
                    "reply": {"message_id": "message-1"},
                }
            )

        receipt = restarted.send_message(
            {
                "conversation": {
                    "vendor": "wecom.bot",
                    "account_id": "bot:test-bot",
                    "conversation_id": "group-42",
                    "kind": "group",
                },
                "content": [{"kind": "text", "text": "challenge id"}],
                "reply": {"message_id": "message-1"},
            }
        )
        assert receipt["status"] == "accepted"
        response_frames = [
            frame
            for frame in scripted.sent_messages
            if frame.get("cmd") == APP_CMD_RESPOND
        ]
        assert len(response_frames) == 1
        assert response_frames[0]["headers"]["req_id"] == "callback-req-7"
    finally:
        restarted.close()


def test_retry_resigns_same_durable_admission_and_preserves_reply_idempotency(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    transport = CapturingHttpTransport(
        [
            (503, b"temporary"),
            (202, b'{"accepted":true,"persisted":true,"eventId":"event"}'),
        ]
    )
    config = integration_config(tmp_path / "retry.sqlite3")
    forwarder = WeComIntegrationForwarder(
        config,
        transport=transport,
        clock=lambda: now[0],
        minimum_retry_seconds=1,
        maximum_retry_seconds=5,
    )
    source = inbound_message()
    proto = message_contract.InboundMessagePayload(
        message_id=source.msg_id,
        sender_id=source.sender_id,
        sender_display_name=source.sender_name,
    )
    proto.conversation.vendor = "wecom.bot"
    proto.conversation.account_id = "bot:test-bot"
    proto.conversation.conversation_id = "group-42"
    proto.conversation.kind = message_contract.CONVERSATION_KIND_GROUP
    proto.content.add().text.text = source.content
    proto.vendor_extension.vendor = "wecom.bot"
    fact = proto.vendor_extension.facts.add()
    fact.name = "transport_request_id"
    fact.value = source.callback_req_id

    try:
        assert (
            forwarder.enqueue(
                source_event_id=source.msg_id,
                sender_id=source.sender_id,
                conversation_id="group-42",
                conversation_kind="group",
                from_self=False,
                message_proto=proto.SerializeToString(),
                source_transport_request_id=source.callback_req_id,
            )
            is True
        )
        assert forwarder.drain_once() is True
        now[0] += 1.1
        assert forwarder.drain_once() is True
        assert len(transport.requests) == 2
        assert transport.requests[0][1] == transport.requests[1][1]
        assert (
            transport.requests[0][2]["X-Cyrene-WeCom-Request-Id"]
            != transport.requests[1][2]["X-Cyrene-WeCom-Request-Id"]
        )
        assert forwarder.queue_status() == {"PENDING": 0, "ACKED": 1, "RETAINED": 0}
        assert (
            forwarder.resolve_reply_context(
                message_id=source.msg_id,
                account_id="bot:test-bot",
                conversation_id="group-42",
                conversation_kind="group",
            )
            == source.callback_req_id
        )
        now[0] += 301
        assert (
            forwarder.resolve_reply_context(
                message_id=source.msg_id,
                account_id="bot:test-bot",
                conversation_id="group-42",
                conversation_kind="group",
            )
            is None
        )
    finally:
        forwarder.close()


def test_permanent_rejection_retains_unacknowledged_transport_row(
    tmp_path: Path,
) -> None:
    transport = CapturingHttpTransport([(422, b'{"accepted":false}')])
    forwarder = WeComIntegrationForwarder(
        integration_config(tmp_path / "rejected.sqlite3"),
        transport=transport,
    )
    source = inbound_message()
    proto = message_contract.InboundMessagePayload(
        message_id=source.msg_id,
        sender_id=source.sender_id,
    )
    proto.conversation.vendor = "wecom.bot"
    proto.conversation.account_id = "bot:test-bot"
    proto.conversation.conversation_id = "group-42"
    proto.conversation.kind = message_contract.CONVERSATION_KIND_GROUP
    proto.vendor_extension.vendor = "wecom.bot"
    fact = proto.vendor_extension.facts.add()
    fact.name = "transport_request_id"
    fact.value = source.callback_req_id
    try:
        forwarder.enqueue(
            source_event_id=source.msg_id,
            sender_id=source.sender_id,
            conversation_id="group-42",
            conversation_kind="group",
            from_self=False,
            message_proto=proto.SerializeToString(),
            source_transport_request_id=source.callback_req_id,
        )
        assert forwarder.drain_once() is True
        assert forwarder.queue_status() == {"PENDING": 0, "ACKED": 0, "RETAINED": 1}
        row = forwarder._db.execute(
            "SELECT body,status FROM wecom_exchange_outbox WHERE source_event_id=?",
            (source.msg_id,),
        ).fetchone()
        assert row["status"] == "RETAINED"
        assert bytes(row["body"])
        assert forwarder.drain_once() is False
        assert len(transport.requests) == 1
    finally:
        forwarder.close()


def test_self_and_duplicate_callbacks_are_durable_without_navigator_forwarding(
    tmp_path: Path,
) -> None:
    nav = NavigatorIntegrationConfig(
        base_url="http://127.0.0.1:4317",
        workspace_id="workspace-test",
        connector_id="activation-wecom-test",
        secret_env_ref="NAVIGATOR_SECRET",
        bearer_token="synthetic-token",
        timeout_seconds=5,
        retry_count=0,
        forward_inbound=True,
        trusted_conversations=(
            TrustedConversation(
                account_id="bot:test-bot",
                conversation_id="group-42",
                kind="group",
                owner_id="owner-test",
                session_id="session-test",
                trusted_senders=frozenset({"member-7", "test-bot"}),
            ),
        ),
    )
    connector = WeComConnector(
        connector_config(tmp_path / "self.sqlite3", navigator=nav),
        integration_http_transport=CapturingHttpTransport(),
    )
    try:
        assert connector._navigator_client is None
        assert connector.connection_health()["navigator"]["forward_inbound"] is False
        message = inbound_message(
            message_id="self-1", sender_id="test-bot", from_self=True
        )
        connector.publish_inbound_message(message)
        connector.publish_inbound_message(message)
        forwarder = connector._integration_forwarder
        assert forwarder is not None
        assert forwarder.queue_status()["PENDING"] == 1
        forwarder.drain_once()
        envelope = json.loads(connector._integration_http_transport.requests[0][1])
        assert envelope["fromSelf"] is True
    finally:
        connector.close()


def test_integration_configuration_fails_closed_on_remote_http_and_secret_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WECOM_EXCHANGE_SOURCE_KEY", SOURCE_KEY.decode())
    value = {
        "binding_id": "activation-wecom-test",
        "mode": "bot",
        "bot_id": "test-bot",
        "bot_secret": "synthetic-bot-secret",
        "integration": {
            "enabled": True,
            "exchange_binding_id": EXCHANGE_BINDING_ID,
            "exchange_base_url": "https://exchange.example.test",
            "organization_id": "org-test",
            "workspace_id": "workspace-test",
            "source_authority": "wecom.bot",
            "bot_namespace": "corp:test-bot",
            "group_conversation_id": "group-42",
            "source_hmac_env_ref": "WECOM_EXCHANGE_SOURCE_KEY",
            "outbox_path": str(tmp_path / "config.sqlite3"),
        },
    }
    parsed = WeComInstanceConfig.from_mapping(value)
    assert parsed.integration is not None
    assert parsed.integration.source_hmac_key == SOURCE_KEY

    value["integration"]["exchange_base_url"] = "http://10.1.2.3:8090"
    with pytest.raises((ConnectorError, ValueError)):
        WeComInstanceConfig.from_mapping(value)
    with pytest.raises(ValueError):
        validate_exchange_base_url("https://exchange.example.test/path")
    with pytest.raises(ValueError):
        load_hmac_key(
            {"WECOM_EXCHANGE_SOURCE_KEY": "too-short"},
            "WECOM_EXCHANGE_SOURCE_KEY",
        )
