"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 store.py                                                         │
│  Module: memory_pgvector_provider                                    │
│  Role: PostgreSQL pool, schema migration, and exact pgvector queries. │
│                                                                      │
│  模块职责：PostgreSQL 连接池、schema 迁移和精确 pgvector 查询。       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import struct
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

from .settings import ProviderSettings

MAX_CONTENT_BYTES = 1_048_576
MAX_METADATA_BYTES = 65_536
MAX_METADATA_ENTRIES = 256
MAX_ITEM_ID_BYTES = 256
MAX_BATCH_ITEMS = 1_000
MAX_TOP_K = 1_000
SCHEMA_VERSION = 3
_MIGRATION_LOCK_KEY = 0x435952454E454D45
_FETCH_SIZE = 256
_MODEL_ID_KEY = "cyrene.embedding.model_id"
_DIMENSIONS_KEY = "cyrene.embedding.dimensions"
_PROVIDER_MODEL_ID_KEY = "cyrene.embedding.provider_model_id"


class MemoryStoreError(ValueError):
    """A stable domain failure returned by the memory capability."""

    def __init__(self, code: int, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class StoredMemory:
    """One memory item with its model profile preserved in metadata."""

    item_id: str
    tenant_id: str
    scope: str
    subject: str
    content: str
    embedding: tuple[float, ...]
    metadata: Mapping[str, str]
    created_at_ms: int
    expires_at_ms: int | None
    ttl_seconds: int | None


@dataclass(frozen=True, slots=True)
class ScoredMemory:
    """One recalled item and its normalized cosine similarity."""

    item: StoredMemory
    score: float


class PgvectorMemoryStore:
    """Persist memory records in PostgreSQL and query exact cosine distances."""

    def __init__(self, settings: ProviderSettings) -> None:
        self._settings = settings
        self._pool: ConnectionPool | None = None
        self._profile_fingerprint = _profile_fingerprint(settings)
        self._initialize_pool()

    def close(self, timeout: float = 5) -> None:
        """Stop and close the connection pool after the runtime shuts down."""

        if self._pool is not None:
            self._pool.close(timeout=timeout)
            self._pool = None

    def store(self, tenant_id: str, item: StoredMemory) -> str:
        """Insert or replace one item, enforcing request and item tenant equality."""

        tenant = _required_tenant(tenant_id)
        record = _normalize_item(
            item,
            expected_tenant=tenant,
            expected_model_id=self._settings.vector_profile_id,
            expected_dimensions=self._settings.embedding_dimensions,
            expected_provider_model_id=self._settings.embedding_model_id,
        )
        vector = _validate_vector(record.embedding, self._settings.embedding_dimensions)
        metadata = _encode_metadata(record.metadata)
        now_ms = _now_ms()
        created_at_ms = record.created_at_ms or now_ms
        expires_at_ms = _effective_expiry(created_at_ms, record.expires_at_ms, record.ttl_seconds)
        try:
            with self._require_pool().connection() as connection:
                connection.execute(
                    """
                    INSERT INTO cyrene_memory_records (
                        tenant_id, item_id, scope, subject, content, embedding,
                        embedding_model_id, embedding_dimensions, metadata_json,
                        created_at_ms, expires_at_ms, ttl_seconds
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, item_id) DO UPDATE SET
                        scope = EXCLUDED.scope,
                        subject = EXCLUDED.subject,
                        content = EXCLUDED.content,
                        embedding = EXCLUDED.embedding,
                        embedding_model_id = EXCLUDED.embedding_model_id,
                        embedding_dimensions = EXCLUDED.embedding_dimensions,
                        metadata_json = EXCLUDED.metadata_json,
                        created_at_ms = EXCLUDED.created_at_ms,
                        expires_at_ms = EXCLUDED.expires_at_ms,
                        ttl_seconds = EXCLUDED.ttl_seconds
                    """,
                    (
                        record.tenant_id,
                        record.item_id,
                        record.scope,
                        record.subject,
                        record.content,
                        Vector(list(vector)),
                        self._settings.vector_profile_id,
                        self._settings.embedding_dimensions,
                        Jsonb(metadata),
                        created_at_ms,
                        expires_at_ms,
                        record.ttl_seconds,
                    ),
                )
        except (psycopg.Error, PoolTimeout) as error:
            raise _storage_error("could not store memory item", retryable=True) from error
        return record.item_id

    def get(self, tenant_id: str, item_id: str) -> StoredMemory | None:
        """Return an unexpired tenant-owned item."""

        tenant = _required_tenant(tenant_id)
        identifier = _required_text(item_id, "item_id", MAX_ITEM_ID_BYTES)
        try:
            with self._require_pool().connection() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM cyrene_memory_records
                    WHERE tenant_id = %s AND item_id = %s
                      AND (expires_at_ms IS NULL OR expires_at_ms > %s)
                    """,
                    (tenant, identifier, _now_ms()),
                ).fetchone()
        except (psycopg.Error, PoolTimeout) as error:
            raise _storage_error("could not read memory item", retryable=True) from error
        return (
            None
            if row is None
            else _record_from_row(row, default_provider_model_id=self._settings.embedding_model_id)
        )

    def recall(
        self,
        *,
        tenant_id: str,
        query_embedding: Sequence[float],
        scope: str | None,
        subject: str | None,
        metadata_filters: Mapping[str, str],
        min_similarity: float,
        top_k: int,
        cancellation: Any | None = None,
    ) -> list[ScoredMemory]:
        """Filter records in SQL before pgvector computes and orders cosine scores."""

        tenant = _required_tenant(tenant_id)
        query = _validate_vector(query_embedding, self._settings.embedding_dimensions)
        threshold = _bounded_similarity(min_similarity)
        limit = _bounded_integer(top_k, "top_k", 1, self._settings.max_top_k)
        filters = _validated_metadata(metadata_filters)
        clauses = [
            "tenant_id = %s",
            "embedding_model_id = %s",
            "embedding_dimensions = %s",
            "(expires_at_ms IS NULL OR expires_at_ms > %s)",
        ]
        parameters: list[Any] = [
            tenant,
            self._settings.vector_profile_id,
            self._settings.embedding_dimensions,
            _now_ms(),
        ]
        if scope is not None:
            clauses.append("scope = %s")
            parameters.append(scope)
        if subject is not None:
            clauses.append("subject = %s")
            parameters.append(subject)
        if filters:
            clauses.append("metadata_json @> %s")
            parameters.append(Jsonb(filters))
        vector = Vector(list(query))
        score = "1.0 - ((embedding <=> %s) / 2.0)"
        sql = f"""
            WITH eligible AS (
                SELECT * FROM cyrene_memory_records
                WHERE {" AND ".join(clauses)}
            )
            SELECT *, {score} AS similarity_score
            FROM eligible
            WHERE {score} >= %s
            ORDER BY embedding <=> %s, item_id
            LIMIT %s
            """
        parameters.extend((vector, vector, threshold, vector, limit))
        results: list[ScoredMemory] = []
        try:
            with (
                self._require_pool().connection() as connection,
                connection.transaction(),
            ):
                cursor_name = f"cyrene_memory_recall_{uuid.uuid4().hex}"
                with connection.cursor(name=cursor_name) as cursor:
                    stop_watcher = threading.Event()
                    watcher = None
                    if cancellation is not None:
                        watcher = threading.Thread(
                            target=_cancel_query_when_requested,
                            args=(connection, cancellation, stop_watcher),
                            name="cyrene-pgvector-cancel",
                            daemon=True,
                        )
                        watcher.start()
                    try:
                        cursor.execute(sql, parameters)
                        while True:
                            rows = cursor.fetchmany(
                                min(self._settings.recall_batch_size, _FETCH_SIZE)
                            )
                            if not rows:
                                break
                            if _cancelled(cancellation):
                                raise MemoryStoreError(5, "recall was cancelled", retryable=True)
                            results.extend(
                                ScoredMemory(
                                    item=_record_from_row(
                                        row,
                                        default_provider_model_id=self._settings.embedding_model_id,
                                    ),
                                    score=float(row["similarity_score"]),
                                )
                                for row in rows
                            )
                    finally:
                        stop_watcher.set()
                        if watcher is not None:
                            watcher.join(timeout=1)
        except MemoryStoreError:
            raise
        except (psycopg.Error, PoolTimeout) as error:
            raise _storage_error("could not recall memories", retryable=True) from error
        return results

    def delete(self, tenant_id: str, item_id: str) -> bool:
        """Delete one item by tenant and item ID."""

        tenant = _required_tenant(tenant_id)
        identifier = _required_text(item_id, "item_id", MAX_ITEM_ID_BYTES)
        try:
            with self._require_pool().connection() as connection:
                result = connection.execute(
                    "DELETE FROM cyrene_memory_records WHERE tenant_id = %s AND item_id = %s",
                    (tenant, identifier),
                )
        except (psycopg.Error, PoolTimeout) as error:
            raise _storage_error("could not delete memory item", retryable=True) from error
        return result.rowcount > 0

    def prune(self, tenant_id: str, before_timestamp_ms: int | None) -> int:
        """Remove expired records and optional older tenant records atomically."""

        tenant = _required_tenant(tenant_id)
        now = _now_ms()
        if before_timestamp_ms is None:
            predicate = "expires_at_ms IS NOT NULL AND expires_at_ms <= %s"
            parameters: tuple[Any, ...] = (tenant, now)
        else:
            if before_timestamp_ms < 0:
                raise ValueError("before_timestamp_ms must be nonnegative")
            predicate = "(expires_at_ms IS NOT NULL AND expires_at_ms <= %s) OR created_at_ms < %s"
            parameters = (tenant, now, before_timestamp_ms)
        try:
            with self._require_pool().connection() as connection:
                row = connection.execute(
                    f"""
                    WITH deleted AS (
                        DELETE FROM cyrene_memory_records
                        WHERE tenant_id = %s AND ({predicate})
                        RETURNING 1
                    )
                    SELECT count(*) AS pruned_count FROM deleted
                    """,
                    parameters,
                ).fetchone()
        except (psycopg.Error, PoolTimeout) as error:
            raise _storage_error("could not prune memory items", retryable=True) from error
        return int(row["pruned_count"])

    def export_batch(
        self,
        tenant_id: str,
        scope: str | None,
        limit: int | None,
        page_token: str | None,
    ) -> tuple[list[StoredMemory], str]:
        """Return a bounded keyset page bound to its tenant and scope."""

        tenant = _required_tenant(tenant_id)
        page_size = (
            self._settings.max_top_k
            if limit is None
            else _bounded_integer(limit, "limit", 1, self._settings.max_top_k)
        )
        after_id = _decode_page_token(page_token, tenant, scope)
        clauses = ["tenant_id = %s", "(expires_at_ms IS NULL OR expires_at_ms > %s)"]
        parameters: list[Any] = [tenant, _now_ms()]
        if scope is not None:
            clauses.append("scope = %s")
            parameters.append(scope)
        if after_id is not None:
            clauses.append("item_id > %s")
            parameters.append(after_id)
        parameters.append(page_size + 1)
        try:
            with self._require_pool().connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM cyrene_memory_records WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY item_id LIMIT %s",
                    parameters,
                ).fetchall()
        except (psycopg.Error, PoolTimeout) as error:
            raise _storage_error("could not export memory batch", retryable=True) from error
        has_more = len(rows) > page_size
        selected = rows[:page_size]
        next_token = (
            _encode_page_token(str(selected[-1]["item_id"]), tenant, scope)
            if has_more and selected
            else ""
        )
        return [
            _record_from_row(row, default_provider_model_id=self._settings.embedding_model_id)
            for row in selected
        ], next_token

    def import_batch(
        self,
        tenant_id: str,
        items: Sequence[StoredMemory],
        *,
        overwrite: bool,
    ) -> int:
        """Import a bounded set in one database transaction, keyed by tenant/item ID."""

        tenant = _required_tenant(tenant_id)
        if len(items) > MAX_BATCH_ITEMS:
            raise MemoryStoreError(4, f"import batch exceeds {MAX_BATCH_ITEMS} items")
        normalized: list[StoredMemory] = []
        for item in items:
            if not {_MODEL_ID_KEY, _DIMENSIONS_KEY, _PROVIDER_MODEL_ID_KEY}.issubset(
                item.metadata
            ):
                raise MemoryStoreError(
                    3,
                    "import vectors must declare their model profile, provider model, "
                    "and dimensions",
                )
            model_id, dimensions = _profile_from_metadata(
                item.metadata,
                default_model_id=self._settings.vector_profile_id,
            )
            if dimensions is None:
                dimensions = len(item.embedding)
            _bounded_integer(dimensions, "embedding_dimensions", 1, 4096)
            provider_model_id = item.metadata.get(
                _PROVIDER_MODEL_ID_KEY, self._settings.embedding_model_id
            )
            if (
                model_id != self._settings.vector_profile_id
                or dimensions != self._settings.embedding_dimensions
                or provider_model_id != self._settings.embedding_model_id
            ):
                raise MemoryStoreError(
                    3,
                    "import vectors must be re-embedded for the active database profile",
                )
            normalized.append(
                _normalize_item(
                    _with_profile_metadata(item, model_id, dimensions),
                    expected_tenant=tenant,
                    expected_model_id=self._settings.vector_profile_id,
                    expected_dimensions=self._settings.embedding_dimensions,
                    expected_provider_model_id=self._settings.embedding_model_id,
                )
            )
        identities = [item.item_id for item in normalized]
        if len(identities) != len(set(identities)):
            raise ValueError("import batch contains duplicate item_id values")
        now_ms = _now_ms()
        model_identities: list[tuple[str, int, StoredMemory]] = []
        for item in normalized:
            model_id, dimensions = _profile_from_metadata(
                item.metadata,
                default_model_id=self._settings.vector_profile_id,
            )
            if dimensions != len(item.embedding):
                raise MemoryStoreError(3, "embedding vector and profile dimensions do not match")
            model_identities.append((model_id, dimensions, item))
        insert = """
            INSERT INTO cyrene_memory_records (
                tenant_id, item_id, scope, subject, content, embedding,
                embedding_model_id, embedding_dimensions, metadata_json,
                created_at_ms, expires_at_ms, ttl_seconds
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
        if overwrite:
            insert += """
                ON CONFLICT (tenant_id, item_id) DO UPDATE SET
                    scope = EXCLUDED.scope,
                    subject = EXCLUDED.subject,
                    content = EXCLUDED.content,
                    embedding = EXCLUDED.embedding,
                    embedding_model_id = EXCLUDED.embedding_model_id,
                    embedding_dimensions = EXCLUDED.embedding_dimensions,
                    metadata_json = EXCLUDED.metadata_json,
                    created_at_ms = EXCLUDED.created_at_ms,
                    expires_at_ms = EXCLUDED.expires_at_ms,
                    ttl_seconds = EXCLUDED.ttl_seconds
                """
        else:
            insert += "ON CONFLICT (tenant_id, item_id) DO NOTHING"
        inserted = 0
        try:
            with (
                self._require_pool().connection() as connection,
                connection.transaction(),
            ):
                for model_id, dimensions, item in model_identities:
                    vector = _validate_vector(item.embedding, dimensions)
                    created_at = item.created_at_ms or now_ms
                    expires_at = _effective_expiry(created_at, item.expires_at_ms, item.ttl_seconds)
                    result = connection.execute(
                        insert,
                        (
                            tenant,
                            item.item_id,
                            item.scope,
                            item.subject,
                            item.content,
                            Vector(list(vector)),
                            model_id,
                            dimensions,
                            Jsonb(_encode_metadata(item.metadata)),
                            created_at,
                            expires_at,
                            item.ttl_seconds,
                        ),
                    )
                    inserted += max(0, result.rowcount)
        except MemoryStoreError:
            raise
        except (psycopg.Error, PoolTimeout) as error:
            raise _storage_error("could not import memory batch", retryable=True) from error
        return inserted

    def _initialize_pool(self) -> None:
        """Install pgvector before type registration, then open and migrate the pool."""

        statement_timeout_ms = max(100, math.ceil(self._settings.statement_timeout_seconds * 1000))
        connection_options = f"-c statement_timeout={statement_timeout_ms}"
        try:
            with psycopg.connect(
                self._settings.database_dsn,
                connect_timeout=max(1, math.ceil(self._settings.connection_timeout_seconds)),
                options=connection_options,
            ) as connection:
                connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            self._pool = ConnectionPool(
                conninfo=self._settings.database_dsn,
                min_size=self._settings.pool_min_size,
                max_size=self._settings.pool_max_size,
                timeout=self._settings.connection_timeout_seconds,
                kwargs={
                    "connect_timeout": max(1, math.ceil(self._settings.connection_timeout_seconds)),
                    "row_factory": dict_row,
                    "options": connection_options,
                },
                configure=self._configure_connection,
                open=True,
                name="cyrene-memory-pgvector",
            )
            self._pool.wait(timeout=self._settings.connection_timeout_seconds)
            self._migrate()
        except ValueError:
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            raise
        except Exception:
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            raise ValueError("PostgreSQL pgvector backend initialization failed") from None

    @staticmethod
    def _configure_connection(connection: psycopg.Connection[Any]) -> None:
        """Register pgvector values on each pooled connection and clear setup work."""

        register_vector(connection)
        connection.commit()

    def _migrate(self) -> None:
        """Apply the schema atomically under one PostgreSQL advisory lock."""

        try:
            with (
                self._require_pool().connection() as connection,
                connection.transaction(),
            ):
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cyrene_memory_schema (
                        singleton SMALLINT PRIMARY KEY CHECK (singleton = 1),
                        version SMALLINT NOT NULL,
                        profile_fingerprint TEXT
                    )
                    """
                )
                connection.execute(
                    "ALTER TABLE cyrene_memory_schema "
                    "ADD COLUMN IF NOT EXISTS profile_fingerprint TEXT"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cyrene_memory_records (
                        tenant_id TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        content TEXT NOT NULL,
                        embedding vector NOT NULL,
                        embedding_model_id TEXT NOT NULL,
                        embedding_dimensions INTEGER NOT NULL CHECK (
                            embedding_dimensions BETWEEN 1 AND 4096
                        ),
                        metadata_json JSONB NOT NULL,
                        created_at_ms BIGINT NOT NULL,
                        expires_at_ms BIGINT,
                        ttl_seconds BIGINT CHECK (ttl_seconds IS NULL OR ttl_seconds >= 0),
                        PRIMARY KEY (tenant_id, item_id)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS cyrene_memory_tenant_scope "
                    "ON cyrene_memory_records(tenant_id, scope)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS cyrene_memory_tenant_subject "
                    "ON cyrene_memory_records(tenant_id, subject)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS cyrene_memory_tenant_expiry "
                    "ON cyrene_memory_records(tenant_id, expires_at_ms)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS cyrene_memory_profile "
                    "ON cyrene_memory_records(tenant_id, embedding_model_id, "
                    "embedding_dimensions)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS cyrene_memory_metadata "
                    "ON cyrene_memory_records USING GIN (metadata_json jsonb_path_ops)"
                )
                row = connection.execute(
                    "SELECT version, profile_fingerprint FROM cyrene_memory_schema "
                    "WHERE singleton = 1"
                ).fetchone()
                if row is not None and int(row["version"]) > SCHEMA_VERSION:
                    raise ValueError("memory database schema is newer than this provider")
                if row is None or int(row["version"]) < SCHEMA_VERSION:
                    connection.execute(
                        "ALTER TABLE cyrene_memory_records DROP CONSTRAINT IF EXISTS "
                        "cyrene_memory_records_embedding_dimensions_check"
                    )
                    connection.execute(
                        "ALTER TABLE cyrene_memory_records ADD CONSTRAINT "
                        "cyrene_memory_records_embedding_dimensions_check "
                        "CHECK (embedding_dimensions BETWEEN 1 AND 4096)"
                    )
                existing_profiles = connection.execute(
                    "SELECT DISTINCT embedding_model_id, embedding_dimensions "
                    "FROM cyrene_memory_records"
                ).fetchall()
                if any(
                    str(profile["embedding_model_id"]) != self._settings.vector_profile_id
                    or int(profile["embedding_dimensions"]) != self._settings.embedding_dimensions
                    for profile in existing_profiles
                ):
                    raise ValueError(
                        "existing PostgreSQL memories use another vector profile; "
                        "use a fresh database"
                    )
                has_fingerprint = bool(row and row["profile_fingerprint"])
                if (
                    existing_profiles
                    and not has_fingerprint
                    and (
                        self._settings.embedding_document_prefix
                        or self._settings.embedding_query_prefix
                        or self._settings.vector_profile_id != self._settings.embedding_model_id
                    )
                ):
                    raise ValueError(
                        "existing memories have no complete profile fingerprint; "
                        "use a fresh database"
                    )
                provider_models = connection.execute(
                    "SELECT DISTINCT metadata_json ->> 'cyrene.embedding.provider_model_id' "
                    "AS provider_model_id FROM cyrene_memory_records "
                    "WHERE metadata_json ? 'cyrene.embedding.provider_model_id'"
                ).fetchall()
                if any(
                    str(item["provider_model_id"]) != self._settings.embedding_model_id
                    for item in provider_models
                ):
                    raise ValueError(
                        "existing PostgreSQL memories use another provider model; "
                        "use a fresh database"
                    )
                if (
                    has_fingerprint
                    and str(row["profile_fingerprint"]) != self._profile_fingerprint
                ):
                    count = int(
                        connection.execute(
                            "SELECT count(*) AS count FROM cyrene_memory_records"
                        ).fetchone()["count"]
                    )
                    if count:
                        raise ValueError(
                            "database vector profile changed; create and verify a fresh "
                            "database before switching"
                        )
                connection.execute(
                    "INSERT INTO cyrene_memory_schema(singleton, version, profile_fingerprint) "
                    "VALUES (1, %s, %s) ON CONFLICT (singleton) DO UPDATE SET "
                    "version = EXCLUDED.version, "
                    "profile_fingerprint = EXCLUDED.profile_fingerprint",
                    (SCHEMA_VERSION, self._profile_fingerprint),
                )
        except psycopg.Error as error:
            raise _storage_error("could not migrate PostgreSQL memory schema") from error

    def _require_pool(self) -> ConnectionPool:
        if self._pool is None:
            raise MemoryStoreError(5, "PostgreSQL memory pool is unavailable", retryable=True)
        return self._pool


def _normalize_item(
    item: StoredMemory,
    *,
    expected_tenant: str,
    expected_model_id: str | None = None,
    expected_dimensions: int | None = None,
    expected_provider_model_id: str | None = None,
) -> StoredMemory:
    tenant = _required_tenant(item.tenant_id)
    expected_tenant = _required_tenant(expected_tenant)
    if tenant != expected_tenant:
        raise MemoryStoreError(2, "item tenant_id does not match request tenant_id")
    item_id = item.item_id.strip() or f"mem_{uuid.uuid4().hex}"
    _required_text(item_id, "item_id", MAX_ITEM_ID_BYTES)
    if not item.content or len(item.content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError("content must be non-empty and within the 1 MiB limit")
    metadata = _validated_metadata(item.metadata)
    if not {_MODEL_ID_KEY, _DIMENSIONS_KEY, _PROVIDER_MODEL_ID_KEY}.issubset(metadata):
        raise MemoryStoreError(
            3,
            "vector metadata must declare its model profile, provider model, and dimensions",
        )
    model_id, dimensions = _profile_from_metadata(
        metadata,
        default_model_id=expected_model_id,
    )
    if dimensions is None:
        dimensions = expected_dimensions or len(item.embedding)
    _bounded_integer(dimensions, "embedding_dimensions", 1, 4096)
    if expected_model_id is not None and model_id != expected_model_id:
        raise MemoryStoreError(3, "embedding model does not match the configured profile")
    if expected_dimensions is not None and dimensions != expected_dimensions:
        raise MemoryStoreError(3, "embedding dimension does not match the configured profile")
    provider_model_id = metadata.get(_PROVIDER_MODEL_ID_KEY, expected_provider_model_id or model_id)
    provider_model_id = _required_text(provider_model_id, "provider_model_id", 256)
    if expected_provider_model_id is not None and provider_model_id != expected_provider_model_id:
        raise MemoryStoreError(3, "embedding provider model does not match the configured profile")
    if dimensions != len(item.embedding):
        raise MemoryStoreError(3, "embedding vector and profile dimensions do not match")
    metadata = _with_profile_metadata(item, model_id, dimensions, provider_model_id).metadata
    if item.ttl_seconds is not None and item.ttl_seconds < 0:
        raise ValueError("ttl_seconds must be nonnegative")
    if item.expires_at_ms is not None and item.expires_at_ms < 0:
        raise ValueError("expires_at_ms must be nonnegative")
    return StoredMemory(
        item_id=item_id,
        tenant_id=tenant,
        scope=_bounded_optional_text(item.scope, "scope", 256),
        subject=_bounded_optional_text(item.subject, "subject", 256),
        content=item.content,
        embedding=tuple(float(value) for value in item.embedding),
        metadata=metadata,
        created_at_ms=max(0, int(item.created_at_ms)),
        expires_at_ms=item.expires_at_ms,
        ttl_seconds=item.ttl_seconds,
    )


def _record_from_row(
    row: Mapping[str, Any], *, default_provider_model_id: str | None = None
) -> StoredMemory:
    vector = row["embedding"]
    if not isinstance(vector, Vector):
        raise MemoryStoreError(5, "database returned an invalid pgvector value")
    embedding = tuple(float(value) for value in vector.to_list())
    dimensions = int(row["embedding_dimensions"])
    model_id = str(row["embedding_model_id"])
    if len(embedding) != dimensions or any(not math.isfinite(value) for value in embedding):
        raise MemoryStoreError(5, "database returned an invalid embedding profile")
    metadata_value = row["metadata_json"]
    metadata = _validated_metadata(metadata_value)
    metadata.setdefault(_MODEL_ID_KEY, model_id)
    metadata.setdefault(_DIMENSIONS_KEY, str(dimensions))
    if default_provider_model_id is not None:
        metadata.setdefault(_PROVIDER_MODEL_ID_KEY, default_provider_model_id)
    return StoredMemory(
        item_id=str(row["item_id"]),
        tenant_id=str(row["tenant_id"]),
        scope=str(row["scope"]),
        subject=str(row["subject"]),
        content=str(row["content"]),
        embedding=embedding,
        metadata=metadata,
        created_at_ms=int(row["created_at_ms"]),
        expires_at_ms=None if row["expires_at_ms"] is None else int(row["expires_at_ms"]),
        ttl_seconds=None if row["ttl_seconds"] is None else int(row["ttl_seconds"]),
    )


def _validate_vector(values: Sequence[float], dimensions: int) -> tuple[float, ...]:
    _bounded_integer(dimensions, "embedding_dimensions", 1, 4096)
    if len(values) != dimensions:
        raise MemoryStoreError(
            3,
            f"embedding dimension mismatch: expected {dimensions}, got {len(values)}",
        )
    vector = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in vector):
        raise ValueError("embedding values must be finite")
    if any(abs(value) > 3.4028234663852886e38 for value in vector):
        raise ValueError("embedding values exceed the float32 range")
    if not any(vector):
        raise ValueError("zero vectors are invalid; provide a real model embedding")
    encoded = struct.pack(f"<{len(vector)}f", *vector)
    quantized = struct.unpack(f"<{len(vector)}f", encoded)
    if any(not math.isfinite(value) for value in quantized) or not any(quantized):
        raise ValueError("embedding becomes invalid after float32 encoding")
    return tuple(quantized)


def _profile_from_metadata(
    metadata: Mapping[str, str], *, default_model_id: str | None
) -> tuple[str, int | None]:
    model_id = metadata.get(_MODEL_ID_KEY, default_model_id)
    if model_id is None:
        raise ValueError("embedding model profile is required")
    model_id = _required_text(model_id, "embedding model profile", 256)
    raw_dimensions = metadata.get(_DIMENSIONS_KEY)
    dimensions = None
    if raw_dimensions is not None:
        try:
            dimensions = int(raw_dimensions)
        except ValueError as error:
            raise ValueError("embedding dimensions metadata must be an integer") from error
        _bounded_integer(dimensions, _DIMENSIONS_KEY, 1, 4096)
    return model_id, dimensions


def _with_profile_metadata(
    item: StoredMemory,
    model_id: str,
    dimensions: int,
    provider_model_id: str | None = None,
) -> StoredMemory:
    metadata = dict(item.metadata)
    metadata[_MODEL_ID_KEY] = model_id
    metadata[_DIMENSIONS_KEY] = str(dimensions)
    metadata[_PROVIDER_MODEL_ID_KEY] = provider_model_id or metadata.get(
        _PROVIDER_MODEL_ID_KEY, model_id
    )
    return StoredMemory(
        item_id=item.item_id,
        tenant_id=item.tenant_id,
        scope=item.scope,
        subject=item.subject,
        content=item.content,
        embedding=item.embedding,
        metadata=metadata,
        created_at_ms=item.created_at_ms,
        expires_at_ms=item.expires_at_ms,
        ttl_seconds=item.ttl_seconds,
    )


def _validated_metadata(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > MAX_METADATA_ENTRIES:
        raise ValueError(f"metadata must be a map with at most {MAX_METADATA_ENTRIES} entries")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 256:
            raise ValueError("metadata keys must be non-empty strings within 256 bytes")
        if not isinstance(item, str) or len(item.encode("utf-8")) > 4096:
            raise ValueError("metadata values must be strings within 4 KiB")
        result[key] = item
    _encode_metadata(result)
    return result


def _encode_metadata(value: Mapping[str, str]) -> dict[str, str]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds the 64 KiB limit")
    return dict(value)


def _effective_expiry(
    created_at_ms: int, expires_at_ms: int | None, ttl_seconds: int | None
) -> int | None:
    if ttl_seconds is None:
        return expires_at_ms
    ttl_expiry = created_at_ms + ttl_seconds * 1000
    return ttl_expiry if expires_at_ms is None else min(expires_at_ms, ttl_expiry)


def _profile_fingerprint(settings: ProviderSettings) -> str:
    payload = json.dumps(
        {
            "document_prefix": settings.embedding_document_prefix,
            "dimensions": settings.embedding_dimensions,
            "profile_id": settings.vector_profile_id,
            "provider_model_id": settings.embedding_model_id,
            "query_prefix": settings.embedding_query_prefix,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cancel_query_when_requested(
    connection: psycopg.Connection[Any], cancellation: Any, stop: threading.Event
) -> None:
    """Interrupt a blocking PostgreSQL query when the host cancels its invocation."""

    while not stop.wait(0.01):
        if _cancelled(cancellation):
            try:
                connection.cancel_safe(timeout=1)
            except psycopg.Error:
                return
            return


def _encode_page_token(item_id: str, tenant_id: str, scope: str | None) -> str:
    payload = json.dumps(
        {"after": item_id, "scope": scope, "tenant": tenant_id}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_page_token(value: str | None, tenant_id: str, scope: str | None) -> str | None:
    if value is None or value == "":
        return None
    if len(value) > 1024:
        raise ValueError("page_token exceeds its length limit")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(
            base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        )
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise ValueError("page_token is invalid") from error
    if (
        not isinstance(decoded, dict)
        or decoded.get("tenant") != tenant_id
        or decoded.get("scope") != scope
    ):
        raise ValueError("page_token does not match the export tenant and scope")
    return _required_text(decoded.get("after"), "page_token.after", MAX_ITEM_ID_BYTES)


def _required_text(value: Any, name: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{name} exceeds its byte limit")
    return value.strip()


def _required_tenant(value: str) -> str:
    try:
        return _required_text(value, "tenant_id", 256)
    except ValueError as error:
        raise MemoryStoreError(2, "tenant_id must be non-empty text") from error


def _bounded_integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _bounded_similarity(value: float) -> float:
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError("min_similarity must be finite and between 0 and 1")
    return result


def _bounded_optional_text(value: str, name: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{name} must be text within {maximum_bytes} bytes")
    return value


def _storage_error(message: str, *, retryable: bool = True) -> MemoryStoreError:
    return MemoryStoreError(5, message, retryable=retryable)


def _cancelled(value: Any | None) -> bool:
    if value is None:
        return False
    checker = getattr(value, "is_cancelled", None)
    return bool(checker()) if callable(checker) else bool(value.is_set())


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
