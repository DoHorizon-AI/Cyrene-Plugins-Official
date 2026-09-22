"""Invocation tests for the model API connector: unary, streamed and cancelled."""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from cyrene_model_provider_contracts import (
    CHAT_COMPLETION_CHUNK_TYPE_URL,
    CHAT_COMPLETION_METHOD,
    CHAT_COMPLETION_REQUEST_TYPE_URL,
    CHAT_COMPLETION_RESPONSE_TYPE_URL,
    CHAT_COMPLETION_V2_METHOD,
    ChatCompletionRequest,
    ChatMessage,
    ChatRole,
    decode_chat_completion_chunk,
    decode_chat_completion_response,
    encode_chat_completion_request,
    encode_chat_completion_request_v2,
)

from model_api_connector import ModelApiConnector, ProviderSettings


class _Upstream(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible peer with a cancellable slow stream."""

    server_version = "connector-test/1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        self.server.requests.append({"path": self.path, "payload": payload})  # type: ignore[attr-defined]
        behavior = self.path.strip("/").split("/", 1)[0]
        if behavior == "error":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if behavior == "malformed":
            body = b"not-json"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if behavior == "slow":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            self.server.slow_started.set()  # type: ignore[attr-defined]
            while not self.server.slow_release.wait(0.05):  # type: ignore[attr-defined]
                if self._peer_disconnected():
                    self.server.slow_disconnected.set()  # type: ignore[attr-defined]
                    return
            return
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in (
                {
                    "choices": [
                        {"delta": {"role": "assistant", "content": ""}, "finish_reason": None}
                    ]
                },
                {"choices": [{"delta": {"content": "real "}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "path"}, "finish_reason": "stop"}]},
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                },
            ):
                self._write_sse(event)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "real path"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_sse(self, value: Any) -> None:
        self.wfile.write(f"data: {json.dumps(value)}\n\n".encode())
        self.wfile.flush()

    def _peer_disconnected(self) -> bool:
        try:
            readable, _, _ = select.select([self.connection], [], [], 0.05)
            if not readable:
                return False
            flags = socket.MSG_PEEK | getattr(socket, "MSG_DONTWAIT", 0)
            return not self.connection.recv(1, flags)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture()
def upstream_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    server.requests = []  # type: ignore[attr-defined]
    server.slow_started = threading.Event()  # type: ignore[attr-defined]
    server.slow_release = threading.Event()  # type: ignore[attr-defined]
    server.slow_disconnected = threading.Event()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.slow_release.set()  # type: ignore[attr-defined]
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _connector(server: ThreadingHTTPServer, behavior: str) -> ModelApiConnector:
    return ModelApiConnector(
        settings=ProviderSettings(
            base_url=f"http://127.0.0.1:{server.server_port}/{behavior}",
            api_key="conformance-only",
            timeout_seconds=8,
        )
    )


def _encode(request: ChatCompletionRequest, *, structured: bool = False) -> bytes:
    return (
        encode_chat_completion_request_v2(request)
        if structured
        else encode_chat_completion_request(request)
    )


def test_unary_completion_round_trips_the_typed_answer(upstream_server) -> None:
    connector = _connector(upstream_server, "success")
    request = ChatCompletionRequest(
        messages=(ChatMessage(role=ChatRole.USER, content="hello"),),
        model="served",
    )

    ok, payload = connector.on_invoke(
        "model.provider.v1",
        CHAT_COMPLETION_METHOD,
        _encode(request),
        request_type_url=CHAT_COMPLETION_REQUEST_TYPE_URL,
    )

    assert ok is True
    assert payload.type_url == CHAT_COMPLETION_RESPONSE_TYPE_URL
    response = decode_chat_completion_response(payload.value)
    assert [chunk.delta for chunk in response.chunks] == ["real path", ""]
    # chat v1 carries no role field; the closing chunk holds reason and usage.
    assert response.chunks[0].role is None
    assert response.chunks[-1].finish_reason == "stop"
    assert response.chunks[-1].prompt_tokens == 1
    assert upstream_server.requests[-1]["path"] == "/success/v1/chat/completions"
    assert upstream_server.requests[-1]["payload"]["messages"] == [
        {"role": "user", "content": "hello"}
    ]


def test_streamed_completion_yields_one_chunk_per_event(upstream_server) -> None:
    connector = _connector(upstream_server, "success")
    request = ChatCompletionRequest(
        messages=(ChatMessage(role=ChatRole.USER, content="hello"),),
        stream=True,
    )

    ok, stream = connector.on_invoke(
        "model.provider.v1",
        CHAT_COMPLETION_METHOD,
        _encode(request),
        stream_results=True,
    )

    assert ok is True
    chunks = [decode_chat_completion_chunk(item.value) for item in stream.items]
    # v1 has no role field, so the role-only preamble is not a frame of its own;
    # the remaining events map one-to-one.
    assert [chunk.delta for chunk in chunks] == ["real ", "path", ""]
    assert chunks[1].finish_reason == "stop"
    assert (chunks[2].prompt_tokens, chunks[2].completion_tokens) == (3, 2)


def test_stream_items_carry_the_chunk_type_url(upstream_server) -> None:
    connector = _connector(upstream_server, "success")
    request = ChatCompletionRequest(
        messages=(ChatMessage(role=ChatRole.USER, content="hello"),), stream=True
    )

    ok, stream = connector.on_invoke(
        "model.provider.v1", CHAT_COMPLETION_METHOD, _encode(request), stream_results=True
    )

    assert ok is True
    assert {item.type_url for item in stream.items} == {CHAT_COMPLETION_CHUNK_TYPE_URL}


def test_v2_stream_keeps_role_and_reported_total(upstream_server) -> None:
    """The structured method carries the role preamble and a reported total."""

    connector = _connector(upstream_server, "success")
    request = ChatCompletionRequest(
        messages=(ChatMessage(role=ChatRole.USER, content="hello"),),
        stream=True,
        include_usage=True,
    )

    ok, stream = connector.on_invoke(
        "model.provider.v1",
        CHAT_COMPLETION_V2_METHOD,
        _encode(request, structured=True),
        stream_results=True,
    )

    assert ok is True
    chunks = [decode_chat_completion_chunk(item.value) for item in stream.items]
    assert [chunk.delta for chunk in chunks] == ["", "real ", "path", ""]
    assert chunks[0].role == "assistant"
    assert chunks[2].finish_reason == "stop"
    assert chunks[3].total_tokens == 5


def test_structured_method_encodes_v2_requests(upstream_server) -> None:
    connector = _connector(upstream_server, "success")
    request = ChatCompletionRequest(
        messages=(ChatMessage(role=ChatRole.USER, content="hello"),), model="served"
    )

    ok, payload = connector.on_invoke(
        "model.provider.v1",
        CHAT_COMPLETION_V2_METHOD,
        _encode(request, structured=True),
        request_type_url=CHAT_COMPLETION_REQUEST_TYPE_URL,
    )

    assert ok is True
    assert payload.type_url == CHAT_COMPLETION_RESPONSE_TYPE_URL
    assert decode_chat_completion_response(payload.value).chunks[0].delta == "real path"


def test_upstream_failures_are_structured(upstream_server) -> None:
    request = ChatCompletionRequest(messages=(ChatMessage(role=ChatRole.USER, content="hello"),))

    error_connector = _connector(upstream_server, "error")
    ok, detail = error_connector.on_invoke(
        "model.provider.v1", CHAT_COMPLETION_METHOD, _encode(request)
    )
    assert ok is False
    assert detail.startswith("EXECUTION_FAILED:")

    malformed_connector = _connector(upstream_server, "malformed")
    ok, detail = malformed_connector.on_invoke(
        "model.provider.v1", CHAT_COMPLETION_METHOD, _encode(request)
    )
    assert ok is False
    assert detail.startswith("EXECUTION_FAILED:")


def test_unsupported_capability_method_and_payload_fail_closed(upstream_server) -> None:
    connector = _connector(upstream_server, "success")
    request = ChatCompletionRequest(messages=(ChatMessage(role=ChatRole.USER, content="hi"),))
    encoded = _encode(request)

    ok, detail = connector.on_invoke("model.other.v1", CHAT_COMPLETION_METHOD, encoded)
    assert ok is False and detail.startswith("INVALID_REQUEST:")

    ok, detail = connector.on_invoke("model.provider.v1", "embeddings", encoded)
    assert ok is False and detail.startswith("METHOD_NOT_SUPPORTED:")

    ok, detail = connector.on_invoke("model.provider.v1", "chat_completion_v9", encoded)
    assert ok is False and detail.startswith("METHOD_NOT_FOUND:")

    ok, detail = connector.on_invoke("model.provider.v1", CHAT_COMPLETION_METHOD, b"\xff\xff")
    assert ok is False and detail.startswith("INVALID_REQUEST:")

    ok, detail = connector.on_invoke(
        "model.provider.v1",
        CHAT_COMPLETION_METHOD,
        encoded,
        request_type_url="type.cyrene.io/other",
    )
    assert ok is False and detail.startswith("INVALID_REQUEST:")


def test_missing_configuration_fails_closed() -> None:
    connector = ModelApiConnector(settings=None)
    request = ChatCompletionRequest(messages=(ChatMessage(role=ChatRole.USER, content="hi"),))

    ok, detail = connector.on_invoke("model.provider.v1", CHAT_COMPLETION_METHOD, _encode(request))

    assert ok is False
    assert detail.startswith("INVALID_CONFIGURATION:")


def test_cancellation_aborts_the_upstream_request(upstream_server) -> None:
    """A cancel must close the upstream socket, not just stop waiting."""

    connector = _connector(upstream_server, "slow")
    request = ChatCompletionRequest(
        messages=(ChatMessage(role=ChatRole.USER, content="hello"),), stream=True
    )
    ok, stream = connector.on_invoke(
        "model.provider.v1",
        CHAT_COMPLETION_METHOD,
        _encode(request),
        request_id="req-1",
        stream_results=True,
    )
    assert ok is True

    received: list[Any] = []

    def consume() -> None:
        received.extend(stream.items)

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    assert upstream_server.slow_started.wait(5)  # type: ignore[attr-defined]

    started = time.monotonic()
    connector.on_cancel("req-1", "client disconnected")

    assert upstream_server.slow_disconnected.wait(5)  # type: ignore[attr-defined]
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert time.monotonic() - started < 4
    # A cancelled stream reports no chunks and never reports usage.
    assert received == []


def test_cancel_for_an_unknown_request_is_a_no_op(upstream_server) -> None:
    connector = _connector(upstream_server, "success")
    connector.on_cancel("never-started", "client disconnected")
