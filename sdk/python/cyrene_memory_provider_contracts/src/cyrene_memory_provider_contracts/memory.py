"""
┌─────────────────────────────────────────────────────────────────────────┐
│ 📄 memory.py                                                            │
│ Module: cyrene_memory_provider_contracts.memory                         │
│ Role: Typed values and protobuf codec for memory.provider.v1.            │
│                                                                         │
│ 模块职责：提供 memory.provider.v1 类型化值及 protobuf 编解码。           │
│ · 不实现网络调用、身份授权或产品记忆策略。                               │
└─────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum

from . import _wire

CAPABILITY_ID = "memory.provider.v1"
INTERFACE_VERSION = "1"

METHOD_STORE = "store"
METHOD_GET = "get"
METHOD_RECALL = "recall"
METHOD_DELETE = "delete"
METHOD_PRUNE = "prune"
METHOD_EXPORT_BATCH = "export_batch"
METHOD_IMPORT_BATCH = "import_batch"
METHODS = (
    METHOD_STORE,
    METHOD_GET,
    METHOD_RECALL,
    METHOD_DELETE,
    METHOD_PRUNE,
    METHOD_EXPORT_BATCH,
    METHOD_IMPORT_BATCH,
)

REQUEST_TYPE_URLS = {
    METHOD_STORE: "type.cyrene.io/cyrene.memory.provider.v1.StoreMemoryRequest",
    METHOD_GET: "type.cyrene.io/cyrene.memory.provider.v1.GetMemoryRequest",
    METHOD_RECALL: "type.cyrene.io/cyrene.memory.provider.v1.RecallMemoryRequest",
    METHOD_DELETE: "type.cyrene.io/cyrene.memory.provider.v1.DeleteMemoryRequest",
    METHOD_PRUNE: "type.cyrene.io/cyrene.memory.provider.v1.PruneMemoryRequest",
    METHOD_EXPORT_BATCH: "type.cyrene.io/cyrene.memory.provider.v1.ExportMemoryRequest",
    METHOD_IMPORT_BATCH: "type.cyrene.io/cyrene.memory.provider.v1.ImportMemoryRequest",
}
RESPONSE_TYPE_URLS = {
    METHOD_STORE: "type.cyrene.io/cyrene.memory.provider.v1.StoreMemoryResponse",
    METHOD_GET: "type.cyrene.io/cyrene.memory.provider.v1.GetMemoryResponse",
    METHOD_RECALL: "type.cyrene.io/cyrene.memory.provider.v1.RecallMemoryResponse",
    METHOD_DELETE: "type.cyrene.io/cyrene.memory.provider.v1.DeleteMemoryResponse",
    METHOD_PRUNE: "type.cyrene.io/cyrene.memory.provider.v1.PruneMemoryResponse",
    METHOD_EXPORT_BATCH: "type.cyrene.io/cyrene.memory.provider.v1.ExportMemoryResponse",
    METHOD_IMPORT_BATCH: "type.cyrene.io/cyrene.memory.provider.v1.ImportMemoryResponse",
}


class MemoryCodecError(ValueError):
    """A payload is malformed for the expected memory-provider message."""


class MemoryErrorCode(IntEnum):
    """Stable error values from ``MemoryErrorCode`` in the canonical proto."""

    UNSPECIFIED = 0
    NOT_FOUND = 1
    TENANT_MISMATCH = 2
    VECTOR_DIMENSION_MISMATCH = 3
    QUOTA_EXCEEDED = 4
    STORAGE_FAILED = 5


@dataclass(frozen=True)
class MemoryItem:
    """One stored memory record, including its isolation and expiry facts."""

    item_id: str = ""
    tenant_id: str = ""
    scope: str = ""
    subject: str = ""
    content: str = ""
    embedding: tuple[float, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    created_at_ms: int = 0
    expires_at_ms: int | None = None
    ttl_seconds: int | None = None


@dataclass(frozen=True)
class MemoryMatch:
    """A recalled item with its similarity score."""

    item: MemoryItem
    score: float


@dataclass(frozen=True)
class MemoryError:
    """A typed memory-provider domain error."""

    code: MemoryErrorCode | int = MemoryErrorCode.UNSPECIFIED
    message: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class StoreMemoryRequest:
    tenant_id: str
    item: MemoryItem


@dataclass(frozen=True)
class StoreMemoryResponse:
    item_id: str | None = None
    error: MemoryError | None = None


@dataclass(frozen=True)
class GetMemoryRequest:
    tenant_id: str
    item_id: str


@dataclass(frozen=True)
class GetMemoryResponse:
    item: MemoryItem | None = None
    error: MemoryError | None = None


@dataclass(frozen=True)
class RecallMemoryRequest:
    tenant_id: str
    scope: str | None = None
    subject: str | None = None
    query_embedding: tuple[float, ...] = ()
    query_text: str | None = None
    metadata_filters: dict[str, str] = field(default_factory=dict)
    min_similarity: float | None = None
    top_k: int | None = None


@dataclass(frozen=True)
class RecallMemoryResponse:
    matches: tuple[MemoryMatch, ...] | None = None
    error: MemoryError | None = None


@dataclass(frozen=True)
class DeleteMemoryRequest:
    tenant_id: str
    item_id: str


@dataclass(frozen=True)
class DeleteMemoryResponse:
    deleted: bool | None = None
    error: MemoryError | None = None


@dataclass(frozen=True)
class PruneMemoryRequest:
    tenant_id: str
    before_timestamp_ms: int | None = None


@dataclass(frozen=True)
class PruneMemoryResponse:
    pruned_count: int | None = None
    error: MemoryError | None = None


@dataclass(frozen=True)
class ExportMemoryRequest:
    tenant_id: str
    scope: str | None = None
    limit: int | None = None
    page_token: str | None = None


@dataclass(frozen=True)
class ExportMemoryResponse:
    items: tuple[MemoryItem, ...] | None = None
    error: MemoryError | None = None
    next_page_token: str = ""


@dataclass(frozen=True)
class ImportMemoryRequest:
    tenant_id: str
    items: tuple[MemoryItem, ...] = ()
    overwrite: bool = False


@dataclass(frozen=True)
class ImportMemoryResponse:
    imported_count: int | None = None
    error: MemoryError | None = None


def _encode_string(number: int, value: str) -> bytes:
    return _wire.encode_string_field(number, value)


def _encode_optional_string(number: int, value: str | None) -> bytes:
    if value is None:
        return b""
    return _wire.encode_len_delimited(number, value.encode("utf-8"))


def _encode_optional_int(number: int, value: int | None) -> bytes:
    if value is None:
        return b""
    return _wire.encode_tag(number, _wire.WIRE_VARINT) + _wire.encode_varint(
        value & ((1 << 64) - 1)
    )


def _encode_optional_float(number: int, value: float | None) -> bytes:
    if value is None:
        return b""
    return _wire.encode_tag(number, _wire.WIRE_FIXED32) + struct.pack("<f", value)


def _encode_bool(number: int, value: bool) -> bytes:
    return _wire.encode_tag(number, _wire.WIRE_VARINT) + _wire.encode_varint(int(value))


def _encode_map(number: int, value: dict[str, str]) -> bytes:
    return b"".join(
        _wire.encode_len_delimited(
            number,
            _encode_optional_string(1, key) + _encode_optional_string(2, item),
        )
        for key, item in sorted(value.items())
    )


def _decode_string(value: bytes | int) -> str:
    if not isinstance(value, bytes):
        raise MemoryCodecError("expected length-delimited string")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MemoryCodecError("invalid UTF-8 in memory payload") from error


def _decode_float(value: bytes | int, wire_type: int) -> float:
    if (
        wire_type != _wire.WIRE_FIXED32
        or not isinstance(value, bytes)
        or len(value) != 4
    ):
        raise MemoryCodecError("expected fixed32 float")
    return struct.unpack("<f", value)[0]


def _decode_float_repeated(value: bytes | int, wire_type: int) -> tuple[float, ...]:
    if wire_type == _wire.WIRE_LEN and isinstance(value, bytes):
        return _wire.decode_packed_floats(value)
    return (_decode_float(value, wire_type),)


def _decode_int64(value: bytes | int, wire_type: int) -> int:
    if wire_type != _wire.WIRE_VARINT or not isinstance(value, int):
        raise MemoryCodecError("expected varint int64")
    return value - (1 << 64) if value & (1 << 63) else value


def _decode_map(value: bytes | int) -> tuple[str, str]:
    if not isinstance(value, bytes):
        raise MemoryCodecError("expected map entry")
    key = ""
    item = ""
    for field_number, wire_type, field_value in _wire.iter_fields(value):
        if field_number in (1, 2) and wire_type == _wire.WIRE_LEN:
            if field_number == 1:
                key = _decode_string(field_value)
            else:
                item = _decode_string(field_value)
    return key, item


def encode_memory_item(item: MemoryItem) -> bytes:
    """Encode one ``MemoryItem`` according to the canonical protobuf schema."""

    parts = [
        _encode_string(1, item.item_id),
        _encode_string(2, item.tenant_id),
        _encode_string(3, item.scope),
        _encode_string(4, item.subject),
        _encode_string(5, item.content),
        _wire.encode_packed_floats(6, item.embedding),
        _encode_map(7, item.metadata),
        _wire.encode_tag(8, _wire.WIRE_VARINT)
        + _wire.encode_varint(item.created_at_ms & ((1 << 64) - 1))
        if item.created_at_ms
        else b"",
        _encode_optional_int(9, item.expires_at_ms),
        _encode_optional_int(10, item.ttl_seconds),
    ]
    return b"".join(parts)


def decode_memory_item(payload: bytes) -> MemoryItem:
    """Decode a ``MemoryItem`` and preserve optional-field presence."""

    values: dict[str, object] = {"embedding": (), "metadata": {}}
    embeddings: list[float] = []
    metadata: dict[str, str] = {}
    for number, wire_type, value in _wire.iter_fields(payload):
        if number in (1, 2, 3, 4, 5) and wire_type == _wire.WIRE_LEN:
            values[
                {1: "item_id", 2: "tenant_id", 3: "scope", 4: "subject", 5: "content"}[
                    number
                ]
            ] = _decode_string(value)
        elif number == 6:
            embeddings.extend(_decode_float_repeated(value, wire_type))
        elif number == 7 and wire_type == _wire.WIRE_LEN:
            key, item = _decode_map(value)
            metadata[key] = item
        elif number == 8:
            values["created_at_ms"] = _decode_int64(value, wire_type)
        elif number in (9, 10):
            decoded = _decode_int64(value, wire_type)
            values["expires_at_ms" if number == 9 else "ttl_seconds"] = decoded
    values["embedding"] = tuple(embeddings)
    values["metadata"] = metadata
    return MemoryItem(**values)  # type: ignore[arg-type]


def _encode_match(match: MemoryMatch) -> bytes:
    score = (
        _wire.encode_tag(2, _wire.WIRE_FIXED32) + struct.pack("<f", match.score)
        if match.score
        else b""
    )
    return _wire.encode_len_delimited(1, encode_memory_item(match.item)) + score


def _decode_match(payload: bytes) -> MemoryMatch:
    item: MemoryItem | None = None
    score = 0.0
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_LEN:
            item = decode_memory_item(value)
        elif number == 2:
            score = _decode_float(value, wire_type)
    if item is None:
        raise MemoryCodecError("MemoryMatch is missing item")
    return MemoryMatch(item=item, score=score)


def _encode_error(error: MemoryError) -> bytes:
    code = int(error.code)
    return (
        (
            _wire.encode_tag(1, _wire.WIRE_VARINT) + _wire.encode_varint(code)
            if code
            else b""
        )
        + _encode_string(2, error.message)
        + (
            _wire.encode_tag(3, _wire.WIRE_VARINT)
            + _wire.encode_varint(int(error.retryable))
            if error.retryable
            else b""
        )
    )


def _decode_error(payload: bytes) -> MemoryError:
    code: int = 0
    message = ""
    retryable = False
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_VARINT and isinstance(value, int):
            code = value
        elif number == 2 and wire_type == _wire.WIRE_LEN:
            message = _decode_string(value)
        elif number == 3 and wire_type == _wire.WIRE_VARINT and isinstance(value, int):
            retryable = bool(value)
    return MemoryError(code=code, message=message, retryable=retryable)


def _encode_request_common(tenant_id: str) -> bytes:
    return _encode_string(1, tenant_id)


def encode_store_request(request: StoreMemoryRequest) -> bytes:
    return _encode_request_common(request.tenant_id) + _wire.encode_len_delimited(
        2, encode_memory_item(request.item)
    )


def decode_store_request(payload: bytes) -> StoreMemoryRequest:
    tenant = ""
    item: MemoryItem | None = None
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_LEN:
            tenant = _decode_string(value)
        elif number == 2 and wire_type == _wire.WIRE_LEN:
            item = decode_memory_item(value)
    if item is None:
        raise MemoryCodecError("StoreMemoryRequest is missing item")
    return StoreMemoryRequest(tenant_id=tenant, item=item)


def encode_store_response(response: StoreMemoryResponse) -> bytes:
    if response.error is not None:
        return _wire.encode_len_delimited(2, _encode_error(response.error))
    if response.item_id is not None:
        return _encode_optional_string(1, response.item_id)
    return b""


def decode_store_response(payload: bytes) -> StoreMemoryResponse:
    item_id: str | None = None
    error: MemoryError | None = None
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_LEN:
            item_id, error = _decode_string(value), None
        elif number == 2 and wire_type == _wire.WIRE_LEN:
            error, item_id = _decode_error(value), None
    return StoreMemoryResponse(item_id=item_id, error=error)


def encode_get_request(request: GetMemoryRequest) -> bytes:
    return _encode_request_common(request.tenant_id) + _encode_string(
        2, request.item_id
    )


def decode_get_request(payload: bytes) -> GetMemoryRequest:
    tenant = item_id = ""
    for number, wire_type, value in _wire.iter_fields(payload):
        if number in (1, 2) and wire_type == _wire.WIRE_LEN:
            if number == 1:
                tenant = _decode_string(value)
            else:
                item_id = _decode_string(value)
    return GetMemoryRequest(tenant_id=tenant, item_id=item_id)


def encode_get_response(response: GetMemoryResponse) -> bytes:
    if response.error is not None:
        return _wire.encode_len_delimited(2, _encode_error(response.error))
    if response.item is not None:
        return _wire.encode_len_delimited(1, encode_memory_item(response.item))
    return b""


def decode_get_response(payload: bytes) -> GetMemoryResponse:
    item: MemoryItem | None = None
    error: MemoryError | None = None
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_LEN:
            item, error = decode_memory_item(value), None
        elif number == 2 and wire_type == _wire.WIRE_LEN:
            error, item = _decode_error(value), None
    return GetMemoryResponse(item=item, error=error)


def encode_recall_request(request: RecallMemoryRequest) -> bytes:
    parts = [
        _encode_request_common(request.tenant_id),
        _encode_optional_string(2, request.scope),
        _encode_optional_string(3, request.subject),
        _wire.encode_packed_floats(4, request.query_embedding),
        _encode_optional_string(5, request.query_text),
        _encode_map(6, request.metadata_filters),
        _encode_optional_float(7, request.min_similarity),
        _wire.encode_tag(8, _wire.WIRE_VARINT) + _wire.encode_varint(request.top_k)
        if request.top_k is not None
        else b"",
    ]
    return b"".join(parts)


def decode_recall_request(payload: bytes) -> RecallMemoryRequest:
    values: dict[str, object] = {"query_embedding": (), "metadata_filters": {}}
    embedding: list[float] = []
    filters: dict[str, str] = {}
    for number, wire_type, value in _wire.iter_fields(payload):
        if number in (1, 2, 3, 5) and wire_type == _wire.WIRE_LEN:
            values[
                {1: "tenant_id", 2: "scope", 3: "subject", 5: "query_text"}[number]
            ] = _decode_string(value)
        elif number == 4:
            embedding.extend(_decode_float_repeated(value, wire_type))
        elif number == 6 and wire_type == _wire.WIRE_LEN:
            key, item = _decode_map(value)
            filters[key] = item
        elif number == 7:
            values["min_similarity"] = _decode_float(value, wire_type)
        elif number == 8:
            values["top_k"] = _decode_int64(value, wire_type)
    values["query_embedding"] = tuple(embedding)
    values["metadata_filters"] = filters
    return RecallMemoryRequest(**values)  # type: ignore[arg-type]


def encode_recall_response(response: RecallMemoryResponse) -> bytes:
    if response.error is not None:
        return _wire.encode_len_delimited(2, _encode_error(response.error))
    if response.matches is not None:
        batch = b"".join(
            _wire.encode_len_delimited(1, _encode_match(match))
            for match in response.matches
        )
        return _wire.encode_len_delimited(1, batch)
    return b""


def decode_recall_response(payload: bytes) -> RecallMemoryResponse:
    matches: tuple[MemoryMatch, ...] | None = None
    error: MemoryError | None = None
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_LEN:
            batch = tuple(
                _decode_match(item)
                for field, wt, item in _wire.iter_fields(value)
                if field == 1 and wt == _wire.WIRE_LEN
            )
            matches, error = batch, None
        elif number == 2 and wire_type == _wire.WIRE_LEN:
            error, matches = _decode_error(value), None
    return RecallMemoryResponse(matches=matches, error=error)


def encode_delete_request(request: DeleteMemoryRequest) -> bytes:
    return _encode_request_common(request.tenant_id) + _encode_string(
        2, request.item_id
    )


def decode_delete_request(payload: bytes) -> DeleteMemoryRequest:
    request = decode_get_request(payload)
    return DeleteMemoryRequest(tenant_id=request.tenant_id, item_id=request.item_id)


def encode_delete_response(response: DeleteMemoryResponse) -> bytes:
    if response.error is not None:
        return _wire.encode_len_delimited(2, _encode_error(response.error))
    if response.deleted is not None:
        return _wire.encode_tag(1, _wire.WIRE_VARINT) + _wire.encode_varint(
            int(response.deleted)
        )
    return b""


def decode_delete_response(payload: bytes) -> DeleteMemoryResponse:
    deleted: bool | None = None
    error: MemoryError | None = None
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_VARINT and isinstance(value, int):
            deleted, error = bool(value), None
        elif number == 2 and wire_type == _wire.WIRE_LEN:
            error, deleted = _decode_error(value), None
    return DeleteMemoryResponse(deleted=deleted, error=error)


def encode_prune_request(request: PruneMemoryRequest) -> bytes:
    return _encode_request_common(request.tenant_id) + _encode_optional_int(
        2, request.before_timestamp_ms
    )


def decode_prune_request(payload: bytes) -> PruneMemoryRequest:
    tenant = ""
    before: int | None = None
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_LEN:
            tenant = _decode_string(value)
        elif number == 2:
            before = _decode_int64(value, wire_type)
    return PruneMemoryRequest(tenant_id=tenant, before_timestamp_ms=before)


def encode_prune_response(response: PruneMemoryResponse) -> bytes:
    if response.error is not None:
        return _wire.encode_len_delimited(2, _encode_error(response.error))
    if response.pruned_count is not None:
        return _wire.encode_tag(1, _wire.WIRE_VARINT) + _wire.encode_varint(
            response.pruned_count & ((1 << 64) - 1)
        )
    return b""


def decode_prune_response(payload: bytes) -> PruneMemoryResponse:
    count: int | None = None
    error: MemoryError | None = None
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1:
            count, error = _decode_int64(value, wire_type), None
        elif number == 2 and wire_type == _wire.WIRE_LEN:
            error, count = _decode_error(value), None
    return PruneMemoryResponse(pruned_count=count, error=error)


def encode_export_batch_request(request: ExportMemoryRequest) -> bytes:
    parts = [
        _encode_request_common(request.tenant_id),
        _encode_optional_string(2, request.scope),
    ]
    if request.limit is not None:
        parts.append(
            _wire.encode_tag(3, _wire.WIRE_VARINT) + _wire.encode_varint(request.limit)
        )
    parts.append(_encode_optional_string(4, request.page_token))
    return b"".join(parts)


def decode_export_batch_request(payload: bytes) -> ExportMemoryRequest:
    values: dict[str, object] = {}
    for number, wire_type, value in _wire.iter_fields(payload):
        if number in (1, 2) and wire_type == _wire.WIRE_LEN:
            values[{1: "tenant_id", 2: "scope"}[number]] = _decode_string(value)
        elif number == 3:
            values["limit"] = _decode_int64(value, wire_type)
        elif number == 4 and wire_type == _wire.WIRE_LEN:
            values["page_token"] = _decode_string(value)
    return ExportMemoryRequest(**values)  # type: ignore[arg-type]


def encode_export_batch_response(response: ExportMemoryResponse) -> bytes:
    if response.error is not None:
        return _wire.encode_len_delimited(2, _encode_error(response.error))
    if response.items is not None:
        batch = b"".join(
            _wire.encode_len_delimited(1, encode_memory_item(item))
            for item in response.items
        )
        batch += _encode_string(2, response.next_page_token)
        return _wire.encode_len_delimited(1, batch)
    return b""


def decode_export_batch_response(payload: bytes) -> ExportMemoryResponse:
    items: tuple[MemoryItem, ...] | None = None
    error: MemoryError | None = None
    next_page_token = ""
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_LEN:
            batch_items: list[MemoryItem] = []
            for field, wt, item in _wire.iter_fields(value):
                if field == 1 and wt == _wire.WIRE_LEN:
                    batch_items.append(decode_memory_item(item))
                elif field == 2 and wt == _wire.WIRE_LEN:
                    next_page_token = _decode_string(item)
            items, error = tuple(batch_items), None
        elif number == 2 and wire_type == _wire.WIRE_LEN:
            error, items = _decode_error(value), None
    return ExportMemoryResponse(
        items=items, error=error, next_page_token=next_page_token
    )


def encode_import_batch_request(request: ImportMemoryRequest) -> bytes:
    parts = [_encode_request_common(request.tenant_id)]
    parts.extend(
        _wire.encode_len_delimited(2, encode_memory_item(item))
        for item in request.items
    )
    if request.overwrite:
        parts.append(_encode_bool(3, True))
    return b"".join(parts)


def decode_import_batch_request(payload: bytes) -> ImportMemoryRequest:
    tenant = ""
    items: list[MemoryItem] = []
    overwrite = False
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1 and wire_type == _wire.WIRE_LEN:
            tenant = _decode_string(value)
        elif number == 2 and wire_type == _wire.WIRE_LEN:
            items.append(decode_memory_item(value))
        elif number == 3 and wire_type == _wire.WIRE_VARINT and isinstance(value, int):
            overwrite = bool(value)
    return ImportMemoryRequest(
        tenant_id=tenant, items=tuple(items), overwrite=overwrite
    )


def encode_import_batch_response(response: ImportMemoryResponse) -> bytes:
    if response.error is not None:
        return _wire.encode_len_delimited(2, _encode_error(response.error))
    if response.imported_count is not None:
        return _wire.encode_tag(1, _wire.WIRE_VARINT) + _wire.encode_varint(
            response.imported_count
        )
    return b""


def decode_import_batch_response(payload: bytes) -> ImportMemoryResponse:
    count: int | None = None
    error: MemoryError | None = None
    for number, wire_type, value in _wire.iter_fields(payload):
        if number == 1:
            count, error = _decode_int64(value, wire_type), None
        elif number == 2 and wire_type == _wire.WIRE_LEN:
            error, count = _decode_error(value), None
    return ImportMemoryResponse(imported_count=count, error=error)


REQUEST_CODECS = {
    METHOD_STORE: (encode_store_request, decode_store_request),
    METHOD_GET: (encode_get_request, decode_get_request),
    METHOD_RECALL: (encode_recall_request, decode_recall_request),
    METHOD_DELETE: (encode_delete_request, decode_delete_request),
    METHOD_PRUNE: (encode_prune_request, decode_prune_request),
    METHOD_EXPORT_BATCH: (encode_export_batch_request, decode_export_batch_request),
    METHOD_IMPORT_BATCH: (encode_import_batch_request, decode_import_batch_request),
}
RESPONSE_CODECS = {
    METHOD_STORE: (encode_store_response, decode_store_response),
    METHOD_GET: (encode_get_response, decode_get_response),
    METHOD_RECALL: (encode_recall_response, decode_recall_response),
    METHOD_DELETE: (encode_delete_response, decode_delete_response),
    METHOD_PRUNE: (encode_prune_response, decode_prune_response),
    METHOD_EXPORT_BATCH: (encode_export_batch_response, decode_export_batch_response),
    METHOD_IMPORT_BATCH: (encode_import_batch_response, decode_import_batch_response),
}
_RESPONSE_VALUE_FIELDS = {
    METHOD_STORE: "item_id",
    METHOD_GET: "item",
    METHOD_RECALL: "matches",
    METHOD_DELETE: "deleted",
    METHOD_PRUNE: "pruned_count",
    METHOD_EXPORT_BATCH: "items",
    METHOD_IMPORT_BATCH: "imported_count",
}


def _validate_response(method: str, response: object) -> None:
    value_field = _RESPONSE_VALUE_FIELDS[method]
    value = getattr(response, value_field, None)
    error = getattr(response, "error", None)
    if (value is None) == (error is None):
        raise MemoryCodecError(f"{method} response must select exactly one result")


def encode_request(method: str, request: object) -> bytes:
    """Encode one method-specific request payload."""

    try:
        encoder, _ = REQUEST_CODECS[method]
    except KeyError as error:
        raise MemoryCodecError(f"unsupported memory method: {method}") from error
    try:
        return encoder(request)  # type: ignore[arg-type]
    except AttributeError as error:
        raise MemoryCodecError(
            f"request does not match memory method: {method}"
        ) from error


def decode_request(method: str, payload: bytes) -> object:
    """Decode one method-specific request payload."""

    try:
        _, decoder = REQUEST_CODECS[method]
    except KeyError as error:
        raise MemoryCodecError(f"unsupported memory method: {method}") from error
    return decoder(payload)


def encode_response(method: str, response: object) -> bytes:
    """Encode one method-specific response payload."""

    try:
        encoder, _ = RESPONSE_CODECS[method]
    except KeyError as error:
        raise MemoryCodecError(f"unsupported memory method: {method}") from error
    try:
        _validate_response(method, response)
        return encoder(response)  # type: ignore[arg-type]
    except AttributeError as error:
        raise MemoryCodecError(
            f"response does not match memory method: {method}"
        ) from error


def decode_response(method: str, payload: bytes) -> object:
    """Decode one method-specific response payload."""

    try:
        _, decoder = RESPONSE_CODECS[method]
    except KeyError as error:
        raise MemoryCodecError(f"unsupported memory method: {method}") from error
    response = decoder(payload)
    _validate_response(method, response)
    return response


__all__ = [
    "CAPABILITY_ID",
    "INTERFACE_VERSION",
    "METHODS",
    "METHOD_DELETE",
    "METHOD_EXPORT_BATCH",
    "METHOD_GET",
    "METHOD_IMPORT_BATCH",
    "METHOD_PRUNE",
    "METHOD_RECALL",
    "METHOD_STORE",
    "REQUEST_TYPE_URLS",
    "RESPONSE_TYPE_URLS",
    "DeleteMemoryRequest",
    "DeleteMemoryResponse",
    "ExportMemoryRequest",
    "ExportMemoryResponse",
    "GetMemoryRequest",
    "GetMemoryResponse",
    "ImportMemoryRequest",
    "ImportMemoryResponse",
    "MemoryCodecError",
    "MemoryError",
    "MemoryErrorCode",
    "MemoryItem",
    "MemoryMatch",
    "PruneMemoryRequest",
    "PruneMemoryResponse",
    "RecallMemoryRequest",
    "RecallMemoryResponse",
    "StoreMemoryRequest",
    "StoreMemoryResponse",
    "decode_delete_request",
    "decode_delete_response",
    "decode_export_batch_request",
    "decode_export_batch_response",
    "decode_get_request",
    "decode_get_response",
    "decode_import_batch_request",
    "decode_import_batch_response",
    "decode_memory_item",
    "decode_prune_request",
    "decode_prune_response",
    "decode_recall_request",
    "decode_recall_response",
    "decode_request",
    "decode_response",
    "decode_store_request",
    "decode_store_response",
    "encode_delete_request",
    "encode_delete_response",
    "encode_export_batch_request",
    "encode_export_batch_response",
    "encode_get_request",
    "encode_get_response",
    "encode_import_batch_request",
    "encode_import_batch_response",
    "encode_memory_item",
    "encode_prune_request",
    "encode_prune_response",
    "encode_recall_request",
    "encode_recall_response",
    "encode_request",
    "encode_response",
    "encode_store_request",
    "encode_store_response",
]
