"""Tenant-scoped SQLite storage for graph nodes and edges.

中文：提供隔离存储、分页检索、有限邻域展开及原子批量迁移。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MAX_IDENTIFIER_LENGTH = 256
MAX_TEXT_LENGTH = 32_768
MAX_METADATA_ITEMS = 128
MAX_SOURCE_REFS = 1_000
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_TOKEN_CHARS = 2_048
MAX_PAGE_TOKEN_PAYLOAD_BYTES = 1_024
MAX_RELATION_FILTERS = 64
_CURSOR_KEY_ID = "page_cursor_hmac_v1"


class GraphStoreError(ValueError):
    """A typed domain failure raised by the persistent graph store."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One tenant and namespace scoped graph node."""

    node_id: str
    tenant_id: str
    namespace: str
    kind: str
    label: str
    description: str | None = None
    metadata: Mapping[str, str] | None = None
    source_refs: Sequence[str] = ()
    created_at_ms: int = 0
    updated_at_ms: int = 0

    def record(self) -> dict[str, Any]:
        """Return a storage-ready mapping with detached metadata containers."""

        result = asdict(self)
        result["metadata"] = dict(self.metadata or {})
        result["source_refs"] = list(self.source_refs)
        return result


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """One directed relationship between nodes in the same graph scope."""

    edge_id: str
    tenant_id: str
    namespace: str
    source_node_id: str
    target_node_id: str
    relation: str
    metadata: Mapping[str, str] | None = None
    source_refs: Sequence[str] = ()
    created_at_ms: int = 0
    updated_at_ms: int = 0

    def record(self) -> dict[str, Any]:
        """Return a storage-ready mapping with detached metadata containers."""

        result = asdict(self)
        result["metadata"] = dict(self.metadata or {})
        result["source_refs"] = list(self.source_refs)
        return result


class SQLiteGraphStore:
    """Persist graph records in a private SQLite file with bounded operations."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        max_nodes_per_namespace: int = 1_000_000,
        max_edges_per_namespace: int = 5_000_000,
        max_batch_items: int = 1_000,
        max_page_size: int = 200,
        max_expand_depth: int = 4,
        max_expand_nodes: int = 200,
        max_expand_edges: int = 1_000,
        max_expand_response_bytes: int = 1_048_576,
    ) -> None:
        self.database_path = Path(database_path)
        self.max_nodes_per_namespace = max_nodes_per_namespace
        self.max_edges_per_namespace = max_edges_per_namespace
        self.max_batch_items = max_batch_items
        self.max_page_size = max_page_size
        self.max_expand_depth = max_expand_depth
        self.max_expand_nodes = max_expand_nodes
        self.max_expand_edges = max_expand_edges
        self.max_expand_response_bytes = max_expand_response_bytes
        self._cursor_key = b""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self.database_path.exists() and self.database_path.is_dir():
            raise GraphStoreError("INVALID_INPUT", "database_path must identify a file")
        self._initialize()

    def upsert_nodes(self, tenant_id: str, namespace: str, nodes: Sequence[GraphNode]) -> int:
        """Atomically insert or update a bounded batch of graph nodes."""

        _validate_scope(tenant_id, namespace)
        self._require_batch(nodes, "nodes")
        validated = [_validate_node(item, tenant_id, namespace) for item in nodes]
        _require_unique_ids(validated, "node_id")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._enforce_count_limit(
                connection,
                "graph_nodes",
                tenant_id,
                namespace,
                validated,
                self.max_nodes_per_namespace,
                "node_id",
            )
            _upsert_nodes(connection, validated)
        return len(validated)

    def get_node(self, tenant_id: str, namespace: str, node_id: str) -> dict[str, Any] | None:
        """Load one node using the complete tenant, namespace, and ID key."""

        _validate_scope(tenant_id, namespace)
        _required_text(node_id, "node_id")
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM graph_nodes WHERE tenant_id=? AND namespace=? AND node_id=?",
                (tenant_id, namespace, node_id),
            ).fetchone()
        return _node_from_row(row) if row is not None else None

    def search_nodes(
        self,
        tenant_id: str,
        namespace: str,
        *,
        query: str = "",
        kind: str | None = None,
        metadata_filters: Mapping[str, str] | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Search node text and metadata with stable, namespace-local keyset paging."""

        _validate_scope(tenant_id, namespace)
        if not isinstance(query, str) or len(query) > MAX_TEXT_LENGTH:
            raise GraphStoreError("INVALID_INPUT", "query exceeds the configured text limit")
        if kind is not None:
            _required_text(kind, "kind")
        if len(metadata_filters or {}) > MAX_METADATA_ITEMS:
            raise GraphStoreError("INVALID_INPUT", "metadata_filters exceeds the configured limit")
        for key, value in (metadata_filters or {}).items():
            _required_text(key, "metadata filter key", max_length=256)
            _required_text(value, "metadata filter value", max_length=4_096)
        size = self._page_size(page_size)
        filters = {
            "query": query,
            "kind": kind,
            "metadata_filters": dict(sorted((metadata_filters or {}).items())),
            "page_size": size,
        }
        scope = _cursor_scope("search_nodes", tenant_id, namespace, filters)
        after = _decode_page_token(page_token, "search_nodes", scope, self._cursor_key)
        sql = "SELECT * FROM graph_nodes WHERE tenant_id=? AND namespace=? AND node_id>?"
        values: list[Any] = [tenant_id, namespace, after]
        if query:
            sql += " AND instr(lower(label || ' ' || coalesce(description, '')), lower(?)) > 0"
            values.append(query)
        if kind:
            sql += " AND kind=?"
            values.append(kind)
        for key, value in sorted((metadata_filters or {}).items()):
            sql += " AND json_extract(metadata_json, ?) = ?"
            values.extend(("$." + json.dumps(key, ensure_ascii=False), value))
        sql += " ORDER BY node_id LIMIT ?"
        values.append(size + 1)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(sql, values).fetchall()
        selected = rows[:size]
        next_token = (
            _encode_page_token("search_nodes", selected[-1]["node_id"], scope, self._cursor_key)
            if len(rows) > size
            else ""
        )
        return [_node_from_row(row) for row in selected], next_token

    def delete_node(self, tenant_id: str, namespace: str, node_id: str) -> bool:
        """Delete one node and cascade its incident edges within the graph scope."""

        _validate_scope(tenant_id, namespace)
        _required_text(node_id, "node_id")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM graph_nodes WHERE tenant_id=? AND namespace=? AND node_id=?",
                (tenant_id, namespace, node_id),
            )
        return cursor.rowcount > 0

    def upsert_edges(self, tenant_id: str, namespace: str, edges: Sequence[GraphEdge]) -> int:
        """Atomically insert or update edges whose endpoints exist in the same scope."""

        _validate_scope(tenant_id, namespace)
        self._require_batch(edges, "edges")
        validated = [_validate_edge(item, tenant_id, namespace) for item in edges]
        _require_unique_ids(validated, "edge_id")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._enforce_count_limit(
                connection,
                "graph_edges",
                tenant_id,
                namespace,
                validated,
                self.max_edges_per_namespace,
                "edge_id",
            )
            _upsert_edges(connection, validated)
        return len(validated)

    def get_edge(self, tenant_id: str, namespace: str, edge_id: str) -> dict[str, Any] | None:
        """Load one edge using the complete tenant, namespace, and ID key."""

        _validate_scope(tenant_id, namespace)
        _required_text(edge_id, "edge_id")
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM graph_edges WHERE tenant_id=? AND namespace=? AND edge_id=?",
                (tenant_id, namespace, edge_id),
            ).fetchone()
        return _edge_from_row(row) if row is not None else None

    def search_edges(
        self,
        tenant_id: str,
        namespace: str,
        *,
        node_id: str | None = None,
        relation: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Search incident edges with stable keyset paging."""

        _validate_scope(tenant_id, namespace)
        if node_id is not None:
            _required_text(node_id, "node_id")
        if relation is not None:
            _required_text(relation, "relation")
        size = self._page_size(page_size)
        filters = {"node_id": node_id, "relation": relation, "page_size": size}
        scope = _cursor_scope("search_edges", tenant_id, namespace, filters)
        after = _decode_page_token(page_token, "search_edges", scope, self._cursor_key)
        sql = "SELECT * FROM graph_edges WHERE tenant_id=? AND namespace=? AND edge_id>?"
        values: list[Any] = [tenant_id, namespace, after]
        if node_id:
            sql += " AND (source_node_id=? OR target_node_id=?)"
            values.extend((node_id, node_id))
        if relation:
            sql += " AND relation=?"
            values.append(relation)
        sql += " ORDER BY edge_id LIMIT ?"
        values.append(size + 1)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(sql, values).fetchall()
        selected = rows[:size]
        next_token = (
            _encode_page_token("search_edges", selected[-1]["edge_id"], scope, self._cursor_key)
            if len(rows) > size
            else ""
        )
        return [_edge_from_row(row) for row in selected], next_token

    def delete_edge(self, tenant_id: str, namespace: str, edge_id: str) -> bool:
        """Delete one edge using the complete graph scope and edge key."""

        _validate_scope(tenant_id, namespace)
        _required_text(edge_id, "edge_id")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM graph_edges WHERE tenant_id=? AND namespace=? AND edge_id=?",
                (tenant_id, namespace, edge_id),
            )
        return cursor.rowcount > 0

    def expand(
        self,
        tenant_id: str,
        namespace: str,
        seed_node_ids: Sequence[str],
        *,
        relation_filters: Sequence[str] = (),
        max_depth: int | None = None,
        node_limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return a bounded undirected neighborhood, retaining directed edge data."""

        _validate_scope(tenant_id, namespace)
        depth_limit = self.max_expand_depth if max_depth is None else max_depth
        result_limit = self.max_expand_nodes if node_limit is None else node_limit
        if type(depth_limit) is not int or not 1 <= depth_limit <= self.max_expand_depth:
            raise GraphStoreError("INVALID_INPUT", "max_depth exceeds configured bounds")
        if type(result_limit) is not int or not 1 <= result_limit <= self.max_expand_nodes:
            raise GraphStoreError("INVALID_INPUT", "node_limit exceeds configured bounds")
        seeds = list(dict.fromkeys(_required_text(item, "seed_node_id") for item in seed_node_ids))
        if not seeds:
            raise GraphStoreError("INVALID_INPUT", "at least one seed node is required")
        if len(seeds) > result_limit:
            raise GraphStoreError("QUOTA_EXCEEDED", "seed node count exceeds node_limit")
        relations = tuple(
            dict.fromkeys(_required_text(item, "relation_filter") for item in relation_filters)
        )
        if len(relations) > MAX_RELATION_FILTERS:
            raise GraphStoreError("INVALID_INPUT", "relation_filters exceeds the configured limit")

        with closing(self._connect()) as connection, connection:
            nodes = self._load_nodes(connection, tenant_id, namespace, seeds)
            missing = set(seeds) - nodes.keys()
            if missing:
                raise GraphStoreError("NOT_FOUND", "one or more seed nodes do not exist")
            visited = set(nodes)
            frontier = set(seeds)
            result_edges: dict[str, dict[str, Any]] = {}
            connection.execute("CREATE TEMP TABLE expansion_frontier (node_id TEXT PRIMARY KEY)")
            connection.execute("CREATE TEMP TABLE expansion_seen_edges (edge_id TEXT PRIMARY KEY)")
            for _ in range(depth_limit):
                if not frontier:
                    break
                connection.execute("DELETE FROM expansion_frontier")
                connection.executemany(
                    "INSERT INTO expansion_frontier (node_id) VALUES (?)",
                    ((node_id,) for node_id in sorted(frontier)),
                )
                sql = (
                    "SELECT edge.* FROM graph_edges AS edge "
                    "WHERE edge.tenant_id=? AND edge.namespace=? "
                    "AND (edge.source_node_id IN (SELECT node_id FROM expansion_frontier) "
                    "OR edge.target_node_id IN (SELECT node_id FROM expansion_frontier)) "
                    "AND NOT EXISTS (SELECT 1 FROM expansion_seen_edges AS seen "
                    "WHERE seen.edge_id=edge.edge_id)"
                )
                values: list[Any] = [tenant_id, namespace]
                if relations:
                    sql += " AND edge.relation IN (" + ",".join("?" for _ in relations) + ")"
                    values.extend(relations)
                sql += " ORDER BY edge.edge_id LIMIT ?"
                remaining_edge_slots = self.max_expand_edges - len(result_edges)
                values.append(remaining_edge_slots + 1)
                next_frontier: set[str] = set()
                cursor = connection.execute(sql, values)
                for row in cursor:
                    edge = _edge_from_row(row)
                    if len(result_edges) >= self.max_expand_edges:
                        raise GraphStoreError(
                            "QUOTA_EXCEEDED", "expanded edge count exceeds configured limit"
                        )
                    edge_id = edge["edge_id"]
                    result_edges[edge_id] = edge
                    connection.execute(
                        "INSERT INTO expansion_seen_edges (edge_id) VALUES (?)", (edge_id,)
                    )
                    other_id = (
                        edge["target_node_id"]
                        if edge["source_node_id"] in frontier
                        else edge["source_node_id"]
                    )
                    if other_id not in visited and other_id not in next_frontier:
                        if len(visited) + len(next_frontier) >= result_limit:
                            raise GraphStoreError(
                                "QUOTA_EXCEEDED", "expanded node count exceeds configured limit"
                            )
                        next_frontier.add(other_id)
                if next_frontier:
                    loaded = self._load_nodes(
                        connection, tenant_id, namespace, sorted(next_frontier)
                    )
                    if loaded.keys() != next_frontier:
                        raise GraphStoreError(
                            "STORAGE_FAILED", "graph edge references a missing node"
                        )
                    nodes.update(loaded)
                    visited.update(loaded)
                frontier = next_frontier
            filtered_edges = [
                edge
                for edge in result_edges.values()
                if edge["source_node_id"] in visited and edge["target_node_id"] in visited
            ]
        return [nodes[key] for key in sorted(nodes)], sorted(
            filtered_edges, key=lambda edge: edge["edge_id"]
        )

    def import_batch(
        self,
        tenant_id: str,
        namespace: str,
        nodes: Sequence[GraphNode],
        edges: Sequence[GraphEdge],
        *,
        overwrite: bool = False,
    ) -> tuple[int, int]:
        """Import bounded node and edge batches atomically, optionally replacing a namespace."""

        _validate_scope(tenant_id, namespace)
        if len(nodes) + len(edges) > self.max_batch_items:
            raise GraphStoreError("QUOTA_EXCEEDED", "import batch exceeds configured item limit")
        validated_nodes = [_validate_node(item, tenant_id, namespace) for item in nodes]
        validated_edges = [_validate_edge(item, tenant_id, namespace) for item in edges]
        _require_unique_ids(validated_nodes, "node_id")
        _require_unique_ids(validated_edges, "edge_id")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if overwrite:
                connection.execute(
                    "DELETE FROM graph_edges WHERE tenant_id=? AND namespace=?",
                    (tenant_id, namespace),
                )
                connection.execute(
                    "DELETE FROM graph_nodes WHERE tenant_id=? AND namespace=?",
                    (tenant_id, namespace),
                )
            self._enforce_count_limit(
                connection,
                "graph_nodes",
                tenant_id,
                namespace,
                validated_nodes,
                self.max_nodes_per_namespace,
                "node_id",
            )
            self._enforce_count_limit(
                connection,
                "graph_edges",
                tenant_id,
                namespace,
                validated_edges,
                self.max_edges_per_namespace,
                "edge_id",
            )
            _upsert_nodes(connection, validated_nodes)
            _upsert_edges(connection, validated_edges)
        return len(validated_nodes), len(validated_edges)

    def export_batch(
        self,
        tenant_id: str,
        namespace: str,
        *,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        """Export a stable combined node/edge stream using a tagged cursor."""

        _validate_scope(tenant_id, namespace)
        size = self._page_size(page_size)
        scope = _cursor_scope("export", tenant_id, namespace, {"page_size": size})
        kind, after = _decode_export_token(page_token, scope, self._cursor_key)
        with closing(self._connect()) as connection, connection:
            nodes = connection.execute(
                "SELECT * FROM graph_nodes WHERE tenant_id=? AND namespace=? "
                "AND node_id>? ORDER BY node_id LIMIT ?",
                (tenant_id, namespace, after if kind == "nodes" else "", size + 1),
            ).fetchall()
            if kind == "nodes" and len(nodes) > size:
                selected = nodes[:size]
                return (
                    [_node_from_row(row) for row in selected],
                    [],
                    _encode_export_token("nodes", selected[-1]["node_id"], scope, self._cursor_key),
                )
            node_records = [_node_from_row(row) for row in nodes] if kind == "nodes" else []
            remaining = size - len(node_records)
            edge_after = after if kind == "edges" else ""
            if remaining == 0:
                has_edges = connection.execute(
                    "SELECT 1 FROM graph_edges WHERE tenant_id=? AND namespace=? LIMIT 1",
                    (tenant_id, namespace),
                ).fetchone()
                next_token = (
                    _encode_export_token("edges", "", scope, self._cursor_key) if has_edges else ""
                )
                return node_records, [], next_token
            edges = connection.execute(
                "SELECT * FROM graph_edges WHERE tenant_id=? AND namespace=? "
                "AND edge_id>? ORDER BY edge_id LIMIT ?",
                (tenant_id, namespace, edge_after, remaining + 1),
            ).fetchall()
            if len(edges) > remaining:
                selected_edges = edges[:remaining]
                return (
                    node_records,
                    [_edge_from_row(row) for row in selected_edges],
                    _encode_export_token(
                        "edges", selected_edges[-1]["edge_id"], scope, self._cursor_key
                    ),
                )
        return node_records, [_edge_from_row(row) for row in edges], ""

    def _initialize(self) -> None:
        """Create the schema and indexes idempotently without dropping existing state."""

        try:
            with closing(self._connect()) as connection, connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > 1:
                    raise GraphStoreError(
                        "STORAGE_FAILED", "graph database schema is newer than this provider"
                    )
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS graph_nodes (
                        tenant_id TEXT NOT NULL,
                        namespace TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        label TEXT NOT NULL,
                        description TEXT,
                        metadata_json TEXT NOT NULL,
                        source_refs_json TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        updated_at_ms INTEGER NOT NULL,
                        PRIMARY KEY (tenant_id, namespace, node_id)
                    );
                    CREATE INDEX IF NOT EXISTS graph_nodes_scope_kind_id
                        ON graph_nodes (tenant_id, namespace, kind, node_id);
                    CREATE TABLE IF NOT EXISTS graph_edges (
                        tenant_id TEXT NOT NULL,
                        namespace TEXT NOT NULL,
                        edge_id TEXT NOT NULL,
                        source_node_id TEXT NOT NULL,
                        target_node_id TEXT NOT NULL,
                        relation TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        source_refs_json TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        updated_at_ms INTEGER NOT NULL,
                        PRIMARY KEY (tenant_id, namespace, edge_id),
                        FOREIGN KEY (tenant_id, namespace, source_node_id)
                            REFERENCES graph_nodes (tenant_id, namespace, node_id)
                            ON DELETE CASCADE,
                        FOREIGN KEY (tenant_id, namespace, target_node_id)
                            REFERENCES graph_nodes (tenant_id, namespace, node_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS graph_edges_scope_source
                        ON graph_edges (tenant_id, namespace, source_node_id, edge_id);
                    CREATE INDEX IF NOT EXISTS graph_edges_scope_target
                        ON graph_edges (tenant_id, namespace, target_node_id, edge_id);
                    CREATE INDEX IF NOT EXISTS graph_edges_scope_relation
                        ON graph_edges (tenant_id, namespace, relation, edge_id);
                    CREATE TABLE IF NOT EXISTS graph_metadata (
                        metadata_key TEXT PRIMARY KEY,
                        metadata_value BLOB NOT NULL
                    );
                    PRAGMA user_version=1;
                    COMMIT;
                    """
                )
                connection.execute("BEGIN IMMEDIATE")
                cursor_key = connection.execute(
                    "SELECT metadata_value FROM graph_metadata WHERE metadata_key=?",
                    (_CURSOR_KEY_ID,),
                ).fetchone()
                if cursor_key is None:
                    key = secrets.token_bytes(32)
                    connection.execute(
                        "INSERT INTO graph_metadata (metadata_key, metadata_value) VALUES (?, ?)",
                        (_CURSOR_KEY_ID, key),
                    )
                    self._cursor_key = key
                else:
                    key = cursor_key["metadata_value"]
                    if not isinstance(key, bytes) or len(key) != 32:
                        raise GraphStoreError("STORAGE_FAILED", "graph cursor key is invalid")
                    self._cursor_key = key
        except sqlite3.Error as error:
            raise GraphStoreError(
                "STORAGE_FAILED", "graph database could not be initialized", retryable=True
            ) from error

    def _connect(self) -> sqlite3.Connection:
        """Open a bounded-wait connection with foreign-key enforcement enabled."""

        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _require_batch(self, records: Sequence[Any], label: str) -> None:
        if len(records) > self.max_batch_items:
            raise GraphStoreError("QUOTA_EXCEEDED", f"{label} batch exceeds configured item limit")

    def _page_size(self, value: int | None) -> int:
        size = min(DEFAULT_PAGE_SIZE, self.max_page_size) if value is None or value == 0 else value
        if type(size) is not int or not 1 <= size <= self.max_page_size:
            raise GraphStoreError("INVALID_INPUT", "page_size exceeds configured bounds")
        return size

    @staticmethod
    def _enforce_count_limit(
        connection: sqlite3.Connection,
        table: str,
        tenant_id: str,
        namespace: str,
        records: Sequence[Mapping[str, Any]],
        maximum: int,
        key: str,
    ) -> None:
        row = connection.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE tenant_id=? AND namespace=?",
            (tenant_id, namespace),
        ).fetchone()
        new_count = 0
        for record_id in {record[key] for record in records}:
            exists = connection.execute(
                f"SELECT 1 FROM {table} WHERE tenant_id=? AND namespace=? AND {key}=?",
                (tenant_id, namespace, record_id),
            ).fetchone()
            new_count += int(exists is None)
        if row["count"] + new_count > maximum:
            raise GraphStoreError("QUOTA_EXCEEDED", f"{table} namespace quota exceeded")

    @staticmethod
    def _load_nodes(
        connection: sqlite3.Connection,
        tenant_id: str,
        namespace: str,
        node_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for batch in _chunks(list(node_ids), 400):
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                "SELECT * FROM graph_nodes WHERE tenant_id=? AND namespace=? AND node_id IN ("
                + placeholders
                + ")",
                [tenant_id, namespace, *batch],
            ).fetchall()
            result.update({row["node_id"]: _node_from_row(row) for row in rows})
        return result


def _upsert_nodes(connection: sqlite3.Connection, records: Sequence[Mapping[str, Any]]) -> None:
    now = _now_ms()
    for record in records:
        existing = connection.execute(
            "SELECT created_at_ms FROM graph_nodes WHERE tenant_id=? AND namespace=? AND node_id=?",
            (record["tenant_id"], record["namespace"], record["node_id"]),
        ).fetchone()
        created = record["created_at_ms"] or (existing["created_at_ms"] if existing else now)
        updated = record["updated_at_ms"] or now
        connection.execute(
            """
            INSERT INTO graph_nodes (
                tenant_id, namespace, node_id, kind, label, description, metadata_json,
                source_refs_json, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, namespace, node_id) DO UPDATE SET
                kind=excluded.kind, label=excluded.label, description=excluded.description,
                metadata_json=excluded.metadata_json, source_refs_json=excluded.source_refs_json,
                updated_at_ms=excluded.updated_at_ms
            """,
            (
                record["tenant_id"],
                record["namespace"],
                record["node_id"],
                record["kind"],
                record["label"],
                record["description"],
                _dump_json(record["metadata"]),
                _dump_json(record["source_refs"]),
                created,
                updated,
            ),
        )


def _upsert_edges(connection: sqlite3.Connection, records: Sequence[Mapping[str, Any]]) -> None:
    now = _now_ms()
    for record in records:
        existing = connection.execute(
            "SELECT created_at_ms FROM graph_edges WHERE tenant_id=? AND namespace=? AND edge_id=?",
            (record["tenant_id"], record["namespace"], record["edge_id"]),
        ).fetchone()
        created = record["created_at_ms"] or (existing["created_at_ms"] if existing else now)
        updated = record["updated_at_ms"] or now
        try:
            connection.execute(
                """
                INSERT INTO graph_edges (
                    tenant_id, namespace, edge_id, source_node_id, target_node_id, relation,
                    metadata_json, source_refs_json, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, namespace, edge_id) DO UPDATE SET
                    source_node_id=excluded.source_node_id, target_node_id=excluded.target_node_id,
                    relation=excluded.relation, metadata_json=excluded.metadata_json,
                    source_refs_json=excluded.source_refs_json, updated_at_ms=excluded.updated_at_ms
                """,
                (
                    record["tenant_id"],
                    record["namespace"],
                    record["edge_id"],
                    record["source_node_id"],
                    record["target_node_id"],
                    record["relation"],
                    _dump_json(record["metadata"]),
                    _dump_json(record["source_refs"]),
                    created,
                    updated,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise GraphStoreError(
                "INVALID_INPUT", "edge endpoint does not exist in the requested graph scope"
            ) from error


def _validate_node(node: GraphNode, tenant_id: str, namespace: str) -> dict[str, Any]:
    record = node.record()
    if record["tenant_id"] != tenant_id or record["namespace"] != namespace:
        raise GraphStoreError("TENANT_MISMATCH", "node scope does not match request scope")
    for key in ("node_id", "tenant_id", "namespace", "kind", "label"):
        maximum = 1_024 if key == "label" else MAX_IDENTIFIER_LENGTH
        _required_text(record[key], key, max_length=maximum)
    if record["description"] is not None:
        _required_text(record["description"], "description", max_length=MAX_TEXT_LENGTH)
    _validate_metadata(record["metadata"])
    _validate_source_refs(record["source_refs"])
    _validate_timestamps(record)
    return record


def _validate_edge(edge: GraphEdge, tenant_id: str, namespace: str) -> dict[str, Any]:
    record = edge.record()
    if record["tenant_id"] != tenant_id or record["namespace"] != namespace:
        raise GraphStoreError("TENANT_MISMATCH", "edge scope does not match request scope")
    for key in (
        "edge_id",
        "tenant_id",
        "namespace",
        "source_node_id",
        "target_node_id",
        "relation",
    ):
        _required_text(record[key], key, max_length=MAX_IDENTIFIER_LENGTH)
    _validate_metadata(record["metadata"])
    _validate_source_refs(record["source_refs"])
    _validate_timestamps(record)
    return record


def _validate_metadata(value: Any) -> None:
    if not isinstance(value, Mapping) or len(value) > MAX_METADATA_ITEMS:
        raise GraphStoreError("INVALID_INPUT", "metadata must be a bounded string map")
    for key, item in value.items():
        _required_text(key, "metadata key", max_length=256)
        _required_text(item, "metadata value", max_length=4_096)


def _validate_source_refs(value: Any) -> None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) > MAX_SOURCE_REFS
    ):
        raise GraphStoreError("INVALID_INPUT", "source_refs must be a bounded string array")
    for item in value:
        _required_text(item, "source_ref", max_length=2_048)


def _validate_timestamps(record: Mapping[str, Any]) -> None:
    for key in ("created_at_ms", "updated_at_ms"):
        value = record[key]
        if type(value) is not int or value < 0:
            raise GraphStoreError("INVALID_INPUT", f"{key} must be a non-negative integer")


def _required_text(value: Any, label: str, *, max_length: int = MAX_IDENTIFIER_LENGTH) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value) > max_length
    ):
        raise GraphStoreError("INVALID_INPUT", f"{label} must be non-empty text within its limit")
    return value


def _require_unique_ids(records: Sequence[Mapping[str, Any]], key: str) -> None:
    values = [record[key] for record in records]
    if len(values) != len(set(values)):
        raise GraphStoreError("INVALID_INPUT", f"batch contains duplicate {key} values")


def _validate_scope(tenant_id: str, namespace: str) -> None:
    _required_text(tenant_id, "tenant_id")
    _required_text(namespace, "namespace")


def _node_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "node_id": row["node_id"],
        "tenant_id": row["tenant_id"],
        "namespace": row["namespace"],
        "kind": row["kind"],
        "label": row["label"],
        "description": row["description"],
        "metadata": json.loads(row["metadata_json"]),
        "source_refs": json.loads(row["source_refs_json"]),
        "created_at_ms": row["created_at_ms"],
        "updated_at_ms": row["updated_at_ms"],
    }


def _edge_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "edge_id": row["edge_id"],
        "tenant_id": row["tenant_id"],
        "namespace": row["namespace"],
        "source_node_id": row["source_node_id"],
        "target_node_id": row["target_node_id"],
        "relation": row["relation"],
        "metadata": json.loads(row["metadata_json"]),
        "source_refs": json.loads(row["source_refs_json"]),
        "created_at_ms": row["created_at_ms"],
        "updated_at_ms": row["updated_at_ms"],
    }


def _cursor_scope(stream: str, tenant_id: str, namespace: str, filters: Mapping[str, Any]) -> str:
    """Hash the complete scope and filters a cursor may continue."""

    payload = {
        "stream": stream,
        "tenant_id": tenant_id,
        "namespace": namespace,
        "filters": dict(filters),
    }
    return hashlib.sha256(_dump_json(payload).encode("utf-8")).hexdigest()


def _encode_page_token(stream: str, after: str, scope: str, key: bytes) -> str:
    return _encode_cursor(stream, "records", after, scope, key)


def _decode_page_token(token: str | None, stream: str, scope: str, key: bytes) -> str:
    if token is None or token == "":
        return ""
    if not isinstance(token, str):
        raise GraphStoreError("INVALID_INPUT", "page_token must be text")
    decoded = _decode_cursor(token, key)
    if (
        decoded["stream"] != stream
        or decoded["phase"] != "records"
        or decoded["scope"] != scope
        or not decoded["after"]
    ):
        raise GraphStoreError("INVALID_INPUT", "page_token does not match this query")
    return decoded["after"]


def _encode_export_token(phase: str, after: str, scope: str, key: bytes) -> str:
    return _encode_cursor("export", phase, after, scope, key)


def _decode_export_token(token: str | None, scope: str, key: bytes) -> tuple[str, str]:
    if token is None or token == "":
        return "nodes", ""
    if not isinstance(token, str):
        raise GraphStoreError("INVALID_INPUT", "page_token must be text")
    decoded = _decode_cursor(token, key)
    phase = decoded["phase"]
    if (
        decoded["stream"] != "export"
        or phase not in {"nodes", "edges"}
        or decoded["scope"] != scope
        or (phase == "nodes" and not decoded["after"])
    ):
        raise GraphStoreError("INVALID_INPUT", "page_token does not match this export")
    return phase, decoded["after"]


def _encode_cursor(stream: str, phase: str, after: str, scope: str, key: bytes) -> str:
    if not isinstance(after, str) or len(after) > MAX_IDENTIFIER_LENGTH or "\x00" in after:
        raise GraphStoreError("INVALID_INPUT", "page cursor value exceeds its limit")
    payload = _dump_json(
        {"v": 1, "stream": stream, "phase": phase, "after": after, "scope": scope}
    ).encode("utf-8")
    if len(payload) > MAX_PAGE_TOKEN_PAYLOAD_BYTES:
        raise GraphStoreError("INVALID_INPUT", "page cursor payload exceeds its limit")
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")
    if len(token) > MAX_PAGE_TOKEN_CHARS:
        raise GraphStoreError("INVALID_INPUT", "page token exceeds its limit")
    return token


def _decode_cursor(token: str, key: bytes) -> dict[str, Any]:
    if (
        not isinstance(token, str)
        or len(token) > MAX_PAGE_TOKEN_CHARS
        or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None
    ):
        raise GraphStoreError("INVALID_INPUT", "page_token encoding is invalid")
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise GraphStoreError("INVALID_INPUT", "page_token encoding is invalid") from error
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != token:
        raise GraphStoreError("INVALID_INPUT", "page_token encoding is not canonical")
    if len(raw) <= 32 or len(raw) > MAX_PAGE_TOKEN_PAYLOAD_BYTES + 32:
        raise GraphStoreError("INVALID_INPUT", "page_token size is invalid")
    payload, signature = raw[:-32], raw[-32:]
    expected = hmac.new(key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise GraphStoreError("INVALID_INPUT", "page_token signature is invalid")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("duplicate cursor field")
            result[name] = value
        return result

    try:
        decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise GraphStoreError("INVALID_INPUT", "page_token payload is malformed") from error
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"v", "stream", "phase", "after", "scope"}
        or type(decoded.get("v")) is not int
        or decoded["v"] != 1
        or not isinstance(decoded.get("stream"), str)
        or not isinstance(decoded.get("phase"), str)
        or not isinstance(decoded.get("after"), str)
        or len(decoded["after"]) > MAX_IDENTIFIER_LENGTH
        or "\x00" in decoded["after"]
        or not isinstance(decoded.get("scope"), str)
        or re.fullmatch(r"[0-9a-f]{64}", decoded["scope"]) is None
        or _dump_json(decoded).encode("utf-8") != payload
    ):
        raise GraphStoreError("INVALID_INPUT", "page_token payload is malformed")
    return decoded


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _chunks(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
