"""Python bindings for the Plugins-owned ``tool.provider.v1`` contract."""

from ._generated import tool_provider_pb2
from .contracts import (
    CALL_TOOL_REQUEST_TYPE_URL,
    CALL_TOOL_RESPONSE_TYPE_URL,
    CAPABILITY_ID,
    INTERFACE_VERSION,
    LIST_TOOLS_REQUEST_TYPE_URL,
    LIST_TOOLS_RESPONSE_TYPE_URL,
    METHOD_CALL_TOOL,
    METHOD_LIST_TOOLS,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
)

__all__ = [
    "CALL_TOOL_REQUEST_TYPE_URL",
    "CALL_TOOL_RESPONSE_TYPE_URL",
    "CAPABILITY_ID",
    "INTERFACE_VERSION",
    "LIST_TOOLS_REQUEST_TYPE_URL",
    "LIST_TOOLS_RESPONSE_TYPE_URL",
    "METHOD_CALL_TOOL",
    "METHOD_LIST_TOOLS",
    "REQUEST_TYPE_URLS",
    "RESPONSE_TYPE_URLS",
    "tool_provider_pb2",
]
