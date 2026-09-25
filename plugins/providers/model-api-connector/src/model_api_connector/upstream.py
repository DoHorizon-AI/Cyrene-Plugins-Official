"""One upstream OpenAI-compatible endpoint, reachable and cancellable.

The connector is the only place that talks HTTP: it owns the socket so a
cancelled invocation can abort an in-flight upstream request instead of leaving
it running until the deadline. A trainer or gateway that stops waiting must not
keep the upstream busy. | 取消必须真正中止上游请求。
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import queue
import socket
import threading
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

from .settings import ProviderSettings

SSE_DATA_PREFIX = "data:"
SSE_DONE = "[DONE]"
MAX_ERROR_BODY_CHARS = 400


class _StreamEnd:
    """Sentinel pushed by the reader thread when the upstream stream finishes.

        中文:reader 线程在上游流结束时推送的 sentinel。
    """


class UpstreamFailure(RuntimeError):
    """The upstream endpoint failed or answered outside the contract.

        中文:上游 Endpoint 失败,或响应内容违反 contract。
    """

    def __init__(self, detail: str, *, status: int | None = None) -> None:
        super().__init__(detail)
        self.status = status


class ProviderCancelled(RuntimeError):
    """The invocation was cancelled while the upstream request was in flight.

        中文:上游请求仍在执行时,调用被取消。
    """


class UpstreamCall:
    """A handle another thread can use to abort one in-flight request.

        中文:供另一个线程中止某个进行中请求的句柄。
    """

    def __init__(self) -> None:
        self._connection: http.client.HTTPConnection | None = None
        self._socket: socket.socket | None = None
        self._response: http.client.HTTPResponse | None = None
        self._closed = threading.Event()
        self._lock = threading.Lock()

    def attach(self, connection: http.client.HTTPConnection) -> None:
        with self._lock:
            self._connection = connection
        if self._closed.is_set():
            self._close_resources()

    def attach_socket(self, sock: socket.socket | None) -> None:
        """Keep an independent handle on the connection so it can be aborted.

        The peer may answer with an HTTP/1.0 response, after which http.client
        marks its own socket closed while the file object keeps the descriptor
        alive; a duplicated descriptor is the handle that can still send FIN.

            中文:为连接保留一个可独立使用的句柄,以便中止请求。对端可能返回 HTTP/1.0 响应;此后 `http.client` 会将自己的 socket 标记为关闭,但文件对象仍保留该文件描述符。复制后的描述符才是仍可发送 FIN 的句柄。
        """

        shim: socket.socket | None = None
        if sock is not None:
            try:
                shim = socket.socket(fileno=os.dup(sock.fileno()))
            except OSError:
                shim = None
        with self._lock:
            self._socket = shim
        if self._closed.is_set():
            self._close_resources()

    def attach_response(self, response: http.client.HTTPResponse) -> None:
        with self._lock:
            self._response = response
        if self._closed.is_set():
            self._close_resources()

    def close(self) -> None:
        """Abort the request; safe to call from a cancellation thread.

            中文:中止请求;可从取消处理线程安全调用。
        """

        self._closed.set()
        self._close_resources()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def raise_if_closed(self) -> None:
        if self._closed.is_set():
            raise ProviderCancelled("upstream request was cancelled")

    def _close_resources(self) -> None:
        with self._lock:
            response = self._response
            self._response = None
            sock = self._socket
            self._socket = None
            connection = self._connection
            self._connection = None
        # A peer that answers with HTTP/1.0 leaves http.client's connection
        # object without a socket of its own, so the response and the captured
        # socket are the handles that actually shut the upstream down.
        # 中文:对端若返回 HTTP/1.0,`http.client` 的 connection 对象就不会再持有自己的 socket。因此,response 和捕获到的 socket 才是真正能够关闭上游的句柄。
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()
        if response is not None:
            with contextlib.suppress(OSError, ValueError):
                response.close()
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.close()


class OpenAICompatibleUpstream:
    """POST one chat-completions body and hand back JSON or an SSE event stream.

        中文:POST 一份 chat-completions 请求正文,并返回 JSON 或 SSE 事件流。
    """

    def __init__(self, settings: ProviderSettings) -> None:
        self._settings = settings

    def complete(self, body: Mapping[str, Any], call: UpstreamCall) -> dict[str, Any]:
        """Send a non-streamed request and return the decoded JSON body.

            中文:发送非流式请求,并返回解码后的 JSON 正文。
        """

        connection, response = self._send(body, call)
        try:
            call.raise_if_closed()
            raw = response.read()
            call.raise_if_closed()
        except (OSError, ValueError) as error:
            if call.closed:
                raise ProviderCancelled("upstream request was cancelled") from error
            raise UpstreamFailure(f"upstream read failed: {error}") from error
        finally:
            connection.close()
        return _decode_json(raw)

    def events(
        self,
        body: Mapping[str, Any],
        call: UpstreamCall,
        *,
        cancellation: Any | None = None,
        poll_interval: float = 0.25,
    ) -> Iterator[dict[str, Any]]:
        """Yield decoded SSE events until ``[DONE]`` or the stream ends.

        A provider that has stopped sending cannot be waited on blindly: the
        consumer polls the runtime's cancellation signal while a reader thread
        holds the socket, so a cancelled client aborts within one interval
        instead of at the deadline. | 空闲上游不能阻塞取消。
        """

        events: queue.Queue[Any] = queue.Queue(maxsize=64)
        outcome: dict[str, BaseException | None] = {"error": None}
        sentinel = _StreamEnd()

        def pump() -> None:
            try:
                for event in self._read_events(body, call):
                    events.put(event)
            except BaseException as error:  # noqa: BLE001 - re-raised on the consumer
                outcome["error"] = error
            finally:
                events.put(sentinel)

        worker = threading.Thread(target=pump, name="cyrene-upstream-reader", daemon=True)
        worker.start()
        try:
            while True:
                if cancellation is not None and cancellation.is_cancelled():
                    call.close()
                    raise ProviderCancelled("upstream request was cancelled")
                try:
                    item = events.get(timeout=poll_interval)
                except queue.Empty:
                    continue
                if item is sentinel:
                    error = outcome["error"]
                    if error is not None:
                        raise error
                    return
                yield item
        finally:
            call.close()
            worker.join(timeout=1.0)

    def _read_events(
        self, body: Mapping[str, Any], call: UpstreamCall
    ) -> Iterator[dict[str, Any]]:
        """Read the upstream SSE stream on the reader thread.

            中文:在 reader 线程上读取上游 SSE 流。
        """

        connection, response = self._send(body, call)
        try:
            while True:
                call.raise_if_closed()
                try:
                    line = response.readline()
                except (OSError, ValueError) as error:
                    if call.closed:
                        raise ProviderCancelled("upstream request was cancelled") from error
                    raise UpstreamFailure(f"upstream stream failed: {error}") from error
                if not line:
                    return
                event = parse_sse_line(line.decode("utf-8", errors="replace"))
                if event is None:
                    continue
                if event == SSE_DONE:
                    return
                yield event
        finally:
            connection.close()

    def _send(
        self, body: Mapping[str, Any], call: UpstreamCall
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        call.raise_if_closed()
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        parsed = urlsplit(self._settings.chat_completions_url)
        host = parsed.hostname or ""
        if not host:
            raise UpstreamFailure("base_url has no host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        factory = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = factory(host, port, timeout=self._settings.timeout_seconds)
        call.attach(connection)
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if body.get("stream") else "application/json",
            "User-Agent": "cyrene-model-api-connector",
        }
        if self._settings.api_key:
            headers["Authorization"] = "Bearer " + self._settings.api_key
        try:
            connection.request("POST", target, body=encoded, headers=headers)
            # The socket must be captured before the response is read: a peer
            # answering with HTTP/1.0 makes http.client drop its own reference.
            # 中文:读取 response 之前必须先捕获 socket:如果对端返回 HTTP/1.0,`http.client` 会丢弃自己对 socket 的引用。
            call.attach_socket(connection.sock)
            response = connection.getresponse()
        except (OSError, ValueError) as error:
            call.close()
            if call.closed:
                raise ProviderCancelled("upstream request was cancelled") from error
            raise UpstreamFailure(f"upstream is unreachable: {error}") from error
        call.attach_response(response)
        if response.status >= 400:
            detail = response.read(4096).decode("utf-8", errors="replace")
            connection.close()
            raise UpstreamFailure(
                f"upstream returned HTTP {response.status}: {detail[:MAX_ERROR_BODY_CHARS]}",
                status=response.status,
            )
        return connection, response


def parse_sse_line(line: str) -> dict[str, Any] | str | None:
    """Decode one SSE line into an event, the done marker, or nothing.

        中文:将一行 SSE 解码为事件、结束标记或空结果。
    """

    stripped = line.strip()
    if not stripped or stripped.startswith(":"):
        return None
    if not stripped.startswith(SSE_DATA_PREFIX):
        return None
    payload = stripped[len(SSE_DATA_PREFIX) :].strip()
    if not payload:
        return None
    if payload == SSE_DONE:
        return SSE_DONE
    try:
        event = json.loads(payload)
    except json.JSONDecodeError as error:
        raise UpstreamFailure("upstream stream carried a non-JSON event") from error
    if not isinstance(event, dict):
        raise UpstreamFailure("upstream stream carried a non-object event")
    return event


def _decode_json(raw: bytes) -> dict[str, Any]:
    if not raw:
        raise UpstreamFailure("upstream returned an empty body")
    try:
        document = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as error:
        raise UpstreamFailure("upstream returned a non-JSON body") from error
    if not isinstance(document, dict):
        raise UpstreamFailure("upstream returned a non-object body")
    return document
