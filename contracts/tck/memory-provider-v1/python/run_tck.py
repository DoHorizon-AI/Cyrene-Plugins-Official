#!/usr/bin/env python3
"""Compare the memory Python SDK codec with generated protobuf messages."""

from __future__ import annotations

import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("usage: run_tck.py <generated-python-root>")

repo = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
sys.path.insert(0, str(repo / "sdk/python/cyrene_memory_provider_contracts/src"))

from cyrene.memory.provider.v1 import memory_provider_pb2 as pb2
from cyrene_memory_provider_contracts import (
    ExportMemoryRequest,
    ExportMemoryResponse,
    MemoryItem,
    RecallMemoryRequest,
    StoreMemoryRequest,
    decode_export_batch_request,
    decode_export_batch_response,
    decode_memory_item,
    decode_recall_request,
    decode_store_request,
    encode_export_batch_request,
    encode_export_batch_response,
    encode_memory_item,
    encode_recall_request,
    encode_store_request,
)

item = MemoryItem(
    item_id="memory-1",
    tenant_id="tenant-a",
    scope="conversation",
    subject="user-a",
    content="prefers concise replies",
    embedding=(0.25, -0.5),
    metadata={"source": "message-9"},
    created_at_ms=1726000000000,
    expires_at_ms=1726086400000,
    ttl_seconds=86400,
)
expected_item = pb2.MemoryItem(
    item_id=item.item_id,
    tenant_id=item.tenant_id,
    scope=item.scope,
    subject=item.subject,
    content=item.content,
    embedding=item.embedding,
    metadata=item.metadata,
    created_at_ms=item.created_at_ms,
    expires_at_ms=item.expires_at_ms,
    ttl_seconds=item.ttl_seconds,
)
assert encode_memory_item(item) == expected_item.SerializeToString(deterministic=True)
assert decode_memory_item(expected_item.SerializeToString()) == item

store = StoreMemoryRequest(tenant_id="tenant-a", item=item)
expected_store = pb2.StoreMemoryRequest(tenant_id=store.tenant_id, item=expected_item)
assert encode_store_request(store) == expected_store.SerializeToString(
    deterministic=True
)
assert decode_store_request(expected_store.SerializeToString()) == store

recall = RecallMemoryRequest(
    tenant_id="tenant-a",
    scope="conversation",
    query_embedding=(0.25, -0.5),
    query_text="what does the user prefer",
    metadata_filters={"source": "message-9"},
    min_similarity=0.0,
    top_k=12,
)
expected_recall = pb2.RecallMemoryRequest(
    tenant_id=recall.tenant_id,
    scope=recall.scope,
    query_embedding=recall.query_embedding,
    query_text=recall.query_text,
    metadata_filters=recall.metadata_filters,
    min_similarity=recall.min_similarity,
    top_k=recall.top_k,
)
assert encode_recall_request(recall) == expected_recall.SerializeToString(
    deterministic=True
)
assert decode_recall_request(expected_recall.SerializeToString()) == recall

export_request = ExportMemoryRequest(
    tenant_id="tenant-a", limit=50, page_token="cursor-2"
)
expected_export = pb2.ExportMemoryRequest(
    tenant_id=export_request.tenant_id,
    limit=export_request.limit,
    page_token=export_request.page_token,
)
assert encode_export_batch_request(export_request) == expected_export.SerializeToString(
    deterministic=True
)
assert (
    decode_export_batch_request(expected_export.SerializeToString()) == export_request
)

export_response = ExportMemoryResponse(items=(item,), next_page_token="cursor-3")
expected_export_response = pb2.ExportMemoryResponse(
    batch=pb2.ExportBatch(items=[expected_item], next_page_token="cursor-3")
)
assert encode_export_batch_response(
    export_response
) == expected_export_response.SerializeToString(deterministic=True)
assert (
    decode_export_batch_response(expected_export_response.SerializeToString())
    == export_response
)

print("memory.provider.v1 Python SDK/generated binding TCK: PASS")
