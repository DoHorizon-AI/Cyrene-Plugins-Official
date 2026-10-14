"""WeCom Smart Robot WebSocket OpenWS client and bot-mode tests.

中文:WeCom 智能机器人 WebSocket OpenWS 客户端与 bot 模式测试。"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

import wecom_connector.bot_client as bot_client_module
from wecom_connector import (
    ConnectorError,
    InboundMessage,
    ScriptedWsTransport,
    WeComBotClient,
    WeComConnector,
    WeComInstanceConfig,
)
from wecom_connector._generated import message_connector_pb2 as message_contract
from wecom_connector.bot_client import (
    APP_CMD_MSG_CALLBACK,
    APP_CMD_RESPOND,
    APP_CMD_SEND,
    APP_CMD_SUBSCRIBE,
    WeComBotRequestError,
    WeComBotUncertainError,
    _receipt_from_ack,
)
from wecom_connector.connector import (
    INBOUND_MESSAGE_TYPE_URL,
)


@pytest.fixture
def scripted_ws() -> ScriptedWsTransport:
    return ScriptedWsTransport()


def test_bot_client_handshake_success(scripted_ws: ScriptedWsTransport) -> None:
    async def run_test() -> None:
        client = WeComBotClient(
            bot_id="bot-test-123",
            bot_secret="sec-test-abc",
            websocket_url="wss://openws.work.weixin.qq.com",
            transport=scripted_ws,
        )

        def handle_send(msg: dict[str, Any]) -> None:
            if msg.get("cmd") == APP_CMD_SUBSCRIBE:
                req_id = msg.get("headers", {}).get("req_id", "")
                scripted_ws.push_inbound(
                    {
                        "cmd": "aibot_subscribe_resp",
                        "headers": {"req_id": req_id},
                        "errcode": 0,
                        "errmsg": "ok",
                    }
                )

        scripted_ws.send_hook = handle_send

        await client.connect()
        assert client.is_connected
        assert len(scripted_ws.sent_messages) == 1
        sub_msg = scripted_ws.sent_messages[0]
        assert sub_msg["cmd"] == APP_CMD_SUBSCRIBE
        assert sub_msg["body"]["bot_id"] == "bot-test-123"
        assert sub_msg["body"]["secret"] == "sec-test-abc"
        await client.stop()

    asyncio.run(run_test())


def test_bot_client_handshake_failure(scripted_ws: ScriptedWsTransport) -> None:
    async def run_test() -> None:
        client = WeComBotClient(
            bot_id="bad-bot",
            bot_secret="bad-sec",
            transport=scripted_ws,
        )

        def handle_send(msg: dict[str, Any]) -> None:
            if msg.get("cmd") == APP_CMD_SUBSCRIBE:
                scripted_ws.push_inbound(
                    {
                        "cmd": "aibot_subscribe_resp",
                        "headers": {"req_id": msg.get("headers", {}).get("req_id", "")},
                        "errcode": 40014,
                        "errmsg": "invalid secret",
                    }
                )

        scripted_ws.send_hook = handle_send

        with pytest.raises(RuntimeError, match="invalid secret"):
            await client.connect()
        assert not client.is_connected

    asyncio.run(run_test())


def test_bot_client_inbound_message_callback(scripted_ws: ScriptedWsTransport) -> None:
    async def run_test() -> None:
        client = WeComBotClient(
            bot_id="bot-1",
            bot_secret="sec-1",
            transport=scripted_ws,
        )

        def handle_send(msg: dict[str, Any]) -> None:
            if msg.get("cmd") in {APP_CMD_SUBSCRIBE, APP_CMD_RESPOND, APP_CMD_SEND}:
                scripted_ws.push_inbound(
                    {
                        "cmd": f"{msg.get('cmd')}_resp",
                        "headers": {"req_id": msg.get("headers", {}).get("req_id", "")},
                        "errcode": 0,
                        "errmsg": "ok",
                    }
                )

        scripted_ws.send_hook = handle_send
        await client.start()

        received: list[InboundMessage] = []

        async def on_message(inbound: InboundMessage) -> None:
            received.append(inbound)

        client.register_handler(on_message)

        # Push simulated inbound user message
        scripted_ws.push_inbound(
            {
                "cmd": APP_CMD_MSG_CALLBACK,
                "headers": {"req_id": "req-in-1"},
                "body": {
                    "msgid": "msg-999",
                    "chatid": "chat-alice",
                    "chattype": "single",
                    "from": {"userid": "usr-alice", "name": "Alice"},
                    "msgtype": "text",
                    "text": {"content": "Hello from WeCom!"},
                },
            }
        )

        await asyncio.sleep(0.05)
        assert len(received) == 1
        inbound = received[0]
        assert inbound.msg_id == "msg-999"
        assert inbound.chat_id == "chat-alice"
        assert inbound.sender_id == "usr-alice"
        assert inbound.sender_name == "Alice"
        assert inbound.content == "Hello from WeCom!"

        await client.stop()

    asyncio.run(run_test())


def test_bot_client_respond_and_send(scripted_ws: ScriptedWsTransport) -> None:
    async def run_test() -> None:
        client = WeComBotClient(
            bot_id="bot-1",
            bot_secret="sec-1",
            transport=scripted_ws,
        )

        def handle_send(msg: dict[str, Any]) -> None:
            if msg.get("cmd") in {APP_CMD_SUBSCRIBE, APP_CMD_RESPOND, APP_CMD_SEND}:
                scripted_ws.push_inbound(
                    {
                        "headers": {"req_id": msg.get("headers", {}).get("req_id", "")},
                        "errcode": 0,
                        "errmsg": "ok",
                    }
                )

        scripted_ws.send_hook = handle_send
        await client.start()
        scripted_ws.push_inbound(
            {
                "cmd": APP_CMD_MSG_CALLBACK,
                "headers": {"req_id": "req-inbound-123"},
                "body": {
                    "msgid": "msg-123",
                    "chatid": "user-123",
                    "chattype": "single",
                    "from": {"userid": "user-123"},
                    "msgtype": "text",
                    "text": {"content": "hello"},
                },
            }
        )
        await asyncio.sleep(0.01)

        # 1. Respond to message
        resp_req = await client.respond(
            "msg-123", "Task completed!", msg_type="markdown", finish=True
        )
        assert resp_req.request_id == "req-inbound-123"

        # 2. Proactive send
        send_req = await client.send_proactive(
            "chat-group-456", "Daily report", chat_type="group"
        )
        assert send_req.request_id.startswith("send-")

        assert len(scripted_ws.sent_messages) == 3
        respond_frame = scripted_ws.sent_messages[1]
        assert respond_frame["cmd"] == APP_CMD_RESPOND
        assert respond_frame["headers"]["req_id"] == "req-inbound-123"
        assert respond_frame["body"]["msgtype"] == "markdown"
        resp_content = respond_frame["body"]["markdown"]["content"]
        assert resp_content == "Task completed!"

        send_frame = scripted_ws.sent_messages[2]
        assert send_frame["cmd"] == APP_CMD_SEND
        assert send_frame["body"]["chatid"] == "chat-group-456"
        assert send_frame["body"]["text"]["content"] == "Daily report"

        await client.stop()

    asyncio.run(run_test())


def test_bot_client_stream_respond(scripted_ws: ScriptedWsTransport) -> None:
    async def run_test() -> None:
        client = WeComBotClient(
            bot_id="bot-1",
            bot_secret="sec-1",
            transport=scripted_ws,
        )

        def handle_send(msg: dict[str, Any]) -> None:
            if msg.get("cmd") in {APP_CMD_SUBSCRIBE, APP_CMD_RESPOND, APP_CMD_SEND}:
                scripted_ws.push_inbound(
                    {
                        "headers": {"req_id": msg.get("headers", {}).get("req_id", "")},
                        "errcode": 0,
                        "errmsg": "ok",
                    }
                )

        scripted_ws.send_hook = handle_send
        await client.start()
        scripted_ws.push_inbound(
            {
                "cmd": APP_CMD_MSG_CALLBACK,
                "headers": {"req_id": "req-stream-1"},
                "body": {
                    "msgid": "msg-stream-1",
                    "chatid": "user-stream",
                    "chattype": "single",
                    "from": {"userid": "user-stream"},
                    "msgtype": "text",
                    "text": {"content": "hello"},
                },
            }
        )
        await asyncio.sleep(0.01)

        async def chunk_generator() -> AsyncIterator[str]:
            yield "Chunk 1; "
            yield "Chunk 2; "
            yield "Final chunk."

        await client.stream_respond("msg-stream-1", chunk_generator())

        # Handshake + 3 stream frames
        assert len(scripted_ws.sent_messages) == 4
        f1 = scripted_ws.sent_messages[1]["body"]
        f2 = scripted_ws.sent_messages[2]["body"]
        f3 = scripted_ws.sent_messages[3]["body"]

        assert f1["msgtype"] == "stream"
        assert f1["stream"]["finish"] is False
        assert f1["stream"]["content"] == "Chunk 1; "

        assert f2["msgtype"] == "stream"
        assert f2["stream"]["finish"] is False
        assert f2["stream"]["content"] == "Chunk 1; Chunk 2; "

        assert f3["msgtype"] == "stream"
        assert f3["stream"]["finish"] is True
        assert f3["stream"]["content"] == "Chunk 1; Chunk 2; Final chunk."

        await client.stop()

    asyncio.run(run_test())


def test_wecom_connector_send_message_bot_mode(
    scripted_ws: ScriptedWsTransport,
) -> None:
    def handle_send(msg: dict[str, Any]) -> None:
        if msg.get("cmd") in {APP_CMD_SUBSCRIBE, APP_CMD_RESPOND, APP_CMD_SEND}:
            response = {
                "headers": {"req_id": msg.get("headers", {}).get("req_id", "")},
                "errcode": 0,
                "errmsg": "ok",
            }
            scripted_ws.push_inbound(response)

    scripted_ws.send_hook = handle_send

    config = WeComInstanceConfig(
        binding_id="wecom.bot.test",
        mode="bot",
        bot_id="bot-sample",
        bot_secret="bot-secret-xyz",
    )

    connector = WeComConnector(config, ws_transport=scripted_ws)
    request = {
        "conversation": {
            "vendor": "wecom.bot",
            "account_id": "bot:bot-sample",
            "conversation_id": "user-bob",
            "kind": "private",
        },
        "content": [{"kind": "text", "text": "Hello Bob from bot"}],
    }

    result = connector.send_message(request)
    assert result["status"] == "accepted"
    assert result["vendor_message_id"] == ""
    assert result["vendor"] == "wecom.bot"
    facts = {fact["name"]: fact["value"] for fact in result["vendor_facts"]}
    assert facts["transport_request_id"].startswith("send-")
    assert facts["ack_errcode"] == "0"

    # Verify official aibot_subscribe and aibot_send_msg wire commands
    assert len(scripted_ws.sent_messages) == 2
    assert scripted_ws.sent_messages[0]["cmd"] == APP_CMD_SUBSCRIBE
    assert scripted_ws.sent_messages[1]["cmd"] == APP_CMD_SEND
    assert (
        scripted_ws.sent_messages[1]["body"]["text"]["content"] == "Hello Bob from bot"
    )
    connector.close()


def test_bot_receipt_round_trips_canonical_delivery_contract(
    scripted_ws: ScriptedWsTransport,
) -> None:
    def handle_send(message: dict[str, Any]) -> None:
        if message.get("cmd") in {APP_CMD_SUBSCRIBE, APP_CMD_SEND}:
            scripted_ws.push_inbound(
                {
                    "headers": {"req_id": message.get("headers", {}).get("req_id", "")},
                    "errcode": 0,
                    "errmsg": "ok",
                }
            )

    scripted_ws.send_hook = handle_send
    connector = WeComConnector(
        WeComInstanceConfig(
            binding_id="wecom.bot.contract",
            mode="bot",
            bot_id="bot-contract",
            bot_secret="secret",
        ),
        ws_transport=scripted_ws,
    )
    request = message_contract.SendMessageRequest()
    request.conversation.vendor = "wecom.bot"
    request.conversation.account_id = "bot:bot-contract"
    request.conversation.conversation_id = "member-1"
    request.conversation.kind = message_contract.CONVERSATION_KIND_PRIVATE
    request.content.add().text.text = "hello"

    ok, typed = connector.on_invoke(
        "message.connector.v1",
        "send_message",
        request.SerializeToString(),
        request_type_url="type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest",
    )

    assert ok is True
    assert typed.type_url == (
        "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult"
    )
    delivery = message_contract.DeliveryResult()
    delivery.ParseFromString(typed.value)
    assert delivery.status == message_contract.DELIVERY_STATUS_ACCEPTED
    assert delivery.vendor_message_id == ""
    assert delivery.vendor_extension.vendor == "wecom.bot"
    facts = {fact.name: fact.value for fact in delivery.vendor_extension.facts}
    assert facts["transport_request_id"].startswith("send-")
    assert facts["ack_errcode"] == "0"
    connector.close()


def test_bot_client_reconnects_after_transport_drop_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bot_client_module, "RECONNECT_BACKOFF", [0.01])
    transports: list[ScriptedWsTransport] = []

    def new_transport() -> ScriptedWsTransport:
        transport = ScriptedWsTransport()

        def handle_send(msg: dict[str, Any]) -> None:
            if msg.get("cmd") == APP_CMD_SUBSCRIBE:
                transport.push_inbound(
                    {
                        "headers": {"req_id": msg.get("headers", {}).get("req_id", "")},
                        "errcode": 0,
                        "errmsg": "ok",
                    }
                )

        transport.send_hook = handle_send
        transports.append(transport)
        return transport

    async def run_test() -> None:
        client = WeComBotClient(
            bot_id="bot-reconnect",
            bot_secret="bot-secret",
            transport_factory=new_transport,
        )
        await client.start()
        assert client.is_connected
        transports[0].drop()

        deadline = asyncio.get_running_loop().time() + 1.0
        while (
            client.reconnect_count < 1 and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)

        assert client.is_connected
        assert len(transports) >= 2
        assert client.reconnect_count == 1
        await client.stop()
        assert client.is_connected is False
        assert client._listen_task is None
        assert client._heartbeat_task is None

    asyncio.run(run_test())


def test_bot_receipt_requires_correlated_ack_and_never_fabricates_message_id() -> None:
    ack = {
        "headers": {"req_id": "aibot_send_msg_123"},
        "errcode": 0,
        "errmsg": "ok",
    }

    receipt = _receipt_from_ack(ack)
    assert receipt.request_id == "aibot_send_msg_123"
    assert receipt.vendor_message_id == ""
    assert receipt.errcode == 0

    with pytest.raises(WeComBotUncertainError, match="correlation"):
        _receipt_from_ack(
            {
                "headers": {"req_id": "different"},
                "errcode": 0,
            },
            "expected",
        )
    with pytest.raises(WeComBotUncertainError, match="errcode"):
        _receipt_from_ack({"headers": {"req_id": "req-missing-code"}})
    with pytest.raises(WeComBotRequestError) as error:
        _receipt_from_ack(
            {
                "headers": {"req_id": "req-vendor-error"},
                "errcode": 45009,
                "errmsg": "rate limit",
            }
        )
    assert error.value.errcode == 45009


def test_production_client_adapts_official_sdk_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOfficialClient:
        def __init__(self, options: Any) -> None:
            self.options = options
            self.handlers: dict[str, Any] = {}
            self.sent: list[tuple[str, Any]] = []

        def on(self, event: str, callback: Any) -> None:
            self.handlers[event] = callback

        async def connect(self) -> None:
            self.handlers["authenticated"]()

        def disconnect(self) -> None:
            return

        async def send_message(
            self, chatid: str, body: dict[str, Any]
        ) -> dict[str, Any]:
            self.sent.append((chatid, body))
            return {
                "headers": {"req_id": "aibot_send_msg_456"},
                "errcode": 0,
                "errmsg": "ok",
            }

    client_instance: FakeOfficialClient | None = None

    def create_client(options: Any) -> FakeOfficialClient:
        nonlocal client_instance
        client_instance = FakeOfficialClient(options)
        return client_instance

    monkeypatch.setitem(
        sys.modules,
        "aibot",
        SimpleNamespace(
            WSClient=create_client,
            WSClientOptions=lambda **values: SimpleNamespace(**values),
        ),
    )

    async def run_test() -> None:
        client = WeComBotClient(
            bot_id="bot-official",
            bot_secret="secret",
            websocket_url="ws://127.0.0.1:8765",
        )
        await client.start()
        receipt = await client.send_proactive(
            "room-1", "accepted only", chat_type="group"
        )
        assert receipt.request_id == "aibot_send_msg_456"
        assert receipt.vendor_message_id == ""
        assert client_instance is not None
        assert client_instance.options.max_reconnect_attempts == -1
        assert client_instance.sent == [
            (
                "room-1",
                {"msgtype": "text", "text": {"content": "accepted only"}},
            )
        ]
        await client.stop()

    asyncio.run(run_test())


def test_subscribed_bot_emits_normalized_attachment_once(
    scripted_ws: ScriptedWsTransport,
) -> None:
    def handle_send(msg: dict[str, Any]) -> None:
        if msg.get("cmd") == APP_CMD_SUBSCRIBE:
            scripted_ws.push_inbound(
                {
                    "headers": {"req_id": msg.get("headers", {}).get("req_id", "")},
                    "errcode": 0,
                    "errmsg": "ok",
                }
            )

    scripted_ws.send_hook = handle_send

    class EventCollector:
        def __init__(self) -> None:
            self.events: list[tuple[str, bytes, str]] = []
            self.ready = threading.Event()

        def emit(self, event_type: str, payload: bytes, type_url: str = "") -> bool:
            self.events.append((event_type, payload, type_url))
            self.ready.set()
            return True

    config = WeComInstanceConfig(
        binding_id="wecom.bot.events",
        mode="bot",
        bot_id="bot-1",
        bot_secret="secret-1",
    )
    connector = WeComConnector(config, ws_transport=scripted_ws)
    emitter = EventCollector()
    assert (
        connector.on_subscribe("sub-1", "message.connector.v1", b"{}", emitter) is None
    )

    callback = {
        "cmd": APP_CMD_MSG_CALLBACK,
        "body": {
            "msgid": "image-1",
            "chatid": "chat-1",
            "chattype": "single",
            "from": {"userid": "member-1", "name": "Member One"},
            "msgtype": "image",
            "image": {"media_id": "media-1", "mime_type": "image/jpeg"},
        },
    }
    scripted_ws.push_inbound(callback)
    assert emitter.ready.wait(1.0)
    scripted_ws.push_inbound(callback)
    time.sleep(0.05)

    assert len(emitter.events) == 1
    event_type, encoded, type_url = emitter.events[0]
    assert event_type == "inbound_message"
    assert type_url == INBOUND_MESSAGE_TYPE_URL
    payload = message_contract.InboundMessagePayload()
    payload.ParseFromString(encoded)
    assert payload.message_id == "image-1"
    assert payload.conversation.vendor == "wecom.bot"
    assert payload.conversation.account_id == "bot:bot-1"
    assert payload.conversation.kind == message_contract.CONVERSATION_KIND_PRIVATE
    assert payload.sender_id == "member-1"
    assert payload.content[0].WhichOneof("kind") == "image"
    assert payload.content[0].image.reference.vendor_media.vendor == "wecom.bot"
    assert payload.content[0].image.reference.vendor_media.account_id == "bot:bot-1"
    assert payload.content[0].image.reference.vendor_media.media_id == "media-1"
    connector.close()


def test_bot_send_cancellation_uses_direct_runtime_request_id(
    scripted_ws: ScriptedWsTransport,
) -> None:
    send_frame = threading.Event()

    def handle_send(msg: dict[str, Any]) -> None:
        if msg.get("cmd") == APP_CMD_SUBSCRIBE:
            scripted_ws.push_inbound(
                {
                    "headers": {"req_id": msg.get("headers", {}).get("req_id", "")},
                    "errcode": 0,
                    "errmsg": "ok",
                }
            )
        elif msg.get("cmd") == APP_CMD_SEND:
            send_frame.set()

    scripted_ws.send_hook = handle_send
    config = WeComInstanceConfig(
        binding_id="wecom.bot.cancel",
        mode="bot",
        bot_id="bot-1",
        bot_secret="secret-1",
    )
    connector = WeComConnector(config, ws_transport=scripted_ws)

    class Cancellation:
        cancelled = threading.Event()

        def is_cancelled(self) -> bool:
            return self.cancelled.is_set()

    cancellation = Cancellation()
    errors: list[ConnectorError] = []

    def send() -> None:
        try:
            connector.send_message(
                {
                    "conversation": {
                        "vendor": "wecom.bot",
                        "account_id": "bot:bot-1",
                        "conversation_id": "chat-1",
                        "kind": "private",
                    },
                    "content": [{"kind": "text", "text": "cancel me"}],
                },
                cancellation=cancellation,
                request_id="runtime-request-1",
            )
        except ConnectorError as exc:
            errors.append(exc)

    thread = threading.Thread(target=send)
    thread.start()
    assert send_frame.wait(1.0)
    cancellation.cancelled.set()
    connector.on_cancel("runtime-request-1", "caller cancelled")
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert errors[0].code == "CANCELLED"
    connector.close()
