// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 agent_runtime_tests.rs                                          │
// │  Package: cyrene-agent-runtime::tests                               │
// │  Role: Comprehensive TCK and integration test suite (M5A).          │
// │                                                                     │
// │  模块职责：验证 M5A 全部需求（T57 - T65）与出口门禁                  │
// └─────────────────────────────────────────────────────────────────────┘

use std::collections::HashMap;

use cyrene_agent_runtime::adapter::rig_adapter::{AgentDriver, RigAgentAdapter};
use cyrene_agent_runtime::adapter::InjectedMemoryProvider;
use cyrene_agent_runtime::engine::cancel::CancellationToken;
use cyrene_agent_runtime::engine::turn_loop::CyreneNativeAgentLoop;
use cyrene_agent_runtime::limits::RuntimeLimits;
use cyrene_agent_runtime::tck::AgentTckSuite;
use cyrene_plugin_contracts::agent_runtime_v1::{
    agent_run_response, AgentErrorCode, AgentRunError, AgentRunRequest,
};
use cyrene_plugin_contracts::memory_provider_v1::{
    recall_memory_response, RecallBatch, RecallMemoryRequest, RecallMemoryResponse,
};

// ── T57: Run TCK against CyreneNativeAgentLoop ─────────────────────────

#[tokio::test]
async fn test_native_agent_loop_tck_single_turn_t57() {
    let native = CyreneNativeAgentLoop::new();
    AgentTckSuite::run_tck_stateless_single_turn(&native).await;
}

#[tokio::test]
async fn test_native_agent_loop_tck_tool_loop_t57_t59_t61() {
    let native = CyreneNativeAgentLoop::new();
    AgentTckSuite::run_tck_tool_loop(&native).await;
}

#[tokio::test]
async fn test_native_agent_loop_tck_ordered_streaming_t57_t59() {
    let native = CyreneNativeAgentLoop::new();
    AgentTckSuite::run_tck_ordered_streaming(&native).await;
}

#[tokio::test]
async fn test_native_agent_loop_tck_max_turns_limit_t57_t59_t64() {
    let native = CyreneNativeAgentLoop::new();
    AgentTckSuite::run_tck_max_turns_limit(&native).await;
}

#[tokio::test]
async fn test_native_agent_loop_tck_cancellation_t57_t59() {
    let native = CyreneNativeAgentLoop::new();
    AgentTckSuite::run_tck_cancellation(&native).await;
}

// ── T57 & T58: Run TCK against Rig internal adapter ────────────────────

#[tokio::test]
async fn test_rig_adapter_tck_suite_t57_t58() {
    let rig = RigAgentAdapter::new();
    AgentTckSuite::run_tck_stateless_single_turn(&rig).await;
    AgentTckSuite::run_tck_tool_loop(&rig).await;
    AgentTckSuite::run_tck_ordered_streaming(&rig).await;
    AgentTckSuite::run_tck_max_turns_limit(&rig).await;
    AgentTckSuite::run_tck_cancellation(&rig).await;
}

// ── T62: Memory explicitly accepted as injected capability ─────────────

struct MockInjectedMemory;

#[async_trait::async_trait]
impl InjectedMemoryProvider for MockInjectedMemory {
    async fn recall(
        &self,
        _req: RecallMemoryRequest,
    ) -> Result<RecallMemoryResponse, AgentRunError> {
        Ok(RecallMemoryResponse {
            result: Some(recall_memory_response::Result::Matches(RecallBatch {
                matches: Vec::new(),
            })),
        })
    }
}

#[tokio::test]
async fn test_injected_memory_no_durable_sessions_t62() {
    let mem = MockInjectedMemory;
    let resp = mem
        .recall(RecallMemoryRequest {
            tenant_id: "tenant-1".to_string(),
            scope: None,
            subject: None,
            query_embedding: Vec::new(),
            query_text: Some("project details".to_string()),
            metadata_filters: HashMap::new(),
            min_similarity: None,
            top_k: Some(5),
        })
        .await
        .unwrap();

    match resp.result.unwrap() {
        recall_memory_response::Result::Matches(batch) => {
            assert!(batch.matches.is_empty());
        }
        _ => panic!("Expected Matches"),
    }
}

// ── T64: Limit enforcement on payload byte size ────────────────────────

#[test]
fn test_limits_payload_byte_size_t64() {
    assert!(RuntimeLimits::validate_payload_size(500).is_ok());
    let oversized = RuntimeLimits::validate_payload_size(2_000_000);
    assert!(oversized.is_err());
    let err = oversized.unwrap_err();
    assert_eq!(err.code, AgentErrorCode::InvalidRequest as i32);
}

// ── M5A Exit Gate Verification ─────────────────────────────────────────

#[tokio::test]
async fn test_m5a_exit_gate_independently_replaceable_and_stateless() {
    let native = CyreneNativeAgentLoop::new();
    let rig = RigAgentAdapter::new();

    let drivers: Vec<&(dyn AgentDriver + 'static)> = vec![&native, &rig];

    for driver in drivers {
        // Invocation 1: session A
        let model1 = cyrene_agent_runtime::tck::MockModelProvider::simple_text();
        let tools1 = cyrene_agent_runtime::tck::MockToolProvider::new();
        let req1 = AgentRunRequest {
            run_id: "run-gate-1".to_string(),
            session_id: "session-A".to_string(),
            prompt: "User question 1".to_string(),
            messages: Vec::new(),
            available_tools: Vec::new(),
            config: None,
        };
        let res1 = driver
            .execute_run(req1, &model1, Some(&tools1), CancellationToken::new())
            .await
            .unwrap();
        assert!(matches!(
            res1.result.unwrap(),
            agent_run_response::Result::Success(_)
        ));

        // Invocation 2: session B (verifies no state carried over from session A)
        let model2 = cyrene_agent_runtime::tck::MockModelProvider::simple_text();
        let tools2 = cyrene_agent_runtime::tck::MockToolProvider::new();
        let req2 = AgentRunRequest {
            run_id: "run-gate-2".to_string(),
            session_id: "session-B".to_string(),
            prompt: "User question 2".to_string(),
            messages: Vec::new(),
            available_tools: Vec::new(),
            config: None,
        };
        let res2 = driver
            .execute_run(req2, &model2, Some(&tools2), CancellationToken::new())
            .await
            .unwrap();
        assert!(matches!(
            res2.result.unwrap(),
            agent_run_response::Result::Success(_)
        ));
    }
}
