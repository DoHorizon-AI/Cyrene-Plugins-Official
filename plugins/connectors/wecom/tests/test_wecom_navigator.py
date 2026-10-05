"""Local Navigator intake tests for the WeCom inbound adapter.

中文:WeCom 入站适配器的本地 Navigator intake 测试。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from wecom_connector import (
    InboundMessage,
    ScriptedWsTransport,
    WeComConnector,
    WeComInstanceConfig,
    notification_recipient_key,
)
from wecom_connector.errors import ConnectorError
from wecom_connector.navigator import NavigatorIntegrationConfig


class FakeNavigatorHandler(BaseHTTPRequestHandler):
    """Small loopback-only fake for the configured Navigator route."""

    records: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], tuple[bytes, str]] = {}
    lock = threading.Lock()
    expected_token = "local-test-bearer"

    def do_POST(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {self.expected_token}":
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8"))
        message_id = payload["messageId"]
        key = (self.path, message_id)
        with self.lock:
            previous = self.seen.get(key)
            if previous is not None and previous[0] != body:
                self.send_error(409)
                return
            duplicate = previous is not None
            event_id = previous[1] if previous else f"event-{len(self.records) + 1}"
            if not duplicate:
                self.seen[key] = (body, event_id)
                self.records.append(payload)
        receipt = {
            "eventId": event_id,
            "duplicate": duplicate,
            "receivedAt": "2026-10-05T00:00:00Z",
            "task": {"id": payload["task"]["id"]},
        }
        encoded = json.dumps(receipt).encode("utf-8")
        self.send_response(200 if duplicate else 201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class FakeOutboxHandler(BaseHTTPRequestHandler):
    """Local notification endpoint fake that records lease transitions."""

    items: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    transitions: list[tuple[str, dict[str, Any]]] = []
    timeline: list[str] = []
    lock = threading.Lock()
    expected_token = "local-test-bearer"

    def do_POST(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {self.expected_token}":
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8"))
        with self.lock:
            if self.path.endswith("/notifications/claim"):
                self.claims.append(payload)
                response: dict[str, Any] = {"items": list(self.items)}
                type(self).items = []
            elif self.path.endswith("/start"):
                self.transitions.append(("start", payload))
                self.timeline.append("start")
                response = {"started": True}
            elif self.path.endswith("/finish"):
                self.transitions.append(("finish", payload))
                self.timeline.append("finish")
                response = {"finished": True}
            else:
                self.send_error(404)
                return
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


@pytest.fixture
def fake_navigator(monkeypatch: pytest.MonkeyPatch):
    FakeNavigatorHandler.records = []
    FakeNavigatorHandler.seen = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeNavigatorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("WECOM_NAVIGATOR_BEARER_TOKEN", "local-test-bearer")
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


@pytest.fixture
def fake_outbox(monkeypatch: pytest.MonkeyPatch):
    FakeOutboxHandler.items = []
    FakeOutboxHandler.claims = []
    FakeOutboxHandler.transitions = []
    FakeOutboxHandler.timeline = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOutboxHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("WECOM_NAVIGATOR_BEARER_TOKEN", "local-test-bearer")
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def navigator_config(base_url: str) -> dict[str, Any]:
    return {
        "base_url": base_url,
        "workspace_id": "workspace-local",
        "secret_env_ref": "WECOM_NAVIGATOR_BEARER_TOKEN",
        "trusted_conversations": [
            {
                "account_id": "bot:bot-1",
                "conversation_id": "trusted-chat",
                "kind": "private",
                "owner_id": "owner-local",
                "session_id": "session-local",
                "trusted_senders": ["member-trusted"],
            }
        ],
    }


def inbound_message(sender_id: str = "member-trusted") -> InboundMessage:
    return InboundMessage(
        msg_id="message-001",
        chat_id="trusted-chat",
        chat_type="single",
        sender_id=sender_id,
        sender_name="Trusted Member",
        msg_type="file",
        content="brief.pdf",
        raw_payload={
            "body": {
                "msg_id": "message-001",
                "chat_id": "trusted-chat",
                "chat_type": "single",
                "from": {"user_id": sender_id, "name": "Trusted Member"},
                "msg_type": "file",
                "file": {
                    "media_id": "media-001",
                    "file_name": "brief.pdf",
                    "mime_type": "application/pdf",
                },
            }
        },
    )


def test_local_navigator_task_intake_is_scoped_and_idempotent(
    fake_navigator: str,
) -> None:
    navigator = NavigatorIntegrationConfig.from_mapping(
        navigator_config(fake_navigator), binding_id="wecom-binding"
    )
    rule = navigator.trusted_rule(
        {
            "conversation": {
                "account_id": "bot:bot-1",
                "conversation_id": "trusted-chat",
                "kind": "private",
            },
            "sender_id": "member-trusted",
        }
    )
    assert rule is not None
    config = WeComInstanceConfig(
        binding_id="wecom-binding",
        mode="bot",
        bot_id="bot-1",
        bot_secret="secret-1",
        navigator=navigator,
    )
    connector = WeComConnector(config)
    first = connector.publish_inbound_message(inbound_message())
    second = connector.publish_inbound_message(inbound_message())

    assert first["navigator_receipt"]["duplicate"] is False
    assert second["navigator_receipt"]["duplicate"] is True
    assert (
        first["navigator_receipt"]["eventId"]
        == second["navigator_receipt"]["eventId"]
    )
    assert len(FakeNavigatorHandler.records) == 1
    path, payload = next(iter(FakeNavigatorHandler.seen))
    assert path == (
        "/api/v1/workspaces/workspace-local/work/connectors/wecom-binding/events"
    )
    received = FakeNavigatorHandler.records[0]
    assert received["type"] == "message.inbound"
    assert received["accountId"] == "bot:bot-1"
    assert received["conversationId"] == "trusted-chat"
    assert received["senderId"] == "member-trusted"
    assert received["task"]["sessionId"] == "session-local"
    assert received["task"]["metadata"]["ownerId"] == "owner-local"
    assert received["task"]["metadata"]["messageId"] == "message-001"
    descriptor = received["task"]["metadata"]["notification"]
    assert descriptor == {
        "type": "wecom.message",
        "connectorId": "wecom-binding",
        "recipient": notification_recipient_key(
            "bot:bot-1", "trusted-chat", "single", "member-trusted"
        ),
        "payload": {
            "accountId": "bot:bot-1",
            "conversationId": "trusted-chat",
            "chatType": "single",
            "senderId": "member-trusted",
        },
    }
    media_reference = received["payload"]["content"][0]["file"]["reference"]
    assert media_reference["vendor_media"]["media_id"] == "media-001"
    assert payload  # The same deterministic request body is used on duplicate delivery.
    connector.close()


def test_untrusted_sender_never_reaches_navigator(fake_navigator: str) -> None:
    navigator = NavigatorIntegrationConfig.from_mapping(
        navigator_config(fake_navigator), binding_id="wecom-binding"
    )
    config = WeComInstanceConfig(
        binding_id="wecom-binding",
        mode="bot",
        bot_id="bot-1",
        bot_secret="secret-1",
        navigator=navigator,
    )
    connector = WeComConnector(config)

    result = connector.publish_inbound_message(inbound_message("member-outsider"))

    assert result["event_emitted"] is False
    assert result["navigator_receipt"] is None
    assert FakeNavigatorHandler.records == []
    connector.close()


def test_navigator_config_rejects_non_loopback_and_missing_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WECOM_NAVIGATOR_BEARER_TOKEN", "local-test-bearer")
    invalid_url = navigator_config("https://navigator.example.test")
    with pytest.raises(ConnectorError, match="loopback-only"):
        NavigatorIntegrationConfig.from_mapping(invalid_url, binding_id="wecom")

    missing_trust = navigator_config("http://127.0.0.1:8080")
    missing_trust["trusted_conversations"] = []
    with pytest.raises(ConnectorError, match="at least one owner/session map"):
        NavigatorIntegrationConfig.from_mapping(missing_trust, binding_id="wecom")


def test_navigator_binding_and_trusted_account_must_match_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WECOM_NAVIGATOR_BEARER_TOKEN", "local-test-bearer")
    mismatched_connector = navigator_config("http://127.0.0.1:8080")
    mismatched_connector["connector_id"] = "another-binding"
    with pytest.raises(ConnectorError, match="match the activation binding_id"):
        NavigatorIntegrationConfig.from_mapping(
            mismatched_connector, binding_id="wecom-binding"
        )

    mismatched_bot = {
        "binding_id": "wecom-binding",
        "mode": "bot",
        "bot_id": "different-bot",
        "bot_secret": "test-secret",
        "navigator": navigator_config("http://127.0.0.1:8080"),
    }
    with pytest.raises(ConnectorError, match="trusted account_id must match"):
        WeComInstanceConfig.from_mapping(mismatched_bot)


@pytest.mark.parametrize(
    ("vendor_errcode", "expected_outcome"),
    [(0, "delivered"), (45009, "failed"), (None, "uncertain")],
)
def test_outbox_marks_started_then_records_vendor_receipt_without_retry(
    fake_outbox: str,
    monkeypatch: pytest.MonkeyPatch,
    vendor_errcode: int | None,
    expected_outcome: str,
) -> None:
    import wecom_connector.bot_client as bot_client_module

    if vendor_errcode is None:
        monkeypatch.setattr(bot_client_module, "CONNECT_TIMEOUT_SECONDS", 0.02)
    navigator = NavigatorIntegrationConfig.from_mapping(
        navigator_config(fake_outbox), binding_id="wecom-binding"
    )
    recipient = notification_recipient_key(
        "bot:bot-1", "trusted-chat", "single", "member-trusted"
    )
    FakeOutboxHandler.items = [
        {
            "notification": {
                "id": "notice-001",
                "type": "wecom.message",
                "connectorId": "wecom-binding",
                "recipient": recipient,
                "payload": {
                    "accountId": "bot:bot-1",
                    "conversationId": "trusted-chat",
                    "chatType": "single",
                    "senderId": "member-trusted",
                    "text": "Task completed: answer",
                },
            },
            "leaseToken": "lease-001",
            "leaseExpiresAt": "2026-10-05T00:02:00Z",
        }
    ]
    config = WeComInstanceConfig(
        binding_id="wecom-binding",
        mode="bot",
        bot_id="bot-1",
        bot_secret="secret-1",
        navigator=navigator,
    )
    transport = ScriptedWsTransport()

    def acknowledge(message: dict[str, Any]) -> None:
        command = message.get("cmd")
        req_id = message.get("headers", {}).get("req_id", "")
        if command == "aibot_subscribe":
            transport.push_inbound(
                {"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}
            )
        elif command == "aibot_send":
            with FakeOutboxHandler.lock:
                FakeOutboxHandler.timeline.append("send")
            if vendor_errcode is not None:
                transport.push_inbound(
                    {
                        "headers": {"req_id": req_id},
                        "errcode": vendor_errcode,
                        "errmsg": "ok" if vendor_errcode == 0 else "rate limited",
                        "msgid": "vendor-message-001",
                    }
                )

    transport.send_hook = acknowledge
    connector = WeComConnector(config, ws_transport=transport)
    try:
        worker = connector.notification_worker
        assert worker is not None
        assert worker.poll_once() == 1
        finish = FakeOutboxHandler.transitions[-1][1]
        assert [name for name, _ in FakeOutboxHandler.transitions] == [
            "start",
            "finish",
        ]
        assert finish["outcome"] == expected_outcome
        assert FakeOutboxHandler.timeline == ["start", "send", "finish"]
        claim = FakeOutboxHandler.claims[0]
        assert claim["type"] == "wecom.message"
        assert claim["connectorId"] == "wecom-binding"
        assert claim["recipient"] == recipient
        send_body = next(
            message["body"]
            for message in transport.sent_messages
            if message.get("cmd") == "aibot_send"
        )
        assert send_body["chat_id"] == "trusted-chat"
        assert send_body["chat_type"] == "single"
        assert send_body["text"]["content"] == "Task completed: answer"
        assert worker.poll_once() == 0
        assert sum(
            message.get("cmd") == "aibot_send"
            for message in transport.sent_messages
        ) == 1
    finally:
        connector.close()
