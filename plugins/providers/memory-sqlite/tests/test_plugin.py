"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_plugin.py                                                   │
│  Module: memory_sqlite_provider.tests                                │
│  Role: Exercise typed DirectPluginRuntime capability dispatch.       │
│                                                                      │
│  模块职责：验证类型化 DirectPluginRuntime capability 分发。          │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cyrene_memory_provider_contracts.memory import (
    CAPABILITY_ID,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    DeleteMemoryRequest,
    ExportMemoryRequest,
    GetMemoryRequest,
    ImportMemoryRequest,
    MemoryErrorCode,
    MemoryItem,
    PruneMemoryRequest,
    RecallMemoryRequest,
    StoreMemoryRequest,
    decode_delete_response,
    decode_export_batch_response,
    decode_get_response,
    decode_import_batch_response,
    decode_prune_response,
    decode_recall_response,
    decode_store_response,
    encode_delete_request,
    encode_export_batch_request,
    encode_get_request,
    encode_import_batch_request,
    encode_prune_request,
    encode_recall_request,
    encode_store_request,
)
from cyrene_plugin_runtime.client import DirectPayload, DirectPluginClient
from cyrene_plugin_runtime.server import serve

from memory_sqlite_provider.plugin import MemorySqliteProvider, TypedPayload
from memory_sqlite_provider.settings import ProviderSettings


class FakeEmbeddingClient:
    """Return a deterministic non-zero vector as if supplied by a real binding."""

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def embed(
        self, inputs: Any, *, cancellation: Any | None = None
    ) -> tuple[tuple[float, ...], ...]:
        del cancellation
        self.inputs.extend(inputs)
        return tuple((1.0, 0.0, 0.0) for _ in inputs)

    def close(self) -> None:
        return None


def _provider(
    tmp_path: Path,
    *,
    embedding_client: Any | None = None,
    embedding_profile_id: str | None = None,
    document_prefix: str = "",
    query_prefix: str = "",
) -> MemorySqliteProvider:
    settings = ProviderSettings(
        database_path=tmp_path / "memory.sqlite3",
        embedding_model_id="embed-v1",
        embedding_dimensions=3,
        embedding_endpoint=None,
        embedding_timeout_seconds=5,
        recall_batch_size=16,
        max_top_k=20,
        embedding_profile_id=embedding_profile_id,
        embedding_document_prefix=document_prefix,
        embedding_query_prefix=query_prefix,
    )
    return MemorySqliteProvider(settings=settings, embedding_client=embedding_client)


def _invoke(provider: MemorySqliteProvider, method: str, request: bytes) -> TypedPayload:
    success, result = provider.on_invoke(
        CAPABILITY_ID,
        method,
        request,
        request_type_url=REQUEST_TYPE_URLS[method],
    )
    assert success is True
    assert isinstance(result, TypedPayload)
    assert result.type_url == RESPONSE_TYPE_URLS[method]
    return result


def test_typed_store_get_recall_delete_prune_and_import_methods(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    try:
        item = MemoryItem(
            item_id="fact-1",
            tenant_id="tenant-a",
            scope="conversation",
            subject="user-1",
            content="The user prefers concise replies.",
            embedding=(1.0, 0.0, 0.0),
            metadata={
                "origin": "message-9",
                "cyrene.embedding.model_id": "embed-v1",
                "cyrene.embedding.dimensions": "3",
                "cyrene.embedding.provider_model_id": "embed-v1",
            },
            created_at_ms=1_800_000_000_000,
        )
        stored = decode_store_response(
            _invoke(
                provider, "store", encode_store_request(StoreMemoryRequest("tenant-a", item))
            ).value
        )
        assert stored.item_id == "fact-1"

        mismatched = MemoryItem(
            item_id="tenant-mismatch",
            tenant_id="tenant-b",
            content="This must not cross the request tenant boundary.",
            embedding=(1.0, 0.0, 0.0),
        )
        mismatch_result = decode_store_response(
            _invoke(
                provider,
                "store",
                encode_store_request(StoreMemoryRequest("tenant-a", mismatched)),
            ).value
        )
        assert mismatch_result.error is not None
        assert mismatch_result.error.code == MemoryErrorCode.TENANT_MISMATCH

        fetched = decode_get_response(
            _invoke(
                provider, "get", encode_get_request(GetMemoryRequest("tenant-a", "fact-1"))
            ).value
        )
        assert fetched.item is not None
        assert fetched.item.metadata["origin"] == "message-9"
        assert fetched.item.metadata["cyrene.embedding.model_id"] == "embed-v1"

        recalled = decode_recall_response(
            _invoke(
                provider,
                "recall",
                encode_recall_request(
                    RecallMemoryRequest(
                        tenant_id="tenant-a",
                        scope="conversation",
                        subject="user-1",
                        query_embedding=(1.0, 0.0, 0.0),
                        metadata_filters={"origin": "message-9"},
                        min_similarity=0.9,
                        top_k=5,
                    )
                ),
            ).value
        )
        assert recalled.matches is not None
        assert [match.item.item_id for match in recalled.matches] == ["fact-1"]

        imported = decode_import_batch_response(
            _invoke(
                provider,
                "import_batch",
                encode_import_batch_request(
                    ImportMemoryRequest(tenant_id="tenant-a", items=(item,), overwrite=False)
                ),
            ).value
        )
        assert imported.imported_count == 0

        second_item = MemoryItem(
            item_id="fact-2",
            tenant_id="tenant-a",
            scope="conversation",
            subject="user-1",
            content="A second source record with the same vector.",
            embedding=(1.0, 0.0, 0.0),
            metadata={
                "cyrene.embedding.model_id": "embed-v1",
                "cyrene.embedding.dimensions": "3",
                "cyrene.embedding.provider_model_id": "embed-v1",
            },
            created_at_ms=1_800_000_000_001,
        )
        imported_second = decode_import_batch_response(
            _invoke(
                provider,
                "import_batch",
                encode_import_batch_request(
                    ImportMemoryRequest(tenant_id="tenant-a", items=(second_item,), overwrite=False)
                ),
            ).value
        )
        assert imported_second.imported_count == 1

        first_export = decode_export_batch_response(
            _invoke(
                provider,
                "export_batch",
                encode_export_batch_request(
                    ExportMemoryRequest(tenant_id="tenant-a", scope="conversation", limit=1)
                ),
            ).value
        )
        assert first_export.items is not None
        assert [value.item_id for value in first_export.items] == ["fact-1"]
        assert first_export.next_page_token
        second_export = decode_export_batch_response(
            _invoke(
                provider,
                "export_batch",
                encode_export_batch_request(
                    ExportMemoryRequest(
                        tenant_id="tenant-a",
                        scope="conversation",
                        limit=1,
                        page_token=first_export.next_page_token,
                    )
                ),
            ).value
        )
        assert second_export.items is not None
        assert [value.item_id for value in second_export.items] == ["fact-2"]
        assert second_export.next_page_token == ""

        pruned = decode_prune_response(
            _invoke(
                provider,
                "prune",
                encode_prune_request(PruneMemoryRequest(tenant_id="tenant-a")),
            ).value
        )
        assert pruned.pruned_count == 0

        deleted = decode_delete_response(
            _invoke(
                provider,
                "delete",
                encode_delete_request(DeleteMemoryRequest("tenant-a", "fact-1")),
            ).value
        )
        assert deleted.deleted is True
    finally:
        provider.on_shutdown(1000)


def test_text_store_requires_a_real_configured_model_provider(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    item = MemoryItem(
        item_id="without-vector",
        tenant_id="tenant-a",
        content="Text without a vector must not be stored with a fake vector.",
    )
    result = _invoke(
        provider,
        "store",
        encode_store_request(StoreMemoryRequest("tenant-a", item)),
    )
    failure = decode_store_response(result.value)
    assert failure.error is not None
    assert failure.error.retryable is False
    assert provider._store is None


def test_text_store_uses_only_the_injected_embedding_provider(tmp_path: Path) -> None:
    provider = _provider(tmp_path, embedding_client=FakeEmbeddingClient())
    item = MemoryItem(
        item_id="real-vector",
        tenant_id="tenant-a",
        content="The host supplied a bound real embedding provider.",
    )
    try:
        result = _invoke(
            provider,
            "store",
            encode_store_request(StoreMemoryRequest("tenant-a", item)),
        )
        assert decode_store_response(result.value).item_id == "real-vector"
    finally:
        provider.on_shutdown(1000)


def test_host_supplied_vector_requires_an_explicit_profile(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    item = MemoryItem(
        item_id="unprofiled-vector",
        tenant_id="tenant-a",
        content="A vector without provenance must not be accepted.",
        embedding=(1.0, 0.0, 0.0),
    )
    try:
        result = decode_store_response(
            _invoke(
                provider,
                "store",
                encode_store_request(StoreMemoryRequest("tenant-a", item)),
            ).value
        )
        assert result.error is not None
        assert "must declare its model profile" in result.error.message
    finally:
        provider.on_shutdown(1000)


def test_query_and_document_prefixes_use_a_distinct_embedding_profile(
    tmp_path: Path,
) -> None:
    embedding_client = FakeEmbeddingClient()
    provider = _provider(
        tmp_path,
        embedding_client=embedding_client,
        embedding_profile_id="embed-v1-query-passage-v1",
        document_prefix="passage: ",
        query_prefix="query: ",
    )
    item = MemoryItem(
        item_id="prefixed-memory",
        tenant_id="tenant-a",
        content="The user prefers concise replies.",
    )
    try:
        stored = decode_store_response(
            _invoke(
                provider,
                "store",
                encode_store_request(StoreMemoryRequest("tenant-a", item)),
            ).value
        )
        assert stored.item_id == "prefixed-memory"
        assert embedding_client.inputs == ["passage: The user prefers concise replies."]

        recalled = decode_recall_response(
            _invoke(
                provider,
                "recall",
                encode_recall_request(
                    RecallMemoryRequest(tenant_id="tenant-a", query_text="concise replies")
                ),
            ).value
        )
        assert recalled.matches is not None
        assert embedding_client.inputs[-1] == "query: concise replies"
        assert recalled.matches[0].item.metadata["cyrene.embedding.model_id"] == (
            "embed-v1-query-passage-v1"
        )
    finally:
        provider.on_shutdown(1000)


def test_provider_rejects_wrong_capability_method_and_type_url(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    success, result = provider.on_invoke(
        "tool.provider.v1",
        "store",
        b"",
        request_type_url=REQUEST_TYPE_URLS["store"],
    )
    assert success is False
    assert "unsupported capability" in str(result)

    success, result = provider.on_invoke(
        CAPABILITY_ID,
        "unknown",
        b"",
        request_type_url="type.cyrene.io/unknown",
    )
    assert success is False
    assert "METHOD_NOT_FOUND" in str(result)

    success, result = provider.on_invoke(CAPABILITY_ID, "store", b"", request_type_url="wrong")
    assert success is False
    assert "request_type_url" in str(result)


def test_provider_serves_typed_calls_over_direct_plugin_runtime(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    server, connection_ref = serve(provider, CAPABILITY_ID, "1", "127.0.0.1:0")
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    item = MemoryItem(
        item_id="rpc-item",
        tenant_id="tenant-a",
        content="This call crosses the local DirectPluginRuntime boundary.",
        embedding=(1.0, 0.0, 0.0),
        metadata={
            "cyrene.embedding.model_id": "embed-v1",
            "cyrene.embedding.dimensions": "3",
            "cyrene.embedding.provider_model_id": "embed-v1",
        },
    )
    try:
        result = client.invoke(
            capability=CAPABILITY_ID,
            interface_version="1",
            method="store",
            request=DirectPayload(
                value=encode_store_request(StoreMemoryRequest("tenant-a", item)),
                type_url=REQUEST_TYPE_URLS["store"],
            ),
            deadline_seconds=2,
        )
        assert result.type_url == RESPONSE_TYPE_URLS["store"]
        assert decode_store_response(result.value).item_id == "rpc-item"
    finally:
        client.close()
        server.stop(0).wait()
        provider.on_shutdown(1000)
