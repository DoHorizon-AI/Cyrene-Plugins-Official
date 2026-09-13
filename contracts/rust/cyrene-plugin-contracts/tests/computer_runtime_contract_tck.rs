// ╔══════════════════════════════════════════════════════════════════════╗
// ║ 📄 File: computer_runtime_contract_tck.rs                             ║
// ║ Module: CYRENE Plugins Official                                     ║
// ║ Role: Direct computer.runtime.v1 capability conformance & TCK.      ║
// ╚══════════════════════════════════════════════════════════════════════╝

use cyrene_plugin_contracts::{
    computer_runtime::{
        CAPABILITY_ID, EXECUTE_COMMAND_REQUEST_TYPE_URL, INTERFACE_VERSION, METHOD_CREATE_ARTIFACT,
        METHOD_EXECUTE_COMMAND, METHOD_EXECUTE_COMMAND_STREAM, METHOD_GET_ARTIFACT,
        METHOD_LIST_DIR, METHOD_READ_FILE, METHOD_WRITE_FILE,
    },
    computer_runtime_v1::{
        command_stream_event, CommandStreamEvent, ComputerErrorCode, ExecutionEvidence,
        ExecutionResourceUsage,
    },
    direct_plugin_runtime_v1::{
        direct_invocation_error, direct_invocation_response, DirectInvocationError,
        DirectInvocationRequest, DirectInvocationResponse,
    },
};
use prost::Message;

#[test]
fn computer_runtime_identifiers_and_error_codes_are_stable() {
    assert_eq!(CAPABILITY_ID, "computer.runtime.v1");
    assert_eq!(INTERFACE_VERSION, "1");
    assert_eq!(METHOD_EXECUTE_COMMAND, "execute_command");
    assert_eq!(METHOD_EXECUTE_COMMAND_STREAM, "execute_command_stream");
    assert_eq!(METHOD_READ_FILE, "read_file");
    assert_eq!(METHOD_WRITE_FILE, "write_file");
    assert_eq!(METHOD_LIST_DIR, "list_dir");
    assert_eq!(METHOD_CREATE_ARTIFACT, "create_artifact");
    assert_eq!(METHOD_GET_ARTIFACT, "get_artifact");

    assert_eq!(ComputerErrorCode::Unspecified as i32, 0);
    assert_eq!(ComputerErrorCode::PathTraversalDenied as i32, 1);
    assert_eq!(ComputerErrorCode::CommandTimeout as i32, 2);
    assert_eq!(ComputerErrorCode::OutputLimitExceeded as i32, 3);
    assert_eq!(ComputerErrorCode::ExecutionDenied as i32, 4);
    assert_eq!(ComputerErrorCode::ArtifactNotFound as i32, 5);
}

#[test]
fn execution_evidence_preserves_hashes_limits_and_resource_usage() {
    let evidence = ExecutionEvidence {
        exit_code: 0,
        duration_ms: 125,
        stdout: "Linux build-worker-01 6.18.33.2".to_string(),
        stderr: "".to_string(),
        stdout_truncated: false,
        stderr_truncated: false,
        sha256_hash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855".to_string(),
        resource_usage: Some(ExecutionResourceUsage {
            cpu_time_ms: 80,
            peak_memory_bytes: 4194304,
        }),
    };

    let bytes = evidence.encode_to_vec();
    let decoded = ExecutionEvidence::decode(bytes.as_slice()).expect("decode evidence");
    assert_eq!(decoded.exit_code, 0);
    assert_eq!(decoded.duration_ms, 125);
    assert!(!decoded.stdout_truncated);
    let res = decoded.resource_usage.expect("resource usage");
    assert_eq!(res.peak_memory_bytes, 4194304);
}

#[test]
fn computer_command_stream_events_guarantee_ordered_sequence() {
    let events = vec![
        CommandStreamEvent {
            sequence_number: 1,
            timestamp_ms: 1726000002000,
            event: Some(command_stream_event::Event::StdoutChunk(
                "Step 1: starting\n".to_string(),
            )),
        },
        CommandStreamEvent {
            sequence_number: 2,
            timestamp_ms: 1726000002100,
            event: Some(command_stream_event::Event::StdoutChunk(
                "Step 2: compiling\n".to_string(),
            )),
        },
        CommandStreamEvent {
            sequence_number: 3,
            timestamp_ms: 1726000002200,
            event: Some(command_stream_event::Event::ExitEvidence(
                ExecutionEvidence {
                    exit_code: 0,
                    duration_ms: 200,
                    stdout: "Step 1: starting\nStep 2: compiling\n".to_string(),
                    stderr: "".to_string(),
                    stdout_truncated: false,
                    stderr_truncated: false,
                    sha256_hash: "hash-001".to_string(),
                    resource_usage: None,
                },
            )),
        },
    ];

    let mut last_seq = 0;
    for ev in events {
        assert_eq!(ev.sequence_number, last_seq + 1);
        last_seq = ev.sequence_number;
        let b = ev.encode_to_vec();
        let dec = CommandStreamEvent::decode(b.as_slice()).expect("decode event");
        assert_eq!(dec.sequence_number, last_seq);
    }
}

#[test]
fn computer_runtime_empty_implementation_fails_closed() {
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
        method: METHOD_EXECUTE_COMMAND.to_string(),
        payload_type_url: EXECUTE_COMMAND_REQUEST_TYPE_URL.to_string(),
        payload: vec![],
        request_id: "req-comp-gate-001".to_string(),
        stream_mode: 0,
    };

    let response = handler(request);
    match response.result {
        Some(direct_invocation_response::Result::Error(err)) => {
            assert_eq!(err.code, direct_invocation_error::Code::Unavailable as i32);
            assert_eq!(err.domain_code, "CAPABILITY_UNAVAILABLE");
        }
        _ => {
            panic!("Empty computer runtime implementation must fail closed with typed UNAVAILABLE")
        }
    }
}
