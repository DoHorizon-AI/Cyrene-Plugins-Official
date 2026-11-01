"""Invocation tests for the model API connector: unary, streamed and cancelled.

中文:模型 API 连接器的调用测试:一元调用、流式调用和取消调用。"""

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

from model_api_connector import ConfigurationError, ModelApiConnector, ProviderSettings


class _Upstream(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible peer with a cancellable slow stream.

    中文:支持取消慢速流的最小 OpenAI 兼容对端。"""

    server_version = "connector-test/1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        self.server.requests.append(
            {
                "path": self.path,
                "payload": payload,
                "authorization": self.headers.get("Authorization"),
            }
        )  # type: ignore[attr-defined]
        behavior = self.path.strip("/").split("/", 1)[0]
        if behavior == "error":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.endswith("/v1/embeddings"):
            if behavior == "throttle":
                self.send_response(429)
                self.send_header("Retry-After", "2")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if behavior == "slow":
                self.send_response(200)
                self.send_header("Content-Length", "1024")
                self.end_headers()
                self.wfile.flush()
                self.server.slow_started.set()  # type: ignore[attr-defined]
                while not self.server.slow_release.wait(0.05):  # type: ignore[attr-defined]
                    if self._peer_disconnected():
                        self.server.slow_disconnected.set()  # type: ignore[attr-defined]
                        return
                return
            total = len(payload["input"])
            indexes = list(range(total - 1, -1, -1))
            embedding_values = [0.0, 0.0] if behavior == "zero" else None
            body = json.dumps(
                {
                    "data": [
                        {
                            "index": index,
                            "embedding": embedding_values or [float(index + 1), -float(index + 1)],
                        }
                        for index in indexes
                    ],
                    "model": "provider-embedding-actual-v1",
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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


def _connector(
    server: ThreadingHTTPServer,
    behavior: str,
    *,
    embeddings_supported: bool = False,
    embeddings_model: str | None = None,
) -> ModelApiConnector:
    return ModelApiConnector(
        settings=ProviderSettings(
            base_url=f"http://127.0.0.1:{server.server_port}/{behavior}",
            api_key="chat-only",
            timeout_seconds=8,
            embeddings_supported=embeddings_supported,
            embeddings_model=embeddings_model,
            embeddings_base_url=f"http://127.0.0.1:{server.server_port}/{behavior}",
            embeddings_api_key="embedding-only",
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
    # 中文:chat v1 不携带角色字段;结束数据块包含结束原因和用量。
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
    # 中文:v1 没有角色字段,因此仅包含角色的前导信息不会单独成为一个帧;其余事件一一映射。
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
    """The structured method carries the role preamble and a reported total.

    中文:结构化方法会承载角色前导信息和已报告的总量。"""

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

    ok, detail = connector.on_invoke(
        "model.provider.v1",
        "embeddings",
        _embedding_request(("text",)),
    )
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


def _embedding_request(inputs: tuple[str, ...], *, model: str | None = None) -> bytes:
    from cyrene_model_provider_contracts import EmbeddingsRequest, encode_embeddings_request

    return encode_embeddings_request(EmbeddingsRequest(inputs=inputs, model=model))


def test_embeddings_use_real_upstream_values_and_restore_provider_index_order(
    upstream_server,
) -> None:
    from cyrene_model_provider_contracts import (
        EMBEDDINGS_REQUEST_TYPE_URL,
        EMBEDDINGS_RESPONSE_TYPE_URL,
        decode_embeddings_response,
    )

    connector = _connector(
        upstream_server,
        "success",
        embeddings_supported=True,
        embeddings_model="configured-embedding-model",
    )
    ok, payload = connector.on_invoke(
        "model.provider.v1",
        "embeddings",
        _embedding_request(("first", "second")),
        request_type_url=EMBEDDINGS_REQUEST_TYPE_URL,
        request_id="embedding-success",
    )

    assert ok is True
    assert payload.type_url == EMBEDDINGS_RESPONSE_TYPE_URL
    response = decode_embeddings_response(payload.value)
    assert response.error is None
    assert response.embeddings is not None
    assert response.embeddings.model == "provider-embedding-actual-v1"
    assert response.embeddings.dimensions == 2
    assert [vector.values for vector in response.embeddings.vectors] == [(1.0, -1.0), (2.0, -2.0)]
    observed = upstream_server.requests[-1]  # type: ignore[attr-defined]
    assert observed["path"] == "/success/v1/embeddings"
    assert observed["payload"] == {
        "model": "configured-embedding-model",
        "input": ["first", "second"],
    }
    assert observed["authorization"] == "Bearer embedding-only"


def test_chat_and_embeddings_use_distinct_endpoints_and_credentials(upstream_server) -> None:
    connector = ModelApiConnector(
        settings=ProviderSettings(
            base_url=f"http://127.0.0.1:{upstream_server.server_port}/chat/v1",
            api_key="chat-secret",
            embeddings_supported=True,
            embeddings_base_url=f"http://127.0.0.1:{upstream_server.server_port}/vectors/v1",
            embeddings_api_key="embedding-secret",
        )
    )
    request = ChatCompletionRequest(messages=(ChatMessage(role=ChatRole.USER, content="hello"),))

    chat_ok, _ = connector.on_invoke("model.provider.v1", CHAT_COMPLETION_METHOD, _encode(request))
    embedding_ok, _ = connector.on_invoke(
        "model.provider.v1",
        "embeddings",
        _embedding_request(("hello",), model="e5-small"),
    )

    assert chat_ok is True
    assert embedding_ok is True
    chat_request, embedding_request = upstream_server.requests  # type: ignore[attr-defined]
    assert chat_request["path"] == "/chat/v1/chat/completions"
    assert chat_request["authorization"] == "Bearer chat-secret"
    assert embedding_request["path"] == "/vectors/v1/embeddings"
    assert embedding_request["authorization"] == "Bearer embedding-secret"


def test_enabling_embeddings_requires_its_own_endpoint() -> None:
    with pytest.raises(ConfigurationError, match="embeddings_base_url"):
        ProviderSettings(base_url="http://127.0.0.1:8000", embeddings_supported=True)


def test_embeddings_preserve_provider_rate_limit_code_and_retry_delay(upstream_server) -> None:
    from cyrene_model_provider_contracts import EmbeddingsResponse, decode_embeddings_response

    connector = _connector(upstream_server, "throttle", embeddings_supported=True)
    ok, payload = connector.on_invoke(
        "model.provider.v1",
        "embeddings",
        _embedding_request(("first",), model="requested-embedding-model"),
    )

    assert ok is True
    response = decode_embeddings_response(payload.value)
    assert isinstance(response, EmbeddingsResponse)
    assert response.embeddings is None
    assert response.error is not None
    assert int(response.error.code) == 5
    assert response.error.retry_after_seconds == 2.0


def test_embeddings_request_limits_and_invalid_provider_vectors_fail_closed(
    upstream_server,
) -> None:
    from cyrene_model_provider_contracts import EmbeddingsRequest, encode_embeddings_request

    from model_api_connector import ProviderMappingError
    from model_api_connector.embeddings import embedding_batch_from_response

    connector = _connector(upstream_server, "success", embeddings_supported=True)
    oversized = encode_embeddings_request(EmbeddingsRequest(inputs=("x" * 32_769,)))
    ok, detail = connector.on_invoke("model.provider.v1", "embeddings", oversized)
    assert ok is False and detail.startswith("INVALID_REQUEST:")

    with pytest.raises(ProviderMappingError, match="finite"):
        embedding_batch_from_response(
            {"model": "actual", "data": [{"index": 0, "embedding": [float("nan")]}]},
            expected_count=1,
        )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"model": "actual", "data": [{"index": 0, "embedding": [1.0]}]}, "count"),
        (
            {
                "model": "actual",
                "data": [
                    {"index": 0, "embedding": [1.0]},
                    {"index": 0, "embedding": [2.0]},
                ],
            },
            "unique",
        ),
        (
            {
                "model": "actual",
                "data": [
                    {"index": 0, "embedding": [1.0, 2.0]},
                    {"index": 1, "embedding": [1.0]},
                ],
            },
            "dimensions",
        ),
        ({"model": "actual", "data": [{"index": 0, "embedding": [float("inf")]}]}, "finite"),
        ({"model": "actual", "data": [{"index": 0, "embedding": [0.0, 0.0]}]}, "zero vector"),
        ({"model": "", "data": [{"index": 0, "embedding": [1.0]}]}, "actual model"),
    ],
)
def test_embedding_response_rejects_incomplete_or_malformed_vectors(body, message) -> None:
    from model_api_connector import ProviderMappingError
    from model_api_connector.embeddings import embedding_batch_from_response

    with pytest.raises(ProviderMappingError, match=message):
        embedding_batch_from_response(
            body, expected_count=2 if message in {"count", "unique", "dimensions"} else 1
        )


def test_embeddings_cancellation_aborts_the_upstream_socket(upstream_server) -> None:
    connector = _connector(upstream_server, "slow", embeddings_supported=True)
    result: list[tuple[bool, Any]] = []

    worker = threading.Thread(
        target=lambda: result.append(
            connector.on_invoke(
                "model.provider.v1",
                "embeddings",
                _embedding_request(("wait",), model="embed-v1"),
                request_id="embedding-cancel",
            )
        ),
        daemon=True,
    )
    worker.start()
    assert upstream_server.slow_started.wait(5)  # type: ignore[attr-defined]
    connector.on_cancel("embedding-cancel", "caller disconnected")
    assert upstream_server.slow_disconnected.wait(5)  # type: ignore[attr-defined]
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert result and result[0][0] is False
    assert str(result[0][1]).startswith("CANCELLED:")


def test_embeddings_reject_all_zero_provider_vectors(upstream_server) -> None:
    connector = _connector(upstream_server, "zero", embeddings_supported=True)
    ok, detail = connector.on_invoke(
        "model.provider.v1",
        "embeddings",
        _embedding_request(("non-empty input",), model="embed-v1"),
    )

    assert ok is False
    assert str(detail).startswith("EXECUTION_FAILED:")


def test_chat_v3_maps_inline_image_audio_and_file_bytes_to_provider_content(
    upstream_server,
) -> None:
    from cyrene_model_provider_contracts import (
        CHAT_COMPLETION_V3_METHOD,
        CHAT_COMPLETION_V3_REQUEST_TYPE_URL,
        AudioContentPart,
        ChatCompletionRequestV3,
        ChatMessageV3,
        FileContentPart,
        ImageContentPart,
        TextContentPart,
        decode_chat_completion_response_v3,
        encode_chat_completion_request_v3,
    )

    connector = _connector(upstream_server, "success")
    request = ChatCompletionRequestV3(
        messages=(
            ChatMessageV3(
                role=ChatRole.USER,
                content=(
                    TextContentPart("inspect these attachments"),
                    ImageContentPart(mime_type="image/png", data=b"png-bytes"),
                    AudioContentPart(mime_type="audio/wav", data=b"wav-bytes"),
                    FileContentPart(
                        mime_type="application/pdf",
                        data=b"pdf-bytes",
                        filename="report.pdf",
                    ),
                ),
            ),
        ),
        model="multimodal-model",
    )

    ok, payload = connector.on_invoke(
        "model.provider.v1",
        CHAT_COMPLETION_V3_METHOD,
        encode_chat_completion_request_v3(request),
        request_type_url=CHAT_COMPLETION_V3_REQUEST_TYPE_URL,
    )

    assert ok is True
    assert decode_chat_completion_response_v3(payload.value).chunks[0].delta == "real path"
    body = upstream_server.requests[-1]["payload"]  # type: ignore[attr-defined]
    parts = body["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "inspect these attachments"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[2] == {
        "type": "input_audio",
        "input_audio": {"data": "d2F2LWJ5dGVz", "format": "wav"},
    }
    assert parts[3]["file"]["filename"] == "report.pdf"
    assert parts[3]["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_chat_v3_rejects_unresolved_artifact_ids_and_wrong_type_url(upstream_server) -> None:
    from cyrene_model_provider_contracts import (
        CHAT_COMPLETION_V3_METHOD,
        CHAT_COMPLETION_V3_REQUEST_TYPE_URL,
        ChatCompletionRequestV3,
        ChatMessageV3,
        ImageContentPart,
        encode_chat_completion_request_v3,
    )

    connector = _connector(upstream_server, "success")
    request = ChatCompletionRequestV3(
        messages=(
            ChatMessageV3(
                role=ChatRole.USER,
                content=(ImageContentPart(mime_type="image/png", artifact_id="media-123"),),
            ),
        ),
        model="multimodal-model",
    )
    encoded = encode_chat_completion_request_v3(request)

    ok, detail = connector.on_invoke(
        "model.provider.v1",
        CHAT_COMPLETION_V3_METHOD,
        encoded,
        request_type_url=CHAT_COMPLETION_V3_REQUEST_TYPE_URL,
    )
    assert ok is False and str(detail).startswith("EXECUTION_FAILED:")
    assert upstream_server.requests == []  # type: ignore[attr-defined]

    ok, detail = connector.on_invoke(
        "model.provider.v1",
        CHAT_COMPLETION_V3_METHOD,
        encoded,
        request_type_url="type.cyrene.io/other",
    )
    assert ok is False and str(detail).startswith("INVALID_REQUEST:")


def test_missing_configuration_fails_closed() -> None:
    connector = ModelApiConnector(settings=None)
    request = ChatCompletionRequest(messages=(ChatMessage(role=ChatRole.USER, content="hi"),))

    ok, detail = connector.on_invoke("model.provider.v1", CHAT_COMPLETION_METHOD, _encode(request))

    assert ok is False
    assert detail.startswith("INVALID_CONFIGURATION:")


def test_cancellation_aborts_the_upstream_request(upstream_server) -> None:
    """A cancel must close the upstream socket, not just stop waiting.

    中文:取消操作必须关闭上游套接字,而不能只停止等待。"""

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
    # 中文:已取消的流不会返回数据块,也不会报告用量。
    assert received == []


def test_cancel_for_an_unknown_request_is_a_no_op(upstream_server) -> None:
    connector = _connector(upstream_server, "success")
    connector.on_cancel("never-started", "client disconnected")
