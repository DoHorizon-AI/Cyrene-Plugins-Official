//! Stable identifiers for the canonical `tool.provider.v1` payloads.

pub const CAPABILITY_ID: &str = "tool.provider.v1";
pub const INTERFACE_VERSION: &str = "1";

pub const METHOD_LIST_TOOLS: &str = "list_tools";
pub const METHOD_CALL_TOOL: &str = "call_tool";

pub const LIST_TOOLS_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest";
pub const LIST_TOOLS_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse";
pub const CALL_TOOL_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest";
pub const CALL_TOOL_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse";
