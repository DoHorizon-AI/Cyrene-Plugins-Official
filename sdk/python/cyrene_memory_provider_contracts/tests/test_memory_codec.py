"""Contract round trips for the memory.provider.v1 Python codec."""

import pytest
from cyrene_memory_provider_contracts import (
    METHOD_EXPORT_BATCH,
    ExportMemoryRequest,
    ExportMemoryResponse,
    MemoryCodecError,
    MemoryItem,
    RecallMemoryRequest,
    StoreMemoryRequest,
    decode_export_batch_request,
    decode_export_batch_response,
    decode_recall_request,
    decode_response,
    decode_store_request,
    encode_export_batch_request,
    encode_export_batch_response,
    encode_recall_request,
    encode_response,
    encode_store_request,
)


def test_store_codec_round_trips_source_metadata_vectors_and_expiry() -> None:
    request = StoreMemoryRequest(
        tenant_id="tenant-a",
        item=MemoryItem(
            item_id="mem-1",
            tenant_id="tenant-a",
            scope="conversation",
            subject="user-1",
            content="prefers concise answers",
            embedding=(0.25, -0.5),
            metadata={"source": "message-8", "kind": "user_fact"},
            created_at_ms=1726000000000,
            expires_at_ms=1726086400000,
            ttl_seconds=86400,
        ),
    )
    assert decode_store_request(encode_store_request(request)) == request


def test_recall_codec_preserves_optional_zero_values() -> None:
    request = RecallMemoryRequest(
        tenant_id="tenant-a",
        scope="conversation",
        query_embedding=(1.0, 2.0),
        query_text="what was the preference",
        min_similarity=0.0,
        top_k=0,
    )
    decoded = decode_recall_request(encode_recall_request(request))
    assert decoded == request


def test_export_codec_supports_cursor_pages_and_empty_page() -> None:
    request = ExportMemoryRequest(tenant_id="tenant-a", limit=50, page_token="cursor-2")
    assert decode_export_batch_request(encode_export_batch_request(request)) == request

    response = ExportMemoryResponse(items=(), next_page_token="cursor-3")
    assert (
        decode_export_batch_response(encode_export_batch_response(response)) == response
    )
    assert METHOD_EXPORT_BATCH == "export_batch"


def test_generic_codec_rejects_responses_without_a_oneof_result() -> None:
    from cyrene_memory_provider_contracts import StoreMemoryResponse

    with pytest.raises(MemoryCodecError, match="select exactly one result"):
        encode_response("store", StoreMemoryResponse())
    with pytest.raises(MemoryCodecError, match="select exactly one result"):
        decode_response("store", b"")
