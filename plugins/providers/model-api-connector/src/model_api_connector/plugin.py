"""Direct-invocation adapter for the OpenAI-compatible model provider connector.

中文:面向 OpenAI 兼容模型提供方连接器的直接调用适配器。"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from cyrene_model_provider_contracts import (
    CAPABILITY_ID,
    CHAT_COMPLETION_CHUNK_TYPE_URL,
    CHAT_COMPLETION_METHOD,
    CHAT_COMPLETION_REQUEST_TYPE_URL,
    CHAT_COMPLETION_RESPONSE_TYPE_URL,
    CHAT_COMPLETION_V2_METHOD,
    EMBEDDINGS_METHOD,
    EMBEDDINGS_REQUEST_TYPE_URL,
    EMBEDDINGS_RESPONSE_TYPE_URL,
    ChatCodecError,
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingsCodecError,
    EmbeddingsResponse,
    decode_chat_completion_request,
    decode_chat_completion_request_v2,
    decode_embeddings_request,
    encode_chat_completion_chunk,
    encode_chat_completion_chunk_v2,
    encode_chat_completion_response,
    encode_chat_completion_response_v2,
    encode_embeddings_response,
)

from .embeddings import (
    embedding_batch_from_response,
    embedding_error_from_upstream,
    to_upstream_embeddings_body,
    validate_embeddings_request,
)
from .mapping import ProviderMappingError, chunk_from_event, response_chunks, to_upstream_body
from .settings import ConfigurationError, ProviderSettings
from .upstream import (
    OpenAICompatibleUpstream,
    ProviderCancelled,
    UpstreamCall,
    UpstreamFailure,
)

PLUGIN_ID = "cyrene.providers.model-api-connector"
PLUGIN_VERSION = "0.2.0"
UNSUPPORTED_METHODS = ("chat_completion_v3",)

_CANCELLED_CODE = "CANCELLED"
_INVALID_REQUEST = "INVALID_REQUEST"
_METHOD_NOT_SUPPORTED = "METHOD_NOT_SUPPORTED"
_METHOD_NOT_FOUND = "METHOD_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """One typed contract payload returned to the runtime.

    中文:返回给运行时的一个有类型契约载荷。"""

    value: bytes
    type_url: str


class ChunkStream:
    """Lazily produced contract chunks for one streamed invocation.

    The gateway forwards each chunk as it arrives, so the first tool-call
    fragment must reach it while the provider is still generating.

        中文:为一次流式调用惰性生成契约数据块。

        中文:网关会在每个数据块到达时立即转发,因此第一个工具调用片段必须在提供方仍在生成内容时送达网关。
    """

    def __init__(self, items: Iterator[TypedPayload]) -> None:
        self.items = items


class ModelApiConnector:
    """Adapt the typed chat contract onto one configured upstream endpoint.

    中文:将有类型的聊天契约适配到一个已配置的上游端点。"""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: ProviderSettings | None = None,
        upstream: OpenAICompatibleUpstream | None = None,
    ) -> None:
        self._settings = settings
        self._upstream = upstream
        self._calls: dict[str, UpstreamCall] = {}
        self._lock = threading.Lock()

    # ── runtime surface ───────────────────────────────────────────────────
    # 中文:── 运行时接口 ───────────────────────────────────────────────────

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: Any | None = None,
        request_id: str | None = None,
        request_type_url: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, Any]:
        """Dispatch one direct invocation without leaking vendor detail.

        中文:分发一次直接调用,不泄露厂商细节。"""

        try:
            if capability != CAPABILITY_ID:
                return False, f"{_INVALID_REQUEST}: unsupported capability {capability!r}"
            if action == EMBEDDINGS_METHOD:
                if request_type_url not in (None, EMBEDDINGS_REQUEST_TYPE_URL):
                    return (
                        False,
                        f"{_INVALID_REQUEST}: request_type_url must be "
                        f"{EMBEDDINGS_REQUEST_TYPE_URL}",
                    )
                if stream_results:
                    return (
                        False,
                        f"{_METHOD_NOT_SUPPORTED}: embeddings does not support streaming results",
                    )
                return self._invoke_embeddings(
                    payload, request_id=request_id, cancellation=cancellation
                )
            if action not in (CHAT_COMPLETION_METHOD, CHAT_COMPLETION_V2_METHOD):
                return (
                    False,
                    f"{_METHOD_NOT_FOUND}: unsupported method {action!r} for {CAPABILITY_ID}",
                )
            if request_type_url not in (None, CHAT_COMPLETION_REQUEST_TYPE_URL):
                return (
                    False,
                    f"{_INVALID_REQUEST}: request_type_url must be"
                    f" {CHAT_COMPLETION_REQUEST_TYPE_URL}",
                )
            structured = action == CHAT_COMPLETION_V2_METHOD
            request = self._decode(payload, structured=structured)
            settings = self._require_settings()
            upstream = self._upstream or OpenAICompatibleUpstream(settings)
            body = to_upstream_body(request)
            call = self._register(request_id)
            if stream_results:
                return True, ChunkStream(
                    self._stream(
                        upstream,
                        body,
                        call,
                        structured=structured,
                        request_id=request_id,
                        cancellation=cancellation,
                    )
                )
            chunks = response_chunks(upstream.complete(body, call), structured=structured)
            self._forget(request_id)
            encoder = (
                encode_chat_completion_response_v2
                if structured
                else encode_chat_completion_response
            )
            return True, TypedPayload(
                encoder(ChatCompletionResponse(chunks=chunks)),
                CHAT_COMPLETION_RESPONSE_TYPE_URL,
            )
        except ProviderCancelled:
            self._forget(request_id)
            return False, f"{_CANCELLED_CODE}: upstream request was cancelled"
        except ConfigurationError as error:
            return False, f"INVALID_CONFIGURATION: {error}"
        except (UpstreamFailure, ProviderMappingError) as error:
            self._forget(request_id)
            return False, f"EXECUTION_FAILED: {error}"
        except ChatCodecError as error:
            return False, f"{_INVALID_REQUEST}: payload is not a chat-completion request ({error})"

    def _invoke_embeddings(
        self,
        payload: bytes,
        *,
        request_id: str | None,
        cancellation: Any | None,
    ) -> tuple[bool, Any]:
        """Invoke one non-streaming embedding operation on the bound provider.

        中文:在当前绑定的提供方上执行一次非流式 embedding 操作。
        """

        try:
            request = decode_embeddings_request(payload)
            try:
                validate_embeddings_request(request)
            except ProviderMappingError as error:
                return False, f"{_INVALID_REQUEST}: {error}"
            settings = self._require_settings()
            if not settings.embeddings_supported:
                return False, f"{_METHOD_NOT_SUPPORTED}: embeddings are disabled for this binding"
            if cancellation is not None and cancellation.is_cancelled():
                raise ProviderCancelled("upstream request was cancelled")
            model = request.model or settings.embeddings_model
            if model is None:
                raise ConfigurationError(
                    "CYRENE_EMBEDDINGS_MODEL is required when request.model is omitted"
                )
            upstream = self._upstream or OpenAICompatibleUpstream(settings)
            body = to_upstream_embeddings_body(request, model=model)
            call = self._register(request_id)
            try:
                try:
                    answer = upstream.complete_embeddings(body, call)
                except UpstreamFailure as error:
                    if error.status is not None:
                        response = EmbeddingsResponse(error=embedding_error_from_upstream(error))
                        return True, TypedPayload(
                            encode_embeddings_response(response), EMBEDDINGS_RESPONSE_TYPE_URL
                        )
                    raise
                batch = embedding_batch_from_response(answer, expected_count=len(request.inputs))
                response = EmbeddingsResponse(embeddings=batch)
                return True, TypedPayload(
                    encode_embeddings_response(response), EMBEDDINGS_RESPONSE_TYPE_URL
                )
            finally:
                self._forget(request_id)
        except ProviderCancelled:
            self._forget(request_id)
            return False, f"{_CANCELLED_CODE}: upstream request was cancelled"
        except ConfigurationError as error:
            return False, f"INVALID_CONFIGURATION: {error}"
        except EmbeddingsCodecError as error:
            return False, f"{_INVALID_REQUEST}: payload is not an embeddings request ({error})"
        except (UpstreamFailure, ProviderMappingError) as error:
            self._forget(request_id)
            return False, f"EXECUTION_FAILED: {error}"

    def on_cancel(self, request_id: str, reason: str) -> None:
        """Abort the upstream request the runtime is no longer waiting for.

        中文:中止运行时已不再等待的上游请求。"""

        with self._lock:
            call = self._calls.get(request_id)
        if call is not None:
            call.close()

    # ── internals ─────────────────────────────────────────────────────────
    # 中文:── 内部实现 ─────────────────────────────────────────────────────

    def _require_settings(self) -> ProviderSettings:
        if self._settings is None:
            self._settings = ProviderSettings.from_environment()
        return self._settings

    def _decode(self, payload: bytes, *, structured: bool) -> ChatCompletionRequest:
        decoder = (
            decode_chat_completion_request_v2 if structured else decode_chat_completion_request
        )
        return decoder(payload)

    def _register(self, request_id: str | None) -> UpstreamCall:
        call = UpstreamCall()
        if request_id:
            with self._lock:
                self._calls[request_id] = call
        return call

    def _forget(self, request_id: str | None) -> None:
        if request_id:
            with self._lock:
                self._calls.pop(request_id, None)

    def _stream(
        self,
        upstream: OpenAICompatibleUpstream,
        body: Mapping[str, Any],
        call: UpstreamCall,
        *,
        structured: bool,
        request_id: str | None,
        cancellation: Any | None = None,
    ) -> Iterator[TypedPayload]:
        """Yield one contract chunk per upstream event, without materialising.

        中文:每收到一个上游事件就产出一个契约数据块,不先将其全部载入内存。"""
        encoder = encode_chat_completion_chunk_v2 if structured else encode_chat_completion_chunk
        try:
            for event in upstream.events(body, call, cancellation=cancellation):
                chunk = chunk_from_event(event, structured=structured)
                if chunk is None:
                    continue
                yield TypedPayload(encoder(chunk), CHAT_COMPLETION_CHUNK_TYPE_URL)
        except ProviderCancelled:
            # A cancelled invocation has no result to report: stop cleanly and
            # let the runtime observe the cancellation it already knows about.
            # 中文:已取消的调用没有结果可报告:应正常停止,并让运行时观察到它已知的取消状态。
            return
        finally:
            self._forget(request_id)
