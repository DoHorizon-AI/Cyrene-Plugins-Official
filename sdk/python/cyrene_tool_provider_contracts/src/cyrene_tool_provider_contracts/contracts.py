"""
┌─────────────────────────────────────────────────────────────────────────┐
│ 📄 contracts.py                                                         │
│ Module: cyrene_tool_provider_contracts.contracts                         │
│ Role: Direct Plugin identifiers for tool.provider.v1.                    │
│                                                                         │
│ 模块职责：提供 tool.provider.v1 的 Direct Plugin 稳定标识符。            │
└─────────────────────────────────────────────────────────────────────────┘
"""

CAPABILITY_ID = "tool.provider.v1"
INTERFACE_VERSION = "1"
METHOD_LIST_TOOLS = "list_tools"
METHOD_CALL_TOOL = "call_tool"
METHODS = (METHOD_LIST_TOOLS, METHOD_CALL_TOOL)

LIST_TOOLS_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest"
LIST_TOOLS_RESPONSE_TYPE_URL = (
    "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse"
)
CALL_TOOL_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest"
CALL_TOOL_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse"

REQUEST_TYPE_URLS = {
    METHOD_LIST_TOOLS: LIST_TOOLS_REQUEST_TYPE_URL,
    METHOD_CALL_TOOL: CALL_TOOL_REQUEST_TYPE_URL,
}
RESPONSE_TYPE_URLS = {
    METHOD_LIST_TOOLS: LIST_TOOLS_RESPONSE_TYPE_URL,
    METHOD_CALL_TOOL: CALL_TOOL_RESPONSE_TYPE_URL,
}

__all__ = [
    "CALL_TOOL_REQUEST_TYPE_URL",
    "CALL_TOOL_RESPONSE_TYPE_URL",
    "CAPABILITY_ID",
    "INTERFACE_VERSION",
    "LIST_TOOLS_REQUEST_TYPE_URL",
    "LIST_TOOLS_RESPONSE_TYPE_URL",
    "METHODS",
    "METHOD_CALL_TOOL",
    "METHOD_LIST_TOOLS",
    "REQUEST_TYPE_URLS",
    "RESPONSE_TYPE_URLS",
]
