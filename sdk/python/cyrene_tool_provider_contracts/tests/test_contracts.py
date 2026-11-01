"""Generated tool.provider.v1 Python binding checks."""

from cyrene_tool_provider_contracts import (
    CALL_TOOL_REQUEST_TYPE_URL,
    CAPABILITY_ID,
    INTERFACE_VERSION,
    METHOD_CALL_TOOL,
    tool_provider_pb2,
)


def test_call_tool_payload_round_trips_canonical_fields() -> None:
    request = tool_provider_pb2.CallToolRequest(
        binding_id="binding-1",
        provider_tool_id="search",
        arguments_json='{"query":"hello"}',
        catalog_version="catalog-4",
    )
    decoded = tool_provider_pb2.CallToolRequest.FromString(request.SerializeToString())
    assert decoded == request
    assert CAPABILITY_ID == "tool.provider.v1"
    assert INTERFACE_VERSION == "1"
    assert METHOD_CALL_TOOL == "call_tool"
    assert CALL_TOOL_REQUEST_TYPE_URL.endswith("CallToolRequest")
