"""Direct-invocation adapter for the canonical knowledge graph contract.

中文：将规范 knowledge.graph.v1 请求接到持久化 SQLite 图存储。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from cyrene_knowledge_graph_contracts import (
    CAPABILITY_ID,
    METHODS,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    KnowledgeGraphCodecError,
    decode_request,
    encode_response,
)
from cyrene_knowledge_graph_contracts import knowledge_graph_pb2 as wire

from .settings import ConfigurationError, GraphSettings
from .sqlite_store import GraphEdge, GraphNode, GraphStoreError, SQLiteGraphStore

PLUGIN_ID = "cyrene.providers.knowledge-graph"
PLUGIN_VERSION = "0.1.0"

_RESPONSE_TYPES = {
    "upsert_nodes": wire.UpsertNodesResponse,
    "get_node": wire.GetNodeResponse,
    "search_nodes": wire.SearchNodesResponse,
    "delete_node": wire.DeleteNodeResponse,
    "upsert_edges": wire.UpsertEdgesResponse,
    "get_edge": wire.GetEdgeResponse,
    "search_edges": wire.SearchEdgesResponse,
    "delete_edge": wire.DeleteEdgeResponse,
    "expand": wire.ExpandGraphResponse,
    "import_batch": wire.ImportGraphResponse,
    "export_batch": wire.ExportGraphResponse,
}
_ERROR_CODES = {
    "NOT_FOUND": wire.KNOWLEDGE_GRAPH_ERROR_CODE_NOT_FOUND,
    "TENANT_MISMATCH": wire.KNOWLEDGE_GRAPH_ERROR_CODE_TENANT_MISMATCH,
    "INVALID_INPUT": wire.KNOWLEDGE_GRAPH_ERROR_CODE_INVALID_INPUT,
    "QUOTA_EXCEEDED": wire.KNOWLEDGE_GRAPH_ERROR_CODE_QUOTA_EXCEEDED,
    "STORAGE_FAILED": wire.KNOWLEDGE_GRAPH_ERROR_CODE_STORAGE_FAILED,
}


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """One protobuf payload and its canonical type URL."""

    value: bytes
    type_url: str


class KnowledgeGraphProvider:
    """Implement tenant-scoped graph operations over a private SQLite database."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: GraphSettings | None = None,
        store: SQLiteGraphStore | None = None,
    ) -> None:
        self._settings = settings
        self._store = store

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        **_: Any,
    ) -> tuple[bool, Any]:
        """Decode, dispatch, and encode one versioned graph operation."""

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action not in METHODS:
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        if request_type_url not in (None, REQUEST_TYPE_URLS[action]):
            return False, f"INVALID_REQUEST: request type URL mismatch for {action}"
        try:
            request = decode_request(action, payload)
        except KnowledgeGraphCodecError:
            return False, f"INVALID_REQUEST: malformed {action} request"
        response = _RESPONSE_TYPES[action]()
        try:
            self._dispatch(action, request, response)
        except GraphStoreError as error:
            _set_error(response, error.code, str(error), retryable=error.retryable)
        except (sqlite3.Error, OSError):
            _set_error(
                response,
                "STORAGE_FAILED",
                "graph storage operation failed",
                retryable=True,
            )
        except ConfigurationError:
            return False, "INVALID_CONFIGURATION: graph provider activation is unavailable"
        return True, TypedPayload(encode_response(action, response), RESPONSE_TYPE_URLS[action])

    def _dispatch(self, action: str, request: Any, response: Any) -> None:
        store = self._require_store()
        if action == "upsert_nodes":
            count = store.upsert_nodes(
                request.tenant_id,
                request.namespace,
                [_node_from_proto(item) for item in request.nodes],
            )
            response.upserted_count = count
        elif action == "get_node":
            record = store.get_node(request.tenant_id, request.namespace, request.node_id)
            if record is None:
                raise GraphStoreError("NOT_FOUND", "graph node was not found")
            _copy_node(response.node, record)
        elif action == "search_nodes":
            records, next_token = store.search_nodes(
                request.tenant_id,
                request.namespace,
                query=request.query if request.HasField("query") else "",
                kind=request.kind if request.HasField("kind") else None,
                metadata_filters=dict(request.metadata_filters),
                page_size=request.page_size if request.HasField("page_size") else None,
                page_token=request.page_token if request.HasField("page_token") else None,
            )
            for record in records:
                _copy_node(response.batch.nodes.add(), record)
            response.batch.next_page_token = next_token
        elif action == "delete_node":
            response.deleted = store.delete_node(
                request.tenant_id, request.namespace, request.node_id
            )
        elif action == "upsert_edges":
            count = store.upsert_edges(
                request.tenant_id,
                request.namespace,
                [_edge_from_proto(item) for item in request.edges],
            )
            response.upserted_count = count
        elif action == "get_edge":
            record = store.get_edge(request.tenant_id, request.namespace, request.edge_id)
            if record is None:
                raise GraphStoreError("NOT_FOUND", "graph edge was not found")
            _copy_edge(response.edge, record)
        elif action == "search_edges":
            records, next_token = store.search_edges(
                request.tenant_id,
                request.namespace,
                node_id=request.node_id if request.HasField("node_id") else None,
                relation=request.relation if request.HasField("relation") else None,
                page_size=request.page_size if request.HasField("page_size") else None,
                page_token=request.page_token if request.HasField("page_token") else None,
            )
            for record in records:
                _copy_edge(response.batch.edges.add(), record)
            response.batch.next_page_token = next_token
        elif action == "delete_edge":
            response.deleted = store.delete_edge(
                request.tenant_id, request.namespace, request.edge_id
            )
        elif action == "expand":
            nodes, edges = store.expand(
                request.tenant_id,
                request.namespace,
                list(request.seed_node_ids),
                relation_filters=list(request.relation_filters),
                max_depth=request.max_depth if request.HasField("max_depth") else None,
                node_limit=request.node_limit if request.HasField("node_limit") else None,
            )
            for record in nodes:
                _copy_node(response.batch.nodes.add(), record)
                if response.ByteSize() > store.max_expand_response_bytes:
                    raise GraphStoreError(
                        "QUOTA_EXCEEDED",
                        "expanded response exceeds configured byte limit",
                    )
            for record in edges:
                _copy_edge(response.batch.edges.add(), record)
                if response.ByteSize() > store.max_expand_response_bytes:
                    raise GraphStoreError(
                        "QUOTA_EXCEEDED",
                        "expanded response exceeds configured byte limit",
                    )
        elif action == "import_batch":
            node_count, edge_count = store.import_batch(
                request.tenant_id,
                request.namespace,
                [_node_from_proto(item) for item in request.nodes],
                [_edge_from_proto(item) for item in request.edges],
                overwrite=request.overwrite,
            )
            response.imported.node_count = node_count
            response.imported.edge_count = edge_count
        elif action == "export_batch":
            nodes, edges, next_token = store.export_batch(
                request.tenant_id,
                request.namespace,
                page_size=request.page_size if request.HasField("page_size") else None,
                page_token=request.page_token if request.HasField("page_token") else None,
            )
            for record in nodes:
                _copy_node(response.batch.nodes.add(), record)
            for record in edges:
                _copy_edge(response.batch.edges.add(), record)
            response.batch.next_page_token = next_token

    def _require_store(self) -> SQLiteGraphStore:
        if self._store is None:
            if self._settings is None:
                self._settings = GraphSettings.from_environment()
            settings = self._settings
            self._store = SQLiteGraphStore(
                settings.database_path,
                max_nodes_per_namespace=settings.max_nodes_per_namespace,
                max_edges_per_namespace=settings.max_edges_per_namespace,
                max_batch_items=settings.max_batch_items,
                max_page_size=settings.max_page_size,
                max_expand_depth=settings.max_expand_depth,
                max_expand_nodes=settings.max_expand_nodes,
                max_expand_edges=settings.max_expand_edges,
                max_expand_response_bytes=settings.max_expand_response_bytes,
            )
        return self._store


def _node_from_proto(value: Any) -> GraphNode:
    return GraphNode(
        node_id=value.node_id,
        tenant_id=value.tenant_id,
        namespace=value.namespace,
        kind=value.kind,
        label=value.label,
        description=value.description if value.HasField("description") else None,
        metadata=dict(value.metadata),
        source_refs=tuple(value.source_refs),
        created_at_ms=value.created_at_ms,
        updated_at_ms=value.updated_at_ms,
    )


def _edge_from_proto(value: Any) -> GraphEdge:
    return GraphEdge(
        edge_id=value.edge_id,
        tenant_id=value.tenant_id,
        namespace=value.namespace,
        source_node_id=value.source_node_id,
        target_node_id=value.target_node_id,
        relation=value.relation,
        metadata=dict(value.metadata),
        source_refs=tuple(value.source_refs),
        created_at_ms=value.created_at_ms,
        updated_at_ms=value.updated_at_ms,
    )


def _copy_node(target: Any, record: dict[str, Any]) -> None:
    target.node_id = record["node_id"]
    target.tenant_id = record["tenant_id"]
    target.namespace = record["namespace"]
    target.kind = record["kind"]
    target.label = record["label"]
    if record["description"] is not None:
        target.description = record["description"]
    target.metadata.update(record["metadata"])
    target.source_refs.extend(record["source_refs"])
    target.created_at_ms = record["created_at_ms"]
    target.updated_at_ms = record["updated_at_ms"]


def _copy_edge(target: Any, record: dict[str, Any]) -> None:
    target.edge_id = record["edge_id"]
    target.tenant_id = record["tenant_id"]
    target.namespace = record["namespace"]
    target.source_node_id = record["source_node_id"]
    target.target_node_id = record["target_node_id"]
    target.relation = record["relation"]
    target.metadata.update(record["metadata"])
    target.source_refs.extend(record["source_refs"])
    target.created_at_ms = record["created_at_ms"]
    target.updated_at_ms = record["updated_at_ms"]


def _set_error(response: Any, code: str, message: str, *, retryable: bool) -> None:
    response.error.code = _ERROR_CODES.get(code, wire.KNOWLEDGE_GRAPH_ERROR_CODE_STORAGE_FAILED)
    response.error.message = message[:1_024]
    response.error.retryable = retryable
