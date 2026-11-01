"""
┌─────────────────────────────────────────────────────────────────────────┐
│ 📄 codec.py                                                             │
│ Module: cyrene_knowledge_graph_contracts.codec                           │
│ Role: Typed method map and protobuf request/response codec.              │
│                                                                         │
│ 模块职责：提供图谱方法映射和 protobuf 请求/响应编解码。                 │
└─────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from google.protobuf.message import DecodeError, Message

from ._generated import knowledge_graph_pb2

CAPABILITY_ID = "knowledge.graph.v1"
INTERFACE_VERSION = "1"
METHODS = (
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

_TYPE_NAMES = {
    "upsert_nodes": ("UpsertNodesRequest", "UpsertNodesResponse"),
    "get_node": ("GetNodeRequest", "GetNodeResponse"),
    "search_nodes": ("SearchNodesRequest", "SearchNodesResponse"),
    "delete_node": ("DeleteNodeRequest", "DeleteNodeResponse"),
    "upsert_edges": ("UpsertEdgesRequest", "UpsertEdgesResponse"),
    "get_edge": ("GetEdgeRequest", "GetEdgeResponse"),
    "search_edges": ("SearchEdgesRequest", "SearchEdgesResponse"),
    "delete_edge": ("DeleteEdgeRequest", "DeleteEdgeResponse"),
    "expand": ("ExpandGraphRequest", "ExpandGraphResponse"),
    "import_batch": ("ImportGraphRequest", "ImportGraphResponse"),
    "export_batch": ("ExportGraphRequest", "ExportGraphResponse"),
}
REQUEST_TYPE_URLS = {
    method: f"type.cyrene.io/cyrene.knowledge.graph.v1.{names[0]}"
    for method, names in _TYPE_NAMES.items()
}
RESPONSE_TYPE_URLS = {
    method: f"type.cyrene.io/cyrene.knowledge.graph.v1.{names[1]}"
    for method, names in _TYPE_NAMES.items()
}
_REQUEST_TYPES = {
    method: getattr(knowledge_graph_pb2, names[0])
    for method, names in _TYPE_NAMES.items()
}
_RESPONSE_TYPES = {
    method: getattr(knowledge_graph_pb2, names[1])
    for method, names in _TYPE_NAMES.items()
}


class KnowledgeGraphCodecError(ValueError):
    """A graph method or payload does not match the canonical contract."""


def encode_request(method: str, request: Message) -> bytes:
    """Validate and encode the method-specific protobuf request."""

    expected = _REQUEST_TYPES.get(method)
    if expected is None:
        raise KnowledgeGraphCodecError(f"unsupported knowledge graph method: {method}")
    if not isinstance(request, expected):
        raise KnowledgeGraphCodecError(f"request does not match method: {method}")
    return request.SerializeToString(deterministic=True)


def decode_request(method: str, payload: bytes) -> Message:
    """Decode the method-specific protobuf request."""

    return _decode(method, payload, _REQUEST_TYPES, "request")


def encode_response(method: str, response: Message) -> bytes:
    """Validate and encode the method-specific protobuf response."""

    expected = _RESPONSE_TYPES.get(method)
    if expected is None:
        raise KnowledgeGraphCodecError(f"unsupported knowledge graph method: {method}")
    if not isinstance(response, expected):
        raise KnowledgeGraphCodecError(f"response does not match method: {method}")
    if not response.WhichOneof("result"):
        raise KnowledgeGraphCodecError("graph response must select a result")
    return response.SerializeToString(deterministic=True)


def decode_response(method: str, payload: bytes) -> Message:
    """Decode the method-specific protobuf response."""

    return _decode(method, payload, _RESPONSE_TYPES, "response")


def _decode(
    method: str, payload: bytes, messages: dict[str, type[Message]], role: str
) -> Message:
    message_type = messages.get(method)
    if message_type is None:
        raise KnowledgeGraphCodecError(f"unsupported knowledge graph method: {method}")
    if not isinstance(payload, bytes):
        raise KnowledgeGraphCodecError("protobuf payload must be bytes")
    message = message_type()
    try:
        message.ParseFromString(payload)
    except DecodeError as error:
        raise KnowledgeGraphCodecError(
            f"invalid knowledge graph {role}: {error}"
        ) from error
    if role == "response" and not message.WhichOneof("result"):
        raise KnowledgeGraphCodecError("graph response must select a result")
    return message


__all__ = [
    "CAPABILITY_ID",
    "INTERFACE_VERSION",
    "METHODS",
    "REQUEST_TYPE_URLS",
    "RESPONSE_TYPE_URLS",
    "KnowledgeGraphCodecError",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
]
