"""Python bindings and codec for the Plugins-owned ``knowledge.graph.v1``."""

from ._generated import knowledge_graph_pb2
from .codec import (
    CAPABILITY_ID,
    INTERFACE_VERSION,
    METHODS,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    KnowledgeGraphCodecError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)

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
    "knowledge_graph_pb2",
]
