// SPDX-License-Identifier: Apache-2.0
//! TCK coverage for the per-run tool catalog snapshot (decision A/B):
//! deterministic flat-name projection, inverse routing, and the loop
//! advertising exactly the snapshot's declarations to the model.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use async_trait::async_trait;

use cyrene_agent_runtime::adapter::model_provider::ModelProvider;
use cyrene_agent_runtime::adapter::rig_adapter::RigAgentAdapter;
use cyrene_agent_runtime::adapter::{
    snapshot_tool_catalog, SnapshotToolProvider, ToolCatalogSnapshot,
};
use cyrene_agent_runtime::engine::cancel::CancellationToken;
use cyrene_agent_runtime::engine::turn_loop::CyreneNativeAgentLoop;
use cyrene_agent_runtime::tck::{AgentTckSuite, MockToolCatalogSource};
use cyrene_plugin_contracts::agent_runtime_v1::{
    agent_run_response, AgentRunError, AgentRunRequest,
};
use cyrene_plugin_contracts::model_provider_v1::{
    ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse,
};
use cyrene_plugin_contracts::tool_provider_v1::{ToolCatalog, ToolDescriptor};

fn descriptor(binding: &str, tool: &str) -> ToolDescriptor {
    ToolDescriptor {
        binding_id: binding.to_string(),
        provider_tool_id: tool.to_string(),
        display_name: tool.to_string(),
        description: format!("{tool} description"),
        input_schema_json: "{\"type\":\"object\",\"properties\":{\"text\":{\"type\":\"string\"}}}"
            .to_string(),
        output_schema_json: None,
    }
}

/// Records the model requests so the test can assert tool advertisement.
struct RecordingModelProvider {
    requests: Mutex<Vec<ChatCompletionRequest>>,
    call_count: AtomicUsize,
    tool_turns: usize,
}

impl RecordingModelProvider {
    fn new(tool_turns: usize) -> Self {
        Self {
            requests: Mutex::new(Vec::new()),
            call_count: AtomicUsize::new(0),
            tool_turns,
        }
    }
}

#[async_trait]
impl ModelProvider for RecordingModelProvider {
    async fn complete(
        &self,
        req: ChatCompletionRequest,
    ) -> Result<ChatCompletionResponse, AgentRunError> {
        self.requests.lock().expect("recording lock").push(req);
        let count = self.call_count.fetch_add(1, Ordering::SeqCst);
        let chunk = if count < self.tool_turns {
            ChatCompletionChunk {
                delta: String::new(),
                finish_reason: Some("tool_calls".to_string()),
                prompt_tokens: Some(10),
                completion_tokens: Some(5),
                role: Some("assistant".to_string()),
                tool_calls: vec![
                    cyrene_plugin_contracts::model_provider_v1::ChatToolCallDelta {
                        index: 0,
                        id: Some(format!("call-{count}")),
                        r#type: Some("function".to_string()),
                        function_name: Some("echo".to_string()),
                        function_arguments: Some("{\"text\":\"hi\"}".to_string()),
                    },
                ],
                total_tokens: Some(15),
            }
        } else {
            ChatCompletionChunk {
                delta: "done".to_string(),
                finish_reason: Some("stop".to_string()),
                prompt_tokens: Some(10),
                completion_tokens: Some(5),
                role: Some("assistant".to_string()),
                tool_calls: Vec::new(),
                total_tokens: Some(15),
            }
        };
        Ok(ChatCompletionResponse {
            chunks: vec![chunk],
        })
    }
}

#[tokio::test]
async fn test_tool_catalog_snapshot_tck_native_loop() {
    let native = CyreneNativeAgentLoop::new();
    AgentTckSuite::run_tck_tool_catalog_snapshot(&native).await;
}

#[tokio::test]
async fn test_tool_catalog_snapshot_tck_rig_adapter() {
    let rig = RigAgentAdapter::new();
    AgentTckSuite::run_tck_tool_catalog_snapshot(&rig).await;
}

#[test]
fn test_projection_sanitizes_and_disambiguates_names() {
    let catalog = ToolCatalog {
        catalog_version: "v1".to_string(),
        tools: vec![
            descriptor("mcp.a", "read/file"),
            descriptor("mcp.b", "read file"),
            descriptor("mcp.c", "plain_tool"),
        ],
    };
    let snapshot = ToolCatalogSnapshot::from_catalog(&catalog);

    assert_eq!(snapshot.tool_count(), 3);
    let names: Vec<&str> = snapshot
        .declarations()
        .iter()
        .map(|declaration| declaration.name.as_str())
        .collect();
    assert_eq!(
        names,
        vec!["mcp_a__read_file", "mcp_b__read_file", "plain_tool"]
    );

    let route = snapshot.route("mcp_a__read_file").expect("route exists");
    assert_eq!(route.binding_id, "mcp.a");
    assert_eq!(route.provider_tool_id, "read/file");
    let route = snapshot.route("plain_tool").expect("route exists");
    assert_eq!(route.binding_id, "mcp.c");
    assert!(snapshot.route("read_file").is_none());

    // Projection is stable for the same catalog.
    let repeated = ToolCatalogSnapshot::from_catalog(&catalog);
    assert_eq!(
        repeated.declarations()[0].name,
        snapshot.declarations()[0].name
    );
}

#[test]
fn test_projection_clamps_long_names_and_keeps_schema() {
    let long_name = "t".repeat(120);
    let catalog = ToolCatalog {
        catalog_version: "v2".to_string(),
        tools: vec![descriptor("mcp.a", &long_name)],
    };
    let snapshot = ToolCatalogSnapshot::from_catalog(&catalog);
    let declaration = &snapshot.declarations()[0];
    assert_eq!(declaration.name.len(), 64);
    assert!(declaration.parameters_json_schema.contains("\"text\""));
    assert!(snapshot.route(&declaration.name).is_some());
}

#[tokio::test]
async fn test_loop_advertises_snapshot_declarations_to_the_model() {
    let source = std::sync::Arc::new(MockToolCatalogSource::new(
        vec![descriptor("mcp.fake", "echo")],
        "catalog-echo",
    ));
    let snapshot = snapshot_tool_catalog(source.as_ref(), None)
        .await
        .expect("snapshot");
    let provider = SnapshotToolProvider::new(snapshot.clone(), source.clone());
    let model = RecordingModelProvider::new(1);

    let request = AgentRunRequest {
        run_id: "run-advertise".to_string(),
        session_id: "session-advertise".to_string(),
        prompt: "echo something".to_string(),
        messages: Vec::new(),
        available_tools: snapshot.declarations().to_vec(),
        config: None,
    };
    let response = CyreneNativeAgentLoop::new()
        .execute_run(request, &model, Some(&provider), CancellationToken::new())
        .await
        .expect("run");
    match response.result.unwrap() {
        agent_run_response::Result::Success(success) => assert_eq!(success.total_turns, 2),
        agent_run_response::Result::Error(error) => panic!("expected success: {error:?}"),
    }

    let requests = model.requests.lock().expect("recording lock");
    assert_eq!(requests.len(), 2);
    for request in requests.iter() {
        assert_eq!(request.tools.len(), 1);
        let function = request.tools[0].function.as_ref().expect("function");
        assert_eq!(function.name, "echo");
        assert_eq!(
            function.parameters_json,
            "{\"type\":\"object\",\"properties\":{\"text\":{\"type\":\"string\"}}}"
        );
    }
    let calls = source.recorded_calls();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].provider_tool_id, "echo");
    assert_eq!(calls[0].arguments_json, "{\"text\":\"hi\"}");
}
