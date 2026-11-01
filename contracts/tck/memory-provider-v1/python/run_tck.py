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
    DeleteMemoryRequest,
    DeleteMemoryResponse,
    ExportMemoryRequest,
    ExportMemoryResponse,
    GetMemoryRequest,
    GetMemoryResponse,
    ImportMemoryRequest,
    ImportMemoryResponse,
    MemoryError,
    MemoryErrorCode,
    MemoryItem,
    MemoryMatch,
    PruneMemoryRequest,
    PruneMemoryResponse,
    RecallMemoryRequest,
    RecallMemoryResponse,
    StoreMemoryRequest,
    StoreMemoryResponse,
    decode_delete_request,
    decode_delete_response,
    decode_export_batch_request,
    decode_export_batch_response,
    decode_get_request,
    decode_get_response,
    decode_import_batch_request,
    decode_import_batch_response,
    decode_memory_item,
    decode_prune_request,
    decode_prune_response,
    decode_recall_request,
    decode_recall_response,
    decode_store_request,
    decode_store_response,
    encode_delete_request,
    encode_delete_response,
    encode_export_batch_request,
    encode_export_batch_response,
    encode_get_request,
    encode_get_response,
    encode_import_batch_request,
    encode_import_batch_response,
    encode_memory_item,
    encode_prune_request,
    encode_prune_response,
    encode_recall_request,
    encode_recall_response,
    encode_store_request,
    encode_store_response,
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
store_response = StoreMemoryResponse(item_id="memory-1")
expected_store_response = pb2.StoreMemoryResponse(item_id="memory-1")
assert encode_store_response(store_response) == expected_store_response.SerializeToString(
    deterministic=True
)
assert decode_store_response(expected_store_response.SerializeToString()) == store_response

get_request = GetMemoryRequest(tenant_id="tenant-a", item_id="memory-1")
expected_get = pb2.GetMemoryRequest(tenant_id="tenant-a", item_id="memory-1")
assert encode_get_request(get_request) == expected_get.SerializeToString(deterministic=True)
assert decode_get_request(expected_get.SerializeToString()) == get_request
get_response = GetMemoryResponse(item=item)
expected_get_response = pb2.GetMemoryResponse(item=expected_item)
assert encode_get_response(get_response) == expected_get_response.SerializeToString(
    deterministic=True
)
assert decode_get_response(expected_get_response.SerializeToString()) == get_response

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
recall_response = RecallMemoryResponse(matches=(MemoryMatch(item=item, score=0.75),))
expected_recall_response = pb2.RecallMemoryResponse(
    matches=pb2.RecallBatch(matches=[pb2.MemoryMatch(item=expected_item, score=0.75)])
)
assert encode_recall_response(recall_response) == expected_recall_response.SerializeToString(
    deterministic=True
)
assert decode_recall_response(expected_recall_response.SerializeToString()) == recall_response

delete_request = DeleteMemoryRequest(tenant_id="tenant-a", item_id="memory-1")
expected_delete = pb2.DeleteMemoryRequest(tenant_id="tenant-a", item_id="memory-1")
assert encode_delete_request(delete_request) == expected_delete.SerializeToString(
    deterministic=True
)
assert decode_delete_request(expected_delete.SerializeToString()) == delete_request
delete_response = DeleteMemoryResponse(deleted=False)
expected_delete_response = pb2.DeleteMemoryResponse(deleted=False)
assert encode_delete_response(delete_response) == expected_delete_response.SerializeToString(
    deterministic=True
)
assert decode_delete_response(expected_delete_response.SerializeToString()) == delete_response

prune_request = PruneMemoryRequest(
    tenant_id="tenant-a", before_timestamp_ms=1726000000000
)
expected_prune = pb2.PruneMemoryRequest(
    tenant_id=prune_request.tenant_id,
    before_timestamp_ms=prune_request.before_timestamp_ms,
)
assert encode_prune_request(prune_request) == expected_prune.SerializeToString(
    deterministic=True
)
assert decode_prune_request(expected_prune.SerializeToString()) == prune_request
prune_response = PruneMemoryResponse(pruned_count=7)
expected_prune_response = pb2.PruneMemoryResponse(pruned_count=7)
assert encode_prune_response(prune_response) == expected_prune_response.SerializeToString(
    deterministic=True
)
assert decode_prune_response(expected_prune_response.SerializeToString()) == prune_response

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

import_request = ImportMemoryRequest(tenant_id="tenant-a", items=(item,), overwrite=True)
expected_import = pb2.ImportMemoryRequest(
    tenant_id=import_request.tenant_id,
    items=[expected_item],
    overwrite=True,
)
assert encode_import_batch_request(import_request) == expected_import.SerializeToString(
    deterministic=True
)
assert decode_import_batch_request(expected_import.SerializeToString()) == import_request
import_response = ImportMemoryResponse(imported_count=1)
expected_import_response = pb2.ImportMemoryResponse(imported_count=1)
assert encode_import_batch_response(import_response) == expected_import_response.SerializeToString(
    deterministic=True
)
assert decode_import_batch_response(expected_import_response.SerializeToString()) == import_response

memory_error = MemoryError(
    code=MemoryErrorCode.NOT_FOUND, message="missing", retryable=False
)
expected_error_response = pb2.GetMemoryResponse(
    error=pb2.MemoryError(code=pb2.MEMORY_ERROR_CODE_NOT_FOUND, message="missing")
)
assert encode_get_response(GetMemoryResponse(error=memory_error)) == (
    expected_error_response.SerializeToString(deterministic=True)
)

print("memory.provider.v1 Python SDK/generated binding TCK: PASS")
