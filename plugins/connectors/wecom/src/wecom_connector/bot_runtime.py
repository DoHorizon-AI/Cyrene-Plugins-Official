"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 bot_runtime.py                                                  │
│  Package: wecom_connector                                           │
│  Role: Own one long-lived event loop for the WeCom bot client.      │
│                                                                     │
│  模块职责：为 WeCom bot client 管理一个长期运行的事件循环。             │
│  · 持有连接与监听任务,允许同步 Plugin runtime 调用安全地提交发送任务。   │
│  · 统一启动、停止和重连期间的 bot 资源生命周期。                       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import Any

from .bot_client import InboundMessage, WeComBotClient


class WeComBotRuntime:
    """Run one bot client on a dedicated loop and expose blocking boundaries.

    中文:在专用事件循环中运行一个 bot client,并提供同步边界供 Plugin runtime 使用。
    """

    def __init__(
        self,
        client: WeComBotClient,
        inbound_handler: Callable[[InboundMessage], Awaitable[None]],
        *,
        startup_timeout_seconds: float = 25.0,
    ) -> None:
        self._client = client
        self._inbound_handler = inbound_handler
        self._startup_timeout_seconds = startup_timeout_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._handler_registered = False
        self._pending_sends: dict[str, Future[str]] = {}

    @property
    def is_connected(self) -> bool:
        """Return the bot transport's current readiness state."""
        return self._client.is_connected

    def start(self) -> None:
        """Start the background loop and establish the initial bot session.

        中文:启动后台事件循环并建立初始 bot 会话。
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if self._client.is_connected:
                    return
                loop = self._loop
            else:
                loop = asyncio.new_event_loop()
                self._loop = loop
                self._thread = threading.Thread(
                    target=self._run_loop,
                    args=(loop,),
                    name="wecom-bot-runtime",
                    daemon=True,
                )
                self._thread.start()
            if loop is None:
                raise RuntimeError("WeCom bot event loop is unavailable")
            started = asyncio.run_coroutine_threadsafe(self._start_client(), loop)
            started.result(timeout=self._startup_timeout_seconds)

    async def _start_client(self) -> None:
        if not self._handler_registered:
            self._client.register_handler(self._inbound_handler)
            self._handler_registered = True
        await self._client.start()

    def send(
        self,
        chat_id: str,
        content: str,
        chat_type: str,
        reply_id: str | None,
        *,
        timeout_seconds: float,
        request_id: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> str:
        """Send one notification through the connected bot and await its receipt.

        中文:通过已连接 bot 发送一条通知,并等待厂商确认回执。
        """
        self.start()
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("WeCom bot runtime is not running")
        if reply_id:
            operation = self._client.respond(reply_id, content)
        else:
            operation = self._client.send_proactive(
                chat_id, content, chat_type=chat_type
            )
        future = asyncio.run_coroutine_threadsafe(operation, loop)
        operation_id = request_id or f"wecom-send-{uuid.uuid4().hex}"
        with self._lock:
            self._pending_sends[operation_id] = future
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                if is_cancelled is not None and is_cancelled():
                    future.cancel()
                    raise RuntimeError("WeCom bot send was cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    future.cancel()
                    raise TimeoutError("WeCom bot send receipt timed out")
                try:
                    return future.result(timeout=min(0.1, remaining))
                except TimeoutError:
                    if future.done():
                        return future.result()
        finally:
            with self._lock:
                if self._pending_sends.get(operation_id) is future:
                    self._pending_sends.pop(operation_id, None)

    def cancel(self, request_id: str) -> None:
        """Cancel the matching in-flight bot send, if that request owns one."""
        with self._lock:
            future = self._pending_sends.get(request_id)
        if future is not None:
            future.cancel()

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        """Cancel bot tasks, close the WebSocket, and join the loop thread.

        中文:取消 bot 任务、关闭 WebSocket 并等待事件循环线程退出。
        """
        with self._lock:
            loop = self._loop
            thread = self._thread
        if loop is None or thread is None:
            return
        if loop.is_running():
            stopped: Future[Any] = asyncio.run_coroutine_threadsafe(
                self._client.stop(), loop
            )
            try:
                stopped.result(timeout=timeout_seconds)
            finally:
                loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=timeout_seconds)
        with self._lock:
            self._loop = None
            self._thread = None

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()
