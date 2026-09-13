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
use tokio::sync::mpsc;

use cyrene_plugin_contracts::agent_runtime_v1::{
    agent_run_response, agent_stream_event, AgentConfig, AgentErrorCode, AgentRunError,
    AgentRunRequest, AgentStreamEvent, ToolCall, ToolCallResult,
};
use cyrene_plugin_contracts::model_provider_v1::{
    ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse, ChatToolCallDelta,
};

use crate::adapter::model_provider::ModelProvider;
use crate::adapter::rig_adapter::AgentDriver;
use crate::adapter::tool_provider::ToolProvider;
use crate::engine::cancel::CancellationToken;

// ── Mock Providers for TCK ─────────────────────────────────────────────

pub struct MockModelProvider {
    pub call_count: AtomicUsize,
    pub return_tool_calls_until: usize,
}

impl MockModelProvider {
    pub fn simple_text() -> Self {
        Self {
            call_count: AtomicUsize::new(0),
            return_tool_calls_until: 0,
        }
    }

    pub fn with_tool_turns(turns: usize) -> Self {
        Self {
            call_count: AtomicUsize::new(0),
            return_tool_calls_until: turns,
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
                        function_name: Some("lookup_data".to_string()),
                        function_arguments: Some("{\"query\":\"cyrene\"}".to_string()),
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
}
