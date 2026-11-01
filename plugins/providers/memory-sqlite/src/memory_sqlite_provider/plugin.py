"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 plugin.py                                                       │
│  Module: memory_sqlite_provider                                      │
│  Role: Serve all seven memory.provider.v1 methods over typed RPC.    │
│                                                                      │
│  模块职责：通过类型化 RPC 提供 memory.provider.v1 的七个方法。        │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from cyrene_memory_provider_contracts.memory import (
    CAPABILITY_ID,
    METHODS,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    DeleteMemoryResponse,
    ExportMemoryResponse,
    GetMemoryResponse,
    ImportMemoryResponse,
    MemoryError,
    MemoryErrorCode,
    MemoryItem,
    MemoryMatch,
    PruneMemoryResponse,
    RecallMemoryResponse,
    StoreMemoryResponse,
    decode_delete_request,
    decode_export_batch_request,
    decode_get_request,
    decode_import_batch_request,
    decode_prune_request,
    decode_recall_request,
    decode_store_request,
    encode_delete_response,
    encode_export_batch_response,
    encode_get_response,
    encode_import_batch_response,
    encode_prune_response,
    encode_recall_response,
    encode_store_response,
)

from .embedding import EmbeddingFailure, ModelEmbeddingClient
from .settings import ConfigurationError, ProviderSettings
from .store import MemoryStoreError, SQLiteMemoryStore, StoredMemory

PLUGIN_ID = "cyrene.providers.memory-sqlite"
PLUGIN_VERSION = "0.1.0"


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """One canonical protobuf response returned to DirectPluginRuntime."""

    value: bytes
    type_url: str


class MemorySqliteProvider:
    """Persistent SQLite implementation of all seven memory provider methods."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: ProviderSettings | None = None,
        store: SQLiteMemoryStore | None = None,
        embedding_client: ModelEmbeddingClient | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._embedding_client = embedding_client
        self._initialization_lock = threading.RLock()

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: Any | None = None,
        request_type_url: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, TypedPayload | str]:
        """Validate, decode and dispatch one canonical memory operation."""

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action not in METHODS:
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        expected_type_url = REQUEST_TYPE_URLS[action]
        if request_type_url != expected_type_url:
            return False, f"INVALID_REQUEST: request_type_url must be {expected_type_url}"
        if stream_results:
            return False, "METHOD_NOT_SUPPORTED: memory methods do not stream"
        if _cancelled(cancellation):
            return False, "CANCELLED: operation cancelled"
        try:
            response = self._dispatch(action, payload, cancellation=cancellation)
            if _cancelled(cancellation):
                return False, "CANCELLED: operation cancelled"
            encoder = _RESPONSE_ENCODERS[action]
            return True, TypedPayload(encoder(response), RESPONSE_TYPE_URLS[action])
        except (
            MemoryStoreError,
            EmbeddingFailure,
            ConfigurationError,
            ValueError,
            TypeError,
        ) as error:
            return self._error_result(action, error)

    def on_shutdown(self, timeout_ms: int) -> None:
        """Close backend and model channels within the host shutdown window."""

        del timeout_ms
        if self._embedding_client is not None:
            self._embedding_client.close()
        if self._store is not None:
            self._store.close()
            self._store = None

    def _dispatch(self, action: str, payload: bytes, *, cancellation: Any | None) -> Any:
        if action == "store":
            request = decode_store_request(payload)
            item = request.item
            if not item.embedding:
                item = self._item_with_embedding(item, cancellation=cancellation)
            return StoreMemoryResponse(
                item_id=self._require_store().store(request.tenant_id, _to_stored(item))
            )
        if action == "get":
            request = decode_get_request(payload)
            result = self._require_store().get(request.tenant_id, request.item_id)
            if result is None:
                return GetMemoryResponse(error=_not_found("memory item was not found"))
            return GetMemoryResponse(item=_to_contract(result))
        if action == "recall":
            request = decode_recall_request(payload)
            query_embedding = request.query_embedding
            if not query_embedding:
                if not request.query_text or not request.query_text.strip():
                    raise ValueError("query_embedding or query_text is required")
                query_embedding = self._embed(
                    (request.query_text,), role="query", cancellation=cancellation
                )[0]
            results = self._require_store().recall(
                tenant_id=request.tenant_id,
                query_embedding=query_embedding,
                scope=request.scope,
                subject=request.subject,
                metadata_filters=request.metadata_filters,
                min_similarity=0.0 if request.min_similarity is None else request.min_similarity,
                top_k=10 if request.top_k is None else request.top_k,
                cancellation=cancellation,
            )
            return RecallMemoryResponse(
                matches=tuple(
                    MemoryMatch(item=_to_contract(result.item), score=result.score)
                    for result in results
                )
            )
        if action == "delete":
            request = decode_delete_request(payload)
            return DeleteMemoryResponse(
                deleted=self._require_store().delete(request.tenant_id, request.item_id)
            )
        if action == "prune":
            request = decode_prune_request(payload)
            return PruneMemoryResponse(
                pruned_count=self._require_store().prune(
                    request.tenant_id, request.before_timestamp_ms
                )
            )
        if action == "export_batch":
            request = decode_export_batch_request(payload)
            items, next_page_token = self._require_store().export_batch(
                request.tenant_id,
                request.scope,
                request.limit,
                request.page_token,
            )
            return ExportMemoryResponse(
                items=tuple(_to_contract(item) for item in items),
                next_page_token=next_page_token,
            )
        if action == "import_batch":
            request = decode_import_batch_request(payload)
            imported = self._require_store().import_batch(
                request.tenant_id,
                tuple(_to_stored(item) for item in request.items),
                overwrite=request.overwrite,
            )
            return ImportMemoryResponse(imported_count=imported)
        raise ValueError("unsupported memory method")

    def _item_with_embedding(self, item: MemoryItem, *, cancellation: Any | None) -> MemoryItem:
        vector = self._embed((item.content,), role="document", cancellation=cancellation)[0]
        metadata = dict(item.metadata)
        settings = self._require_settings()
        metadata.setdefault("cyrene.embedding.model_id", settings.vector_profile_id)
        metadata.setdefault("cyrene.embedding.dimensions", str(settings.embedding_dimensions))
        metadata.setdefault("cyrene.embedding.provider_model_id", settings.embedding_model_id)
        return MemoryItem(
            item_id=item.item_id,
            tenant_id=item.tenant_id,
            scope=item.scope,
            subject=item.subject,
            content=item.content,
            embedding=vector,
            metadata=metadata,
            created_at_ms=item.created_at_ms,
            expires_at_ms=item.expires_at_ms,
            ttl_seconds=item.ttl_seconds,
        )

    def _embed(
        self,
        inputs: tuple[str, ...],
        *,
        role: str,
        cancellation: Any | None,
    ) -> tuple[tuple[float, ...], ...]:
        with self._initialization_lock:
            settings = self._require_settings()
            if settings.embedding_endpoint is None and self._embedding_client is None:
                raise EmbeddingFailure(
                    "a real model.provider.v1 endpoint or host-provided vector is required"
                )
            if self._embedding_client is None:
                assert settings.embedding_endpoint is not None
                self._embedding_client = ModelEmbeddingClient(
                    settings.embedding_endpoint,
                    model_id=settings.embedding_model_id,
                    dimensions=settings.embedding_dimensions,
                    timeout_seconds=settings.embedding_timeout_seconds,
                )
            client = self._embedding_client
            prefix = (
                settings.embedding_query_prefix
                if role == "query"
                else settings.embedding_document_prefix
            )
        return client.embed(tuple(prefix + value for value in inputs), cancellation=cancellation)

    def _require_store(self) -> SQLiteMemoryStore:
        with self._initialization_lock:
            if self._store is None:
                settings = self._require_settings()
                self._store = SQLiteMemoryStore(
                    settings.database_path,
                    embedding_model_id=settings.vector_profile_id,
                    embedding_dimensions=settings.embedding_dimensions,
                    recall_batch_size=settings.recall_batch_size,
                    max_top_k=settings.max_top_k,
                )
            return self._store

    def _require_settings(self) -> ProviderSettings:
        with self._initialization_lock:
            if self._settings is None:
                self._settings = ProviderSettings.from_environment()
            return self._settings

    @staticmethod
    def _error_result(action: str, error: Exception) -> tuple[bool, TypedPayload | str]:
        if isinstance(error, MemoryStoreError):
            code = error.code
            retryable = error.retryable
            message = str(error)
        else:
            code = MemoryErrorCode.STORAGE_FAILED
            retryable = bool(getattr(error, "retryable", False))
            message = str(error) or "memory provider operation failed"
        response = _RESPONSE_TYPES[action](
            error=MemoryError(code=code, message=message, retryable=retryable)
        )
        return True, TypedPayload(
            value=_RESPONSE_ENCODERS[action](response),
            type_url=RESPONSE_TYPE_URLS[action],
        )


_RESPONSE_TYPES: dict[str, Any] = {
    "store": StoreMemoryResponse,
    "get": GetMemoryResponse,
    "recall": RecallMemoryResponse,
    "delete": DeleteMemoryResponse,
    "prune": PruneMemoryResponse,
    "export_batch": ExportMemoryResponse,
    "import_batch": ImportMemoryResponse,
}
_RESPONSE_ENCODERS: dict[str, Any] = {
    "store": encode_store_response,
    "get": encode_get_response,
    "recall": encode_recall_response,
    "delete": encode_delete_response,
    "prune": encode_prune_response,
    "export_batch": encode_export_batch_response,
    "import_batch": encode_import_batch_response,
}


def _to_stored(item: MemoryItem) -> StoredMemory:
    return StoredMemory(
        item_id=item.item_id,
        tenant_id=item.tenant_id,
        scope=item.scope,
        subject=item.subject,
        content=item.content,
        embedding=tuple(item.embedding),
        metadata=dict(item.metadata),
        created_at_ms=item.created_at_ms,
        expires_at_ms=item.expires_at_ms,
        ttl_seconds=item.ttl_seconds,
    )


def _to_contract(item: StoredMemory) -> MemoryItem:
    return MemoryItem(
        item_id=item.item_id,
        tenant_id=item.tenant_id,
        scope=item.scope,
        subject=item.subject,
        content=item.content,
        embedding=item.embedding,
        metadata=dict(item.metadata),
        created_at_ms=item.created_at_ms,
        expires_at_ms=item.expires_at_ms,
        ttl_seconds=item.ttl_seconds,
    )


def _not_found(message: str) -> MemoryError:
    return MemoryError(code=MemoryErrorCode.NOT_FOUND, message=message, retryable=False)


def _cancelled(value: Any | None) -> bool:
    if value is None:
        return False
    checker = getattr(value, "is_cancelled", None)
    return bool(checker()) if callable(checker) else bool(value.is_set())
