"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 embeddings.py                                                     │
│  Module: model_api_connector.embeddings                              │
│  Role: Validate bounded embedding inputs and provider vector batches. │
│                                                                      │
│  模块职责：校验有界的 embedding 输入与 Provider 向量批次               │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from cyrene_model_provider_contracts import (
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingsRequest,
    EmbeddingVector,
)

from .mapping import ProviderMappingError

MAX_EMBEDDING_INPUTS = 128
MAX_EMBEDDING_INPUT_CHARS = 32_768
MAX_EMBEDDING_BATCH_BYTES = 1_048_576
MAX_EMBEDDING_DIMENSIONS = 16_384
MAX_FLOAT32 = 3.4028234663852886e38


def validate_embeddings_request(request: EmbeddingsRequest) -> None:
    """Reject unbounded or malformed embedding inputs before sending HTTP.

    中文:发送 HTTP 前拒绝无界或格式错误的 embedding 输入。
    """

    if not request.inputs or len(request.inputs) > MAX_EMBEDDING_INPUTS:
        raise ProviderMappingError(
            f"inputs must contain between 1 and {MAX_EMBEDDING_INPUTS} items"
        )
    total_bytes = 0
    for index, value in enumerate(request.inputs):
        if not isinstance(value, str) or not value.strip():
            raise ProviderMappingError(f"inputs[{index}] must be non-empty text")
        if len(value) > MAX_EMBEDDING_INPUT_CHARS:
            raise ProviderMappingError(f"inputs[{index}] exceeds the maximum text length")
        total_bytes += len(value.encode("utf-8"))
    if total_bytes > MAX_EMBEDDING_BATCH_BYTES:
        raise ProviderMappingError("embedding input batch exceeds the maximum encoded size")
    if request.model is not None and (
        not isinstance(request.model, str) or not request.model.strip()
    ):
        raise ProviderMappingError("model must be non-empty text when provided")
    if request.model is not None and len(request.model) > 256:
        raise ProviderMappingError("model exceeds the maximum length")


def to_upstream_embeddings_body(request: EmbeddingsRequest, *, model: str) -> dict[str, Any]:
    """Build one bounded OpenAI-compatible embeddings request body.

    中文:构造一份有界的 OpenAI 兼容 embeddings 请求体。
    """

    validate_embeddings_request(request)
    return {"model": model, "input": list(request.inputs)}


def embedding_batch_from_response(
    body: Mapping[str, Any], *, expected_count: int
) -> EmbeddingBatch:
    """Validate response order, count, dimensions and actual provider model.

    中文:校验响应顺序、数量、维数和提供方实际返回的模型。
    """

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ProviderMappingError("upstream embedding response must include its actual model")
    if len(model) > 256:
        raise ProviderMappingError("upstream embedding model exceeds the maximum length")
    data = body.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise ProviderMappingError(
            "upstream embedding count does not match the requested input count"
        )

    ordered: list[Mapping[str, Any] | None] = [None] * expected_count
    for position, item in enumerate(data):
        if not isinstance(item, Mapping):
            raise ProviderMappingError(f"upstream embedding data[{position}] must be an object")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < expected_count:
            raise ProviderMappingError(f"upstream embedding data[{position}].index is invalid")
        if ordered[index] is not None:
            raise ProviderMappingError("upstream embedding indexes must be unique")
        ordered[index] = item

    vectors: list[EmbeddingVector] = []
    dimensions: int | None = None
    for index, item in enumerate(ordered):
        if item is None:
            raise ProviderMappingError(f"upstream embedding response is missing index {index}")
        values = item.get("embedding")
        if (
            not isinstance(values, Sequence)
            or isinstance(values, (str, bytes, bytearray))
            or not values
        ):
            raise ProviderMappingError(
                f"upstream embedding data[{index}].embedding must be a non-empty array"
            )
        if dimensions is None:
            dimensions = len(values)
            if dimensions > MAX_EMBEDDING_DIMENSIONS:
                raise ProviderMappingError(
                    "upstream embedding dimensions exceed the supported maximum"
                )
        elif len(values) != dimensions:
            raise ProviderMappingError("upstream embedding dimensions differ within the batch")
        normalized: list[float] = []
        for value_index, value in enumerate(values):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ProviderMappingError(
                    f"upstream embedding data[{index}].embedding[{value_index}] must be numeric"
                )
            numeric = float(value)
            if not math.isfinite(numeric) or abs(numeric) > MAX_FLOAT32:
                raise ProviderMappingError(
                    f"upstream embedding data[{index}].embedding[{value_index}] "
                    "is outside finite float32 range"
                )
            normalized.append(numeric)
        if not any(normalized):
            raise ProviderMappingError(f"upstream embedding data[{index}] is an all-zero vector")
        vectors.append(EmbeddingVector(values=tuple(normalized)))

    assert dimensions is not None
    return EmbeddingBatch(vectors=tuple(vectors), dimensions=dimensions, model=model)


def embedding_error_from_upstream(error: Exception) -> EmbeddingError:
    """Map a provider HTTP rejection to the typed embedding error envelope.

    中文:将提供方 HTTP 拒绝转换为有类型的 embedding 错误。
    """

    status = getattr(error, "status", None)
    retry_after = getattr(error, "retry_after_seconds", None)
    if status == 429:
        return EmbeddingError(
            code=5,
            message="embedding provider rate limited the request",
            retry_after_seconds=retry_after,
        )
    if status == 404:
        return EmbeddingError(
            code=2, message="embedding model is not available", retry_after_seconds=None
        )
    if status is not None and 400 <= status < 500:
        return EmbeddingError(
            code=1, message="embedding provider rejected the request", retry_after_seconds=None
        )
    return EmbeddingError(
        code=4, message="embedding provider request failed", retry_after_seconds=retry_after
    )
