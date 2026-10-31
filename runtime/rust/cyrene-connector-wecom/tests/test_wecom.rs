// SPDX-License-Identifier: Apache-2.0
//! Unit and integration tests for the pure Rust WeCom connector.

use prost::Message;
use std::sync::Arc;

use cyrene_connector_wecom::{
    ConnectorMode, WeComCliClient, WeComConfig, WeComService, WeComToolProvider,
};
use cyrene_plugin_contracts::direct_plugin_runtime_v1::{
    direct_invocation_response::Result as InvocationResult, DirectInvocationRequest,
};
use cyrene_plugin_contracts::tool_provider::{
    CAPABILITY_ID as TOOL_PROVIDER_CAPABILITY, LIST_TOOLS_RESPONSE_TYPE_URL, METHOD_LIST_TOOLS,
};
use cyrene_plugin_contracts::tool_provider_v1::{
    call_tool_response, list_tools_response, CallToolRequest, ListToolsRequest,
    ToolProviderErrorCode,
};

#[test]
fn test_config_validation_modes() {
    // App mode missing fields
    let mut cfg = WeComConfig {
        mode: ConnectorMode::App,
        ..Default::default()
    };
    assert!(cfg.validate().is_err());

    cfg.corp_id = Some("ww123".to_string());
    cfg.corp_secret = Some("sec123".to_string());
    cfg.agent_id = Some(1000002);
    assert!(cfg.validate().is_ok());

    // Bot mode
    let mut bot_cfg = WeComConfig {
        mode: ConnectorMode::Bot,
        ..Default::default()
    };
    assert!(bot_cfg.validate().is_err());
    bot_cfg.bot_key = Some("bot-key".to_string());
    bot_cfg.bot_secret = Some("bot-secret".to_string());
    assert!(bot_cfg.validate().is_ok());

    // Hybrid mode
    let hybrid_cfg = WeComConfig {
        mode: ConnectorMode::Hybrid,
        corp_id: Some("ww123".to_string()),
        corp_secret: Some("sec123".to_string()),
        agent_id: Some(1000002),
        bot_key: Some("bot-key".to_string()),
        bot_secret: Some("bot-secret".to_string()),
        ..Default::default()
    };
    assert!(hybrid_cfg.validate().is_ok());
}

#[tokio::test]
async fn test_tool_provider_list_tools() {
    let cli = Arc::new(WeComCliClient::new(None).unwrap());
    let provider = WeComToolProvider::new(cli);

    let req = ListToolsRequest { binding_id: None };
    let resp = provider.list_tools(&req);

    match resp.result.unwrap() {
        list_tools_response::Result::Catalog(catalog) => {
            assert_eq!(catalog.tools.len(), 8);
            let tool_ids: Vec<&str> = catalog
                .tools
                .iter()
                .map(|t| t.provider_tool_id.as_str())
                .collect();
            assert!(tool_ids.contains(&"wecom_auth_status"));
            assert!(tool_ids.contains(&"wecom_contact_search"));
            assert!(tool_ids.contains(&"wecom_calendar_list"));
            assert!(tool_ids.contains(&"wecom_calendar_create"));
            assert!(tool_ids.contains(&"wecom_todo_list"));
            assert!(tool_ids.contains(&"wecom_todo_create"));
            assert!(tool_ids.contains(&"wecom_doc_search"));
            assert!(tool_ids.contains(&"wecom_cli_exec"));
        }
        list_tools_response::Result::Error(e) => {
            panic!("Unexpected error: {:?}", e);
        }
    }
}

#[tokio::test]
async fn test_tool_provider_call_auth_status() {
    let cli = Arc::new(WeComCliClient::new(None).unwrap());
    let provider = WeComToolProvider::new(cli);

    let req = CallToolRequest {
        binding_id: "wecom.cli".to_string(),
        provider_tool_id: "wecom_auth_status".to_string(),
        arguments_json: "{}".to_string(),
        catalog_version: None,
    };

    let resp = provider.call_tool(&req).await;
    match resp.result.unwrap() {
        call_tool_response::Result::Outcome(outcome) => {
            assert!(!outcome.is_error);
            assert_eq!(outcome.content.len(), 1);
            let part = &outcome.content[0];
            if let Some(
                cyrene_plugin_contracts::tool_provider_v1::tool_content_part::Content::Text(ref t),
            ) = part.content
            {
                assert!(t.text.contains("authorized"));
                assert!(t.text.contains("1.3.4"));
            } else {
                panic!("Expected text content");
            }
        }
        call_tool_response::Result::Error(e) => {
            panic!("Call failed: {:?}", e);
        }
    }
}

#[tokio::test]
async fn test_tool_provider_call_unknown_tool() {
    let cli = Arc::new(WeComCliClient::new(None).unwrap());
    let provider = WeComToolProvider::new(cli);

    let req = CallToolRequest {
        binding_id: "wecom.cli".to_string(),
        provider_tool_id: "wecom_non_existent".to_string(),
        arguments_json: "{}".to_string(),
        catalog_version: None,
    };

    let resp = provider.call_tool(&req).await;
    match resp.result.unwrap() {
        call_tool_response::Result::Error(e) => {
            assert_eq!(e.code, ToolProviderErrorCode::ToolNotFound as i32);
        }
        call_tool_response::Result::Outcome(_) => {
            panic!("Expected tool not found error");
        }
    }
}

#[tokio::test]
async fn test_tool_provider_call_invalid_json() {
    let cli = Arc::new(WeComCliClient::new(None).unwrap());
    let provider = WeComToolProvider::new(cli);

    let req = CallToolRequest {
        binding_id: "wecom.cli".to_string(),
        provider_tool_id: "wecom_contact_search".to_string(),
        arguments_json: "{not valid json".to_string(),
        catalog_version: None,
    };

    let resp = provider.call_tool(&req).await;
    match resp.result.unwrap() {
        call_tool_response::Result::Error(e) => {
            assert_eq!(e.code, ToolProviderErrorCode::InvalidArguments as i32);
        }
        call_tool_response::Result::Outcome(_) => {
            panic!("Expected invalid arguments error");
        }
    }
}

#[tokio::test]
async fn test_tool_provider_cli_exec_disallowed_command() {
    let cli = Arc::new(WeComCliClient::new(None).unwrap());
    let provider = WeComToolProvider::new(cli);

    let req = CallToolRequest {
        binding_id: "wecom.cli".to_string(),
        provider_tool_id: "wecom_cli_exec".to_string(),
        arguments_json: r#"{"args": ["rm", "-rf", "/"]}"#.to_string(),
        catalog_version: None,
    };

    let resp = provider.call_tool(&req).await;
    match resp.result.unwrap() {
        call_tool_response::Result::Error(e) => {
            assert_eq!(e.code, ToolProviderErrorCode::ExecutionFailed as i32);
            assert!(e.message.contains("whitelist"));
        }
        call_tool_response::Result::Outcome(_) => {
            panic!("Expected execution failure error");
        }
    }
}

#[tokio::test]
async fn test_service_direct_invocation_dispatch() {
    let cli = Arc::new(WeComCliClient::new(None).unwrap());
    let provider = Arc::new(WeComToolProvider::new(cli));
    let cfg = WeComConfig::default();
    let service = WeComService::new(cfg, None, None, Some(provider));

    // 1. Dispatch list_tools
    let list_req = ListToolsRequest { binding_id: None };
    let mut payload = Vec::new();
    list_req.encode(&mut payload).unwrap();

    let direct_req = DirectInvocationRequest {
        capability: TOOL_PROVIDER_CAPABILITY.to_string(),
        interface_version: "1".to_string(),
        method: METHOD_LIST_TOOLS.to_string(),
        payload_type_url: String::new(),
        payload,
        request_id: "req-1".to_string(),
        stream_mode: 0,
    };

    let resp = service.handle_invoke(direct_req).await.unwrap();
    match resp.result.unwrap() {
        InvocationResult::Payload(p) => {
            assert_eq!(p.type_url, LIST_TOOLS_RESPONSE_TYPE_URL);
            assert!(!p.value.is_empty());
        }
        InvocationResult::Error(e) => {
            panic!("Invocation failed: {:?}", e);
        }
    }

    // 2. Dispatch unknown capability
    let unknown_req = DirectInvocationRequest {
        capability: "non.existent.capability".to_string(),
        interface_version: "1".to_string(),
        method: "foo".to_string(),
        payload_type_url: String::new(),
        payload: vec![],
        request_id: "req-2".to_string(),
        stream_mode: 0,
    };

    let resp2 = service.handle_invoke(unknown_req).await.unwrap();
    match resp2.result.unwrap() {
        InvocationResult::Error(e) => {
            assert_eq!(e.domain_code, "METHOD_NOT_FOUND");
        }
        InvocationResult::Payload(_) => {
            panic!("Expected method not found error");
        }
    }
}
