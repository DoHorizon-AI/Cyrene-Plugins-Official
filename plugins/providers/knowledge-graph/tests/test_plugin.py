"""DirectPluginRuntime tests for canonical knowledge.graph.v1 methods.

中文：验证 protobuf SDK 接线、读写方法及错误映射。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cyrene_knowledge_graph_contracts import (
    CAPABILITY_ID,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    decode_response,
    encode_request,
)
from cyrene_knowledge_graph_contracts import knowledge_graph_pb2 as wire

from knowledge_graph_provider import GraphSettings, KnowledgeGraphProvider, TypedPayload


def _invoke(provider: KnowledgeGraphProvider, method: str, request: Any, response_type: Any) -> Any:
    ok, result = provider.on_invoke(
        CAPABILITY_ID,
        method,
        encode_request(method, request),
        request_type_url=REQUEST_TYPE_URLS[method],
    )
    assert ok is True
    assert isinstance(result, TypedPayload)
    assert result.type_url == RESPONSE_TYPE_URLS[method]
    response = decode_response(method, result.value)
    assert isinstance(response, response_type)
    return response


def _provider(tmp_path: Path) -> KnowledgeGraphProvider:
    settings = GraphSettings.from_mapping({"database_path": str(tmp_path / "graph.db")})
    return KnowledgeGraphProvider(settings=settings)


def _node(node_id: str, label: str) -> wire.GraphNode:
    return wire.GraphNode(
        node_id=node_id,
        tenant_id="tenant-a",
        namespace="chat",
        kind="person",
        label=label,
        metadata={"source": "conversation"},
        source_refs=[f"message:{node_id}"],
    )


def test_all_graph_methods_round_trip_through_the_canonical_sdk(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    upsert = _invoke(
        provider,
        "upsert_nodes",
        wire.UpsertNodesRequest(
            tenant_id="tenant-a",
            namespace="chat",
            nodes=[_node("alice", "Alice"), _node("bob", "Bob")],
        ),
        wire.UpsertNodesResponse,
    )
    assert upsert.upserted_count == 2

    edge = wire.GraphEdge(
        edge_id="alice-knows-bob",
        tenant_id="tenant-a",
        namespace="chat",
        source_node_id="alice",
        target_node_id="bob",
        relation="knows",
        source_refs=["message:relationship"],
    )
    assert (
        _invoke(
            provider,
            "upsert_edges",
            wire.UpsertEdgesRequest(tenant_id="tenant-a", namespace="chat", edges=[edge]),
            wire.UpsertEdgesResponse,
        ).upserted_count
        == 1
    )

    get_node = _invoke(
        provider,
        "get_node",
        wire.GetNodeRequest(tenant_id="tenant-a", namespace="chat", node_id="alice"),
        wire.GetNodeResponse,
    )
    assert get_node.node.label == "Alice"
    assert list(get_node.node.source_refs) == ["message:alice"]

    search_nodes = _invoke(
        provider,
        "search_nodes",
        wire.SearchNodesRequest(tenant_id="tenant-a", namespace="chat", query="ali", page_size=1),
        wire.SearchNodesResponse,
    )
    assert [item.node_id for item in search_nodes.batch.nodes] == ["alice"]
    assert search_nodes.batch.next_page_token == ""

    get_edge = _invoke(
        provider,
        "get_edge",
        wire.GetEdgeRequest(tenant_id="tenant-a", namespace="chat", edge_id="alice-knows-bob"),
        wire.GetEdgeResponse,
    )
    assert get_edge.edge.target_node_id == "bob"

    search_edges = _invoke(
        provider,
        "search_edges",
        wire.SearchEdgesRequest(
            tenant_id="tenant-a", namespace="chat", node_id="bob", relation="knows"
        ),
        wire.SearchEdgesResponse,
    )
    assert [item.edge_id for item in search_edges.batch.edges] == ["alice-knows-bob"]

    expanded = _invoke(
        provider,
        "expand",
        wire.ExpandGraphRequest(
            tenant_id="tenant-a",
            namespace="chat",
            seed_node_ids=["alice"],
            max_depth=1,
            node_limit=10,
        ),
        wire.ExpandGraphResponse,
    )
    assert {item.node_id for item in expanded.batch.nodes} == {"alice", "bob"}
    assert [item.edge_id for item in expanded.batch.edges] == ["alice-knows-bob"]

    export = _invoke(
        provider,
        "export_batch",
        wire.ExportGraphRequest(tenant_id="tenant-a", namespace="chat", page_size=10),
        wire.ExportGraphResponse,
    )
    assert len(export.batch.nodes) == 2
    assert len(export.batch.edges) == 1
    imported = _invoke(
        provider,
        "import_batch",
        wire.ImportGraphRequest(
            tenant_id="tenant-b",
            namespace="imported",
            nodes=[
                wire.GraphNode(
                    node_id="copy-alice",
                    tenant_id="tenant-b",
                    namespace="imported",
                    kind="person",
                    label="Alice copy",
                ),
                wire.GraphNode(
                    node_id="copy-bob",
                    tenant_id="tenant-b",
                    namespace="imported",
                    kind="person",
                    label="Bob copy",
                ),
            ],
            overwrite=True,
        ),
        wire.ImportGraphResponse,
    )
    assert imported.imported.node_count == 2
    assert imported.imported.edge_count == 0

    assert _invoke(
        provider,
        "delete_edge",
        wire.DeleteEdgeRequest(tenant_id="tenant-a", namespace="chat", edge_id="alice-knows-bob"),
        wire.DeleteEdgeResponse,
    ).deleted
    assert _invoke(
        provider,
        "delete_node",
        wire.DeleteNodeRequest(tenant_id="tenant-a", namespace="chat", node_id="bob"),
        wire.DeleteNodeResponse,
    ).deleted


def test_invalid_request_type_and_scope_error_are_reported_by_contract(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    ok, message = provider.on_invoke(
        CAPABILITY_ID,
        "get_node",
        b"",
        request_type_url=REQUEST_TYPE_URLS["get_edge"],
    )
    assert ok is False
    assert "type URL mismatch" in message

    response = _invoke(
        provider,
        "upsert_nodes",
        wire.UpsertNodesRequest(
            tenant_id="tenant-a", namespace="chat", nodes=[_node("wrong", "Wrong")]
        ),
        wire.UpsertNodesResponse,
    )
    # A scope-less request fails closed before it can query the namespace.
    invalid_scope = _invoke(
        provider,
        "search_nodes",
        wire.SearchNodesRequest(tenant_id="", namespace="chat"),
        wire.SearchNodesResponse,
    )
    assert response.upserted_count == 1
    assert invalid_scope.WhichOneof("result") == "error"
    assert invalid_scope.error.code == wire.KNOWLEDGE_GRAPH_ERROR_CODE_INVALID_INPUT


def test_expand_response_byte_limit_returns_typed_quota_error(tmp_path: Path) -> None:
    settings = GraphSettings.from_mapping(
        {
            "database_path": str(tmp_path / "graph.db"),
            "max_expand_response_bytes": 256,
        }
    )
    provider = KnowledgeGraphProvider(settings=settings)
    large_node = wire.GraphNode(
        node_id="large",
        tenant_id="tenant-a",
        namespace="chat",
        kind="document",
        label="Large source",
        description="x" * 1_024,
    )
    _invoke(
        provider,
        "upsert_nodes",
        wire.UpsertNodesRequest(
            tenant_id="tenant-a",
            namespace="chat",
            nodes=[large_node, _node("neighbor", "Neighbor")],
        ),
        wire.UpsertNodesResponse,
    )
    _invoke(
        provider,
        "upsert_edges",
        wire.UpsertEdgesRequest(
            tenant_id="tenant-a",
            namespace="chat",
            edges=[
                wire.GraphEdge(
                    edge_id="large-neighbor",
                    tenant_id="tenant-a",
                    namespace="chat",
                    source_node_id="large",
                    target_node_id="neighbor",
                    relation="contains",
                )
            ],
        ),
        wire.UpsertEdgesResponse,
    )
    response = _invoke(
        provider,
        "expand",
        wire.ExpandGraphRequest(
            tenant_id="tenant-a",
            namespace="chat",
            seed_node_ids=["large"],
            max_depth=1,
        ),
        wire.ExpandGraphResponse,
    )
    assert response.WhichOneof("result") == "error"
    assert response.error.code == wire.KNOWLEDGE_GRAPH_ERROR_CODE_QUOTA_EXCEEDED
