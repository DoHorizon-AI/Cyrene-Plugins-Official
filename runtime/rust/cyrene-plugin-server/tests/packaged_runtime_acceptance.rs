// SPDX-License-Identifier: Apache-2.0
//! Packaged-runtime acceptance (W2-3): start the packaged `cyrene-plugin-server`
//! binary the way the plugin manifest launches it, then drive the acceptance
//! checklist through DirectPluginRuntime against that real process.
//!
//! Checklist: install/start (packaged binary + health), direct dispatch,
//! ExecuteCommand, ListDir, cancellation, timeout, descendant process
//! cleanup, path traversal denial, environment filtering, artifact roundtrip.
//!
//! Linux-first: the managed commands require bash/sleep/printf, so the suite
//! is compiled on unix only until the Windows acceptance environment exists.
#![cfg(unix)]

pub mod proto {
    #![allow(clippy::result_large_err)]
    tonic::include_proto!("cyrene.plugin.runtime.v1");
}

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use prost::Message;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};
use tonic::transport::Channel;

use proto::direct_invocation_error::Code;
use proto::direct_invocation_response::Result as InvocationResult;
use proto::direct_plugin_runtime_client::DirectPluginRuntimeClient;
use proto::{health_response, DirectInvocationRequest, DirectStreamMode, HealthRequest};

use cyrene_plugin_contracts::computer_runtime_v1::{
    command_execution_response, create_artifact_response, get_artifact_response, list_dir_response,
    CommandExecutionRequest, CommandExecutionResponse, ComputerError, ComputerErrorCode,
    CreateArtifactRequest, CreateArtifactResponse, GetArtifactRequest, GetArtifactResponse,
    ListDirRequest, ListDirResponse,
};

async fn start_packaged_runtime() -> (Child, DirectPluginRuntimeClient<Channel>) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_cyrene-plugin-server"))
        .args(["--host", "127.0.0.1", "--port", "0"])
        .env("CYRENE_ACCEPTANCE_SECRET_TOKEN", "must-not-leak")
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true)
        .spawn()
        .expect("spawn packaged server");
    let stdout = child.stdout.take().expect("piped stdout");
    let mut lines = BufReader::new(stdout).lines();
    let address = tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            let line = lines
                .next_line()
                .await
                .expect("read announcement line")
                .expect("server stdout closed before announcing");
            if let Some(rest) = line.strip_prefix("[CYRENE_SERVER_STARTED] addr=") {
                return rest.trim().to_string();
            }
        }
    })
    .await
    .expect("packaged server did not announce its address in time");
    let client = DirectPluginRuntimeClient::connect(format!("http://{address}"))
        .await
        .expect("connect to packaged server");
    (child, client)
}

async fn invoke(
    client: &mut DirectPluginRuntimeClient<Channel>,
    method: &str,
    payload_type_url: &str,
    payload: Vec<u8>,
) -> proto::DirectInvocationResponse {
    client
        .invoke(DirectInvocationRequest {
            interface_version: "1".to_string(),
            capability: "computer.runtime.v1".to_string(),
            method: method.to_string(),
            payload,
            payload_type_url: payload_type_url.to_string(),
            request_id: format!("accept-{method}"),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .expect("invoke transport")
        .into_inner()
}

async fn run_command(
    client: &mut DirectPluginRuntimeClient<Channel>,
    request: CommandExecutionRequest,
) -> proto::DirectInvocationResponse {
    let mut payload = Vec::new();
    request.encode(&mut payload).unwrap();
    invoke(
        client,
        "ExecuteCommand",
        "type.cyrene.io/cyrene.computer.runtime.v1.CommandExecutionRequest",
        payload,
    )
    .await
}

fn expect_error(response: proto::DirectInvocationResponse) -> ComputerError {
    match response.result.unwrap() {
        InvocationResult::Payload(payload) => {
            let decoded = CommandExecutionResponse::decode(&payload.value[..]).unwrap();
            match decoded.result.unwrap() {
                command_execution_response::Result::Error(error) => error,
                command_execution_response::Result::Evidence(evidence) => {
                    panic!("expected an error, got evidence: {evidence:?}")
                }
            }
        }
        InvocationResult::Error(error) => panic!("invoke failed: {error:?}"),
    }
}

fn expect_evidence(
    response: proto::DirectInvocationResponse,
) -> cyrene_plugin_contracts::computer_runtime_v1::ExecutionEvidence {
    match response.result.unwrap() {
        InvocationResult::Payload(payload) => {
            let decoded = CommandExecutionResponse::decode(&payload.value[..]).unwrap();
            match decoded.result.unwrap() {
                command_execution_response::Result::Evidence(evidence) => evidence,
                command_execution_response::Result::Error(error) => {
                    panic!("expected evidence, got error: {error:?}")
                }
            }
        }
        InvocationResult::Error(error) => panic!("invoke failed: {error:?}"),
    }
}

fn command(
    command: String,
    timeout_ms: i64,
    env: HashMap<String, String>,
) -> CommandExecutionRequest {
    CommandExecutionRequest {
        command,
        cwd: None,
        env,
        timeout_ms: Some(timeout_ms),
        max_output_bytes: None,
    }
}

async fn wait_for_pid_file(path: &Path) -> i32 {
    for _ in 0..100 {
        if let Ok(contents) = tokio::fs::read_to_string(path).await {
            if let Ok(pid) = contents.trim().parse::<i32>() {
                return pid;
            }
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("managed command never wrote its pid file at {path:?}");
}

async fn wait_for_process_exit(pid: i32, timeout: Duration) -> bool {
    let proc_entry = Path::new("/proc").join(pid.to_string());
    let _ = tokio::time::timeout(timeout, async {
        while proc_entry.exists() {
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await;
    !proc_entry.exists()
}

fn grandchild_pid_file(directory: &tempfile::TempDir) -> PathBuf {
    directory.path().join("grandchild.pid")
}

#[tokio::test]
async fn test_packaged_runtime_start_dispatch_and_bounded_filesystem() {
    let (_server, mut client) = start_packaged_runtime().await;

    // install/start + direct dispatch
    let health = client.health(HealthRequest {}).await.unwrap().into_inner();
    assert_eq!(health.status, health_response::Status::Serving as i32);
    assert!(health
        .capabilities
        .iter()
        .any(|capability| capability == "computer.runtime.v1"));

    // ExecuteCommand
    let evidence = expect_evidence(
        run_command(
            &mut client,
            command("printf 'packaged-hello'".to_string(), 5_000, HashMap::new()),
        )
        .await,
    );
    assert_eq!(evidence.exit_code, 0);
    assert_eq!(evidence.stdout, "packaged-hello");

    // ListDir
    let mut payload = Vec::new();
    ListDirRequest {
        path: ".".to_string(),
        max_depth: Some(1),
    }
    .encode(&mut payload)
    .unwrap();
    let response = invoke(
        &mut client,
        "ListDir",
        "type.cyrene.io/cyrene.computer.runtime.v1.ListDirRequest",
        payload,
    )
    .await;
    let entries = match response.result.unwrap() {
        InvocationResult::Payload(payload) => {
            match ListDirResponse::decode(&payload.value[..])
                .unwrap()
                .result
                .unwrap()
            {
                list_dir_response::Result::Entries(entries) => entries.entries,
                list_dir_response::Result::Error(error) => panic!("list_dir error: {error:?}"),
            }
        }
        InvocationResult::Error(error) => panic!("list_dir failed: {error:?}"),
    };
    assert!(entries
        .iter()
        .any(|entry| entry.name == "Cargo.toml" && !entry.is_directory));

    // Path traversal denial
    let mut payload = Vec::new();
    ListDirRequest {
        path: "../".to_string(),
        max_depth: Some(1),
    }
    .encode(&mut payload)
    .unwrap();
    let response = invoke(
        &mut client,
        "ListDir",
        "type.cyrene.io/cyrene.computer.runtime.v1.ListDirRequest",
        payload,
    )
    .await;
    match response.result.unwrap() {
        InvocationResult::Payload(payload) => {
            match ListDirResponse::decode(&payload.value[..])
                .unwrap()
                .result
                .unwrap()
            {
                list_dir_response::Result::Error(error) => {
                    assert_eq!(error.code, ComputerErrorCode::PathTraversalDenied as i32);
                }
                list_dir_response::Result::Entries(_) => {
                    panic!("traversal must be denied")
                }
            }
        }
        InvocationResult::Error(error) => panic!("list_dir failed: {error:?}"),
    }
}

#[tokio::test]
async fn test_packaged_runtime_environment_filtering_and_artifact_roundtrip() {
    let (_server, mut client) = start_packaged_runtime().await;

    // Host secrets never reach the managed command; requested names carrying
    // sensitive patterns are stripped; benign requested names pass through.
    let mut env = HashMap::new();
    env.insert(
        "CYRENE_ACCEPTANCE_VISIBLE".to_string(),
        "visible-ok".to_string(),
    );
    env.insert(
        "CYRENE_REQUEST_TOKEN".to_string(),
        "request-secret".to_string(),
    );
    let evidence = expect_evidence(
        run_command(
            &mut client,
            command(
                "printf '%s|%s|%s' \"${CYRENE_ACCEPTANCE_SECRET_TOKEN-unset}\" \"${CYRENE_ACCEPTANCE_VISIBLE-unset}\" \"${CYRENE_REQUEST_TOKEN-unset}\"".to_string(),
                5_000,
                env,
            ),
        )
        .await,
    );
    assert_eq!(evidence.stdout, "unset|visible-ok|unset");

    // Artifact roundtrip.
    let artifact_data = b"packaged acceptance artifact".to_vec();
    let mut payload = Vec::new();
    CreateArtifactRequest {
        name: "acceptance.bin".to_string(),
        mime_type: "application/octet-stream".to_string(),
        data: artifact_data.clone(),
    }
    .encode(&mut payload)
    .unwrap();
    let response = invoke(
        &mut client,
        "CreateArtifact",
        "type.cyrene.io/cyrene.computer.runtime.v1.CreateArtifactRequest",
        payload,
    )
    .await;
    let artifact_id = match response.result.unwrap() {
        InvocationResult::Payload(payload) => {
            match CreateArtifactResponse::decode(&payload.value[..])
                .unwrap()
                .result
                .unwrap()
            {
                create_artifact_response::Result::Artifact(metadata) => metadata.artifact_id,
                create_artifact_response::Result::Error(error) => {
                    panic!("create_artifact error: {error:?}")
                }
            }
        }
        InvocationResult::Error(error) => panic!("create_artifact failed: {error:?}"),
    };

    let mut payload = Vec::new();
    GetArtifactRequest {
        artifact_id: artifact_id.clone(),
    }
    .encode(&mut payload)
    .unwrap();
    let response = invoke(
        &mut client,
        "GetArtifact",
        "type.cyrene.io/cyrene.computer.runtime.v1.GetArtifactRequest",
        payload,
    )
    .await;
    match response.result.unwrap() {
        InvocationResult::Payload(payload) => {
            match GetArtifactResponse::decode(&payload.value[..])
                .unwrap()
                .result
                .unwrap()
            {
                get_artifact_response::Result::Payload(payload) => {
                    assert_eq!(payload.data, artifact_data);
                    assert_eq!(payload.metadata.unwrap().artifact_id, artifact_id);
                }
                get_artifact_response::Result::Error(error) => {
                    panic!("get_artifact error: {error:?}")
                }
            }
        }
        InvocationResult::Error(error) => panic!("get_artifact failed: {error:?}"),
    }
}

#[tokio::test]
async fn test_packaged_runtime_timeout_kills_the_process_group() {
    let directory = tempfile::tempdir().unwrap();
    let pid_file = grandchild_pid_file(&directory);
    let (_server, mut client) = start_packaged_runtime().await;

    let response = run_command(
        &mut client,
        command(
            format!("sleep 60 & echo $! > {}; wait", pid_file.display()),
            700,
            HashMap::new(),
        ),
    )
    .await;
    let error = expect_error(response);
    assert_eq!(error.code, ComputerErrorCode::CommandTimeout as i32);

    let pid = wait_for_pid_file(&pid_file).await;
    assert!(
        wait_for_process_exit(pid, Duration::from_secs(5)).await,
        "timed-out managed command left descendant {pid} running"
    );
}

#[tokio::test]
async fn test_packaged_runtime_stream_cancellation_kills_the_process_group() {
    let directory = tempfile::tempdir().unwrap();
    let pid_file = grandchild_pid_file(&directory);
    let (_server, mut client) = start_packaged_runtime().await;

    let mut payload = Vec::new();
    command(
        format!("sleep 60 & echo $! > {}; wait", pid_file.display()),
        60_000,
        HashMap::new(),
    )
    .encode(&mut payload)
    .unwrap();
    let stream = client
        .invoke_stream(DirectInvocationRequest {
            interface_version: "1".to_string(),
            capability: "computer.runtime.v1".to_string(),
            method: "ExecuteCommandStream".to_string(),
            payload,
            payload_type_url: "type.cyrene.io/cyrene.computer.runtime.v1.CommandExecutionRequest"
                .to_string(),
            request_id: "accept-stream-cancel".to_string(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .expect("open stream")
        .into_inner();

    let pid = wait_for_pid_file(&pid_file).await;
    drop(stream);

    assert!(
        wait_for_process_exit(pid, Duration::from_secs(5)).await,
        "cancelled stream left descendant {pid} running"
    );
    let _ = Code::InvalidRequest; // keep the error-code import used on all paths
}
