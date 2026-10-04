"""WeCom Smart Robot WebSocket OpenWS client and bot-mode tests.

中文:WeCom 智能机器人 WebSocket OpenWS 客户端与 bot 模式测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from wecom_connector import (
    InboundMessage,
    ScriptedWsTransport,
    WeComBotClient,
    WeComConnector,
    WeComInstanceConfig,
)
from wecom_connector.bot_client import (
    APP_CMD_MSG_CALLBACK,
    APP_CMD_RESPOND,
    APP_CMD_SEND,
    APP_CMD_SUBSCRIBE,
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
            if msg.get("cmd") == APP_CMD_SUBSCRIBE:
                scripted_ws.push_inbound({"errcode": 0, "errmsg": "ok"})

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
                    "msg_id": "msg-999",
                    "chat_id": "chat-alice",
                    "chat_type": "single",
                    "from": {"user_id": "usr-alice", "name": "Alice"},
                    "msg_type": "text",
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
            if msg.get("cmd") == APP_CMD_SUBSCRIBE:
                scripted_ws.push_inbound({"errcode": 0, "errmsg": "ok"})

        scripted_ws.send_hook = handle_send
        await client.connect()

        # 1. Respond to message
        resp_req = await client.respond(
            "msg-123", "Task completed!", msg_type="markdown", finish=True
        )
        assert resp_req.startswith("resp-")

        # 2. Proactive send
        send_req = await client.send_proactive(
            "chat-group-456", "Daily report", chat_type="group"
        )
        assert send_req.startswith("send-")

        assert len(scripted_ws.sent_messages) == 3
        respond_frame = scripted_ws.sent_messages[1]
        assert respond_frame["cmd"] == APP_CMD_RESPOND
        assert respond_frame["body"]["msg_id"] == "msg-123"
        assert respond_frame["body"]["response"]["finish"] is True
        resp_content = respond_frame["body"]["response"]["markdown"]["content"]
        assert resp_content == "Task completed!"

        send_frame = scripted_ws.sent_messages[2]
        assert send_frame["cmd"] == APP_CMD_SEND
        assert send_frame["body"]["chat_id"] == "chat-group-456"
        assert send_frame["body"]["chat_type"] == "group"
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
            if msg.get("cmd") == APP_CMD_SUBSCRIBE:
                scripted_ws.push_inbound({"errcode": 0, "errmsg": "ok"})

        scripted_ws.send_hook = handle_send
        await client.connect()

        async def chunk_generator() -> AsyncIterator[str]:
            yield "Chunk 1; "
            yield "Chunk 2; "
            yield "Final chunk."

        await client.stream_respond("msg-stream-1", chunk_generator())

        # Handshake + 3 stream frames
        assert len(scripted_ws.sent_messages) == 4
        f1 = scripted_ws.sent_messages[1]["body"]["response"]
        f2 = scripted_ws.sent_messages[2]["body"]["response"]
        f3 = scripted_ws.sent_messages[3]["body"]["response"]

        assert f1["finish"] is False
        assert f1["markdown"]["content"] == "Chunk 1; "

        assert f2["finish"] is False
        assert f2["markdown"]["content"] == "Chunk 1; Chunk 2; "

        assert f3["finish"] is True
        assert f3["markdown"]["content"] == "Chunk 1; Chunk 2; Final chunk."

        await client.stop()

    asyncio.run(run_test())


def test_wecom_connector_send_message_bot_mode(
    scripted_ws: ScriptedWsTransport,
) -> None:
    def handle_send(msg: dict[str, Any]) -> None:
        if msg.get("cmd") == APP_CMD_SUBSCRIBE:
            scripted_ws.push_inbound({"errcode": 0, "errmsg": "ok"})

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
            "vendor": "wecom.app",
            "account_id": "bot:sample",
            "conversation_id": "user-bob",
            "kind": "private",
        },
        "content": [{"kind": "text", "text": "Hello Bob from bot"}],
    }

    result = connector.send_message(request)
    assert result["status"] == "accepted"
    assert result["vendor_message_id"].startswith("send-")

    # Verify aibot_subscribe and aibot_send were transmitted
    assert len(scripted_ws.sent_messages) == 2
    assert scripted_ws.sent_messages[0]["cmd"] == APP_CMD_SUBSCRIBE
    assert scripted_ws.sent_messages[1]["cmd"] == APP_CMD_SEND
    assert (
        scripted_ws.sent_messages[1]["body"]["text"]["content"] == "Hello Bob from bot"
    )
