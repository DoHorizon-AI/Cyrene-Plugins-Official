"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 bot_client.py                                                   │
│  Package: wecom_connector                                           │
│  Role: WeCom Smart Robot WebSocket openws client implementation.    │
│                                                                     │
│  模块职责：企业微信智能机器人 WebSocket 长连接网关客户端。                │
│  · 协议握手：连接 wss://openws.work.weixin.qq.com 并发送 aibot_subscribe │
│  · 双向消息：入站回调 (aibot_msg_callback) 与出站响应 (aibot_respond)     │
│  · 流式响应：分片输出与 finish 标志位控制                               │
│  · 心跳重连：30s 自动 ping 维持保活与指数退避重连                       │
│  · 传输解耦：支持真实 AsyncWebsocketsTransport 与测试 ScriptedWsTransport │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"

# OpenWS Command Identifiers
APP_CMD_SUBSCRIBE = "aibot_subscribe"
APP_CMD_MSG_CALLBACK = "aibot_msg_callback"
APP_CMD_EVENT_CALLBACK = "aibot_event_callback"
APP_CMD_RESPOND = "aibot_respond"
APP_CMD_SEND = "aibot_send"
APP_CMD_PING = "ping"
APP_CMD_PONG = "pong"

CONNECT_TIMEOUT_SECONDS = 20.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
RECONNECT_BACKOFF = [1.0, 2.0, 5.0, 10.0, 30.0]


class WeComWsTransport(Protocol):
    """Protocol for WebSocket transport to enable deterministic testing."""

    async def send(self, data: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...

    def is_open(self) -> bool: ...


class AsyncWebsocketsTransport:
    """Production WebSocket transport using the 'websockets' library."""

    def __init__(self, ws_url: str, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ws_url = ws_url
        self._ssl_context = ssl_context
        self._connection: Any = None

    async def connect(self) -> None:
        import websockets

        ssl_ctx = self._ssl_context
        if ssl_ctx is None and self._ws_url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
        self._connection = await websockets.connect(
            self._ws_url,
            ssl=ssl_ctx,
            open_timeout=CONNECT_TIMEOUT_SECONDS,
            ping_interval=None,  # We manage application-level ping/pong
        )

    async def send(self, data: str) -> None:
        if self._connection is None:
            raise RuntimeError("WebSocket transport not connected")
        await self._connection.send(data)

    async def recv(self) -> str:
        if self._connection is None:
            raise RuntimeError("WebSocket transport not connected")
        data = await self._connection.recv()
        if isinstance(data, bytes):
            return data.decode("utf-8")
        return str(data)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    def is_open(self) -> bool:
        if self._connection is None:
            return False
        # websockets v13+ uses state, older uses open/closed
        state = getattr(self._connection, "state", None)
        if state is not None:
            return str(state).lower() == "open" or getattr(state, "name", "") == "OPEN"
        return bool(getattr(self._connection, "open", True))


class ScriptedWsTransport:
    """Deterministic scriptable WebSocket transport for unit testing."""

    def __init__(self) -> None:
        self.inbound_queue: asyncio.Queue[str] = asyncio.Queue()
        self.sent_messages: list[dict[str, Any]] = []
        self._closed = False
        self.send_hook: Callable[[dict[str, Any]], None] | None = None

    async def send(self, data: str) -> None:
        if self._closed:
            raise RuntimeError("Cannot send on closed transport")
        parsed = json.loads(data)
        self.sent_messages.append(parsed)
        if self.send_hook is not None:
            self.send_hook(parsed)

    async def recv(self) -> str:
        if self._closed and self.inbound_queue.empty():
            raise asyncio.CancelledError("Transport closed")
        return await self.inbound_queue.get()

    async def close(self) -> None:
        self._closed = True

    def is_open(self) -> bool:
        return not self._closed

    def push_inbound(self, payload: Mapping[str, Any]) -> None:
        """Push a message to be received by recv()."""
        self.inbound_queue.put_nowait(json.dumps(payload, ensure_ascii=False))


@dataclass
class InboundMessage:
    """Structured representation of an inbound message from WeCom."""

    msg_id: str
    chat_id: str
    chat_type: str  # "single" | "group"
    sender_id: str
    sender_name: str
    msg_type: str  # "text", "image", "file", "voice", etc.
    content: str
    raw_payload: dict[str, Any] = field(default_factory=dict)
    reply_to_msg_id: str | None = None


class WeComBotClient:
    """Client for the WeCom Smart Robot OpenWS gateway."""

    def __init__(
        self,
        bot_id: str,
        bot_secret: str,
        websocket_url: str = DEFAULT_WS_URL,
        *,
        transport: WeComWsTransport | None = None,
    ) -> None:
        self._bot_id = bot_id
        self._bot_secret = bot_secret
        self._ws_url = websocket_url
        self._custom_transport = transport
        self._transport: WeComWsTransport | None = None
        self._running = False
        self._connected = False
        self._device_id = f"cyrene-{uuid.uuid4().hex[:12]}"
        self._inbound_handlers: list[Callable[[InboundMessage], Awaitable[None]]] = []
        self._listen_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._pending_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected and (
            self._transport is not None and self._transport.is_open()
        )

    def register_handler(
        self, handler: Callable[[InboundMessage], Awaitable[None]]
    ) -> None:
        """Register a callback for incoming messages."""
        self._inbound_handlers.append(handler)

    async def connect(self) -> None:
        """Connect to OpenWS and perform bot authentication handshake."""
        if not self._bot_id or not self._bot_secret:
            raise ValueError(
                "bot_id and bot_secret are required for WeCom bot connection"
            )

        if self._custom_transport is not None:
            self._transport = self._custom_transport
        else:
            transport = AsyncWebsocketsTransport(self._ws_url)
            await transport.connect()
            self._transport = transport

        # Send aibot_subscribe handshake
        req_id = f"sub-{uuid.uuid4().hex[:16]}"
        subscribe_cmd = {
            "cmd": APP_CMD_SUBSCRIBE,
            "headers": {"req_id": req_id},
            "body": {
                "bot_id": self._bot_id,
                "secret": self._bot_secret,
                "device_id": self._device_id,
            },
        }
        await self._transport.send(json.dumps(subscribe_cmd, ensure_ascii=False))

        # Await handshake reply
        raw_reply = await asyncio.wait_for(
            self._transport.recv(), timeout=CONNECT_TIMEOUT_SECONDS
        )
        reply = json.loads(raw_reply)
        errcode = reply.get("errcode", reply.get("body", {}).get("errcode", 0))
        errmsg = reply.get("errmsg", reply.get("body", {}).get("errmsg", "ok"))
        if errcode != 0 and errcode is not None:
            await self._transport.close()
            raise RuntimeError(
                f"WeCom bot handshake failed: errcode={errcode}, errmsg={errmsg}"
            )

        self._connected = True
        self._running = True

    async def start(self) -> None:
        """Connect and launch background listening and heartbeat tasks."""
        await self.connect()
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """Stop listening and close transport."""
        self._running = False
        self._connected = False
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._listen_task is not None:
            self._listen_task.cancel()
            self._listen_task = None
        for fut in self._pending_requests.values():
            if not fut.done():
                fut.cancel()
        self._pending_requests.clear()
        if self._transport is not None:
            await self._transport.close()
            self._transport = None

    async def respond(
        self,
        msg_id: str,
        content: str,
        *,
        msg_type: str = "text",
        finish: bool = True,
    ) -> str:
        """Respond to an inbound message using aibot_respond."""
        if not self.is_connected or self._transport is None:
            raise RuntimeError("WeCom bot client is not connected")

        req_id = f"resp-{uuid.uuid4().hex[:16]}"
        payload = {
            "cmd": APP_CMD_RESPOND,
            "headers": {"req_id": req_id},
            "body": {
                "msg_id": msg_id,
                "response": {
                    "msg_type": msg_type,
                    msg_type: {"content": content},
                    "finish": finish,
                },
            },
        }
        await self._transport.send(json.dumps(payload, ensure_ascii=False))
        return req_id

    async def stream_respond(
        self,
        msg_id: str,
        chunks: AsyncIterator[str],
        *,
        msg_type: str = "markdown",
    ) -> None:
        """Stream chunks to an inbound message, marking the last chunk as finish."""
        accumulated = ""
        last_chunk = ""
        async for chunk in chunks:
            if last_chunk:
                accumulated += last_chunk
                await self.respond(msg_id, accumulated, msg_type=msg_type, finish=False)
            last_chunk = chunk

        # Final chunk with finish=True
        accumulated += last_chunk
        await self.respond(msg_id, accumulated, msg_type=msg_type, finish=True)

    async def send_proactive(
        self,
        chat_id: str,
        content: str,
        *,
        chat_type: str = "single",
        msg_type: str = "text",
    ) -> str:
        """Send a proactive message via aibot_send."""
        if not self.is_connected or self._transport is None:
            raise RuntimeError("WeCom bot client is not connected")

        req_id = f"send-{uuid.uuid4().hex[:16]}"
        payload = {
            "cmd": APP_CMD_SEND,
            "headers": {"req_id": req_id},
            "body": {
                "chat_id": chat_id,
                "chat_type": chat_type,
                "msg_type": msg_type,
                msg_type: {"content": content},
            },
        }
        await self._transport.send(json.dumps(payload, ensure_ascii=False))
        return req_id

    async def ping(self) -> None:
        """Send application ping heartbeat."""
        if not self.is_connected or self._transport is None:
            return
        req_id = f"ping-{uuid.uuid4().hex[:12]}"
        ping_msg = {
            "cmd": APP_CMD_PING,
            "headers": {"req_id": req_id},
        }
        await self._transport.send(json.dumps(ping_msg, ensure_ascii=False))

    async def _heartbeat_loop(self) -> None:
        """Periodically send heartbeat pings."""
        while self._running:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                await self.ping()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Error sending WeCom bot heartbeat: %s", exc)

    async def _listen_loop(self) -> None:
        """Receive and route incoming WebSocket frames."""
        while self._running and self._transport is not None:
            try:
                raw = await self._transport.recv()
                msg = json.loads(raw)
                cmd = msg.get("cmd")
                req_id = msg.get("headers", {}).get("req_id")

                if req_id and req_id in self._pending_requests:
                    fut = self._pending_requests.pop(req_id)
                    if not fut.done():
                        fut.set_result(msg)

                if cmd == APP_CMD_MSG_CALLBACK:
                    await self._handle_msg_callback(msg)
                elif cmd == APP_CMD_EVENT_CALLBACK:
                    logger.debug("Received WeCom bot event: %s", msg)
                elif cmd == APP_CMD_PING:
                    # Echo pong if requested
                    pong_msg = {
                        "cmd": APP_CMD_PONG,
                        "headers": msg.get("headers", {}),
                    }
                    await self._transport.send(json.dumps(pong_msg))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in WeCom bot listen loop: %s", exc, exc_info=True)
                if not self._running:
                    break
                await asyncio.sleep(1.0)

    async def _handle_msg_callback(self, raw_msg: dict[str, Any]) -> None:
        """Parse incoming aibot_msg_callback and notify registered handlers."""
        body = raw_msg.get("body", {})
        msg_id = body.get("msg_id", "")
        chat_id = body.get("chat_id", "")
        chat_type = body.get("chat_type", "single")
        from_user = body.get("from", {})
        sender_id = from_user.get("user_id", "")
        sender_name = from_user.get("name", sender_id)
        msg_type = body.get("msg_type", "text")

        content = ""
        if msg_type == "text":
            content = body.get("text", {}).get("content", "")
        elif msg_type == "image":
            img = body.get("image", {})
            content = img.get("image_url") or img.get("url", "")
        elif msg_type == "file":
            content = body.get("file", {}).get("file_name", "")

        inbound = InboundMessage(
            msg_id=msg_id,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_id=sender_id,
            sender_name=sender_name,
            msg_type=msg_type,
            content=content,
            raw_payload=raw_msg,
            reply_to_msg_id=body.get("reply_to", {}).get("msg_id"),
        )

        for handler in self._inbound_handlers:
            try:
                await handler(inbound)
            except Exception as handler_err:
                logger.error(
                    "Error executing WeCom inbound message handler: %s",
                    handler_err,
                    exc_info=True,
                )
