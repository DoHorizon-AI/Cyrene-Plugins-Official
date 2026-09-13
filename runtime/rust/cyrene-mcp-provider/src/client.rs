//! Minimal MCP stdio transport: newline-delimited JSON-RPC 2.0 over a child
//! process, scoped to a single operation.
//!
//! Sessions are per-operation on purpose. The provider never holds a
//! long-lived process, and dropping the session (timeout or cancellation)
//! kills the child through `kill_on_drop`. Long-lived process supervision is
//! explicitly out of scope for this provider; the production path reuses the
//! managed execution primitives owned by the launcher (see the capability
//! expansion plan, decision C).

use std::collections::VecDeque;
use std::fmt;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, Lines};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};
use tokio::sync::Mutex;

/// Client-side MCP protocol version implemented by this provider.
pub const MCP_PROTOCOL_VERSION: &str = "2024-11-05";

const STDERR_TAIL_LINES: usize = 32;

/// One configured MCP server binding.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct McpServerConfig {
    /// Binding that owns every tool this server exposes.
    pub binding_id: String,
    pub command: String,
    pub args: Vec<String>,
    /// Explicit environment additions. The child environment is otherwise
    /// cleared except for `PATH`, so a server cannot inherit ambient secrets.
    pub env: Vec<(String, String)>,
    /// Per-request deadline.
    pub timeout: Duration,
}

impl McpServerConfig {
    /// Build one server configuration with the default 30s request deadline.
    pub fn new(
        binding_id: impl Into<String>,
        command: impl Into<String>,
        args: Vec<String>,
    ) -> Self {
        Self {
            binding_id: binding_id.into(),
            command: command.into(),
            args,
            env: Vec::new(),
            timeout: Duration::from_secs(30),
        }
    }

    /// Override the per-request deadline.
    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    /// Add one explicit environment variable for the child process.
    pub fn with_env(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.env.push((key.into(), value.into()));
        self
    }
}

/// Transport-level failures; the provider maps these to contract error codes.
#[derive(Debug)]
pub enum McpClientError {
    Spawn(String),
    ProcessExited { stderr_tail: String },
    Timeout { method: String, timeout: Duration },
    Malformed(String),
    Protocol { code: i64, message: String },
    Io(String),
}

impl fmt::Display for McpClientError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Spawn(message) => write!(formatter, "failed to start MCP server: {message}"),
            Self::ProcessExited { stderr_tail } => {
                write!(
                    formatter,
                    "MCP server exited before answering: {stderr_tail}"
                )
            }
            Self::Timeout { method, timeout } => write!(
                formatter,
                "MCP call '{method}' timed out after {}ms",
                timeout.as_millis()
            ),
            Self::Malformed(message) => {
                write!(formatter, "malformed MCP response: {message}")
            }
            Self::Protocol { code, message } => {
                write!(formatter, "MCP provider error {code}: {message}")
            }
            Self::Io(message) => write!(formatter, "MCP transport error: {message}"),
        }
    }
}

/// One short-lived MCP session speaking newline-delimited JSON-RPC 2.0.
pub struct McpStdioClient {
    child: Child,
    stdin: ChildStdin,
    lines: Lines<BufReader<ChildStdout>>,
    stderr_tail: Arc<Mutex<VecDeque<String>>>,
    next_id: u64,
}

impl McpStdioClient {
    /// Start the configured server and complete the MCP initialize handshake.
    pub async fn start(config: &McpServerConfig) -> Result<Self, McpClientError> {
        let mut command = Command::new(&config.command);
        command
            .args(&config.args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .env_clear()
            .kill_on_drop(true);
        if let Some(path) = std::env::var_os("PATH") {
            command.env("PATH", path);
        }
        for (key, value) in &config.env {
            command.env(key, value);
        }

        let mut child = command
            .spawn()
            .map_err(|error| McpClientError::Spawn(error.to_string()))?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| McpClientError::Spawn("child stdin was not piped".to_string()))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| McpClientError::Spawn("child stdout was not piped".to_string()))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| McpClientError::Spawn("child stderr was not piped".to_string()))?;

        let stderr_tail = Arc::new(Mutex::new(VecDeque::new()));
        let tail = Arc::clone(&stderr_tail);
        tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let mut guard = tail.lock().await;
                if guard.len() == STDERR_TAIL_LINES {
                    guard.pop_front();
                }
                guard.push_back(line);
            }
        });

        let mut client = Self {
            child,
            stdin,
            lines: BufReader::new(stdout).lines(),
            stderr_tail,
            next_id: 1,
        };
        client.handshake(config.timeout).await?;
        Ok(client)
    }

    /// Perform the MCP `initialize` handshake followed by `initialized`.
    async fn handshake(&mut self, timeout: Duration) -> Result<(), McpClientError> {
        let params = json!({
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": "cyrene-mcp-provider",
                "version": env!("CARGO_PKG_VERSION"),
            },
        });
        let _ = self.request("initialize", params, timeout).await?;
        self.notify("notifications/initialized", json!({})).await
    }

    /// Send one request and await the matching response.
    pub async fn request(
        &mut self,
        method: &str,
        params: Value,
        timeout: Duration,
    ) -> Result<Value, McpClientError> {
        let id = self.next_id;
        self.next_id += 1;
        let message = json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method,
            "params": params,
        });
        self.write_message(&message).await?;

        match tokio::time::timeout(timeout, self.read_response(id)).await {
            Ok(result) => result,
            Err(_) => {
                // Best-effort cancellation for the abandoned request.
                let cancel = json!({
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": { "requestId": id, "reason": "deadline exceeded" },
                });
                let _ = self.write_message(&cancel).await;
                Err(McpClientError::Timeout {
                    method: method.to_string(),
                    timeout,
                })
            }
        }
    }

    /// Send one notification (no response expected).
    pub async fn notify(&mut self, method: &str, params: Value) -> Result<(), McpClientError> {
        let message = json!({ "jsonrpc": "2.0", "method": method, "params": params });
        self.write_message(&message).await
    }

    /// Close stdin and wait briefly, then kill the child if it lingers.
    pub async fn shutdown(mut self) {
        let _ = self.stdin.shutdown().await;
        match tokio::time::timeout(Duration::from_secs(2), self.child.wait()).await {
            Ok(_) => {}
            Err(_) => {
                let _ = self.child.kill().await;
            }
        }
    }

    async fn read_response(&mut self, id: u64) -> Result<Value, McpClientError> {
        loop {
            let line = {
                let next = self.lines.next_line();
                next.await
                    .map_err(|error| McpClientError::Io(error.to_string()))?
            };
            let Some(line) = line else {
                return Err(McpClientError::ProcessExited {
                    stderr_tail: self.stderr_text().await,
                });
            };
            let text = line.trim();
            if text.is_empty() {
                continue;
            }
            let message: Value = serde_json::from_str(text)
                .map_err(|error| McpClientError::Malformed(format!("{error}: {text}")))?;

            let Some(message_id) = message.get("id") else {
                // Notification; v1 has no use for server notifications.
                continue;
            };
            if message_id.as_u64() == Some(id) {
                if let Some(error) = message.get("error") {
                    let code = error.get("code").and_then(Value::as_i64).unwrap_or(0);
                    let text = error
                        .get("message")
                        .and_then(Value::as_str)
                        .unwrap_or("provider returned an error")
                        .to_string();
                    return Err(McpClientError::Protocol {
                        code,
                        message: text,
                    });
                }
                return Ok(message.get("result").cloned().unwrap_or(Value::Null));
            }
            if message.get("method").is_some() {
                // Server-initiated request; v1 answers method-not-found.
                self.respond_unsupported(message_id.clone()).await?;
            }
            // Responses to other request ids are not expected in v1; ignore.
        }
    }

    async fn respond_unsupported(&mut self, id: Value) -> Result<(), McpClientError> {
        let message = json!({
            "jsonrpc": "2.0",
            "id": id,
            "error": {
                "code": -32601,
                "message": "cyrene-mcp-provider v1 does not implement client-side methods",
            },
        });
        self.write_message(&message).await
    }

    async fn write_message(&mut self, message: &Value) -> Result<(), McpClientError> {
        let mut encoded = serde_json::to_vec(message)
            .map_err(|error| McpClientError::Malformed(error.to_string()))?;
        encoded.push(b'\n');
        self.stdin
            .write_all(&encoded)
            .await
            .map_err(|error| McpClientError::Io(error.to_string()))?;
        self.stdin
            .flush()
            .await
            .map_err(|error| McpClientError::Io(error.to_string()))
    }

    async fn stderr_text(&self) -> String {
        let guard = self.stderr_tail.lock().await;
        guard.iter().cloned().collect::<Vec<_>>().join(" | ")
    }
}
