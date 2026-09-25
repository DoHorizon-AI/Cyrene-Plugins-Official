//! Minimal MCP stdio transport: newline-delimited JSON-RPC 2.0 over a child
//! process, with one logical session per binding.
//!
//! A session is a stable JSON-RPC conversation: `tools/list` and every later
//! `tools/call` for the binding share it, so a catalog snapshot and the calls
//! planned against it stay on one server-side session. Session *lifecycle*
//! (spawn, supervision, teardown) belongs to the injected
//! [`McpSessionAdapter`]; the default adapter uses `kill_on_drop` children for
//! tests and local development, while production hosts inject an adapter
//! backed by managed execution (capability expansion plan, decision C).
//! 最小 MCP stdio 传输：通过子进程使用以换行符分隔的 JSON-RPC 2.0，每个 binding 使用一个逻辑 session。
//! session 是稳定的 JSON-RPC 会话：同一 binding 的 tools/list 和后续所有 tools/call 共用该 session，因此目录快照以及基于快照规划的调用都在同一个 server session 上执行。session 生命周期（启动、监管、拆除）由注入的 McpSessionAdapter 负责；默认 adapter 使用 kill_on_drop 子进程，仅用于测试和本地开发，生产 host 应注入基于托管执行的 adapter（能力扩展计划，决策 C）。

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
/// 此 provider 实现的客户端 MCP 协议版本。
pub const MCP_PROTOCOL_VERSION: &str = "2024-11-05";

const STDERR_TAIL_LINES: usize = 32;

/// One configured MCP server binding.
/// 一个已配置的 MCP server binding。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct McpServerConfig {
    /// Binding that owns every tool this server exposes.
    /// 拥有该 server 所暴露全部工具的 binding。
    pub binding_id: String,
    pub command: String,
    pub args: Vec<String>,
    /// Explicit environment additions. The child environment is otherwise
    /// cleared except for `PATH`, so a server cannot inherit ambient secrets.
    /// 为子进程显式添加的环境变量。除此之外，子进程环境会被清空，只保留 PATH，以免 server 继承环境中的 secret。
    pub env: Vec<(String, String)>,
    /// Per-request deadline.
    /// 每个 request 的 deadline。
    pub timeout: Duration,
}

impl McpServerConfig {
    /// Build one server configuration with the default 30s request deadline.
    /// 使用默认 30 秒 request deadline 构建一个 server 配置。
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
    /// 覆盖每个 request 的 deadline。
    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    /// Add one explicit environment variable for the child process.
    /// 为子进程添加一个显式环境变量。
    pub fn with_env(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.env.push((key.into(), value.into()));
        self
    }
}

/// Transport-level failures; the provider maps these to contract error codes.
/// 传输层失败；provider 会将其映射为 contract error code。
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

/// One logical MCP session: a stable JSON-RPC conversation with one server
/// binding for as long as the session is open.
/// 一个逻辑 MCP session：只要 session 保持打开，它就持续对应同一 server binding 的稳定 JSON-RPC 会话。
#[async_trait::async_trait]
pub trait McpSession: Send {
    /// Send one request and await the matching response.
    /// 发送一个 request 并等待匹配的 response。
    async fn request(
        &mut self,
        method: &str,
        params: Value,
        timeout: Duration,
    ) -> Result<Value, McpClientError>;

    /// Send one notification (no response expected).
    /// 发送一个 notification（不需要 response）。
    async fn notify(&mut self, method: &str, params: Value) -> Result<(), McpClientError>;

    /// Close the session and release its process resources.
    /// 关闭 session 并释放其进程资源。
    async fn close(self: Box<Self>);
}

/// Opens and closes MCP sessions; the host owns process lifecycle through it.
///
/// The provider never spawns or supervises processes itself. Production hosts
/// inject an adapter backed by managed execution; the default
/// [`StdioProcessAdapter`] spawns one child per session for tests and local
/// development.
/// 打开和关闭 MCP session；host 通过此接口持有进程生命周期。
/// provider 自身从不启动或监管进程。生产 host 注入基于托管执行的 adapter；默认 StdioProcessAdapter 为测试和本地开发按 session 启动一个子进程。
#[async_trait::async_trait]
pub trait McpSessionAdapter: Send + Sync {
    /// Open one session for a configured server binding.
    /// 为已配置的 server binding 打开一个 session。
    async fn open(&self, server: &McpServerConfig) -> Result<Box<dyn McpSession>, McpClientError>;
}

/// Default adapter: one stdio child process per session (test/dev posture).
/// 默认 adapter：每个 session 使用一个 stdio 子进程（测试/开发配置）。
#[derive(Debug, Default)]
pub struct StdioProcessAdapter;

#[async_trait::async_trait]
impl McpSessionAdapter for StdioProcessAdapter {
    async fn open(&self, server: &McpServerConfig) -> Result<Box<dyn McpSession>, McpClientError> {
        Ok(Box::new(McpStdioClient::start(server).await?))
    }
}

#[async_trait::async_trait]
impl McpSession for McpStdioClient {
    async fn request(
        &mut self,
        method: &str,
        params: Value,
        timeout: Duration,
    ) -> Result<Value, McpClientError> {
        McpStdioClient::request(self, method, params, timeout).await
    }

    async fn notify(&mut self, method: &str, params: Value) -> Result<(), McpClientError> {
        McpStdioClient::notify(self, method, params).await
    }

    async fn close(self: Box<Self>) {
        (*self).shutdown().await;
    }
}

/// One short-lived MCP session speaking newline-delimited JSON-RPC 2.0.
/// 一个使用换行分隔 JSON-RPC 2.0 的短生命周期 MCP session。
pub struct McpStdioClient {
    child: Child,
    stdin: ChildStdin,
    lines: Lines<BufReader<ChildStdout>>,
    stderr_tail: Arc<Mutex<VecDeque<String>>>,
    next_id: u64,
}

impl McpStdioClient {
    /// Start the configured server and complete the MCP initialize handshake.
    /// 启动已配置的 server 并完成 MCP initialize 握手。
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
    /// 执行 MCP initialize 握手，然后发送 initialized notification。
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
    /// 发送一个 request 并等待匹配的 response。
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
                // 尽力取消已放弃的 request。
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
    /// 发送一个 notification（不需要 response）。
    pub async fn notify(&mut self, method: &str, params: Value) -> Result<(), McpClientError> {
        let message = json!({ "jsonrpc": "2.0", "method": method, "params": params });
        self.write_message(&message).await
    }

    /// Close stdin and wait briefly, then kill the child if it lingers.
    /// 关闭 stdin 并短暂等待；如果子进程仍未退出，则终止它。
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
                // 这是 Notification；v1 不处理 server notification。
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
                // 这是 server 发起的 request；v1 以 method-not-found 响应。
                self.respond_unsupported(message_id.clone()).await?;
            }
            // Responses to other request ids are not expected in v1; ignore.
            // v1 不预期收到其他 request ID 的 response；忽略它们。
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
