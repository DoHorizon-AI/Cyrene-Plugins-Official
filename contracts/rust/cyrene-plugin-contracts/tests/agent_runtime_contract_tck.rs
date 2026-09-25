// ╔══════════════════════════════════════════════════════════════════════╗
// ║ 📄 File: agent_runtime_contract_tck.rs                               ║
// ║ Module: CYRENE Plugins Official                                     ║
// ║ Role: Direct agent.runtime.v1 capability conformance & TCK.         ║
// ╚══════════════════════════════════════════════════════════════════════╝
// 中文：文件：agent_runtime_contract_tck.rs
// 中文：模块：CYRENE Plugins Official
// 中文：职责：对直接使用 agent.runtime.v1 capability 的实现执行一致性验证与 TCK。

use cyrene_plugin_contracts::{
    agent_runtime::{
        CAPABILITY_ID, INTERFACE_VERSION, METHOD_RUN, METHOD_RUN_STREAM, RUN_REQUEST_TYPE_URL,
    },
    agent_runtime_v1::{
        agent_stream_event, AgentConfig, AgentErrorCode, AgentRunRequest, AgentStreamEvent,
        ChatMessage, ContentDeltaEvent, RunCompletedEvent, RunStartedEvent, ToolCall,
        ToolCallCompletedEvent, ToolCallResult, ToolCallStartedEvent, ToolDeclaration, UsageStats,
    },
    direct_plugin_runtime_v1::{
        direct_invocation_error, direct_invocation_response, DirectInvocationError,
        DirectInvocationRequest, DirectInvocationResponse,
    },
};
use prost::Message;

#[test]
fn agent_runtime_identifiers_and_error_codes_are_stable() {
    assert_eq!(CAPABILITY_ID, "agent.runtime.v1");
    assert_eq!(INTERFACE_VERSION, "1");
    assert_eq!(METHOD_RUN, "run");
    assert_eq!(METHOD_RUN_STREAM, "run_stream");

    assert_eq!(AgentErrorCode::Unspecified as i32, 0);
    assert_eq!(AgentErrorCode::InvalidRequest as i32, 1);
    assert_eq!(AgentErrorCode::TurnLimitExceeded as i32, 2);
    assert_eq!(AgentErrorCode::ModelUnavailable as i32, 3);
    assert_eq!(AgentErrorCode::ToolExecutionFailed as i32, 4);
    assert_eq!(AgentErrorCode::ContextWindowExceeded as i32, 5);
    assert_eq!(AgentErrorCode::Cancelled as i32, 6);
    assert_eq!(AgentErrorCode::DeadlineExceeded as i32, 7);
    assert_eq!(AgentErrorCode::Internal as i32, 8);
}

#[test]
fn agent_run_request_preserves_optional_fields_and_wire_round_trip() {
    let request = AgentRunRequest {
        run_id: "run-test-101".to_string(),
        session_id: "session-xyz".to_string(),
        prompt: "Summarize deployment logs".to_string(),
        messages: vec![ChatMessage {
            role: "user".to_string(),
            content: "Summarize deployment logs".to_string(),
            name: None,
            tool_call_id: None,
        }],
        available_tools: vec![ToolDeclaration {
            name: "read_log".to_string(),
            description: "Read system logs".to_string(),
            parameters_json_schema: "{\"type\":\"object\"}".to_string(),
        }],
        config: Some(AgentConfig {
            max_turns: Some(5),
            timeout_ms: Some(30000),
            model_selector: Some("gpt-4o".to_string()),
            system_instruction: Some("You are a helpful DevOps assistant".to_string()),
            temperature: Some(0.2),
            top_p: Some(0.95),
            max_tool_concurrency: Some(2),
        }),
    };

    let encoded = request.encode_to_vec();
    assert!(!encoded.is_empty());

    let decoded = AgentRunRequest::decode(encoded.as_slice()).expect("must decode");
    assert_eq!(decoded.run_id, "run-test-101");
    assert_eq!(decoded.session_id, "session-xyz");
    let cfg = decoded.config.expect("config must be present");
    assert_eq!(cfg.max_turns, Some(5));
    assert_eq!(cfg.temperature, Some(0.2));
}

#[test]
fn agent_stream_events_guarantee_ordered_monotonic_sequence() {
    let events: Vec<AgentStreamEvent> = vec![
        AgentStreamEvent {
            sequence_number: 1,
            timestamp_ms: 1726000001000,
            event: Some(agent_stream_event::Event::RunStarted(RunStartedEvent {
                run_id: "run-seq-1".to_string(),
                resolved_model: "claude-3-5-sonnet".to_string(),
            })),
        },
        AgentStreamEvent {
            sequence_number: 2,
            timestamp_ms: 1726000001200,
            event: Some(agent_stream_event::Event::ContentDelta(ContentDeltaEvent {
                text_delta: "Hello ".to_string(),
            })),
        },
        AgentStreamEvent {
            sequence_number: 3,
            timestamp_ms: 1726000001300,
            event: Some(agent_stream_event::Event::ToolCallStarted(
                ToolCallStartedEvent {
                    tool_call: Some(ToolCall {
                        call_id: "call-1".to_string(),
                        tool_name: "query_db".to_string(),
                        arguments_json: "{}".to_string(),
                    }),
                },
            )),
        },
        AgentStreamEvent {
            sequence_number: 4,
            timestamp_ms: 1726000001400,
            event: Some(agent_stream_event::Event::ToolCallCompleted(
                ToolCallCompletedEvent {
                    result: Some(ToolCallResult {
                        call_id: "call-1".to_string(),
                        tool_name: "query_db".to_string(),
                        output_json: "{\"rows\":0}".to_string(),
                        is_error: false,
                    }),
                },
            )),
        },
        AgentStreamEvent {
            sequence_number: 5,
            timestamp_ms: 1726000001500,
            event: Some(agent_stream_event::Event::RunCompleted(RunCompletedEvent {
                finish_reason: "stop".to_string(),
                output_text: "Done".to_string(),
                total_turns: 1,
                usage: Some(UsageStats {
                    prompt_tokens: 15,
                    completion_tokens: 5,
                    total_tokens: 20,
                    model_call_count: 1,
                    tool_call_count: 1,
                    execution_duration_ms: 500,
                }),
            })),
        },
    ];

    let mut last_seq = 0;
    for ev in events {
        assert_eq!(ev.sequence_number, last_seq + 1);
        last_seq = ev.sequence_number;

        let bytes = ev.encode_to_vec();
        let decoded = AgentStreamEvent::decode(bytes.as_slice()).expect("decode stream event");
        assert_eq!(decoded.sequence_number, last_seq);
    }
}

#[test]
fn agent_runtime_empty_implementation_fails_closed() {
    let handler = |req: DirectInvocationRequest| -> DirectInvocationResponse {
        DirectInvocationResponse {
            result: Some(direct_invocation_response::Result::Error(
                DirectInvocationError {
                    code: direct_invocation_error::Code::Unavailable as i32,
                    message: format!("Plugin capability {} is not implemented", req.capability),
                    retryable: true,
                    domain_code: "CAPABILITY_UNAVAILABLE".to_string(),
                },
            )),
        }
    };

    let request = DirectInvocationRequest {
        capability: CAPABILITY_ID.to_string(),
        interface_version: INTERFACE_VERSION.to_string(),
        method: METHOD_RUN.to_string(),
        payload_type_url: RUN_REQUEST_TYPE_URL.to_string(),
        payload: vec![],
        request_id: "req-gate-001".to_string(),
        stream_mode: 1,
    };

    let response = handler(request);
    match response.result {
        Some(direct_invocation_response::Result::Error(err)) => {
            assert_eq!(err.code, direct_invocation_error::Code::Unavailable as i32);
            assert_eq!(err.domain_code, "CAPABILITY_UNAVAILABLE");
        }
        _ => panic!("Empty implementation must fail closed with typed UNAVAILABLE"),
    }
}
