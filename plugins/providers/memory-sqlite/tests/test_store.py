"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_store.py                                                    │
│  Module: memory_sqlite_provider.tests                                │
│  Role: Verify tenant isolation, durable vectors and bounded recall.  │
│                                                                      │
│  模块职责：验证租户隔离、向量持久化和有界召回。                        │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from memory_sqlite_provider.store import MemoryStoreError, SQLiteMemoryStore, StoredMemory


def _store(
    path: Path,
    *,
    model: str = "embed-v1",
    provider_model: str | None = None,
    document_prefix: str = "",
    query_prefix: str = "",
) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(
        path,
        embedding_model_id=model,
        embedding_dimensions=3,
        recall_batch_size=16,
        max_top_k=20,
        provider_model_id=provider_model,
        document_prefix=document_prefix,
        query_prefix=query_prefix,
    )


def _item(
    item_id: str,
    *,
    tenant: str = "tenant-a",
    embedding: tuple[float, ...] = (1.0, 0.0, 0.0),
    scope: str = "conversation",
    subject: str = "user-1",
    metadata: dict[str, str] | None = None,
    created: int = 1_800_000_000_000,
    expires: int | None = None,
) -> StoredMemory:
    item_metadata = {
        "cyrene.embedding.model_id": "embed-v1",
        "cyrene.embedding.dimensions": str(len(embedding)),
        "cyrene.embedding.provider_model_id": "embed-v1",
    }
    item_metadata.update(metadata or {})
    return StoredMemory(
        item_id=item_id,
        tenant_id=tenant,
        scope=scope,
        subject=subject,
        content=f"same body for {item_id}",
        embedding=embedding,
        metadata=item_metadata,
        created_at_ms=created,
        expires_at_ms=expires,
        ttl_seconds=None,
    )


def test_recall_filters_tenant_scope_subject_metadata_and_expiry_before_scoring(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "memory.sqlite3")
    try:
        store.store("tenant-a", _item("allowed", metadata={"source": "private"}))
        store.store("tenant-b", _item("wrong-tenant", tenant="tenant-b"))
        store.store("tenant-a", _item("wrong-scope", scope="other"))
        store.store("tenant-a", _item("wrong-subject", subject="user-2"))
        store.store("tenant-a", _item("wrong-metadata", metadata={"source": "public"}))
        store.store("tenant-a", _item("expired", expires=1_700_000_000_000))

        matches = store.recall(
            tenant_id="tenant-a",
            query_embedding=(1.0, 0.0, 0.0),
            scope="conversation",
            subject="user-1",
            metadata_filters={"source": "private"},
            min_similarity=0.99,
            top_k=5,
        )

        assert [match.item.item_id for match in matches] == ["allowed"]
        assert matches[0].score == pytest.approx(1.0)
        assert store.get("tenant-b", "allowed") is None
        assert store.delete("tenant-b", "allowed") is False
        assert store.get("tenant-a", "allowed") is not None
    finally:
        store.close()


def test_recall_keeps_bounded_top_k_and_rejects_foreign_profile_imports(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    store = _store(path)
    try:
        for index in range(75):
            direction = (float(index + 1), float(index % 3), 0.1)
            store.store("tenant-a", _item(f"item-{index:03}", embedding=direction))
        imported = _item(
            "old-profile",
            metadata={
                "cyrene.embedding.model_id": "embed-old",
                "cyrene.embedding.dimensions": "3",
                "cyrene.embedding.provider_model_id": "embed-old",
            },
        )
        with pytest.raises(MemoryStoreError, match="re-embedded"):
            store.import_batch("tenant-a", [imported], overwrite=False)
        matches = store.recall(
            tenant_id="tenant-a",
            query_embedding=(1.0, 0.0, 0.0),
            scope=None,
            subject=None,
            metadata_filters={},
            min_similarity=0.0,
            top_k=5,
        )
        assert len(matches) == 5
        assert all(match.item.item_id != "old-profile" for match in matches)
    finally:
        store.close()


def test_restart_preserves_records_and_model_profile_identity(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    first = _store(path)
    first.store("tenant-a", _item("persisted"))
    first.close()

    reopened = _store(path)
    try:
        item = reopened.get("tenant-a", "persisted")
        assert item is not None
        assert item.embedding == pytest.approx((1.0, 0.0, 0.0))
        assert item.metadata["cyrene.embedding.model_id"] == "embed-v1"
        assert item.metadata["cyrene.embedding.dimensions"] == "3"
        assert (
            reopened.recall(
                tenant_id="tenant-a",
                query_embedding=(1.0, 0.0, 0.0),
                scope=None,
                subject=None,
                metadata_filters={},
                min_similarity=0.9,
                top_k=1,
            )[0].item.item_id
            == "persisted"
        )
    finally:
        reopened.close()


def test_populated_database_rejects_model_or_preprocessing_profile_drift(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profile.sqlite3"
    active = _store(
        path,
        model="e5-query-passage-v1",
        provider_model="intfloat/e5-small-v2",
        document_prefix="passage: ",
        query_prefix="query: ",
    )
    active.store(
        "tenant-a",
        _item(
            "fact",
            metadata={
                "cyrene.embedding.model_id": "e5-query-passage-v1",
                "cyrene.embedding.dimensions": "3",
                "cyrene.embedding.provider_model_id": "intfloat/e5-small-v2",
            },
        ),
    )
    active.close()

    with pytest.raises(ValueError, match="vector profile changed"):
        _store(
            path,
            model="e5-query-passage-v1",
            provider_model="intfloat/e5-small-v2",
            document_prefix="passage: ",
            query_prefix="search: ",
        )
    with pytest.raises(ValueError, match="vector profile changed"):
        _store(
            path,
            model="another-profile",
            provider_model="another-model",
        )


def test_zero_vectors_and_wrong_embedding_profiles_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path / "memory.sqlite3")
    try:
        with pytest.raises(ValueError, match="zero vectors"):
            store.store("tenant-a", _item("zero", embedding=(0.0, 0.0, 0.0)))
        with pytest.raises(MemoryStoreError, match="dimension"):
            store.store("tenant-a", _item("short", embedding=(1.0, 0.0)))
        with pytest.raises(MemoryStoreError, match="model"):
            store.store(
                "tenant-a",
                _item(
                    "foreign-profile",
                    metadata={"cyrene.embedding.model_id": "another-model"},
                ),
            )
        with pytest.raises(MemoryStoreError, match="does not match"):
            store.store("tenant-a", _item("mismatch", tenant="tenant-b"))
        with pytest.raises(ValueError, match="float32"):
            store.store(
                "tenant-a",
                _item("large-float", embedding=(1.0e50, 0.0, 0.0)),
            )
        with pytest.raises(ValueError, match="float32 encoding"):
            store.store(
                "tenant-a",
                _item("underflow", embedding=(1.0e-50, 0.0, 0.0)),
            )
        with pytest.raises(ValueError, match="zero vectors"):
            store.recall(
                tenant_id="tenant-a",
                query_embedding=(0.0, 0.0, 0.0),
                scope=None,
                subject=None,
                metadata_filters={},
                min_similarity=0,
                top_k=1,
            )
    finally:
        store.close()


def test_import_is_atomic_idempotent_and_preserves_same_text_as_distinct_items(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "memory.sqlite3")
    try:
        first = replace(_item("source-one"), content="same source text")
        second = replace(_item("source-two"), content="same source text")
        invalid = _item("invalid", embedding=(0.0, 0.0, 0.0))
        with pytest.raises(MemoryStoreError, match="must declare"):
            store.import_batch(
                "tenant-a", [replace(first, metadata={})], overwrite=False
            )
        with pytest.raises(ValueError, match="zero vectors"):
            store.import_batch("tenant-a", [first, invalid], overwrite=False)
        assert store.get("tenant-a", "source-one") is None

        assert store.import_batch("tenant-a", [first, second], overwrite=False) == 2
        assert store.import_batch("tenant-a", [first, second], overwrite=False) == 0
        exported, _ = store.export_batch("tenant-a", None, 10, None)
        assert {item.item_id for item in exported} == {"source-one", "source-two"}
        assert len({item.content for item in exported}) == 1

        with pytest.raises(MemoryStoreError, match="tenant_id"):
            store.import_batch(
                "tenant-a", [_item("wrong-tenant", tenant="tenant-b")], overwrite=False
            )
    finally:
        store.close()


def test_export_uses_bounded_scope_bound_keyset_pages(tmp_path: Path) -> None:
    store = _store(tmp_path / "memory.sqlite3")
    try:
        for item_id in ("a", "b", "c", "d"):
            store.store("tenant-a", _item(item_id))
        store.store("tenant-a", _item("other-scope", scope="profile"))

        first, cursor = store.export_batch("tenant-a", "conversation", 2, None)
        second, end_cursor = store.export_batch("tenant-a", "conversation", 2, cursor)
        assert [item.item_id for item in first] == ["a", "b"]
        assert [item.item_id for item in second] == ["c", "d"]
        assert cursor
        assert end_cursor == ""
        with pytest.raises(ValueError, match="scope"):
            store.export_batch("tenant-a", "profile", 2, cursor)
        with pytest.raises(ValueError, match="tenant"):
            store.export_batch("tenant-b", "conversation", 2, cursor)
    finally:
        store.close()


def test_prune_removes_only_expired_or_old_records_for_the_requested_tenant(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "memory.sqlite3")
    try:
        store.store("tenant-a", _item("expired", expires=1_700_000_000_000))
        store.store("tenant-a", _item("old", created=1_700_000_000_000))
        store.store(
            "tenant-b",
            _item("other-tenant", tenant="tenant-b", expires=1_700_000_000_000),
        )

        assert store.prune("tenant-a", None) == 1
        assert store.prune("tenant-a", 1_750_000_000_000) == 1
        assert store.get("tenant-b", "other-tenant") is None
    finally:
        store.close()


def test_database_path_rejects_symlink_targets(tmp_path: Path) -> None:
    target = tmp_path / "actual.sqlite3"
    target.touch()
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic link"):
        _store(linked)
