"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_pgvector_store.py                                           │
│  Module: memory_pgvector_provider.tests                              │
│  Role: Verify real PostgreSQL/pgvector persistence and filtered recall.│
│                                                                      │
│  模块职责：验证真实 PostgreSQL/pgvector 持久化和过滤召回。            │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import replace

import psycopg
import pytest
from cyrene_memory_provider_contracts.memory import (
    CAPABILITY_ID,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    DeleteMemoryRequest,
    ExportMemoryRequest,
    GetMemoryRequest,
    ImportMemoryRequest,
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

from memory_pgvector_provider.plugin import MemoryPgvectorProvider
from memory_pgvector_provider.settings import ProviderSettings
from memory_pgvector_provider.store import MemoryStoreError, PgvectorMemoryStore, StoredMemory

_DSN = os.environ.get("CYRENE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="requires an isolated local PostgreSQL database with pgvector installed",
)
_TEST_DIMENSIONS = 3072


class FixtureEmbeddingClient:
    """Deterministic local model binding double for the direct RPC adapter test."""

    def __init__(self, dimensions: int = _TEST_DIMENSIONS) -> None:
        self.inputs: list[str] = []
        self.dimensions = dimensions

    def embed(self, inputs, *, cancellation=None):
        del cancellation
        self.inputs.extend(inputs)
        return tuple(_vector(self.dimensions) for _ in inputs)

    def close(self) -> None:
        return None


def _settings(dsn: str, *, dimensions: int = _TEST_DIMENSIONS) -> ProviderSettings:
    return ProviderSettings(
        database_dsn=dsn,
        embedding_model_id="test-embedding-v1",
        embedding_dimensions=dimensions,
        embedding_endpoint=None,
        embedding_timeout_seconds=2,
        pool_min_size=1,
        pool_max_size=3,
        connection_timeout_seconds=5,
        recall_batch_size=16,
        max_top_k=20,
    )


def _vector(dimensions: int = _TEST_DIMENSIONS) -> tuple[float, ...]:
    return (1.0,) + (0.0,) * (dimensions - 1)


def _item(
    tenant: str,
    item_id: str,
    *,
    embedding: tuple[float, ...] | None = None,
    scope: str = "conversation",
    subject: str = "user-1",
    metadata: dict[str, str] | None = None,
    expires_at_ms: int | None = None,
) -> StoredMemory:
    vector = _vector() if embedding is None else embedding
    item_metadata = {
        "cyrene.embedding.model_id": "test-embedding-v1",
        "cyrene.embedding.dimensions": str(len(vector)),
        "cyrene.embedding.provider_model_id": "test-embedding-v1",
    }
    item_metadata.update(metadata or {})
    return StoredMemory(
        item_id=item_id,
        tenant_id=tenant,
        scope=scope,
        subject=subject,
        content="same text across distinct item identities",
        embedding=vector,
        metadata=item_metadata,
        created_at_ms=1_800_000_000_000,
        expires_at_ms=expires_at_ms,
        ttl_seconds=None,
    )


@pytest.fixture
def store() -> PgvectorMemoryStore:
    assert _DSN is not None
    result = PgvectorMemoryStore(_settings(_DSN))
    try:
        yield result
    finally:
        with result._require_pool().connection() as connection:
            connection.execute(
                "DELETE FROM cyrene_memory_records WHERE tenant_id LIKE 'memory-test-%'"
            )
        result.close()


def test_pool_migration_persistence_and_profile_filtered_recall(
    store: PgvectorMemoryStore,
) -> None:
    tenant = f"memory-test-{uuid.uuid4().hex}"
    other_tenant = f"memory-test-{uuid.uuid4().hex}"
    store.store(tenant, _item(tenant, "source-a", metadata={"source": "private"}))
    store.store(tenant, _item(tenant, "source-b", scope="profile"))
    store.store(other_tenant, _item(other_tenant, "other-tenant"))
    old_profile = _item(
        tenant,
        "old-profile",
        metadata={
            "cyrene.embedding.model_id": "older-model",
            "cyrene.embedding.dimensions": str(_TEST_DIMENSIONS),
        },
    )
    with pytest.raises(MemoryStoreError, match="re-embedded"):
        store.import_batch(tenant, [old_profile], overwrite=False)

    matches = store.recall(
        tenant_id=tenant,
        query_embedding=_vector(),
        scope="conversation",
        subject="user-1",
        metadata_filters={"source": "private"},
        min_similarity=0.99,
        top_k=5,
    )
    assert [match.item.item_id for match in matches] == ["source-a"]
    assert matches[0].score == pytest.approx(1.0)
    assert store.get(other_tenant, "source-a") is None
    assert store.delete(other_tenant, "source-a") is False

    reopened = PgvectorMemoryStore(_settings(_DSN))
    try:
        fetched = reopened.get(tenant, "source-a")
        assert fetched is not None
        assert fetched.metadata["cyrene.embedding.model_id"] == "test-embedding-v1"
        assert reopened.recall(
            tenant_id=tenant,
            query_embedding=_vector(),
            scope=None,
            subject=None,
            metadata_filters={},
            min_similarity=0.9,
            top_k=10,
        )
    finally:
        reopened.close()


def test_import_is_transactional_idempotent_and_keeps_same_text_sources_distinct(
    store: PgvectorMemoryStore,
) -> None:
    tenant = f"memory-test-{uuid.uuid4().hex}"
    first = _item(tenant, "source-a")
    second = _item(tenant, "source-b")
    invalid = _item(tenant, "source-invalid", embedding=(0.0,) * _TEST_DIMENSIONS)
    with pytest.raises(MemoryStoreError, match="must declare"):
        store.import_batch(tenant, [replace(first, metadata={})], overwrite=False)
    with pytest.raises(ValueError, match="zero vectors"):
        store.import_batch(tenant, [first, invalid], overwrite=False)
    assert store.get(tenant, "source-a") is None

    assert store.import_batch(tenant, [first, second], overwrite=False) == 2
    assert store.import_batch(tenant, [first, second], overwrite=False) == 0
    exported, _ = store.export_batch(tenant, None, 10, None)
    assert [item.item_id for item in exported] == ["source-a", "source-b"]
    assert len({item.content for item in exported}) == 1

    with pytest.raises(MemoryStoreError, match="does not match"):
        store.import_batch(tenant, [_item("other-tenant", "mismatch")], overwrite=False)


def test_export_cursor_is_bounded_and_bound_to_tenant_and_scope(
    store: PgvectorMemoryStore,
) -> None:
    tenant = f"memory-test-{uuid.uuid4().hex}"
    for identifier in ("a", "b", "c"):
        store.store(tenant, _item(tenant, identifier))
    first, token = store.export_batch(tenant, "conversation", 2, None)
    second, end = store.export_batch(tenant, "conversation", 2, token)
    assert [item.item_id for item in first] == ["a", "b"]
    assert [item.item_id for item in second] == ["c"]
    assert token and not end
    with pytest.raises(ValueError, match="tenant"):
        store.export_batch(f"memory-test-{uuid.uuid4().hex}", "conversation", 2, token)
    with pytest.raises(ValueError, match="scope"):
        store.export_batch(tenant, "profile", 2, token)
    with pytest.raises(ValueError, match="another vector profile"):
        PgvectorMemoryStore(
            replace(
                _settings(_DSN),
                embedding_profile_id="another-profile",
            )
        )


def test_pgvector_exact_recall_supports_large_contract_dimensions() -> None:
    assert _DSN is not None
    dimensions = _TEST_DIMENSIONS
    store = PgvectorMemoryStore(_settings(_DSN, dimensions=dimensions))
    tenant = f"memory-test-{uuid.uuid4().hex}"
    vector = _vector(dimensions)
    try:
        store.store(tenant, _item(tenant, "large-vector", embedding=vector))
        item = store.get(tenant, "large-vector")
        assert item is not None
        assert len(item.embedding) == dimensions
        matches = store.recall(
            tenant_id=tenant,
            query_embedding=vector,
            scope=None,
            subject=None,
            metadata_filters={},
            min_similarity=0.99,
            top_k=1,
        )
        assert [match.item.item_id for match in matches] == ["large-vector"]
        assert matches[0].score == pytest.approx(1.0)
    finally:
        with store._require_pool().connection() as connection:
            connection.execute(
                "DELETE FROM cyrene_memory_records WHERE tenant_id = %s", (tenant,)
            )
        store.close()


def test_cancelled_blocked_recall_returns_connection_to_pool() -> None:
    assert _DSN is not None
    store = PgvectorMemoryStore(replace(_settings(_DSN), pool_max_size=1))
    cancellation = threading.Event()
    started = threading.Event()
    failures: list[Exception] = []

    def recall() -> None:
        started.set()
        try:
            store.recall(
                tenant_id="memory-test-cancel",
                query_embedding=_vector(),
                scope=None,
                subject=None,
                metadata_filters={},
                min_similarity=0,
                top_k=1,
                cancellation=cancellation,
            )
        except Exception as error:
            failures.append(error)

    try:
        with psycopg.connect(_DSN) as blocker, blocker.transaction():
            blocker.execute("LOCK TABLE cyrene_memory_records IN ACCESS EXCLUSIVE MODE")
            worker = threading.Thread(target=recall)
            worker.start()
            assert started.wait(timeout=2)
            time.sleep(0.25)
            cancellation.set()
            worker.join(timeout=4)
        assert not worker.is_alive(), "cancel did not interrupt the blocked recall"

        assert failures and isinstance(failures[0], MemoryStoreError)
        with store._require_pool().connection() as connection:
            assert connection.execute("SELECT 1 AS alive").fetchone()["alive"] == 1
    finally:
        store.close()


def test_all_seven_methods_cross_the_typed_direct_plugin_runtime_boundary() -> None:
    assert _DSN is not None
    settings = _settings(_DSN)
    embedding_client = FixtureEmbeddingClient(settings.embedding_dimensions)
    provider = MemoryPgvectorProvider(
        settings=settings,
        embedding_client=embedding_client,
    )
    server, endpoint = serve(provider, CAPABILITY_ID, "1", "127.0.0.1:0")
    client = DirectPluginClient.for_local_connection_ref(endpoint)
    tenant = f"memory-test-{uuid.uuid4().hex}"
    first = MemoryItem(
        item_id="rpc-a",
        tenant_id=tenant,
        scope="conversation",
        subject="user-1",
        content="typed runtime message",
    )

    def invoke(method: str, body: bytes):
        return client.invoke(
            capability=CAPABILITY_ID,
            interface_version="1",
            method=method,
            request=DirectPayload(value=body, type_url=REQUEST_TYPE_URLS[method]),
            deadline_seconds=5,
        )

    try:
        stored_payload = invoke("store", encode_store_request(StoreMemoryRequest(tenant, first)))
        assert stored_payload.type_url == RESPONSE_TYPE_URLS["store"]
        assert decode_store_response(stored_payload.value).item_id == "rpc-a"
        assert embedding_client.inputs == ["typed runtime message"]

        fetched_payload = invoke("get", encode_get_request(GetMemoryRequest(tenant, "rpc-a")))
        assert decode_get_response(fetched_payload.value).item is not None

        recalled_payload = invoke(
            "recall",
            encode_recall_request(
                RecallMemoryRequest(tenant_id=tenant, query_text="typed runtime query")
            ),
        )
        assert decode_recall_response(recalled_payload.value).matches
        assert embedding_client.inputs[-1] == "typed runtime query"

        second = MemoryItem(
            item_id="rpc-b",
            tenant_id=tenant,
            scope="conversation",
            subject="user-1",
            content="another source with the same body",
            embedding=_vector(),
            metadata={
                "cyrene.embedding.model_id": "test-embedding-v1",
                "cyrene.embedding.dimensions": str(_TEST_DIMENSIONS),
                "cyrene.embedding.provider_model_id": "test-embedding-v1",
            },
        )
        imported_payload = invoke(
            "import_batch",
            encode_import_batch_request(
                ImportMemoryRequest(tenant_id=tenant, items=(second,), overwrite=False)
            ),
        )
        assert decode_import_batch_response(imported_payload.value).imported_count == 1

        first_page = decode_export_batch_response(
            invoke(
                "export_batch",
                encode_export_batch_request(ExportMemoryRequest(tenant_id=tenant, limit=1)),
            ).value
        )
        assert first_page.items is not None
        assert len(first_page.items) == 1
        assert first_page.next_page_token

        pruned_payload = invoke("prune", encode_prune_request(PruneMemoryRequest(tenant_id=tenant)))
        assert decode_prune_response(pruned_payload.value).pruned_count == 0

        deleted_payload = invoke(
            "delete", encode_delete_request(DeleteMemoryRequest(tenant, "rpc-a"))
        )
        assert decode_delete_response(deleted_payload.value).deleted is True
    finally:
        client.close()
        server.stop(0).wait()
        active_store = provider._store
        if active_store is not None:
            with active_store._require_pool().connection() as connection:
                connection.execute(
                    "DELETE FROM cyrene_memory_records WHERE tenant_id = %s", (tenant,)
                )
        provider.on_shutdown(1000)


def test_model_input_prefixes_are_selected_by_embedding_role() -> None:
    settings = replace(
        _settings("postgresql://unused"),
        embedding_profile_id="test-embedding-v1-query-passage-v1",
        embedding_document_prefix="passage: ",
        embedding_query_prefix="query: ",
    )
    client = FixtureEmbeddingClient(settings.embedding_dimensions)
    provider = MemoryPgvectorProvider(settings=settings, embedding_client=client)
    try:
        provider._embed(("document",), role="document", cancellation=None)
        provider._embed(("question",), role="query", cancellation=None)
        assert client.inputs == ["passage: document", "query: question"]
    finally:
        provider.on_shutdown(1000)
