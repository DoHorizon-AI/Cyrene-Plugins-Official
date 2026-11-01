"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 embedding.py                                                    │
│  Module: memory_sqlite_provider                                      │
│  Role: Obtain real vectors through a configured model.provider.v1.  │
│                                                                      │
│  模块职责：通过已配置的 model.provider.v1 获取真实向量。              │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from typing import Any

from cyrene_model_provider_contracts import (
    CAPABILITY_ID as MODEL_CAPABILITY_ID,
)
from cyrene_model_provider_contracts import (
    EMBEDDINGS_METHOD,
    EMBEDDINGS_REQUEST_TYPE_URL,
    EMBEDDINGS_RESPONSE_TYPE_URL,
    EmbeddingsCodecError,
    EmbeddingsRequest,
    decode_embeddings_response,
    encode_embeddings_request,
)
from cyrene_model_provider_contracts import (
    INTERFACE_VERSION as MODEL_INTERFACE_VERSION,
)
from cyrene_plugin_runtime.client import (
    DirectPayload,
    DirectPluginClient,
    DirectPluginDeadlineExceeded,
    DirectPluginError,
    DirectPluginTransportError,
)


class EmbeddingFailure(ValueError):
    """The configured model provider did not return the required real vector."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class _CancellationAdapter:
    """Expose the host cancellation signal through the SDK event interface."""

    def __init__(self, cancellation: Any | None) -> None:
        self._cancellation = cancellation

    def is_set(self) -> bool:
        if self._cancellation is None:
            return False
        checker = getattr(self._cancellation, "is_cancelled", None)
        return bool(checker()) if callable(checker) else bool(self._cancellation.is_set())


class ModelEmbeddingClient:
    """Call one host-resolved local model endpoint and verify its profile."""

    def __init__(
        self,
        endpoint: str,
        *,
        model_id: str,
        dimensions: int,
        timeout_seconds: float,
    ) -> None:
        self._endpoint = endpoint
        self._model_id = model_id
        self._dimensions = dimensions
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._client: DirectPluginClient | None = None

    def embed(
        self, inputs: Sequence[str], *, cancellation: Any | None = None
    ) -> tuple[tuple[float, ...], ...]:
        """Return one finite, non-zero, model-bound vector per input string."""

        if not inputs or any(not isinstance(value, str) or not value.strip() for value in inputs):
            raise EmbeddingFailure("embedding inputs must be non-empty strings")
        if len(inputs) > 128:
            raise EmbeddingFailure("embedding request exceeds the 128 input batch limit")
        cancel_event = _CancellationAdapter(cancellation)
        if cancel_event.is_set():
            raise EmbeddingFailure("embedding was cancelled")
        try:
            client = self._require_client()
            payload = client.invoke(
                capability=MODEL_CAPABILITY_ID,
                interface_version=MODEL_INTERFACE_VERSION,
                method=EMBEDDINGS_METHOD,
                request=DirectPayload(
                    value=encode_embeddings_request(
                        EmbeddingsRequest(inputs=tuple(inputs), model=self._model_id)
                    ),
                    type_url=EMBEDDINGS_REQUEST_TYPE_URL,
                ),
                deadline_seconds=self._timeout_seconds,
                cancel_event=cancel_event,  # type: ignore[arg-type]
            )
        except (DirectPluginError, EmbeddingsCodecError, OSError, ValueError) as error:
            raise EmbeddingFailure(
                "model.provider.v1 embeddings call failed",
                retryable=isinstance(
                    error, (DirectPluginTransportError, DirectPluginDeadlineExceeded)
                ),
            ) from error
        if payload.type_url != EMBEDDINGS_RESPONSE_TYPE_URL:
            raise EmbeddingFailure("model.provider.v1 returned an unexpected response type")
        try:
            response = decode_embeddings_response(payload.value)
        except EmbeddingsCodecError as error:
            raise EmbeddingFailure("model.provider.v1 response is malformed") from error
        if response.error is not None or response.embeddings is None:
            raise EmbeddingFailure("model.provider.v1 did not return embeddings")
        batch = response.embeddings
        if batch.model != self._model_id:
            raise EmbeddingFailure("embedding model identity does not match the configured profile")
        if batch.dimensions != self._dimensions or len(batch.vectors) != len(inputs):
            raise EmbeddingFailure("embedding batch dimensions or item count do not match")
        vectors: list[tuple[float, ...]] = []
        for vector in batch.vectors:
            values = tuple(float(value) for value in vector.values)
            if len(values) != self._dimensions or any(not math.isfinite(value) for value in values):
                raise EmbeddingFailure("model.provider.v1 returned an invalid vector")
            if not any(values):
                raise EmbeddingFailure("model.provider.v1 returned a zero vector")
            vectors.append(values)
        if cancel_event.is_set():
            raise EmbeddingFailure("embedding was cancelled")
        return tuple(vectors)

    def close(self) -> None:
        """Close the reusable model-provider channel during plugin shutdown."""

        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _require_client(self) -> DirectPluginClient:
        with self._lock:
            if self._client is None:
                self._client = DirectPluginClient.for_local_connection_ref(self._endpoint)
            return self._client
