"""Behavior tests for persistent, tenant-isolated knowledge graph storage.

中文：验证节点边读写、分页、图展开、恢复及批量事务边界。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_graph_provider.sqlite_store import (
    GraphEdge,
    GraphNode,
    GraphStoreError,
    SQLiteGraphStore,
)


def _node(
    node_id: str,
    label: str,
    *,
    tenant: str = "tenant-a",
    namespace: str = "conversation",
    kind: str = "person",
    metadata: dict[str, str] | None = None,
) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        tenant_id=tenant,
        namespace=namespace,
        kind=kind,
        label=label,
        metadata=metadata or {},
        source_refs=(f"message:{node_id}",),
    )


def _edge(
    edge_id: str,
    source: str,
    target: str,
    relation: str = "knows",
    *,
    tenant: str = "tenant-a",
    namespace: str = "conversation",
) -> GraphEdge:
    return GraphEdge(
        edge_id=edge_id,
        tenant_id=tenant,
        namespace=namespace,
        source_node_id=source,
        target_node_id=target,
        relation=relation,
        source_refs=(f"message:{edge_id}",),
    )


def test_tenant_namespace_isolation_crud_and_node_delete_cascade(tmp_path: Path) -> None:
    store = SQLiteGraphStore(tmp_path / "graph.db")
    store.upsert_nodes(
        "tenant-a",
        "conversation",
        [_node("same", "Alice"), _node("other", "Bob")],
    )
    store.upsert_nodes("tenant-b", "conversation", [_node("same", "Mallory", tenant="tenant-b")])
    store.upsert_edges("tenant-a", "conversation", [_edge("edge-1", "same", "other")])

    assert store.get_node("tenant-a", "conversation", "same")["label"] == "Alice"
    assert store.get_node("tenant-b", "conversation", "same")["label"] == "Mallory"
    assert store.get_node("tenant-a", "another-namespace", "same") is None
    assert store.get_edge("tenant-b", "conversation", "edge-1") is None
    assert store.delete_node("tenant-a", "conversation", "same") is True
    assert store.get_edge("tenant-a", "conversation", "edge-1") is None
    assert store.delete_node("tenant-a", "conversation", "same") is False


def test_search_filters_are_literal_and_page_tokens_continue(tmp_path: Path) -> None:
    store = SQLiteGraphStore(tmp_path / "graph.db", max_page_size=2)
    store.upsert_nodes(
        "tenant-a",
        "conversation",
        [
            _node("01", "Cyrene Memory", metadata={"area": "chat"}),
            _node("02", "Cyrene Memory Later", metadata={"area": "chat"}),
            _node("03", "Memory % underscore", metadata={"area": "other"}),
        ],
    )

    first, token = store.search_nodes(
        "tenant-a",
        "conversation",
        query="memory",
        metadata_filters={"area": "chat"},
        page_size=1,
    )
    reopened = SQLiteGraphStore(tmp_path / "graph.db", max_page_size=2)
    second, next_token = reopened.search_nodes(
        "tenant-a",
        "conversation",
        query="memory",
        metadata_filters={"area": "chat"},
        page_size=1,
        page_token=token,
    )
    assert [item["node_id"] for item in first + second] == ["01", "02"]
    assert token
    assert next_token == ""

    literal, _ = store.search_nodes("tenant-a", "conversation", query="%", page_size=2)
    assert [item["node_id"] for item in literal] == ["03"]


def test_search_cursor_is_bound_to_scope_filters_and_page_size(tmp_path: Path) -> None:
    store = SQLiteGraphStore(tmp_path / "graph.db", max_page_size=4)
    store.upsert_nodes(
        "tenant-a",
        "conversation",
        [
            _node("01", "memory one", metadata={"area": "chat"}),
            _node("02", "memory two", metadata={"area": "chat"}),
        ],
    )
    _, token = store.search_nodes(
        "tenant-a",
        "conversation",
        query="memory",
        kind="person",
        metadata_filters={"area": "chat"},
        page_size=1,
    )
    assert token

    changed_scopes = [
        {"tenant_id": "tenant-b"},
        {"namespace": "private"},
        {"query": "alice"},
        {"kind": "place"},
        {"metadata_filters": {"area": "other"}},
        {"page_size": 2},
    ]
    for changed in changed_scopes:
        arguments = {
            "tenant_id": "tenant-a",
            "namespace": "conversation",
            "query": "memory",
            "kind": "person",
            "metadata_filters": {"area": "chat"},
            "page_size": 1,
            "page_token": token,
        }
        arguments.update(changed)
        with pytest.raises(GraphStoreError, match="page_token"):
            store.search_nodes(**arguments)

    tampered = ("A" if token[0] != "A" else "B") + token[1:]
    with pytest.raises(GraphStoreError, match="signature"):
        store.search_nodes(
            "tenant-a",
            "conversation",
            query="memory",
            kind="person",
            metadata_filters={"area": "chat"},
            page_size=1,
            page_token=tampered,
        )
    with pytest.raises(GraphStoreError, match="encoding"):
        store.search_nodes("tenant-a", "conversation", page_token="***")
    with pytest.raises(GraphStoreError, match="encoding"):
        store.search_nodes("tenant-a", "conversation", page_token="A" * 3_000)


def test_expand_is_bounded_bidirectional_and_relation_filtered(tmp_path: Path) -> None:
    store = SQLiteGraphStore(tmp_path / "graph.db", max_expand_depth=3, max_expand_nodes=3)
    store.upsert_nodes(
        "tenant-a",
        "conversation",
        [_node("a", "A"), _node("b", "B"), _node("c", "C"), _node("d", "D")],
    )
    store.upsert_edges(
        "tenant-a",
        "conversation",
        [
            _edge("ab", "a", "b", "knows"),
            _edge("bc", "c", "b", "works_with"),
            _edge("cd", "c", "d", "knows"),
        ],
    )

    nodes, edges = store.expand("tenant-a", "conversation", ["a"], max_depth=2)
    assert [item["node_id"] for item in nodes] == ["a", "b", "c"]
    assert {item["edge_id"] for item in edges} == {"ab", "bc"}
    filtered_nodes, filtered_edges = store.expand(
        "tenant-a", "conversation", ["a"], relation_filters=["knows"], max_depth=3
    )
    assert [item["node_id"] for item in filtered_nodes] == ["a", "b"]
    assert [item["edge_id"] for item in filtered_edges] == ["ab"]
    with pytest.raises(GraphStoreError, match="node_limit"):
        store.expand("tenant-a", "conversation", ["a"], node_limit=4)


def test_high_degree_star_stops_at_edge_or_node_quota(tmp_path: Path) -> None:
    path = tmp_path / "star.db"
    edge_bounded = SQLiteGraphStore(
        path,
        max_batch_items=300,
        max_expand_nodes=250,
        max_expand_edges=8,
    )
    nodes = [_node("center", "Center")] + [
        _node(f"leaf-{index:03d}", f"Leaf {index}") for index in range(200)
    ]
    edges = [_edge(f"spoke-{index:03d}", "center", f"leaf-{index:03d}") for index in range(200)]
    edge_bounded.upsert_nodes("tenant-a", "conversation", nodes)
    edge_bounded.upsert_edges("tenant-a", "conversation", edges)
    with pytest.raises(GraphStoreError, match="edge count"):
        edge_bounded.expand("tenant-a", "conversation", ["center"], max_depth=1)

    node_bounded = SQLiteGraphStore(
        path,
        max_expand_nodes=3,
        max_expand_edges=100,
    )
    with pytest.raises(GraphStoreError, match="node count"):
        node_bounded.expand("tenant-a", "conversation", ["center"], max_depth=1)


def test_batch_import_is_atomic_and_export_pages_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "graph.db"
    store = SQLiteGraphStore(path, max_batch_items=10, max_page_size=2)
    store.upsert_nodes("tenant-a", "conversation", [_node("old", "Retained")])

    with pytest.raises(GraphStoreError, match="endpoint"):
        store.import_batch(
            "tenant-a",
            "conversation",
            [_node("new-a", "A"), _node("new-b", "B")],
            [_edge("bad", "new-a", "missing")],
            overwrite=True,
        )
    assert store.get_node("tenant-a", "conversation", "old") is not None
    assert store.get_node("tenant-a", "conversation", "new-a") is None

    nodes = [_node("a", "A"), _node("b", "B"), _node("c", "C")]
    edges = [_edge("ab", "a", "b"), _edge("bc", "b", "c")]
    assert store.import_batch("tenant-a", "conversation", nodes, edges) == (3, 2)
    exported_nodes: list[str] = []
    exported_edges: list[str] = []
    token = None
    while True:
        page_nodes, page_edges, token = store.export_batch(
            "tenant-a", "conversation", page_size=2, page_token=token
        )
        exported_nodes.extend(item["node_id"] for item in page_nodes)
        exported_edges.extend(item["edge_id"] for item in page_edges)
        if not token:
            break
    assert exported_nodes == ["a", "b", "c", "old"]
    assert exported_edges == ["ab", "bc"]

    reopened = SQLiteGraphStore(path)
    assert reopened.get_node("tenant-a", "conversation", "c")["label"] == "C"
    assert reopened.get_edge("tenant-a", "conversation", "bc")["target_node_id"] == "c"


def test_export_page_boundary_moves_from_nodes_to_edges_without_loss(tmp_path: Path) -> None:
    store = SQLiteGraphStore(tmp_path / "graph.db", max_page_size=2)
    store.upsert_nodes("tenant-a", "conversation", [_node("a", "A"), _node("b", "B")])
    store.upsert_edges("tenant-a", "conversation", [_edge("ab", "a", "b")])

    nodes, edges, token = store.export_batch("tenant-a", "conversation", page_size=2)
    assert [item["node_id"] for item in nodes] == ["a", "b"]
    assert edges == []
    assert token
    with pytest.raises(GraphStoreError, match="page_token"):
        store.export_batch("tenant-b", "conversation", page_size=2, page_token=token)
    with pytest.raises(GraphStoreError, match="page_token"):
        store.export_batch("tenant-a", "conversation", page_size=1, page_token=token)
    next_nodes, next_edges, next_token = store.export_batch(
        "tenant-a", "conversation", page_size=2, page_token=token
    )
    assert next_nodes == []
    assert [item["edge_id"] for item in next_edges] == ["ab"]
    assert next_token == ""


def test_scope_mismatch_quota_and_unknown_edge_endpoint_fail_closed(tmp_path: Path) -> None:
    store = SQLiteGraphStore(tmp_path / "graph.db", max_nodes_per_namespace=1, max_batch_items=2)
    with pytest.raises(GraphStoreError, match="scope"):
        store.upsert_nodes("tenant-a", "conversation", [_node("a", "A", tenant="tenant-b")])
    store.upsert_nodes("tenant-a", "conversation", [_node("a", "A")])
    with pytest.raises(GraphStoreError, match="quota"):
        store.upsert_nodes("tenant-a", "conversation", [_node("b", "B")])
    with pytest.raises(GraphStoreError, match="duplicate node_id"):
        store.upsert_nodes("tenant-a", "conversation", [_node("a", "A1"), _node("a", "A2")])
    with pytest.raises(GraphStoreError, match="endpoint"):
        store.upsert_edges("tenant-a", "conversation", [_edge("bad", "a", "missing")])
    assert store.get_edge("tenant-a", "conversation", "bad") is None
