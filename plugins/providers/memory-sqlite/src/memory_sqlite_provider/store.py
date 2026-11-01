"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 store.py                                                         │
│  Module: memory_sqlite_provider                                      │
│  Role: Durable SQLite records and bounded tenant-scoped vector scan. │
│                                                                      │
│  模块职责：持久 SQLite 记忆记录与有界、租户隔离的向量扫描。            │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import base64
import binascii
import heapq
import json
import math
import os
import sqlite3
import struct
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_CONTENT_BYTES = 1_048_576
MAX_METADATA_BYTES = 65_536
MAX_METADATA_ENTRIES = 256
MAX_ITEM_ID_BYTES = 256
MAX_BATCH_ITEMS = 1_000
DEFAULT_BATCH_SIZE = 256
MAX_TOP_K = 1_000
MODEL_ID_KEY = "cyrene.embedding.model_id"
DIMENSIONS_KEY = "cyrene.embedding.dimensions"
SCHEMA_VERSION = 1
_FETCH_SIZE = 256


class MemoryStoreError(ValueError):
    """A stable domain failure returned by the memory capability."""

    def __init__(self, code: int, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class StoredMemory:
    """One memory item with its embedding profile kept as storage metadata."""

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


class SQLiteMemoryStore:
    """Persist memories in one private database file with a fixed model profile.

    The database records each item's model identifier and vector dimension.
    Recall filters tenant, scope, subject, metadata, profile and expiry before
    scoring; it streams rows and retains only the best ``top_k`` candidates.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        embedding_model_id: str,
        embedding_dimensions: int,
        recall_batch_size: int = DEFAULT_BATCH_SIZE,
        max_top_k: int = 100,
    ) -> None:
        self.database_path = _absolute_database_path(database_path)
        self.embedding_model_id = _required_text(embedding_model_id, "embedding_model_id", 256)
        self.embedding_dimensions = _bounded_integer(
            embedding_dimensions, "embedding_dimensions", 1, 4096
        )
        self.recall_batch_size = _bounded_integer(recall_batch_size, "recall_batch_size", 16, 4096)
        self.max_top_k = _bounded_integer(max_top_k, "max_top_k", 1, MAX_TOP_K)
        self._lock = threading.RLock()
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.database_path.parent.stat().st_mode & 0o022:
            raise ValueError("database parent directory must not be group/world writable")
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._migrate()
        _restrict_file_permissions(self.database_path)

    def close(self) -> None:
        """Close the database connection after the host stops this instance."""

        with self._lock:
            self._connection.close()

    def store(self, tenant_id: str, item: StoredMemory) -> str:
        """Insert or replace a record under the tenant and item identity."""

        tenant = _required_tenant(tenant_id)
        record = _normalize_item(
            item,
            expected_tenant=tenant,
            expected_model_id=self.embedding_model_id,
            expected_dimensions=self.embedding_dimensions,
        )
        embedding = _pack_vector(record.embedding, self.embedding_dimensions)
        metadata = _encode_metadata(record.metadata)
        now_ms = _now_ms()
        created_at_ms = record.created_at_ms or now_ms
        expires_at_ms = _effective_expiry(created_at_ms, record.expires_at_ms, record.ttl_seconds)
        with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO memory_records (
                        tenant_id, item_id, scope, subject, content, embedding,
                        embedding_model_id, embedding_dimensions, metadata_json,
                        created_at_ms, expires_at_ms, ttl_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (tenant_id, item_id) DO UPDATE SET
                        scope = excluded.scope,
                        subject = excluded.subject,
                        content = excluded.content,
                        embedding = excluded.embedding,
                        embedding_model_id = excluded.embedding_model_id,
                        embedding_dimensions = excluded.embedding_dimensions,
                        metadata_json = excluded.metadata_json,
                        created_at_ms = excluded.created_at_ms,
                        expires_at_ms = excluded.expires_at_ms,
                        ttl_seconds = excluded.ttl_seconds
                    """,
                    (
                        record.tenant_id,
                        record.item_id,
                        record.scope,
                        record.subject,
                        record.content,
                        embedding,
                        self.embedding_model_id,
                        self.embedding_dimensions,
                        metadata,
                        created_at_ms,
                        expires_at_ms,
                        record.ttl_seconds,
                    ),
                )
            except sqlite3.Error as error:
                raise _storage_error("could not store memory item") from error
        return record.item_id

    def get(self, tenant_id: str, item_id: str) -> StoredMemory | None:
        """Return an unexpired item only when it belongs to the requested tenant."""

        tenant = _required_tenant(tenant_id)
        identifier = _required_text(item_id, "item_id", MAX_ITEM_ID_BYTES)
        with self._lock:
            try:
                row = self._connection.execute(
                    """
                    SELECT * FROM memory_records
                    WHERE tenant_id = ? AND item_id = ? AND
                        (expires_at_ms IS NULL OR expires_at_ms > ?)
                    """,
                    (tenant, identifier, _now_ms()),
                ).fetchone()
            except sqlite3.Error as error:
                raise _storage_error("could not read memory item") from error
        return None if row is None else self._record_from_row(row)

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
        """Stream eligible rows, filter metadata, then retain only bounded top matches."""

        tenant = _required_tenant(tenant_id)
        query = _validated_vector(query_embedding, self.embedding_dimensions)
        threshold = _bounded_similarity(min_similarity)
        limit = _bounded_integer(top_k, "top_k", 1, self.max_top_k)
        filters = _validated_metadata(metadata_filters)
        clauses = [
            "tenant_id = ?",
            "embedding_model_id = ?",
            "embedding_dimensions = ?",
            "(expires_at_ms IS NULL OR expires_at_ms > ?)",
        ]
        parameters: list[Any] = [
            tenant,
            self.embedding_model_id,
            self.embedding_dimensions,
            _now_ms(),
        ]
        if scope is not None:
            clauses.append("scope = ?")
            parameters.append(scope)
        if subject is not None:
            clauses.append("subject = ?")
            parameters.append(subject)
        sql = "SELECT * FROM memory_records WHERE " + " AND ".join(clauses)
        heap: list[tuple[float, int, StoredMemory]] = []
        sequence = 0
        with self._lock:
            try:
                cursor = self._connection.execute(sql, parameters)
                while True:
                    rows = cursor.fetchmany(self.recall_batch_size)
                    if not rows:
                        break
                    for row in rows:
                        if _cancelled(cancellation):
                            raise MemoryStoreError(5, "recall was cancelled", retryable=True)
                        metadata = _decode_metadata(row["metadata_json"])
                        if any(metadata.get(key) != value for key, value in filters.items()):
                            continue
                        item = self._record_from_row(row, metadata=metadata)
                        score = _cosine_similarity(query, item.embedding)
                        if score < threshold:
                            continue
                        sequence += 1
                        candidate = (score, sequence, item)
                        if len(heap) < limit:
                            heapq.heappush(heap, candidate)
                        elif candidate[0] > heap[0][0]:
                            heapq.heapreplace(heap, candidate)
            except sqlite3.Error as error:
                raise _storage_error("could not recall memories") from error
        ranked = sorted(heap, key=lambda candidate: (-candidate[0], candidate[1]))
        return [ScoredMemory(item=item, score=score) for score, _, item in ranked]

    def delete(self, tenant_id: str, item_id: str) -> bool:
        """Delete one tenant-owned item without probing other tenant records."""

        tenant = _required_tenant(tenant_id)
        identifier = _required_text(item_id, "item_id", MAX_ITEM_ID_BYTES)
        with self._lock:
            try:
                result = self._connection.execute(
                    "DELETE FROM memory_records WHERE tenant_id = ? AND item_id = ?",
                    (tenant, identifier),
                )
            except sqlite3.Error as error:
                raise _storage_error("could not delete memory item") from error
        return result.rowcount > 0

    def prune(self, tenant_id: str, before_timestamp_ms: int | None) -> int:
        """Remove expired tenant-owned items and optional older records."""

        tenant = _required_tenant(tenant_id)
        now = _now_ms()
        if before_timestamp_ms is None:
            sql = "DELETE FROM memory_records WHERE tenant_id = ? AND expires_at_ms <= ?"
            params: tuple[Any, ...] = (tenant, now)
        else:
            sql = """
                DELETE FROM memory_records
                WHERE tenant_id = ? AND
                    ((expires_at_ms IS NOT NULL AND expires_at_ms <= ?) OR created_at_ms < ?)
                """
            params = (tenant, now, before_timestamp_ms)
        with self._lock:
            try:
                result = self._connection.execute(sql, params)
            except sqlite3.Error as error:
                raise _storage_error("could not prune memory items") from error
        return max(0, result.rowcount)

    def export_batch(
        self,
        tenant_id: str,
        scope: str | None,
        limit: int | None,
        page_token: str | None,
    ) -> tuple[list[StoredMemory], str]:
        """Read a deterministic, bounded batch for one tenant and optional scope."""

        tenant = _required_tenant(tenant_id)
        maximum = (
            self.max_top_k if limit is None else _bounded_integer(limit, "limit", 1, self.max_top_k)
        )
        clauses = ["tenant_id = ?", "(expires_at_ms IS NULL OR expires_at_ms > ?)"]
        parameters: list[Any] = [tenant, _now_ms()]
        if scope is not None:
            clauses.append("scope = ?")
            parameters.append(scope)
        after_id = _decode_page_token(page_token, tenant, scope)
        if after_id is not None:
            clauses.append("item_id > ?")
            parameters.append(after_id)
        parameters.append(maximum + 1)
        with self._lock:
            try:
                rows = self._connection.execute(
                    "SELECT * FROM memory_records WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY item_id LIMIT ?",
                    parameters,
                ).fetchall()
            except sqlite3.Error as error:
                raise _storage_error("could not export memory batch") from error
        has_more = len(rows) > maximum
        selected = rows[:maximum]
        next_token = (
            _encode_page_token(str(selected[-1]["item_id"]), tenant, scope)
            if has_more and selected
            else ""
        )
        return [self._record_from_row(row) for row in selected], next_token

    def import_batch(
        self,
        tenant_id: str,
        items: Sequence[StoredMemory],
        *,
        overwrite: bool,
    ) -> int:
        """Import one bounded batch atomically and preserve distinct item identities."""

        tenant = _required_tenant(tenant_id)
        if len(items) > MAX_BATCH_ITEMS:
            raise MemoryStoreError(4, f"import batch exceeds {MAX_BATCH_ITEMS} items")
        normalized: list[StoredMemory] = []
        for item in items:
            model_id, dimensions = _profile_from_metadata(
                item.metadata, default_model_id=self.embedding_model_id
            )
            if dimensions is None:
                dimensions = len(item.embedding)
            profiled = _with_profile_metadata(item, model_id, dimensions)
            normalized.append(_normalize_item(profiled, expected_tenant=tenant))
        identities = [item.item_id for item in normalized]
        if len(identities) != len(set(identities)):
            raise ValueError("import batch contains duplicate item_id values")
        now = _now_ms()
        inserted = 0
        with self._lock:
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                for item in normalized:
                    model_id, dimensions = _profile_from_metadata(
                        item.metadata, default_model_id=self.embedding_model_id
                    )
                    if dimensions is None:
                        dimensions = len(item.embedding)
                    item = _with_profile_metadata(item, model_id, dimensions)
                    embedding = _pack_vector(item.embedding, dimensions)
                    metadata = _encode_metadata(item.metadata)
                    created_at = item.created_at_ms or now
                    expires_at = _effective_expiry(created_at, item.expires_at_ms, item.ttl_seconds)
                    conflict_action = (
                        "DO UPDATE SET scope=excluded.scope, subject=excluded.subject, "
                        "content=excluded.content, embedding=excluded.embedding, "
                        "embedding_model_id=excluded.embedding_model_id, "
                        "embedding_dimensions=excluded.embedding_dimensions, "
                        "metadata_json=excluded.metadata_json, "
                        "created_at_ms=excluded.created_at_ms, "
                        "expires_at_ms=excluded.expires_at_ms, ttl_seconds=excluded.ttl_seconds"
                        if overwrite
                        else "DO NOTHING"
                    )
                    result = connection.execute(
                        f"""
                        INSERT INTO memory_records (
                            tenant_id, item_id, scope, subject, content, embedding,
                            embedding_model_id, embedding_dimensions, metadata_json,
                            created_at_ms, expires_at_ms, ttl_seconds
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (tenant_id, item_id) {conflict_action}
                        """,
                        (
                            tenant,
                            item.item_id,
                            item.scope,
                            item.subject,
                            item.content,
                            embedding,
                            model_id,
                            dimensions,
                            metadata,
                            created_at,
                            expires_at,
                            item.ttl_seconds,
                        ),
                    )
                    inserted += max(0, result.rowcount)
                connection.execute("COMMIT")
            except (sqlite3.Error, ValueError, MemoryStoreError) as error:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if isinstance(error, MemoryStoreError):
                    raise
                if isinstance(error, ValueError):
                    raise
                raise _storage_error("could not import memory batch") from error
        return inserted

    def _migrate(self) -> None:
        """Apply the idempotent schema migration under a write lock."""

        with self._lock:
            connection = self._connection
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise ValueError("memory database schema is newer than this provider")
                if version == SCHEMA_VERSION:
                    return
                connection.execute("BEGIN IMMEDIATE")
                statements = (
                    """
                    CREATE TABLE IF NOT EXISTS memory_records (
                        tenant_id TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        content TEXT NOT NULL,
                        embedding BLOB NOT NULL,
                        embedding_model_id TEXT NOT NULL,
                        embedding_dimensions INTEGER NOT NULL,
                        metadata_json TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        expires_at_ms INTEGER,
                        ttl_seconds INTEGER,
                        PRIMARY KEY (tenant_id, item_id)
                    )
                    """,
                    "CREATE INDEX IF NOT EXISTS memory_records_tenant_scope "
                    "ON memory_records(tenant_id, scope)",
                    "CREATE INDEX IF NOT EXISTS memory_records_tenant_subject "
                    "ON memory_records(tenant_id, subject)",
                    "CREATE INDEX IF NOT EXISTS memory_records_tenant_expiry "
                    "ON memory_records(tenant_id, expires_at_ms)",
                    "CREATE INDEX IF NOT EXISTS memory_records_profile "
                    "ON memory_records(tenant_id, embedding_model_id, embedding_dimensions)",
                )
                for statement in statements:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except (sqlite3.Error, ValueError) as error:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if isinstance(error, ValueError):
                    raise
                raise _storage_error("could not migrate memory database") from error

    def _record_from_row(
        self, row: sqlite3.Row, *, metadata: Mapping[str, str] | None = None
    ) -> StoredMemory:
        dimensions = int(row["embedding_dimensions"])
        vector = _unpack_vector(bytes(row["embedding"]), dimensions)
        stored_metadata = (
            _decode_metadata(row["metadata_json"]) if metadata is None else dict(metadata)
        )
        stored_metadata.setdefault(MODEL_ID_KEY, str(row["embedding_model_id"]))
        stored_metadata.setdefault(DIMENSIONS_KEY, str(dimensions))
        return StoredMemory(
            item_id=str(row["item_id"]),
            tenant_id=str(row["tenant_id"]),
            scope=str(row["scope"]),
            subject=str(row["subject"]),
            content=str(row["content"]),
            embedding=vector,
            metadata=stored_metadata,
            created_at_ms=int(row["created_at_ms"]),
            expires_at_ms=None if row["expires_at_ms"] is None else int(row["expires_at_ms"]),
            ttl_seconds=None if row["ttl_seconds"] is None else int(row["ttl_seconds"]),
        )


def _normalize_item(
    item: StoredMemory,
    *,
    expected_tenant: str,
    expected_model_id: str | None = None,
    expected_dimensions: int | None = None,
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
    model_id, profile_dimensions = _profile_from_metadata(
        metadata, default_model_id=expected_model_id
    )
    if profile_dimensions is None:
        profile_dimensions = expected_dimensions or len(item.embedding)
    if expected_model_id is not None and model_id != expected_model_id:
        raise MemoryStoreError(3, "embedding model does not match the configured profile")
    if expected_dimensions is not None and profile_dimensions != expected_dimensions:
        raise MemoryStoreError(3, "embedding dimension does not match the configured profile")
    if profile_dimensions != len(item.embedding):
        raise MemoryStoreError(3, "embedding vector and profile dimensions do not match")
    metadata = _with_profile_metadata(metadata, model_id, profile_dimensions)
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


def _validated_vector(values: Sequence[float], dimensions: int | None = None) -> tuple[float, ...]:
    if dimensions is not None and len(values) != dimensions:
        raise MemoryStoreError(
            3,
            f"embedding dimension mismatch: expected {dimensions}, got {len(values)}",
        )
    if not 1 <= len(values) <= 4096:
        raise ValueError("embedding dimension must be between 1 and 4096")
    vector = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in vector):
        raise ValueError("embedding values must be finite")
    if any(abs(value) > 3.4028234663852886e38 for value in vector):
        raise ValueError("embedding values exceed the float32 range")
    if not any(value != 0.0 for value in vector):
        raise ValueError("zero vectors are invalid; provide a real model embedding")
    return vector


def _pack_vector(values: Sequence[float], dimensions: int | None = None) -> bytes:
    vector = _validated_vector(values, dimensions)
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(value: bytes, dimensions: int) -> tuple[float, ...]:
    _bounded_integer(dimensions, "stored embedding dimensions", 1, 4096)
    if len(value) != dimensions * 4:
        raise MemoryStoreError(5, "stored embedding payload has an invalid length")
    vector = struct.unpack(f"<{dimensions}f", value)
    if any(not math.isfinite(item) for item in vector) or not any(vector):
        raise MemoryStoreError(5, "stored embedding payload is invalid")
    return tuple(vector)


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    cosine = max(-1.0, min(1.0, dot / (left_norm * right_norm)))
    return (cosine + 1.0) / 2.0


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


def _profile_from_metadata(
    metadata: Mapping[str, str], *, default_model_id: str | None
) -> tuple[str, int | None]:
    model_id = metadata.get(MODEL_ID_KEY, default_model_id)
    if model_id is None:
        raise ValueError("embedding model profile is required")
    model_id = _required_text(model_id, "embedding model profile", 256)
    raw_dimensions = metadata.get(DIMENSIONS_KEY)
    dimensions = None
    if raw_dimensions is not None:
        try:
            dimensions = int(raw_dimensions)
        except ValueError as error:
            raise ValueError("embedding dimensions metadata must be an integer") from error
        _bounded_integer(dimensions, DIMENSIONS_KEY, 1, 4096)
    return model_id, dimensions


def _with_profile_metadata(
    item: StoredMemory | Mapping[str, str], model_id: str, dimensions: int
) -> Any:
    metadata = dict(item.metadata if isinstance(item, StoredMemory) else item)
    metadata[MODEL_ID_KEY] = model_id
    metadata[DIMENSIONS_KEY] = str(dimensions)
    return (
        metadata
        if not isinstance(item, StoredMemory)
        else StoredMemory(
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
    )


def _encode_metadata(value: Mapping[str, str]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds the 64 KiB limit")
    return encoded


def _decode_metadata(value: str) -> dict[str, str]:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise MemoryStoreError(5, "stored metadata is invalid") from error
    if not isinstance(decoded, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in decoded.items()
    ):
        raise MemoryStoreError(5, "stored metadata is invalid")
    return decoded


def _effective_expiry(
    created_at_ms: int, expires_at_ms: int | None, ttl_seconds: int | None
) -> int | None:
    if ttl_seconds is None:
        return expires_at_ms
    ttl_expiry = created_at_ms + ttl_seconds * 1000
    return ttl_expiry if expires_at_ms is None else min(expires_at_ms, ttl_expiry)


def _absolute_database_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise ValueError("database_path must be an absolute file path")
    if path.is_symlink():
        raise ValueError("database_path must not be a symbolic link")
    if path.exists() and not path.is_file():
        raise ValueError("database_path must identify a regular file")
    return path


def _restrict_file_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.exists():
                os.chmod(sidecar, 0o600)
    except OSError as error:
        raise ValueError("memory database permissions could not be restricted") from error


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


def _required_text(value: str, name: str, maximum_bytes: int) -> str:
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


def _storage_error(message: str) -> MemoryStoreError:
    return MemoryStoreError(5, message, retryable=True)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _cancelled(value: Any | None) -> bool:
    if value is None:
        return False
    checker = getattr(value, "is_cancelled", None)
    return bool(checker()) if callable(checker) else bool(value.is_set())
