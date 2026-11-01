"""Public knowledge.graph.v1 payload codec checks."""

import pytest
from cyrene_knowledge_graph_contracts import (
    CAPABILITY_ID,
    METHODS,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    KnowledgeGraphCodecError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    knowledge_graph_pb2,
)


def test_graph_methods_and_direct_payload_types_are_stable() -> None:
    assert CAPABILITY_ID == "knowledge.graph.v1"
    assert METHODS == (
        "upsert_nodes",
        "get_node",
        "search_nodes",
        "delete_node",
        "upsert_edges",
        "get_edge",
        "search_edges",
        "delete_edge",
        "expand",
        "import_batch",
        "export_batch",
    )
    assert REQUEST_TYPE_URLS["expand"].endswith("ExpandGraphRequest")
    assert RESPONSE_TYPE_URLS["export_batch"].endswith("ExportGraphResponse")


def test_node_batch_round_trips_metadata_and_source_references() -> None:
    request = knowledge_graph_pb2.UpsertNodesRequest(
        tenant_id="tenant-a",
        namespace="conversation",
        nodes=[
            knowledge_graph_pb2.GraphNode(
                node_id="node-1",
                tenant_id="tenant-a",
                namespace="conversation",
                kind="person",
                label="Morgan",
                description="A source-backed entity",
                metadata={"evidence": "user-stated"},
                source_refs=["message-7"],
                created_at_ms=1726000000000,
                updated_at_ms=1726000000001,
            )
        ],
    )
    encoded = encode_request("upsert_nodes", request)
    decoded = decode_request("upsert_nodes", encoded)
    assert decoded == request
    assert REQUEST_TYPE_URLS["upsert_nodes"].endswith("UpsertNodesRequest")


def test_response_and_invalid_method_contracts() -> None:
    response = knowledge_graph_pb2.ExportGraphResponse(
        batch=knowledge_graph_pb2.ExportGraphBatch(next_page_token="page-2")
    )
    assert (
        decode_response("export_batch", encode_response("export_batch", response))
        == response
    )
    with pytest.raises(KnowledgeGraphCodecError, match="unsupported"):
        encode_request("run_dream", knowledge_graph_pb2.UpsertNodesRequest())
    with pytest.raises(KnowledgeGraphCodecError, match="does not match"):
        encode_request("get_node", knowledge_graph_pb2.UpsertNodesRequest())
    with pytest.raises(KnowledgeGraphCodecError, match="select a result"):
        encode_response("export_batch", knowledge_graph_pb2.ExportGraphResponse())
