// ╔══════════════════════════════════════════════════════════════════════╗
// ║ 📄 File: tool_provider_contract_tck.rs                               ║
// ║ Module: CYRENE Plugins Official                                     ║
// ║ Role: Direct tool.provider.v1 capability conformance & TCK.         ║
// ╚══════════════════════════════════════════════════════════════════════╝
// 中文：文件：tool_provider_contract_tck.rs
// 中文：模块：CYRENE Plugins Official
// 中文：职责：对直接使用 tool.provider.v1 capability 的实现执行一致性验证与 TCK。

use cyrene_plugin_contracts::tool_provider::{
    CALL_TOOL_REQUEST_TYPE_URL, CALL_TOOL_RESPONSE_TYPE_URL, CAPABILITY_ID, INTERFACE_VERSION,
    LIST_TOOLS_REQUEST_TYPE_URL, LIST_TOOLS_RESPONSE_TYPE_URL, METHOD_CALL_TOOL, METHOD_LIST_TOOLS,
};
use cyrene_plugin_contracts::tool_provider_v1::{
    call_tool_response, list_tools_response, tool_content_part, CallToolRequest, CallToolResponse,
    ListToolsRequest, ListToolsResponse, ToolCallOutcome, ToolCatalog, ToolContentPart,
    ToolDescriptor, ToolJsonContent, ToolProviderError, ToolProviderErrorCode, ToolTextContent,
};
use prost::Message;

#[test]
fn tool_provider_identifiers_and_error_codes_are_stable() {
    assert_eq!(CAPABILITY_ID, "tool.provider.v1");
    assert_eq!(INTERFACE_VERSION, "1");
    assert_eq!(METHOD_LIST_TOOLS, "list_tools");
    assert_eq!(METHOD_CALL_TOOL, "call_tool");
    assert_eq!(
        LIST_TOOLS_REQUEST_TYPE_URL,
        "type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest"
    );
    assert_eq!(
        LIST_TOOLS_RESPONSE_TYPE_URL,
        "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse"
    );
    assert_eq!(
        CALL_TOOL_REQUEST_TYPE_URL,
        "type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest"
    );
    assert_eq!(
        CALL_TOOL_RESPONSE_TYPE_URL,
        "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse"
    );

    assert_eq!(ToolProviderErrorCode::Unspecified as i32, 0);
    assert_eq!(ToolProviderErrorCode::ToolNotFound as i32, 1);
    assert_eq!(ToolProviderErrorCode::InvalidArguments as i32, 2);
    assert_eq!(ToolProviderErrorCode::ExecutionFailed as i32, 3);
    assert_eq!(ToolProviderErrorCode::ProviderError as i32, 4);
    assert_eq!(ToolProviderErrorCode::Timeout as i32, 5);
}

#[test]
fn tool_catalog_round_trip_preserves_identity_and_schema_facts() {
    let request = ListToolsRequest {
        binding_id: Some("mcp.local".to_string()),
    };
    let decoded_request = ListToolsRequest::decode(request.encode_to_vec().as_slice()).unwrap();
    assert_eq!(decoded_request, request);

    let mut catalog = ToolCatalog {
        catalog_version: "mcp.local@2026-09-13T00:00:00Z".to_string(),
        tools: vec![],
    };
    catalog.tools.push(ToolDescriptor {
        binding_id: "mcp.local".to_string(),
        provider_tool_id: "weather.get_forecast".to_string(),
        display_name: "Weather".to_string(),
        description: "Look up a forecast".to_string(),
        input_schema_json: "{\"type\":\"object\"}".to_string(),
        output_schema_json: Some("{\"type\":\"object\"}".to_string()),
    });
    // Identity is the (binding_id, provider_tool_id) pair; a second tool may
    // share the display name without colliding.
    // 中文：身份由 `(binding_id, provider_tool_id)` 组成；不同工具即使共享展示名称，也不会发生冲突。
    catalog.tools.push(ToolDescriptor {
        binding_id: "mcp.remote".to_string(),
        provider_tool_id: "weather.get_forecast".to_string(),
        display_name: "Weather".to_string(),
        description: String::new(),
        input_schema_json: "{\"type\":\"object\"}".to_string(),
        output_schema_json: None,
    });

    let response = ListToolsResponse {
        result: Some(list_tools_response::Result::Catalog(catalog)),
    };
    let decoded = ListToolsResponse::decode(response.encode_to_vec().as_slice()).unwrap();
    assert_eq!(decoded, response);
    let decoded_catalog = match decoded.result.as_ref().unwrap() {
        list_tools_response::Result::Catalog(catalog) => catalog,
        list_tools_response::Result::Error(error) => panic!("unexpected error: {error:?}"),
    };
    assert_eq!(decoded_catalog.tools.len(), 2);
    assert_eq!(decoded_catalog.tools[0].binding_id, "mcp.local");
    assert_eq!(
        decoded_catalog.tools[0].provider_tool_id,
        "weather.get_forecast"
    );
    assert!(decoded_catalog.tools[1].output_schema_json.is_none());
}

#[test]
fn call_tool_round_trip_preserves_arguments_and_content_parts() {
    let request = CallToolRequest {
        binding_id: "mcp.local".to_string(),
        provider_tool_id: "weather.get_forecast".to_string(),
        arguments_json: "{\"city\":\"Paris\"}".to_string(),
        catalog_version: Some("mcp.local@2026-09-13T00:00:00Z".to_string()),
    };
    let decoded_request = CallToolRequest::decode(request.encode_to_vec().as_slice()).unwrap();
    assert_eq!(decoded_request, request);

    let outcome = ToolCallOutcome {
        content: vec![
            ToolContentPart {
                content: Some(tool_content_part::Content::Text(ToolTextContent {
                    text: "21C and clear".to_string(),
                })),
            },
            ToolContentPart {
                content: Some(tool_content_part::Content::Json(ToolJsonContent {
                    json: "{\"temp_c\":21}".to_string(),
                })),
            },
        ],
        is_error: false,
    };
    let response = CallToolResponse {
        result: Some(call_tool_response::Result::Outcome(outcome)),
    };
    let decoded = CallToolResponse::decode(response.encode_to_vec().as_slice()).unwrap();
    assert_eq!(decoded, response);

    let denied = CallToolResponse {
        result: Some(call_tool_response::Result::Error(ToolProviderError {
            code: ToolProviderErrorCode::ToolNotFound as i32,
            message: "no such tool in this binding".to_string(),
            retryable: false,
        })),
    };
    let decoded_denied = CallToolResponse::decode(denied.encode_to_vec().as_slice()).unwrap();
    match decoded_denied.result.unwrap() {
        call_tool_response::Result::Error(error) => {
            assert_eq!(error.code, ToolProviderErrorCode::ToolNotFound as i32);
            assert!(!error.retryable);
        }
        call_tool_response::Result::Outcome(_) => panic!("expected a typed error"),
    }
}
