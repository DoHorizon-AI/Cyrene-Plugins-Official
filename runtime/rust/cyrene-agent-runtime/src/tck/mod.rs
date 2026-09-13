// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 mod.rs (tck)                                                    │
// │  Package: cyrene-agent-runtime::tck                                 │
// │  Role: Unified Technology Compatibility Kit (TCK) suite (T57).      │
// │                                                                     │
// │  模块职责：统一 TCK 套件，以同一套标准测试用例对比验证：             │
// │           1. CyreneNativeAgentLoop (生产推荐原生 Rust 循环)          │
// │           2. RigAgentAdapter (隐藏在内部 Adapter 的 Rig 实现)       │
// │           3. DotnetNativeAotCandidate (C# Native AOT 宿主候选)      │
// └─────────────────────────────────────────────────────────────────────┘

use async_trait::async_trait;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use tokio::sync::mpsc;

use cyrene_plugin_contracts::agent_runtime_v1::{
    agent_run_response, agent_stream_event, AgentConfig, AgentErrorCode, AgentRunError,
    AgentRunRequest, AgentStreamEvent, ToolCall, ToolCallResult,
};
use cyrene_plugin_contracts::model_provider_v1::{
    ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse, ChatToolCallDelta,
};
use cyrene_plugin_contracts::tool_provider_v1::{
    call_tool_response, list_tools_response, tool_content_part, CallToolRequest, CallToolResponse,
    ListToolsResponse, ToolCallOutcome, ToolCatalog, ToolContentPart, ToolDescriptor,
    ToolTextContent,
};

use crate::adapter::model_provider::ModelProvider;
use crate::adapter::rig_adapter::AgentDriver;
use crate::adapter::tool_catalog::{
    snapshot_tool_catalog, SnapshotToolProvider, ToolCatalogSource,
};
use crate::adapter::tool_provider::ToolProvider;
use crate::engine::cancel::CancellationToken;

// ── Mock Providers for TCK ─────────────────────────────────────────────

pub struct MockModelProvider {
    pub call_count: AtomicUsize,
    pub return_tool_calls_until: usize,
    tool_name: String,
    tool_arguments: String,
}

impl MockModelProvider {
    pub fn simple_text() -> Self {
        Self::with_tool_call("lookup_data", "{\"query\":\"cyrene\"}", 0)
    }

    pub fn with_tool_turns(turns: usize) -> Self {
        Self::with_tool_call("lookup_data", "{\"query\":\"cyrene\"}", turns)
    }

    /// Ask for one named tool for the first `turns` model calls.
    pub fn with_tool_call(
        tool_name: impl Into<String>,
        tool_arguments: impl Into<String>,
        turns: usize,
    ) -> Self {
        Self {
            call_count: AtomicUsize::new(0),
            return_tool_calls_until: turns,
            tool_name: tool_name.into(),
            tool_arguments: tool_arguments.into(),
        }
    }
}

#[async_trait]
impl ModelProvider for MockModelProvider {
    async fn complete(
        &self,
        _req: ChatCompletionRequest,
    ) -> Result<ChatCompletionResponse, AgentRunError> {
        let count = self.call_count.fetch_add(1, Ordering::SeqCst);
        if count < self.return_tool_calls_until {
            Ok(ChatCompletionResponse {
                chunks: vec![ChatCompletionChunk {
                    delta: String::new(),
                    finish_reason: Some("tool_calls".to_string()),
                    prompt_tokens: Some(20),
                    completion_tokens: Some(10),
                    role: Some("assistant".to_string()),
                    tool_calls: vec![ChatToolCallDelta {
                        index: 0,
                        id: Some(format!("call-{}", count)),
                        r#type: Some("function".to_string()),
                        function_name: Some(self.tool_name.clone()),
                        function_arguments: Some(self.tool_arguments.clone()),
                    }],
                    total_tokens: Some(30),
                }],
            })
        } else {
            Ok(ChatCompletionResponse {
                chunks: vec![ChatCompletionChunk {
                    delta: "Final synthesized answer".to_string(),
                    finish_reason: Some("stop".to_string()),
                    prompt_tokens: Some(30),
                    completion_tokens: Some(15),
                    role: Some("assistant".to_string()),
                    tool_calls: Vec::new(),
                    total_tokens: Some(45),
                }],
            })
        }
    }
}

pub struct MockToolProvider {
    pub execution_count: AtomicUsize,
}

impl Default for MockToolProvider {
    fn default() -> Self {
        Self::new()
    }
}

impl MockToolProvider {
    pub fn new() -> Self {
        Self {
            execution_count: AtomicUsize::new(0),
        }
    }
}

#[async_trait]
impl ToolProvider for MockToolProvider {
    async fn execute(&self, call: &ToolCall) -> Result<ToolCallResult, AgentRunError> {
        self.execution_count.fetch_add(1, Ordering::SeqCst);
        Ok(ToolCallResult {
            call_id: call.call_id.clone(),
            tool_name: call.tool_name.clone(),
            output_json: "{\"status\":\"success\",\"fact\":\"cyrene native engine\"}".to_string(),
            is_error: false,
        })
    }
}

/// Deterministic tool.provider.v1 source for TCK vectors.
pub struct MockToolCatalogSource {
    catalog: ToolCatalog,
    calls: Mutex<Vec<CallToolRequest>>,
}

impl MockToolCatalogSource {
    pub fn new(tools: Vec<ToolDescriptor>, catalog_version: &str) -> Self {
        Self {
            catalog: ToolCatalog {
                catalog_version: catalog_version.to_string(),
                tools,
            },
            calls: Mutex::new(Vec::new()),
        }
    }

    /// Calls recorded so far, in order.
    pub fn recorded_calls(&self) -> Vec<CallToolRequest> {
        self.calls.lock().expect("mock source lock").clone()
    }
}

#[async_trait]
impl ToolCatalogSource for MockToolCatalogSource {
    async fn list_tools(&self, _binding_id: Option<&str>) -> ListToolsResponse {
        ListToolsResponse {
            result: Some(list_tools_response::Result::Catalog(self.catalog.clone())),
        }
    }

    async fn call_tool(&self, request: &CallToolRequest) -> CallToolResponse {
        self.calls
            .lock()
            .expect("mock source lock")
            .push(request.clone());
        CallToolResponse {
            result: Some(call_tool_response::Result::Outcome(ToolCallOutcome {
                content: vec![ToolContentPart {
                    content: Some(tool_content_part::Content::Text(ToolTextContent {
                        text: "mock tool result".to_string(),
                    })),
                }],
                is_error: false,
            })),
        }
    }
}

fn mock_descriptor(binding_id: &str, provider_tool_id: &str) -> ToolDescriptor {
    ToolDescriptor {
        binding_id: binding_id.to_string(),
        provider_tool_id: provider_tool_id.to_string(),
        display_name: provider_tool_id.to_string(),
        description: format!("Mock tool {provider_tool_id}"),
        input_schema_json: "{\"type\":\"object\"}".to_string(),
        output_schema_json: None,
    }
}

// ── TCK Test Runner ───────────────────────────────────────────────────

pub struct AgentTckSuite;

impl AgentTckSuite {
    /// Test Vector 1: Stateless single-turn execution
    pub async fn run_tck_stateless_single_turn(driver: &(dyn AgentDriver + 'static)) {
        let model = MockModelProvider::simple_text();
        let tools = MockToolProvider::new();

        let req = AgentRunRequest {
            run_id: "tck-run-1".to_string(),
            session_id: "session-alpha".to_string(),
            prompt: "Hello World".to_string(),
            messages: Vec::new(),
            available_tools: Vec::new(),
            config: None,
        };

        let resp = driver
            .execute_run(req, &model, Some(&tools), CancellationToken::new())
            .await
            .expect("TCK single turn execution must succeed");

        match resp.result.unwrap() {
            agent_run_response::Result::Success(succ) => {
                assert_eq!(succ.run_id, "tck-run-1");
                assert_eq!(succ.output_text, "Final synthesized answer");
                assert_eq!(succ.total_turns, 1);
                assert_eq!(succ.finish_reason, "stop");
                assert!(succ.usage.unwrap().total_tokens > 0);
            }
            agent_run_response::Result::Error(e) => panic!("Expected success, got error: {:?}", e),
        }
    }

    /// Test Vector 2: Multi-turn tool loop execution (T59, T61)
    pub async fn run_tck_tool_loop(driver: &(dyn AgentDriver + 'static)) {
        let model = MockModelProvider::with_tool_turns(2);
        let tools = MockToolProvider::new();

        let req = AgentRunRequest {
            run_id: "tck-run-tool-loop".to_string(),
            session_id: "session-beta".to_string(),
            prompt: "Investigate weather and compile".to_string(),
            messages: Vec::new(),
            available_tools: Vec::new(),
            config: Some(AgentConfig {
                max_turns: Some(5),
                ..Default::default()
            }),
        };

        let resp = driver
            .execute_run(req, &model, Some(&tools), CancellationToken::new())
            .await
            .expect("TCK tool loop execution must succeed");

        match resp.result.unwrap() {
            agent_run_response::Result::Success(succ) => {
                assert_eq!(succ.run_id, "tck-run-tool-loop");
                assert_eq!(succ.total_turns, 3); // 2 tool turns + 1 final completion
                assert_eq!(tools.execution_count.load(Ordering::SeqCst), 2);
            }
            agent_run_response::Result::Error(e) => panic!("Expected success, got error: {:?}", e),
        }
    }

    /// Test Vector 3: Strictly ordered monotonic streaming events (T59)
    pub async fn run_tck_ordered_streaming(driver: &(dyn AgentDriver + 'static)) {
        let model = MockModelProvider::with_tool_turns(1);
        let tools = MockToolProvider::new();
        let (tx, mut rx) = mpsc::channel::<AgentStreamEvent>(100);

        let req = AgentRunRequest {
            run_id: "tck-stream-run".to_string(),
            session_id: "session-stream".to_string(),
            prompt: "Streaming prompt".to_string(),
            messages: Vec::new(),
            available_tools: Vec::new(),
            config: None,
        };

        let stream_fut = async {
            let res = driver
                .execute_stream(req, &model, Some(&tools), CancellationToken::new(), tx)
                .await;
            assert!(res.is_ok(), "Streaming execute must succeed");
        };

        let collect_fut = async {
            let mut events = Vec::new();
            while let Some(evt) = rx.recv().await {
                events.push(evt);
            }
            events
        };

        let (_, events) = tokio::join!(stream_fut, collect_fut);

        assert!(!events.is_empty(), "Must receive streaming events");
        // Verify strictly monotonic sequence numbers starting at 1
        for (idx, evt) in events.iter().enumerate() {
            assert_eq!(evt.sequence_number, (idx + 1) as i64);
            assert!(evt.timestamp_ms > 0);
        }

        // Verify first event is RunStarted
        assert!(matches!(
            events.first().unwrap().event.as_ref().unwrap(),
            agent_stream_event::Event::RunStarted(_)
        ));

        // Verify last event is RunCompleted terminal event
        assert!(matches!(
            events.last().unwrap().event.as_ref().unwrap(),
            agent_stream_event::Event::RunCompleted(_)
        ));
    }

    /// Test Vector 4: Maximum turns limit enforcement (T59, T64)
    pub async fn run_tck_max_turns_limit(driver: &(dyn AgentDriver + 'static)) {
        // Model always requests tools (infinite loop without bound)
        let model = MockModelProvider::with_tool_turns(100);
        let tools = MockToolProvider::new();

        let req = AgentRunRequest {
            run_id: "tck-turn-limit".to_string(),
            session_id: "session-limit".to_string(),
            prompt: "Infinite turn prompt".to_string(),
            messages: Vec::new(),
            available_tools: Vec::new(),
            config: Some(AgentConfig {
                max_turns: Some(3), // Limit to 3 turns
                ..Default::default()
            }),
        };

        let resp = driver
            .execute_run(req, &model, Some(&tools), CancellationToken::new())
            .await
            .expect("Execution should return bounded result");

        match resp.result.unwrap() {
            agent_run_response::Result::Error(err) => {
                assert_eq!(err.code, AgentErrorCode::TurnLimitExceeded as i32);
            }
            agent_run_response::Result::Success(_) => {
                panic!("Expected TurnLimitExceeded error, but got success!");
            }
        }
    }

    /// Test Vector 5: Cooperative cancellation propagation (T51, T59)
    pub async fn run_tck_cancellation(driver: &(dyn AgentDriver + 'static)) {
        let model = MockModelProvider::with_tool_turns(10);
        let tools = MockToolProvider::new();
        let cancel = CancellationToken::new();
        cancel.cancel(); // Pre-cancel

        let req = AgentRunRequest {
            run_id: "tck-cancelled-run".to_string(),
            session_id: "session-cancel".to_string(),
            prompt: "Run before cancel".to_string(),
            messages: Vec::new(),
            available_tools: Vec::new(),
            config: None,
        };

        let resp = driver
            .execute_run(req, &model, Some(&tools), cancel)
            .await
            .expect("Execution should return cancellation result");

        match resp.result.unwrap() {
            agent_run_response::Result::Error(err) => {
                assert_eq!(err.code, AgentErrorCode::Cancelled as i32);
            }
            agent_run_response::Result::Success(_) => {
                panic!("Expected Cancelled error, but got success!");
            }
        }
    }

    /// Test Vector 6: per-run tool catalog snapshot with flat-name routing.
    pub async fn run_tck_tool_catalog_snapshot(driver: &(dyn AgentDriver + 'static)) {
        let source = Arc::new(MockToolCatalogSource::new(
            vec![mock_descriptor("mcp.mock", "lookup_data")],
            "catalog-v1",
        ));
        let snapshot = snapshot_tool_catalog(source.as_ref(), None)
            .await
            .expect("snapshot must succeed");
        assert_eq!(snapshot.tool_count(), 1);
        assert_eq!(snapshot.declarations()[0].name, "lookup_data");
        assert_eq!(snapshot.catalog_version(), "catalog-v1");
        let provider = SnapshotToolProvider::new(snapshot.clone(), source.clone());

        let model = MockModelProvider::with_tool_turns(1);
        let request = AgentRunRequest {
            run_id: "tck-run-catalog".to_string(),
            session_id: "session-catalog".to_string(),
            prompt: "Use the tool".to_string(),
            messages: Vec::new(),
            available_tools: snapshot.declarations().to_vec(),
            config: None,
        };
        let response = driver
            .execute_run(request, &model, Some(&provider), CancellationToken::new())
            .await
            .expect("snapshot-backed run must succeed");
        match response.result.unwrap() {
            agent_run_response::Result::Success(success) => assert_eq!(success.total_turns, 2),
            agent_run_response::Result::Error(error) => {
                panic!("expected success, got {error:?}")
            }
        }
        let calls = source.recorded_calls();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].binding_id, "mcp.mock");
        assert_eq!(calls[0].provider_tool_id, "lookup_data");
        assert_eq!(calls[0].catalog_version.as_deref(), Some("catalog-v1"));

        // A flat name outside the snapshot fails closed before any call.
        let other_source = Arc::new(MockToolCatalogSource::new(
            vec![mock_descriptor("mcp.other", "different_tool")],
            "catalog-v2",
        ));
        let other_snapshot = snapshot_tool_catalog(other_source.as_ref(), None)
            .await
            .expect("snapshot must succeed");
        let other_provider = SnapshotToolProvider::new(other_snapshot, other_source.clone());
        let model2 = MockModelProvider::with_tool_turns(1);
        let request2 = AgentRunRequest {
            run_id: "tck-run-catalog-miss".to_string(),
            session_id: "session-catalog".to_string(),
            prompt: "Use the tool".to_string(),
            messages: Vec::new(),
            available_tools: Vec::new(),
            config: None,
        };
        let response2 = driver
            .execute_run(
                request2,
                &model2,
                Some(&other_provider),
                CancellationToken::new(),
            )
            .await
            .expect("run must return a typed error response");
        match response2.result.unwrap() {
            agent_run_response::Result::Error(error) => {
                assert_eq!(error.domain_details, "tools.not_in_snapshot");
            }
            agent_run_response::Result::Success(_) => {
                panic!("a tool outside the snapshot must not execute")
            }
        }
        assert!(other_source.recorded_calls().is_empty());
    }
}
