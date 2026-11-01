"""
┌─────────────────────────────────────────────────────────────────────────┐
│ 📄 embeddings.py                                                        │
│ Module: cyrene_model_provider_contracts.embeddings                       │
│ Role: Typed values and protobuf codec for model.provider.v1 embeddings.  │
│                                                                         │
│ 模块职责：提供 model.provider.v1 embeddings 类型化值和 protobuf 编解码。 │
│ · 保持零依赖；向量生成与模型路由由外部 Provider 管理。                   │
└─────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import IntEnum

from . import _wire

CAPABILITY_ID = "model.provider.v1"
INTERFACE_VERSION = "1"
EMBEDDINGS_METHOD = "embeddings"
EMBEDDINGS_REQUEST_TYPE_URL = (
    "type.cyrene.io/cyrene.model.provider.v1.EmbeddingsRequest"
)
EMBEDDINGS_RESPONSE_TYPE_URL = (
    "type.cyrene.io/cyrene.model.provider.v1.EmbeddingsResponse"
)


class EmbeddingsCodecError(ValueError):
    """An embedding payload is malformed or violates the wire contract."""


class EmbeddingErrorCode(IntEnum):
    """Stable wire values from ``EmbeddingError.Code`` in the canonical proto."""

    UNSPECIFIED = 0
    INVALID_INPUT = 1
    MODEL_NOT_AVAILABLE = 2
    DIMENSION_MISMATCH = 3
    PROVIDER_ERROR = 4
    RATE_LIMITED = 5


@dataclass(frozen=True)
class EmbeddingsRequest:
    """Ordered input texts and an optional provider-supported model selector."""

    inputs: tuple[str, ...]
    model: str | None = None


@dataclass(frozen=True)
class EmbeddingVector:
    """One ordered dense embedding vector."""

    values: tuple[float, ...]


@dataclass(frozen=True)
class EmbeddingBatch:
    """Vectors returned for a complete request batch."""

    vectors: tuple[EmbeddingVector, ...]
    dimensions: int
    model: str


@dataclass(frozen=True)
class EmbeddingError:
    """A typed provider outcome; transport failures remain outside the payload."""

    code: EmbeddingErrorCode | int
    message: str
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class EmbeddingsResponse:
    """Exactly one provider batch or typed domain error when well-formed."""

    embeddings: EmbeddingBatch | None = None
    error: EmbeddingError | None = None


def _encode_duration(seconds: float) -> bytes:
    if not math.isfinite(seconds) or seconds < 0:
        raise EmbeddingsCodecError(
            "retry_after_seconds must be finite and non-negative"
        )
    whole_seconds = math.floor(seconds)
    nanos = round((seconds - whole_seconds) * 1_000_000_000)
    if nanos == 1_000_000_000:
        whole_seconds += 1
        nanos = 0
    body = b""
    if whole_seconds:
        body += _wire.encode_tag(1, _wire.WIRE_VARINT) + _wire.encode_varint(
            whole_seconds
        )
    if nanos:
        body += _wire.encode_tag(2, _wire.WIRE_VARINT) + _wire.encode_varint(nanos)
    return body


def _decode_duration(payload: bytes) -> float:
    seconds = 0
    nanos = 0
    for number, wire_type, value in _wire.iter_fields(payload):
        if (
            number in (1, 2)
            and wire_type == _wire.WIRE_VARINT
            and isinstance(value, int)
        ):
            if number == 1:
                seconds = value - (1 << 64) if value & (1 << 63) else value
            else:
                nanos = value - (1 << 32) if value & (1 << 31) else value
    return seconds + nanos / 1_000_000_000


def encode_embeddings_request(request: EmbeddingsRequest) -> bytes:
    """Encode a canonical ``EmbeddingsRequest`` protobuf payload."""

    payload = b"".join(
        _wire.encode_len_delimited(1, item.encode("utf-8")) for item in request.inputs
    )
    if request.model is not None:
        payload += _wire.encode_len_delimited(2, request.model.encode("utf-8"))
    return payload


def decode_embeddings_request(payload: bytes) -> EmbeddingsRequest:
    """Decode a canonical ``EmbeddingsRequest`` payload."""

    inputs: list[str] = []
    model: str | None = None
    for number, wire_type, value in _wire.iter_fields(payload):
        if (
            number in (1, 2)
            and wire_type == _wire.WIRE_LEN
            and isinstance(value, bytes)
        ):
            try:
                decoded = value.decode("utf-8")
            except UnicodeDecodeError as error:
                raise EmbeddingsCodecError(
                    "embedding text is not valid UTF-8"
                ) from error
            if number == 1:
                inputs.append(decoded)
            else:
                model = decoded
    return EmbeddingsRequest(inputs=tuple(inputs), model=model)


def _encode_vector(vector: EmbeddingVector) -> bytes:
    return _wire.encode_packed_floats(1, vector.values)


def _decode_vector(payload: bytes) -> EmbeddingVector:
    values: list[float] = []
    for number, wire_type, value in _wire.iter_fields(payload):
        if number != 1:
            continue
        if wire_type == _wire.WIRE_LEN and isinstance(value, bytes):
            values.extend(_wire.decode_packed_floats(value))
        elif (
            wire_type == _wire.WIRE_FIXED32
            and isinstance(value, bytes)
            and len(value) == 4
        ):
            values.append(struct.unpack("<f", value)[0])
        else:
            raise EmbeddingsCodecError("embedding vector values must be float32")
    return EmbeddingVector(values=tuple(values))


def _encode_batch(batch: EmbeddingBatch) -> bytes:
    payload = b"".join(
        _wire.encode_len_delimited(1, _encode_vector(vector))
        for vector in batch.vectors
    )
    if batch.dimensions:
        payload += _wire.encode_tag(2, _wire.WIRE_VARINT) + _wire.encode_varint(
            batch.dimensions
        )
    payload += _wire.encode_string_field(3, batch.model)
    return payload


def _decode_batch(payload: bytes) -> EmbeddingBatch:
    vectors: list[EmbeddingVector] = []
    dimensions = 0
    model = ""
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_LEN and isinstance(value, bytes):
            vectors.append(_decode_vector(value))
        elif number == 2 and wire_type == _wire.WIRE_VARINT and isinstance(value, int):
            dimensions = value
        elif number == 3 and wire_type == _wire.WIRE_LEN and isinstance(value, bytes):
            try:
                model = value.decode("utf-8")
            except UnicodeDecodeError as error:
                raise EmbeddingsCodecError("model is not valid UTF-8") from error
    return EmbeddingBatch(vectors=tuple(vectors), dimensions=dimensions, model=model)


def _encode_error(error: EmbeddingError) -> bytes:
    code = int(error.code)
    payload = (
        _wire.encode_tag(1, _wire.WIRE_VARINT) + _wire.encode_varint(code)
        if code
        else b""
    )
    payload += _wire.encode_string_field(2, error.message)
    if error.retry_after_seconds is not None:
        payload += _wire.encode_len_delimited(
            3, _encode_duration(error.retry_after_seconds)
        )
    return payload


def _decode_error(payload: bytes) -> EmbeddingError:
    code: int = 0
    message = ""
    retry_after: float | None = None
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_VARINT and isinstance(value, int):
            code = value
        elif number == 2 and wire_type == _wire.WIRE_LEN and isinstance(value, bytes):
            try:
                message = value.decode("utf-8")
            except UnicodeDecodeError as error:
                raise EmbeddingsCodecError(
                    "error message is not valid UTF-8"
                ) from error
        elif number == 3 and wire_type == _wire.WIRE_LEN and isinstance(value, bytes):
            retry_after = _decode_duration(value)
    return EmbeddingError(code=code, message=message, retry_after_seconds=retry_after)


def encode_embeddings_response(response: EmbeddingsResponse) -> bytes:
    """Encode the selected batch/error result of an ``EmbeddingsResponse``."""

    if response.embeddings is not None and response.error is not None:
        raise EmbeddingsCodecError("response cannot contain both embeddings and error")
    if response.embeddings is not None:
        return _wire.encode_len_delimited(1, _encode_batch(response.embeddings))
    if response.error is not None:
        return _wire.encode_len_delimited(2, _encode_error(response.error))
    return b""


def decode_embeddings_response(payload: bytes) -> EmbeddingsResponse:
    """Decode the selected batch/error result of an ``EmbeddingsResponse``."""

    embeddings: EmbeddingBatch | None = None
    error: EmbeddingError | None = None
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_LEN and isinstance(value, bytes):
            embeddings, error = _decode_batch(value), None
        elif number == 2 and wire_type == _wire.WIRE_LEN and isinstance(value, bytes):
            error, embeddings = _decode_error(value), None
    return EmbeddingsResponse(embeddings=embeddings, error=error)


__all__ = [
    "CAPABILITY_ID",
    "EMBEDDINGS_METHOD",
    "EMBEDDINGS_REQUEST_TYPE_URL",
    "EMBEDDINGS_RESPONSE_TYPE_URL",
    "INTERFACE_VERSION",
    "EmbeddingBatch",
    "EmbeddingError",
    "EmbeddingErrorCode",
    "EmbeddingVector",
    "EmbeddingsCodecError",
    "EmbeddingsRequest",
    "EmbeddingsResponse",
    "decode_embeddings_request",
    "decode_embeddings_response",
    "encode_embeddings_request",
    "encode_embeddings_response",
]
