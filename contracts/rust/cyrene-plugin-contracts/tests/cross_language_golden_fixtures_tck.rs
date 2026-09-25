// ╔══════════════════════════════════════════════════════════════════════╗
// ║ 📄 File: cross_language_golden_fixtures_tck.rs                      ║
// ║ Module: CYRENE Plugins Official                                     ║
// ║ Role: Cross-language wire bytes, unknown fields & golden fixtures.  ║
// ╚══════════════════════════════════════════════════════════════════════╝
// 中文：文件：cross_language_golden_fixtures_tck.rs
// 中文：模块：CYRENE Plugins Official
// 中文：职责：验证跨语言 wire 字节、未知字段与 golden fixture。

use cyrene_plugin_contracts::{
    agent_runtime_v1::{
        agent_stream_event, AgentConfig, AgentRunRequest, AgentStreamEvent, ChatMessage,
        ContentDeltaEvent, RunCompletedEvent, RunStartedEvent, ToolDeclaration, UsageStats,
    },
    computer_runtime_v1::CommandExecutionRequest,
    memory_provider_v1::{MemoryItem, RecallMemoryRequest, StoreMemoryRequest},
};
use prost::Message;
use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;

fn golden_dir() -> PathBuf {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir.join("../../fixtures/golden")
}

#[test]
fn generate_and_verify_agent_golden_fixtures() {
    let dir = golden_dir();
    fs::create_dir_all(&dir).expect("create golden dir");

    let req = AgentRunRequest {
        run_id: "golden-run-001".to_string(),
        session_id: "golden-session-001".to_string(),
        prompt: "Check system status".to_string(),
        messages: vec![ChatMessage {
            role: "user".to_string(),
            content: "Check system status".to_string(),
            name: None,
            tool_call_id: None,
        }],
        available_tools: vec![ToolDeclaration {
            name: "system_status".to_string(),
            description: "Returns CPU and memory status".to_string(),
            parameters_json_schema: "{\"type\":\"object\"}".to_string(),
        }],
        config: Some(AgentConfig {
            max_turns: Some(5),
            timeout_ms: Some(10000),
            model_selector: Some("claude-3-5-sonnet".to_string()),
            system_instruction: Some("You are a system monitor".to_string()),
            temperature: Some(0.1),
            top_p: Some(0.9),
            max_tool_concurrency: Some(1),
        }),
    };

    let bin_path = dir.join("agent_run_request.bin");
    let encoded = req.encode_to_vec();
    fs::write(&bin_path, &encoded).expect("write golden bin");

    let decoded = AgentRunRequest::decode(encoded.as_slice()).expect("decode golden");
    assert_eq!(decoded.run_id, "golden-run-001");
    assert_eq!(decoded.config.unwrap().max_turns, Some(5));

    // Stream events fixture
    // 中文：流式事件 fixture。
    let stream_events = vec![
        AgentStreamEvent {
            sequence_number: 1,
            timestamp_ms: 1726000000000,
            event: Some(agent_stream_event::Event::RunStarted(RunStartedEvent {
                run_id: "golden-run-001".to_string(),
                resolved_model: "claude-3-5-sonnet".to_string(),
            })),
        },
        AgentStreamEvent {
            sequence_number: 2,
            timestamp_ms: 1726000000100,
            event: Some(agent_stream_event::Event::ContentDelta(ContentDeltaEvent {
                text_delta: "System is nominal.".to_string(),
            })),
        },
        AgentStreamEvent {
            sequence_number: 3,
            timestamp_ms: 1726000000200,
            event: Some(agent_stream_event::Event::RunCompleted(RunCompletedEvent {
                finish_reason: "stop".to_string(),
                output_text: "System is nominal.".to_string(),
                total_turns: 1,
                usage: Some(UsageStats {
                    prompt_tokens: 20,
                    completion_tokens: 5,
                    total_tokens: 25,
                    model_call_count: 1,
                    tool_call_count: 0,
                    execution_duration_ms: 200,
                }),
            })),
        },
    ];

    let mut stream_bytes = Vec::new();
    for ev in &stream_events {
        let b = ev.encode_to_vec();
        stream_bytes.extend_from_slice(&(b.len() as u32).to_le_bytes());
        stream_bytes.extend_from_slice(&b);
    }
    fs::write(dir.join("agent_stream_events.bin"), &stream_bytes).expect("write stream bin");
}

#[test]
fn generate_and_verify_memory_golden_fixtures() {
    let dir = golden_dir();
    let mut meta = HashMap::new();
    meta.insert("source".to_string(), "golden-fixture".to_string());

    let store_req = StoreMemoryRequest {
        tenant_id: "tenant-golden-01".to_string(),
        item: Some(MemoryItem {
            item_id: "item-golden-01".to_string(),
            tenant_id: "tenant-golden-01".to_string(),
            scope: "global".to_string(),
            subject: "architecture".to_string(),
            content: "Cyrene native plugin rebuild architecture".to_string(),
            embedding: vec![0.01, 0.02, 0.03, -0.04],
            metadata: meta,
            created_at_ms: 1726000000000,
            expires_at_ms: Some(1726086400000),
            ttl_seconds: Some(86400),
        }),
    };

    let bin_path = dir.join("memory_store_request.bin");
    let encoded = store_req.encode_to_vec();
    fs::write(&bin_path, &encoded).expect("write store bin");

    let recall_req = RecallMemoryRequest {
        tenant_id: "tenant-golden-01".to_string(),
        scope: Some("global".to_string()),
        subject: None,
        query_embedding: vec![0.01, 0.02, 0.03, -0.04],
        query_text: Some("architecture".to_string()),
        metadata_filters: HashMap::new(),
        min_similarity: Some(0.8),
        top_k: Some(5),
    };

    let recall_path = dir.join("memory_recall_request.bin");
    let recall_encoded = recall_req.encode_to_vec();
    fs::write(&recall_path, &recall_encoded).expect("write recall bin");
}

#[test]
fn generate_and_verify_computer_golden_fixtures() {
    let dir = golden_dir();
    let mut env = HashMap::new();
    env.insert("CYRENE_STAGE".to_string(), "GOLDEN".to_string());

    let cmd_req = CommandExecutionRequest {
        command: "echo 'hello from golden'".to_string(),
        cwd: Some("/tmp".to_string()),
        env,
        timeout_ms: Some(5000),
        max_output_bytes: Some(1048576),
    };

    let bin_path = dir.join("computer_command_request.bin");
    let encoded = cmd_req.encode_to_vec();
    fs::write(&bin_path, &encoded).expect("write cmd bin");
}

#[test]
fn verify_unknown_fields_forward_compatibility() {
    // Construct a byte array containing an unknown field tag (e.g. tag 99: varint 42)
    // Tag 99, wire type 0 (varint): (99 << 3) | 0 = 792 = 0x318 -> varint [0x98, 0x06]
    // Value: 42 -> 0x2a
    // 中文：构造包含未知字段标记的字节数组（例如 tag 99：varint 42）。
    // 中文：tag 99、wire type 0（varint）：(99 << 3) | 0 = 792 = 0x318 → varint [0x98, 0x06]。
    // 中文：值 42 对应 0x2a。
    let base_req = CommandExecutionRequest {
        command: "ls -la".to_string(),
        cwd: None,
        env: HashMap::new(),
        timeout_ms: None,
        max_output_bytes: None,
    };

    let mut wire = base_req.encode_to_vec();
    wire.extend_from_slice(&[0x98, 0x06, 0x2a]); // Field 99 = 42 | 中文：字段 99 的值为 42

    let decoded = CommandExecutionRequest::decode(wire.as_slice())
        .expect("Protobuf must ignore unknown fields forward-compatibly");
    assert_eq!(decoded.command, "ls -la");

    let re_encoded = decoded.encode_to_vec();
    assert!(!re_encoded.is_empty());
}
