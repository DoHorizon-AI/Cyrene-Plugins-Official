"""Direct-invocation adapter for the OpenAI-compatible model provider connector."""

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
    ChatCodecError,
    ChatCompletionRequest,
    ChatCompletionResponse,
    decode_chat_completion_request,
    decode_chat_completion_request_v2,
    encode_chat_completion_chunk,
    encode_chat_completion_chunk_v2,
    encode_chat_completion_response,
    encode_chat_completion_response_v2,
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
PLUGIN_VERSION = "0.1.0"
EMBEDDINGS_METHOD = "embeddings"
UNSUPPORTED_METHODS = ("embeddings", "chat_completion_v3")

_CANCELLED_CODE = "CANCELLED"
_INVALID_REQUEST = "INVALID_REQUEST"
_METHOD_NOT_SUPPORTED = "METHOD_NOT_SUPPORTED"
_METHOD_NOT_FOUND = "METHOD_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """One typed contract payload returned to the runtime."""

    value: bytes
    type_url: str


class ChunkStream:
    """Lazily produced contract chunks for one streamed invocation.

    The gateway forwards each chunk as it arrives, so the first tool-call
    fragment must reach it while the provider is still generating.
    """

    def __init__(self, items: Iterator[TypedPayload]) -> None:
        self.items = items


class ModelApiConnector:
    """Adapt the typed chat contract onto one configured upstream endpoint."""

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
        """Dispatch one direct invocation without leaking vendor detail."""

        try:
            if capability != CAPABILITY_ID:
                return False, f"{_INVALID_REQUEST}: unsupported capability {capability!r}"
            if action == EMBEDDINGS_METHOD:
                return (
                    False,
                    f"{_METHOD_NOT_SUPPORTED}: this connector implements chat completions only",
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

    def on_cancel(self, request_id: str, reason: str) -> None:
        """Abort the upstream request the runtime is no longer waiting for."""

        with self._lock:
            call = self._calls.get(request_id)
        if call is not None:
            call.close()

    # ── internals ─────────────────────────────────────────────────────────

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
        """Yield one contract chunk per upstream event, without materialising."""
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
            return
        finally:
            self._forget(request_id)
