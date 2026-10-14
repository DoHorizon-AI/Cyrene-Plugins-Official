"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 bot_client.py                                                   │
│  Package: wecom_connector                                           │
│  Role: WeCom Smart Robot WebSocket openws client implementation.    │
│                                                                     │
│  模块职责：企业微信智能机器人 WebSocket 长连接网关客户端。                │
│  · 协议握手：连接 wss://openws.work.weixin.qq.com 并发送 aibot_subscribe │
│  · 双向消息：入站回调 (aibot_msg_callback) 与出站响应 (aibot_respond_msg) │
│  · 流式响应：分片输出与 finish 标志位控制                               │
│  · 心跳重连：30s 自动 ping 维持保活与指数退避重连                       │
│  · 生产连接使用固定版本官方 SDK；ScriptedWsTransport 仅供离线测试    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"

# OpenWS Command Identifiers
APP_CMD_SUBSCRIBE = "aibot_subscribe"
APP_CMD_MSG_CALLBACK = "aibot_msg_callback"
APP_CMD_EVENT_CALLBACK = "aibot_event_callback"
APP_CMD_RESPOND = "aibot_respond_msg"
APP_CMD_SEND = "aibot_send_msg"
APP_CMD_PING = "ping"
APP_CMD_PONG = "pong"

CONNECT_TIMEOUT_SECONDS = 20.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
RECONNECT_BACKOFF = [1.0, 2.0, 5.0, 10.0, 30.0]


class WeComBotRequestError(RuntimeError):
    """Vendor acknowledgement rejection with its stable error code."""

    def __init__(self, errcode: int | str, errmsg: str) -> None:
        super().__init__(errmsg)
        self.errcode = errcode
        self.errmsg = errmsg


class WeComBotUncertainError(TimeoutError):
    """A bot write may have reached WeCom, but no trustworthy ACK arrived."""

    def __init__(self, request_id: str, message: str) -> None:
        super().__init__(message)
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class WeComBotReceipt:
    """Vendor ACK facts kept separate from a canonical vendor message ID."""

    request_id: str
    vendor_message_id: str = ""
    errcode: int | str = 0


class WeComWsTransport(Protocol):
    """Protocol for WebSocket transport to enable deterministic testing."""

    async def send(self, data: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...

    def is_open(self) -> bool: ...


class ScriptedWsTransport:
    """Deterministic scriptable WebSocket transport for unit testing."""

    def __init__(self) -> None:
        self.inbound_queue: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
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
        self._loop = asyncio.get_running_loop()
        value = await self.inbound_queue.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        self._closed = True

    def is_open(self) -> bool:
        return not self._closed

    def push_inbound(self, payload: Mapping[str, Any]) -> None:
        """Push a message to be received by recv()."""
        value = json.dumps(payload, ensure_ascii=False)
        self._enqueue(value)

    def drop(self, reason: str = "scripted WebSocket dropped") -> None:
        """Cause one receive to fail so reconnect behavior can be tested.

        中文:模拟一次 WebSocket 断线,以验证重连行为。
        """

        self._closed = True
        self._enqueue(ConnectionError(reason))

    def _enqueue(self, value: str | BaseException) -> None:
        loop = self._loop
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if loop is not None and loop.is_running() and current_loop is not loop:
            loop.call_soon_threadsafe(self.inbound_queue.put_nowait, value)
        else:
            self.inbound_queue.put_nowait(value)


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
    callback_req_id: str = ""


class WeComBotClient:
    """Client for the WeCom Smart Robot OpenWS gateway."""

    def __init__(
        self,
        bot_id: str,
        bot_secret: str,
        websocket_url: str = DEFAULT_WS_URL,
        *,
        transport: WeComWsTransport | None = None,
        transport_factory: Callable[[], WeComWsTransport] | None = None,
    ) -> None:
        self._bot_id = bot_id
        self._bot_secret = bot_secret
        self._ws_url = websocket_url
        self._custom_transport = transport
        self._transport_factory = transport_factory
        self._custom_transport_used = False
        self._transport: WeComWsTransport | None = None
        self._running = False
        self._connected = False
        self._device_id = f"cyrene-{uuid.uuid4().hex[:12]}"
        self._inbound_handlers: list[Callable[[InboundMessage], Awaitable[None]]] = []
        self._listen_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._pending_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reconnect_count = 0
        self._last_error: str | None = None
        self._sdk_client: Any = None
        self._sdk_authenticated = False
        self._callback_req_ids: OrderedDict[str, str] = OrderedDict()
        self._callback_req_ids_lock = threading.Lock()

    @property
    def _uses_official_sdk(self) -> bool:
        """Use the pinned official SDK for every non-injected production socket."""
        return self._custom_transport is None and self._transport_factory is None

    @property
    def is_connected(self) -> bool:
        if self._uses_official_sdk:
            return self._connected and self._sdk_authenticated
        return (
            self._connected
            and self._transport is not None
            and self._transport.is_open()
        )

    @property
    def reconnect_count(self) -> int:
        """Return the number of successful reconnects after the initial session."""
        return self._reconnect_count

    @property
    def last_error(self) -> str | None:
        """Return a bounded, secret-free last transport error for health reporting."""
        return self._last_error

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

        if self._uses_official_sdk:
            await self._connect_official_sdk()
            return

        transport = await self._new_transport()
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
        try:
            raw_reply = await asyncio.wait_for(
                transport.recv(), timeout=CONNECT_TIMEOUT_SECONDS
            )
            reply = json.loads(raw_reply)
        except BaseException:
            await transport.close()
            if self._transport is transport:
                self._transport = None
            raise
        try:
            _receipt_from_ack(reply, req_id)
        except WeComBotRequestError as exc:
            await transport.close()
            self._transport = None
            raise RuntimeError(
                "WeCom bot handshake failed: "
                f"errcode={exc.errcode}, errmsg={exc.errmsg}"
            ) from exc
        except WeComBotUncertainError:
            await transport.close()
            self._transport = None
            raise

        self._connected = True
        self._running = True
        self._last_error = None

    async def _new_transport(self) -> WeComWsTransport:
        """Create a fresh transport for the initial connection or a reconnect."""
        if self._transport_factory is not None:
            transport = self._transport_factory()
        elif self._custom_transport is not None and not self._custom_transport_used:
            transport = self._custom_transport
            self._custom_transport_used = True
        else:
            raise RuntimeError("production WebSocket uses the official WeCom SDK")
        return transport

    async def _connect_official_sdk(self) -> None:
        """Start the official WeCom SDK and wait until its auth ACK arrives."""
        from aibot import WSClient, WSClientOptions

        if self._sdk_client is None:
            self._sdk_client = WSClient(
                WSClientOptions(
                    bot_id=self._bot_id,
                    secret=self._bot_secret,
                    ws_url=self._ws_url,
                    max_reconnect_attempts=-1,
                )
            )
            self._sdk_client.on("authenticated", self._on_sdk_authenticated)
            self._sdk_client.on("disconnected", self._on_sdk_disconnected)
            self._sdk_client.on("message", self._on_sdk_message)
            self._sdk_client.on("event", self._on_sdk_event)
            self._sdk_client.on("error", self._on_sdk_error)
        if self._sdk_authenticated:
            return
        authenticated = asyncio.Event()
        self._sdk_ready_event = authenticated
        await self._sdk_client.connect()
        try:
            await asyncio.wait_for(
                authenticated.wait(), timeout=CONNECT_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            self._sdk_client.disconnect()
            self._sdk_client = None
            raise RuntimeError(
                "WeCom bot SDK authentication acknowledgement timed out"
            ) from exc
        self._sdk_ready_event = None
        self._connected = True
        self._running = True
        self._last_error = None

    def _on_sdk_authenticated(self) -> None:
        self._sdk_authenticated = True
        self._connected = True
        event = getattr(self, "_sdk_ready_event", None)
        if event is not None:
            event.set()
        elif self._running:
            self._reconnect_count += 1

    def _on_sdk_disconnected(self, reason: str) -> None:
        self._sdk_authenticated = False
        self._connected = False
        self._last_error = str(reason)[:512]
        self._fail_pending_requests(ConnectionError("WeCom SDK connection lost"))

    def _on_sdk_error(self, error: BaseException) -> None:
        self._last_error = str(error)[:512]

    async def _on_sdk_message(self, frame: Mapping[str, Any]) -> None:
        await self._handle_msg_callback(dict(frame))

    async def _on_sdk_event(self, frame: Mapping[str, Any]) -> None:
        logger.debug("Received WeCom bot event callback")

    async def start(self) -> None:
        """Connect and launch background listening and heartbeat tasks."""
        if self._running and self._listen_task is not None:
            return
        await self.connect()
        if self._uses_official_sdk:
            return
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """Stop listening and close transport."""
        self._running = False
        self._connected = False
        self._sdk_authenticated = False
        tasks = [
            task
            for task in (self._heartbeat_task, self._listen_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        for fut in self._pending_requests.values():
            if not fut.done():
                fut.cancel()
        self._pending_requests.clear()
        if self._sdk_client is not None:
            self._sdk_client.disconnect()
            self._sdk_client = None
        if self._transport is not None:
            await self._transport.close()
            self._transport = None
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._heartbeat_task = None
        self._listen_task = None

    async def respond(
        self,
        msg_id: str,
        content: str,
        *,
        msg_type: str = "text",
        finish: bool = True,
    ) -> WeComBotReceipt:
        """Respond to a callback using its original OpenWS request identity."""
        if self._uses_official_sdk:
            callback_req_id = self._callback_request_id(msg_id)
            if not callback_req_id:
                raise ValueError("WeCom callback request ID is unavailable for reply")
            frame = {"headers": {"req_id": callback_req_id}}
            body = {"msgtype": msg_type, msg_type: {"content": content}}
            if msg_type == "stream":
                body.pop("stream", None)
                body["stream"] = {
                    "id": f"stream-{uuid.uuid4().hex}",
                    "content": content,
                    "finish": finish,
                }
            try:
                response = await self._sdk_client.reply(frame, body)
            except Exception as exc:
                raise _sdk_ack_error(exc, callback_req_id) from exc
            return _receipt_from_ack(response, callback_req_id)
        if not self.is_connected or self._transport is None:
            raise RuntimeError("WeCom bot client is not connected")

        req_id = self._callback_request_id(msg_id)
        if not req_id:
            raise ValueError("WeCom callback request ID is unavailable for reply")
        payload = {
            "cmd": APP_CMD_RESPOND,
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": msg_type,
                msg_type: {"content": content},
            },
        }
        response = await self._send_with_ack(req_id, payload)
        return _receipt_from_ack(response, req_id)

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
        callback_req_id = self._callback_request_id(msg_id)
        if not callback_req_id:
            raise ValueError(
                "WeCom callback request ID is unavailable for stream reply"
            )
        stream_id = f"stream-{uuid.uuid4().hex}"
        async for chunk in chunks:
            if last_chunk:
                accumulated += last_chunk
                await self._send_stream_chunk(
                    callback_req_id, stream_id, accumulated, False
                )
            last_chunk = chunk

        # Final chunk with finish=True
        accumulated += last_chunk
        await self._send_stream_chunk(callback_req_id, stream_id, accumulated, True)

    async def _send_stream_chunk(
        self, callback_req_id: str, stream_id: str, content: str, finish: bool
    ) -> WeComBotReceipt:
        """Send one official stream response using the callback req_id."""
        if self._uses_official_sdk:
            frame = {"headers": {"req_id": callback_req_id}}
            try:
                response = await self._sdk_client.reply_stream(
                    frame, stream_id, content, finish
                )
            except Exception as exc:
                raise _sdk_ack_error(exc, callback_req_id) from exc
            return _receipt_from_ack(response, callback_req_id)
        payload = {
            "cmd": APP_CMD_RESPOND,
            "headers": {"req_id": callback_req_id},
            "body": {
                "msgtype": "stream",
                "stream": {"id": stream_id, "content": content, "finish": finish},
            },
        }
        response = await self._send_with_ack(callback_req_id, payload)
        return _receipt_from_ack(response, callback_req_id)

    async def send_proactive(
        self,
        chat_id: str,
        content: str,
        *,
        chat_type: str = "single",
        msg_type: str = "text",
    ) -> WeComBotReceipt:
        """Send a proactive message through the official OpenWS SDK surface."""
        if self._uses_official_sdk:
            if not self.is_connected or self._sdk_client is None:
                raise RuntimeError("WeCom bot client is not connected")
            body = {"msgtype": msg_type, msg_type: {"content": content}}
            try:
                response = await self._sdk_client.send_message(chat_id, body)
            except Exception as exc:
                raise _sdk_ack_error(exc, "") from exc
            return _receipt_from_ack(response, "")
        if not self.is_connected or self._transport is None:
            raise RuntimeError("WeCom bot client is not connected")

        req_id = f"send-{uuid.uuid4().hex[:16]}"
        payload = {
            "cmd": APP_CMD_SEND,
            "headers": {"req_id": req_id},
            "body": {
                "chatid": chat_id,
                "msgtype": msg_type,
                msg_type: {"content": content},
            },
        }
        response = await self._send_with_ack(req_id, payload)
        return _receipt_from_ack(response, req_id)

    async def _send_with_ack(
        self, req_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Send one request and wait for its matching vendor acknowledgement."""
        if not self.is_connected or self._transport is None:
            raise RuntimeError("WeCom bot client is not connected")
        future = asyncio.get_running_loop().create_future()
        self._pending_requests[req_id] = future
        try:
            await self._transport.send(json.dumps(payload, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=CONNECT_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            if self._pending_requests.get(req_id) is future:
                self._pending_requests.pop(req_id, None)
            if not future.done():
                future.cancel()
            raise WeComBotUncertainError(
                req_id, "WeCom bot acknowledgement timed out after request submission"
            ) from exc
        except asyncio.CancelledError:
            if self._pending_requests.get(req_id) is future:
                self._pending_requests.pop(req_id, None)
            if not future.done():
                future.cancel()
            raise
        except Exception as exc:
            if self._pending_requests.get(req_id) is future:
                self._pending_requests.pop(req_id, None)
            if not future.done():
                future.cancel()
            raise WeComBotUncertainError(
                req_id, "WeCom bot transport failed after request submission"
            ) from exc
        if _ack_request_id(response) != req_id:
            raise WeComBotUncertainError(
                req_id, "WeCom bot acknowledgement correlation failed"
            )
        errcode = response.get("errcode")
        errmsg = response.get("errmsg", "")
        if isinstance(errcode, bool) or not isinstance(errcode, int):
            raise WeComBotUncertainError(
                req_id, "WeCom bot acknowledgement has no valid errcode"
            )
        if errcode != 0:
            raise WeComBotRequestError(errcode, str(errmsg))
        return response

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
        backoff_index = 0
        while self._running:
            transport = self._transport
            if transport is None:
                try:
                    await self.connect()
                    self._reconnect_count += 1
                    backoff_index = 0
                    transport = self._transport
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    self._connected = False
                    self._last_error = str(exc)[:512]
                    delay = RECONNECT_BACKOFF[
                        min(backoff_index, len(RECONNECT_BACKOFF) - 1)
                    ]
                    backoff_index += 1
                    await asyncio.sleep(delay)
                    continue
            if transport is None:
                continue
            try:
                raw = await transport.recv()
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
                    await transport.send(json.dumps(pong_msg))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in WeCom bot listen loop: %s", exc, exc_info=True)
                if not self._running:
                    break
                self._connected = False
                self._last_error = str(exc)[:512]
                self._fail_pending_requests(exc)
                if self._transport is transport:
                    self._transport = None
                try:
                    await transport.close()
                except Exception:
                    logger.debug(
                        "Failed to close dropped WeCom transport", exc_info=True
                    )
                delay = RECONNECT_BACKOFF[
                    min(backoff_index, len(RECONNECT_BACKOFF) - 1)
                ]
                backoff_index += 1
                await asyncio.sleep(delay)

    def _fail_pending_requests(self, error: BaseException) -> None:
        """Fail outstanding sends when their connection is no longer usable."""
        pending = tuple(self._pending_requests.items())
        self._pending_requests.clear()
        for request_id, future in pending:
            if not future.done():
                future.set_exception(
                    WeComBotUncertainError(
                        request_id,
                        f"WeCom connection lost before acknowledgement: {error}",
                    )
                )

    async def _handle_msg_callback(self, raw_msg: dict[str, Any]) -> None:
        """Parse incoming aibot_msg_callback and notify registered handlers."""
        body = raw_msg.get("body", {})
        msg_id = body.get("msgid", "")
        chat_id = body.get("chatid", "")
        chat_type = body.get("chattype", "single")
        from_user = body.get("from", {})
        sender_id = from_user.get("userid", "")
        sender_name = from_user.get("name", sender_id)
        msg_type = body.get("msgtype", "text")

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
            reply_to_msg_id=(
                body.get("reply_to", {}).get("msgid")
                if isinstance(body.get("reply_to"), Mapping)
                else None
            ),
            callback_req_id=str(raw_msg.get("headers", {}).get("req_id", "")),
        )
        if inbound.msg_id and inbound.callback_req_id:
            self._remember_callback_request_id(inbound.msg_id, inbound.callback_req_id)

        for handler in self._inbound_handlers:
            try:
                await handler(inbound)
            except Exception as handler_err:
                logger.error(
                    "Error executing WeCom inbound message handler: %s",
                    handler_err,
                    exc_info=True,
                )

    def restore_callback_request_id(self, msg_id: str, callback_req_id: str) -> None:
        """Restore one callback mapping from the connector's trusted local outbox.

        Callers must first verify the message, bot account, conversation, and
        bounded local expiry through the integration forwarder. This method is
        never exposed through a vendor payload or Plugin capability.
        中文：仅供 connector 恢复本机持久化且已校验的回调映射。
        """

        if (
            not isinstance(msg_id, str)
            or not msg_id
            or not isinstance(callback_req_id, str)
            or not callback_req_id
            or len(msg_id) > 300
            or len(callback_req_id) > 300
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in callback_req_id
            )
        ):
            raise ValueError("persisted WeCom reply context is invalid")
        self._remember_callback_request_id(msg_id, callback_req_id)

    def _callback_request_id(self, msg_id: str) -> str | None:
        """Read a callback correlation under the shared client lock."""

        with self._callback_req_ids_lock:
            return self._callback_req_ids.get(msg_id)

    def _remember_callback_request_id(self, msg_id: str, callback_req_id: str) -> None:
        """Store one SDK callback correlation and bound the in-memory cache."""

        with self._callback_req_ids_lock:
            self._callback_req_ids[msg_id] = callback_req_id
            self._callback_req_ids.move_to_end(msg_id)
            while len(self._callback_req_ids) > 4096:
                self._callback_req_ids.popitem(last=False)


def _ack_request_id(response: Mapping[str, Any]) -> str:
    """Read the ACK correlation identity without accepting a generated fallback."""
    headers = response.get("headers")
    if not isinstance(headers, Mapping):
        return ""
    value = headers.get("req_id")
    return value if isinstance(value, str) else ""


def _receipt_from_ack(
    response: Mapping[str, Any], expected_request_id: str = ""
) -> WeComBotReceipt:
    """Validate a successful official ACK and keep its request ID separate."""
    request_id = _ack_request_id(response)
    if not request_id or (expected_request_id and request_id != expected_request_id):
        raise WeComBotUncertainError(
            expected_request_id or request_id,
            "WeCom bot acknowledgement correlation failed",
        )
    errcode = response.get("errcode")
    if isinstance(errcode, bool) or not isinstance(errcode, int):
        raise WeComBotUncertainError(
            request_id, "WeCom bot acknowledgement has no valid errcode"
        )
    if errcode != 0:
        raise WeComBotRequestError(errcode, str(response.get("errmsg", "")))
    body = response.get("body", {})
    if not isinstance(body, Mapping):
        body = {}
    for key in ("msgid",):
        value = response.get(key, body.get(key))
        if isinstance(value, str) and value:
            return WeComBotReceipt(request_id, value, errcode)
    return WeComBotReceipt(request_id, "", errcode)


def _sdk_ack_error(error: BaseException, request_id: str) -> BaseException:
    """Translate SDK ACK failures into rejection or explicit uncertainty."""
    match = re.search(r"errcode=([0-9]+)", str(error))
    if match:
        errcode = int(match.group(1))
        errmsg_match = re.search(r"errmsg=(.*?)(?:\)$|$)", str(error))
        errmsg = errmsg_match.group(1) if errmsg_match else str(error)
        return WeComBotRequestError(errcode, errmsg)
    return WeComBotUncertainError(
        request_id, "WeCom bot SDK did not return a trustworthy acknowledgement"
    )
