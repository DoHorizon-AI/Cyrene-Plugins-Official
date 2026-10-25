//! Real Linux integration checks for the optional Microsoft MXC backend.

#![cfg(all(feature = "mxc", target_os = "linux"))]

use cyrene_computer_runtime::{
    computer_runtime_v1::{
        command_execution_response, command_stream_event, CommandExecutionRequest,
        CommandExecutionResponse, ComputerErrorCode,
    },
    ComputerExecutionBackend, ComputerRuntimeLimits, ComputerRuntimeService, IsolationLevel,
};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tempfile::tempdir;
use tokio::sync::mpsc;

fn helper_path() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_cyrene-mxc-exec"))
}

fn runtime(workspace_root: &Path, artifact_root: &Path, helper: PathBuf) -> ComputerRuntimeService {
    ComputerRuntimeService::with_execution_backend(
        vec![workspace_root.to_path_buf()],
        artifact_root.to_path_buf(),
        ComputerRuntimeLimits::default(),
        ComputerExecutionBackend::MicrosoftMxc,
        Some(helper),
    )
    .expect("the MXC startup containment probe must pass on a supported host")
}

fn request(command: impl Into<String>, cwd: &Path) -> CommandExecutionRequest {
    CommandExecutionRequest {
        command: command.into(),
        cwd: Some(cwd.to_string_lossy().into_owned()),
        env: HashMap::new(),
        timeout_ms: Some(5_000),
        max_output_bytes: None,
    }
}

fn evidence(
    response: CommandExecutionResponse,
) -> cyrene_computer_runtime::computer_runtime_v1::ExecutionEvidence {
    match response.result.expect("terminal response") {
        command_execution_response::Result::Evidence(value) => value,
        command_execution_response::Result::Error(error) => {
            panic!("MXC execution failed: {}", error.message)
        }
    }
}

fn quote(path: &Path) -> String {
    format!("'{}'", path.to_string_lossy().replace('\'', "'\\''"))
}

#[tokio::test]
async fn mxc_requires_a_real_start_marker_and_preserves_real_exit_255() {
    let workspace = tempdir().unwrap();
    let artifact_root = tempdir().unwrap();
    let service = runtime(workspace.path(), artifact_root.path(), helper_path());
    assert_eq!(
        service.isolation_level(),
        IsolationLevel::MicrosoftMxcProcessNamespace
    );
    assert_eq!(service.bubblewrap_version(), Some("0.13.1"));

    let result = service
        .execute_command(
            request("printf 'workload-started\\n'; exit 255", workspace.path()),
            None,
        )
        .await;
    let evidence = evidence(result);
    assert_eq!(evidence.exit_code, 255);
    assert_eq!(evidence.stdout, "workload-started\n");
    assert!(!evidence.stdout.contains("CYRENE_MXC_STARTED_"));
}

#[tokio::test]
async fn mxc_forwards_only_filtered_request_environment_and_forces_private_home() {
    let workspace = tempdir().unwrap();
    let artifact_root = tempdir().unwrap();
    let service = runtime(workspace.path(), artifact_root.path(), helper_path());
    let mut command = request(
        "printf '%s|%s|%s|%s' \"$SAFE_APP_VAR\" \"$HOME\" \"$TMPDIR\" \"${CYRENE_PRIVATE_TEST_TOKEN-unset}\"",
        workspace.path(),
    );
    command
        .env
        .insert("SAFE_APP_VAR".into(), "request-value".into());
    command.env.insert(
        "CYRENE_PRIVATE_TEST_TOKEN".into(),
        "must-not-enter-workload".into(),
    );
    command
        .env
        .insert("HOME".into(), "/host/home-must-not-pass".into());
    command
        .env
        .insert("TMPDIR".into(), "/host/tmp-must-not-pass".into());

    let result = service.execute_command(command, None).await;
    let evidence = evidence(result);
    assert_eq!(evidence.stdout, "request-value|/tmp/cyrene-home|/tmp|unset");
}

#[tokio::test]
async fn mxc_mounts_only_the_workspace_for_writes_and_blocks_host_loopback() {
    let workspace = tempdir().unwrap();
    let outside = tempdir().unwrap();
    let artifact_root = tempdir().unwrap();
    let secret_path = outside.path().join("host-secret.txt");
    std::fs::write(&secret_path, "host-only-value").unwrap();
    let service = runtime(workspace.path(), artifact_root.path(), helper_path());

    let mut command = request(
        format!(
            "printf 'workspace-value' > allowed.txt; if cat {} >/dev/null 2>&1; then echo OUTSIDE_READ; else echo OUTSIDE_BLOCKED; fi; cat allowed.txt",
            quote(&secret_path)
        ),
        workspace.path(),
    );
    command.timeout_ms = Some(5_000);
    let filesystem_evidence = evidence(service.execute_command(command, None).await);
    assert_eq!(filesystem_evidence.exit_code, 0);
    assert!(filesystem_evidence.stdout.contains("OUTSIDE_BLOCKED"));
    assert!(!filesystem_evidence.stdout.contains("host-only-value"));
    assert_eq!(
        std::fs::read_to_string(workspace.path().join("allowed.txt")).unwrap(),
        "workspace-value"
    );

    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    listener.set_nonblocking(true).unwrap();
    let port = listener.local_addr().unwrap().port();
    let command = format!(
        "if /bin/bash -c 'echo blocked-test > /dev/tcp/127.0.0.1/{port}' 2>/dev/null; then echo LOOPBACK_CONNECTED; else echo LOOPBACK_BLOCKED; fi"
    );
    let evidence = evidence(
        service
            .execute_command(request(command, workspace.path()), None)
            .await,
    );
    assert!(evidence.stdout.contains("LOOPBACK_BLOCKED"));
    assert!(!evidence.stdout.contains("LOOPBACK_CONNECTED"));
    assert!(matches!(
        listener.accept(),
        Err(ref error) if error.kind() == std::io::ErrorKind::WouldBlock
    ));
}

#[tokio::test]
async fn mxc_drains_both_pipes_while_applying_the_shared_output_limit() {
    let workspace = tempdir().unwrap();
    let artifact_root = tempdir().unwrap();
    let service = ComputerRuntimeService::with_execution_backend(
        vec![workspace.path().to_path_buf()],
        artifact_root.path().to_path_buf(),
        ComputerRuntimeLimits {
            max_output_bytes: 4096,
            ..ComputerRuntimeLimits::default()
        },
        ComputerExecutionBackend::MicrosoftMxc,
        Some(helper_path()),
    )
    .expect("the MXC startup containment probe must pass on a supported host");
    let result = service
        .execute_command(
            request(
                "head -c 200000 /dev/zero | tr '\\0' x; head -c 200000 /dev/zero | tr '\\0' y >&2",
                workspace.path(),
            ),
            None,
        )
        .await;
    let evidence = evidence(result);
    assert!(evidence.stdout_truncated);
    assert!(evidence.stderr_truncated);
    assert!(evidence.stdout.len() <= 4096);
    assert!(evidence.stderr.len() <= 4096);
    let mut hasher = Sha256::new();
    hasher.update(evidence.stdout.as_bytes());
    hasher.update(b"|");
    hasher.update(evidence.stderr.as_bytes());
    assert_eq!(evidence.sha256_hash, format!("{:x}", hasher.finalize()));
}

#[tokio::test]
async fn mxc_streams_workload_output_without_leaking_the_start_marker() {
    let workspace = tempdir().unwrap();
    let artifact_root = tempdir().unwrap();
    let service = Arc::new(runtime(
        workspace.path(),
        artifact_root.path(),
        helper_path(),
    ));
    let (sender, mut receiver) = mpsc::channel(32);
    let command_service = Arc::clone(&service);
    let command = request(
        "printf 'first\\n'; sleep 0.05; printf 'second\\n'",
        workspace.path(),
    );
    let execute = tokio::spawn(async move {
        command_service
            .execute_command_stream(command, None, sender)
            .await
    });
    let mut stdout = String::new();
    let mut terminal = None;
    while let Some(event) = receiver.recv().await {
        match event.event {
            Some(command_stream_event::Event::StdoutChunk(chunk)) => stdout.push_str(&chunk),
            Some(command_stream_event::Event::ExitEvidence(evidence)) => terminal = Some(evidence),
            Some(command_stream_event::Event::StreamError(error)) => {
                panic!("MXC stream failed: {}", error.message)
            }
            _ => {}
        }
    }
    execute.await.unwrap().unwrap();
    assert_eq!(stdout, "first\nsecond\n");
    assert!(!stdout.contains("CYRENE_MXC_STARTED_"));
    let terminal = terminal.expect("verified terminal evidence");
    assert_eq!(terminal.exit_code, 0);
    let mut hasher = Sha256::new();
    hasher.update(terminal.stdout.as_bytes());
    hasher.update(b"|");
    hasher.update(terminal.stderr.as_bytes());
    assert_eq!(terminal.sha256_hash, format!("{:x}", hasher.finalize()));
}

#[tokio::test]
async fn mxc_timeout_and_stream_future_abort_remove_detached_descendants() {
    let workspace = tempdir().unwrap();
    let artifact_root = tempdir().unwrap();
    let service = Arc::new(runtime(
        workspace.path(),
        artifact_root.path(),
        helper_path(),
    ));

    let timeout_marker = workspace.path().join("timeout-descendant.txt");
    let mut timeout_request = request(
        format!(
            "setsid /bin/sh -c 'sleep 1; printf alive > {}' >/dev/null 2>&1 & sleep 30",
            quote(&timeout_marker)
        ),
        workspace.path(),
    );
    timeout_request.timeout_ms = Some(350);
    let timeout_response = service.execute_command(timeout_request, None).await;
    assert!(matches!(
        timeout_response.result,
        Some(command_execution_response::Result::Error(ref error))
            if error.code == ComputerErrorCode::CommandTimeout as i32
    ));
    tokio::time::sleep(Duration::from_millis(1300)).await;
    assert!(!timeout_marker.exists());

    let abort_marker = workspace.path().join("abort-descendant.txt");
    let (sender, _receiver) = mpsc::channel(32);
    let command = request(
        format!(
            "setsid /bin/sh -c 'sleep 1; printf alive > {}' >/dev/null 2>&1 & sleep 30",
            quote(&abort_marker)
        ),
        workspace.path(),
    );
    let command_service = Arc::clone(&service);
    let task = tokio::spawn(async move {
        command_service
            .execute_command_stream(command, None, sender)
            .await
    });
    tokio::time::sleep(Duration::from_millis(250)).await;
    task.abort();
    let _ = task.await;
    tokio::time::sleep(Duration::from_millis(1300)).await;
    assert!(!abort_marker.exists());
}

#[tokio::test]
async fn mxc_cooperative_cancellation_stops_the_workload() {
    let workspace = tempdir().unwrap();
    let artifact_root = tempdir().unwrap();
    let service = Arc::new(runtime(
        workspace.path(),
        artifact_root.path(),
        helper_path(),
    ));
    let cancellation = Arc::new(AtomicBool::new(false));
    let command_service = Arc::clone(&service);
    let command_cancel = Arc::clone(&cancellation);
    let command = request("sleep 30", workspace.path());
    let task = tokio::spawn(async move {
        command_service
            .execute_command(command, Some(command_cancel))
            .await
    });
    tokio::time::sleep(Duration::from_millis(200)).await;
    cancellation.store(true, Ordering::SeqCst);
    let response = task.await.unwrap();
    assert!(matches!(
        response.result,
        Some(command_execution_response::Result::Error(ref error))
            if error.code == ComputerErrorCode::CommandTimeout as i32
    ));
}

#[tokio::test]
async fn mxc_unary_future_drop_kills_detached_descendants() {
    let workspace = tempdir().unwrap();
    let artifact_root = tempdir().unwrap();
    let service = Arc::new(runtime(
        workspace.path(),
        artifact_root.path(),
        helper_path(),
    ));
    let marker = workspace.path().join("unary-abort-descendant.txt");
    let command = request(
        format!(
            "setsid /bin/sh -c 'sleep 1; printf alive > {}' >/dev/null 2>&1 & sleep 30",
            quote(&marker)
        ),
        workspace.path(),
    );
    let command_service = Arc::clone(&service);
    let task = tokio::spawn(async move { command_service.execute_command(command, None).await });
    tokio::time::sleep(Duration::from_millis(250)).await;
    task.abort();
    let _ = task.await;
    tokio::time::sleep(Duration::from_millis(1300)).await;
    assert!(!marker.exists());
}

#[tokio::test]
async fn mxc_selection_fails_closed_when_the_trusted_helper_is_missing_or_fake() {
    let workspace = tempdir().unwrap();
    let artifact_root = tempdir().unwrap();
    let missing = workspace.path().join("missing-helper");
    let missing_result = ComputerRuntimeService::with_execution_backend(
        vec![workspace.path().to_path_buf()],
        artifact_root.path().to_path_buf(),
        ComputerRuntimeLimits::default(),
        ComputerExecutionBackend::MicrosoftMxc,
        Some(missing),
    );
    assert!(missing_result.is_err());

    let fake_dir = tempdir().unwrap();
    let fake_helper = fake_dir.path().join("fake-mxc-helper");
    std::fs::write(&fake_helper, b"#!/bin/sh\nexit 0\n").unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&fake_helper, std::fs::Permissions::from_mode(0o700)).unwrap();
    let fake_result = ComputerRuntimeService::with_execution_backend(
        vec![workspace.path().to_path_buf()],
        artifact_root.path().to_path_buf(),
        ComputerRuntimeLimits::default(),
        ComputerExecutionBackend::MicrosoftMxc,
        Some(fake_helper),
    );
    assert!(fake_result.is_err());

    let old_runtime = tempdir().unwrap();
    let old_helper = old_runtime.path().join("cyrene-mxc-exec");
    std::fs::copy(helper_path(), &old_helper).unwrap();
    let old_bubblewrap = old_runtime.path().join("bwrap");
    std::fs::write(
        &old_bubblewrap,
        b"#!/bin/sh\nprintf 'bubblewrap 0.11.9\\n'\n",
    )
    .unwrap();
    std::fs::set_permissions(&old_bubblewrap, std::fs::Permissions::from_mode(0o700)).unwrap();
    let outdated_result = ComputerRuntimeService::with_execution_backend(
        vec![workspace.path().to_path_buf()],
        artifact_root.path().to_path_buf(),
        ComputerRuntimeLimits::default(),
        ComputerExecutionBackend::MicrosoftMxc,
        Some(old_helper),
    );
    let outdated_error = match outdated_result {
        Err(error) => error,
        Ok(_) => panic!("an outdated trusted sibling Bubblewrap must block MXC startup"),
    };
    assert!(
        outdated_error.contains("below the required 0.12.0 security floor"),
        "unexpected outdated Bubblewrap error: {outdated_error}"
    );

    let no_receipt_helper = fake_dir.path().join("no-receipt-helper");
    std::fs::write(
        &no_receipt_helper,
        b"#!/bin/sh\nif [ \"$1\" = \"--probe\" ]; then echo 'CYRENE_MXC_PROBE_OK backend=mxc sdk=1.0.1 containment=bubblewrap bwrap=0.13.1'; exit 0; fi\nexit 0\n",
    )
    .unwrap();
    std::fs::set_permissions(&no_receipt_helper, std::fs::Permissions::from_mode(0o700)).unwrap();
    let service = ComputerRuntimeService::with_execution_backend(
        vec![workspace.path().to_path_buf()],
        artifact_root.path().to_path_buf(),
        ComputerRuntimeLimits::default(),
        ComputerExecutionBackend::MicrosoftMxc,
        Some(no_receipt_helper),
    )
    .expect("the startup probe output is present");
    let response = service
        .execute_command(request("echo must-not-verify", workspace.path()), None)
        .await;
    match response.result.expect("terminal response") {
        command_execution_response::Result::Error(error) => {
            assert_eq!(error.code, ComputerErrorCode::ExecutionDenied as i32);
            assert!(error.message.contains("terminal receipt"));
        }
        command_execution_response::Result::Evidence(_) => {
            panic!("a zero launcher exit without a terminal receipt must not be accepted")
        }
    }
}
