// SPDX-License-Identifier: Apache-2.0
//! Integration tests for the MCP stdio `tool.provider.v1` implementation.
//!
//! The fake MCP server fixture is a real child process speaking real
//! newline-delimited JSON-RPC, so these tests exercise process start, stdio
//! framing, snapshot enforcement, typed errors, timeouts, and cancellation
//! kill semantics. It requires a system `python3`.

use std::path::{Path, PathBuf};
use std::time::Duration;

use cyrene_mcp_provider::{McpServerConfig, McpToolProvider};
use cyrene_plugin_contracts::tool_provider_v1::{
    call_tool_response, list_tools_response, tool_content_part, CallToolRequest, CallToolResponse,
    ListToolsResponse, ToolCatalog, ToolProviderError, ToolProviderErrorCode,
};

const FIXTURE: &str = "tests/fixtures/fake_mcp_server.py";

fn fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(FIXTURE)
}

fn fake_server(timeout: Duration) -> McpServerConfig {
    McpServerConfig::new(
        "mcp.fake",
        "python3",
        vec![fixture_path().to_string_lossy().into_owned()],
    )
    .with_timeout(timeout)
}

fn server_with_pid_file(directory: &Path, timeout: Duration) -> (McpServerConfig, PathBuf) {
    let pid_path = directory.join("server.pid");
    let mut args = vec![fixture_path().to_string_lossy().into_owned()];
    args.push("--pid-file".to_string());
    args.push(pid_path.to_string_lossy().into_owned());
    let config = McpServerConfig::new("mcp.fake", "python3", args).with_timeout(timeout);
    (config, pid_path)
}

fn catalog(response: ListToolsResponse) -> ToolCatalog {
    match response.result {
        Some(list_tools_response::Result::Catalog(catalog)) => catalog,
        Some(list_tools_response::Result::Error(error)) => {
            panic!("expected a catalog, got error {error:?}")
        }
        None => panic!("catalog response carried no payload"),
    }
}

fn provider_error(response: CallToolResponse) -> ToolProviderError {
    match response.result {
        Some(call_tool_response::Result::Error(error)) => error,
        other => panic!("expected a typed error, got {other:?}"),
    }
}

fn call_request(tool: &str, arguments: &str) -> CallToolRequest {
    CallToolRequest {
        binding_id: "mcp.fake".to_string(),
        provider_tool_id: tool.to_string(),
        arguments_json: arguments.to_string(),
        catalog_version: None,
    }
}

#[tokio::test]
async fn list_tools_returns_a_deterministic_snapshot_catalog() {
    let provider = McpToolProvider::new(vec![fake_server(Duration::from_secs(10))]);
    assert!(provider.is_configured());
    assert_eq!(provider.bindings(), vec!["mcp.fake".to_string()]);

    let first = catalog(provider.list_tools(None).await);
    assert_eq!(first.tools.len(), 3);
    assert!(first.catalog_version.starts_with("sha256:"));
    for tool in &first.tools {
        assert_eq!(tool.binding_id, "mcp.fake");
    }
    let echo = &first.tools[0];
    assert_eq!(echo.provider_tool_id, "echo");
    assert!(echo.input_schema_json.contains("\"text\""));
    assert!(echo.output_schema_json.is_none());

    let second = catalog(provider.list_tools(None).await);
    assert_eq!(first.catalog_version, second.catalog_version);
}

#[tokio::test]
async fn call_tool_dispatches_and_enforces_the_recorded_snapshot() {
    let provider = McpToolProvider::new(vec![fake_server(Duration::from_secs(10))]);
    let _ = provider.list_tools(None).await;

    let echo = provider
        .call_tool(&call_request("echo", r#"{"text":"hello"}"#))
        .await;
    let outcome = match echo.result {
        Some(call_tool_response::Result::Outcome(outcome)) => outcome,
        other => panic!("expected an outcome, got {other:?}"),
    };
    assert!(!outcome.is_error);
    assert_eq!(outcome.content.len(), 1);
    match &outcome.content[0].content {
        Some(tool_content_part::Content::Text(text)) => assert_eq!(text.text, "hello"),
        other => panic!("expected text content, got {other:?}"),
    }

    let failing = provider.call_tool(&call_request("fail", "{}")).await;
    match failing.result {
        Some(call_tool_response::Result::Outcome(outcome)) => assert!(outcome.is_error),
        other => panic!("expected a tool-level failure outcome, got {other:?}"),
    }

    let unknown = provider.call_tool(&call_request("missing", "{}")).await;
    let error = provider_error(unknown);
    assert_eq!(error.code, ToolProviderErrorCode::ToolNotFound as i32);
    assert!(!error.retryable);
}

#[tokio::test]
async fn call_tool_without_a_snapshot_still_dispatches_and_validates_arguments() {
    let provider = McpToolProvider::new(vec![fake_server(Duration::from_secs(10))]);
    let echo = provider
        .call_tool(&call_request("echo", r#"{"text":"direct"}"#))
        .await;
    match echo.result {
        Some(call_tool_response::Result::Outcome(outcome)) => assert!(!outcome.is_error),
        other => panic!("expected an outcome, got {other:?}"),
    }

    let invalid = provider.call_tool(&call_request("echo", "not-json")).await;
    let error = provider_error(invalid);
    assert_eq!(error.code, ToolProviderErrorCode::InvalidArguments as i32);

    let unknown_binding = CallToolRequest {
        binding_id: "mcp.unknown".to_string(),
        provider_tool_id: "echo".to_string(),
        arguments_json: "{}".to_string(),
        catalog_version: None,
    };
    let error = provider_error(provider.call_tool(&unknown_binding).await);
    assert_eq!(error.code, ToolProviderErrorCode::InvalidArguments as i32);
}

#[tokio::test]
async fn timeout_is_typed_and_reaps_the_child_process() {
    let directory = tempfile::tempdir().unwrap();
    let (config, pid_path) = server_with_pid_file(directory.path(), Duration::from_millis(400));
    let provider = McpToolProvider::new(vec![config]);

    let response = provider
        .call_tool(&call_request("slow", r#"{"ms":10000}"#))
        .await;
    let error = provider_error(response);
    assert_eq!(error.code, ToolProviderErrorCode::Timeout as i32);
    assert!(error.retryable);

    let pid = wait_for_pid_file(&pid_path);
    assert!(
        wait_for_process_exit(&pid, Duration::from_secs(5)),
        "timed-out MCP child {pid} was not reaped"
    );
}

#[tokio::test]
async fn cancelled_call_kills_the_child_process() {
    let directory = tempfile::tempdir().unwrap();
    let (config, pid_path) = server_with_pid_file(directory.path(), Duration::from_secs(30));
    let provider = McpToolProvider::new(vec![config]);
    let request = call_request("slow", r#"{"ms":10000}"#);

    {
        let call = provider.call_tool(&request);
        tokio::pin!(call);
        tokio::select! {
            result = &mut call => panic!("call unexpectedly completed: {result:?}"),
            () = tokio::time::sleep(Duration::from_millis(600)) => {}
        }
    }

    let pid = wait_for_pid_file(&pid_path);
    assert!(
        wait_for_process_exit(&pid, Duration::from_secs(5)),
        "cancelled MCP child {pid} was not reaped"
    );
}

#[tokio::test]
async fn spawn_failure_maps_to_a_provider_error() {
    let config = McpServerConfig::new("mcp.broken", "/nonexistent/cyrene-mcp-server", Vec::new())
        .with_timeout(Duration::from_millis(500));
    let provider = McpToolProvider::new(vec![config]);
    let response = provider.list_tools(None).await;
    match response.result {
        Some(list_tools_response::Result::Error(error)) => {
            assert_eq!(error.code, ToolProviderErrorCode::ProviderError as i32);
        }
        other => panic!("expected a typed error, got {other:?}"),
    }
}

fn wait_for_pid_file(path: &Path) -> i32 {
    for _ in 0..50 {
        if let Ok(contents) = std::fs::read_to_string(path) {
            if let Ok(pid) = contents.trim().parse::<i32>() {
                return pid;
            }
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    panic!("fake MCP server never wrote its pid file at {path:?}");
}

fn wait_for_process_exit(pid: &i32, timeout: Duration) -> bool {
    let deadline = std::time::Instant::now() + timeout;
    let proc_entry = Path::new("/proc").join(pid.to_string());
    while std::time::Instant::now() < deadline {
        if !proc_entry.exists() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}
