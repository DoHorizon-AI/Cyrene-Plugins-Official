// SPDX-License-Identifier: Apache-2.0
//! DirectPluginRuntime gRPC Service Implementation (R08, R09)
//!
//! Provides the canonical gRPC host for Rust native capabilities:
//! - agent.runtime.v1
//! - memory.provider.v1
//! - computer.runtime.v1
//! - tool.provider.v1 (fixed Computer Runtime facade plus optional MCP bindings)
//!
//! Enforces strict fail-closed protection against:
//! - Mismatched interface versions (must be "1")
//! - Unknown capabilities and methods
//! - Invalid or spoofed Type URLs
//! - Corrupted or unparseable Protobuf payloads
//!
//! Dispatch accepts the canonical contract method spelling first (`run`,
//! `list_dir`, ...) and keeps the legacy PascalCase ids (`Run`, `ListDir`, ...)
//! as interface-version 1 compatibility aliases; see `canonical_method`.

use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Request, Response, Status};

use prost::Message;

use cyrene_agent_runtime::adapter::tool_catalog::ToolCatalogSource;
use cyrene_agent_runtime::engine::turn_loop::CyreneNativeAgentLoop;
use cyrene_agent_runtime::tck::{MockModelProvider, MockToolProvider};
use cyrene_agent_runtime::CancellationToken;
use cyrene_computer_runtime::{
    ComputerExecutionBackend, ComputerRuntimeLimits, ComputerRuntimeService,
};
use cyrene_mcp_provider::{McpServerConfig, McpToolProvider};
use cyrene_memory_runtime::backend::sqlite::SqliteMemoryBackend;
use cyrene_memory_runtime::embedding::ContractModelEmbeddingClient;
use cyrene_memory_runtime::CyreneMemoryService;

const COMPUTER_STREAM_TERMINAL_GRACE: Duration = Duration::from_millis(100);
const COMPUTER_STREAM_CHANNEL_CAPACITY: usize = 8;

use cyrene_plugin_contracts::agent_runtime_v1::{
    agent_run_response, AgentRunRequest, AgentRunResponse, AgentStreamEvent,
};
use cyrene_plugin_contracts::computer_runtime_v1::{
    CommandExecutionRequest, CommandStreamEvent, ComputerError, ComputerErrorCode,
    CreateArtifactRequest, GetArtifactRequest, ListDirRequest, ReadFileRequest, WriteFileRequest,
};
use cyrene_plugin_contracts::memory_provider_v1::{
    DeleteMemoryRequest, GetMemoryRequest, RecallMemoryRequest, StoreMemoryRequest,
};
use cyrene_plugin_contracts::tool_provider_v1::{
    call_tool_response, list_tools_response, CallToolRequest, CallToolResponse, ListToolsRequest,
    ListToolsResponse, ToolCatalog, ToolProviderError, ToolProviderErrorCode,
};

#[path = "computer_tools.rs"]
mod computer_tools;
use self::computer_tools::{ComputerToolProvider, COMPUTER_BINDING_ID};

pub const PLUGIN_ID: &str = "cyrene.tools.computer-runtime";

use crate::proto::direct_invocation_error::Code;
use crate::proto::direct_invocation_response::Result as InvocationResult;
use crate::proto::direct_plugin_runtime_server::DirectPluginRuntime;
use crate::proto::direct_stream_item::Event;
use crate::proto::{
    health_response, DirectInvocationError, DirectInvocationRequest, DirectInvocationResponse,
    DirectPayload, DirectStreamEnd, DirectStreamItem, HealthRequest, HealthResponse,
};

/// Maps legacy PascalCase dispatch ids to their canonical contract method
/// names. New consumers and the capability catalog use the canonical spelling;
/// the aliases stay dispatchable for the lifetime of interface version 1 and
/// are only removable through the breaking-change process in
/// `contracts/VERSIONING.md` (W6-3).
fn canonical_method<'a>(capability: &str, method: &'a str) -> &'a str {
    match (capability, method) {
        ("agent.runtime.v1", "Run") => "run",
        ("agent.runtime.v1", "RunStream") => "run_stream",
        ("memory.provider.v1", "StoreMemory") => "store",
        ("memory.provider.v1", "GetMemory") => "get",
        ("memory.provider.v1", "RecallMemory") => "recall",
        ("memory.provider.v1", "DeleteMemory") => "delete",
        ("computer.runtime.v1", "ExecuteCommand") => "execute_command",
        ("computer.runtime.v1", "ExecuteCommandStream") => "execute_command_stream",
        ("computer.runtime.v1", "ReadFile") => "read_file",
        ("computer.runtime.v1", "WriteFile") => "write_file",
        ("computer.runtime.v1", "ListDir") => "list_dir",
        ("computer.runtime.v1", "CreateArtifact") => "create_artifact",
        ("computer.runtime.v1", "GetArtifact") => "get_artifact",
        _ => method,
    }
}

pub struct DirectPluginRuntimeServiceImpl {
    memory: Option<Arc<CyreneMemoryService>>,
    computer: Arc<ComputerRuntimeService>,
    computer_tools: Arc<ComputerToolProvider>,
    mcp: Option<Arc<McpToolProvider>>,
    simulated_agent_dependencies: bool,
    command_default_timeout_ms: u64,
    command_max_timeout_ms: u64,
}

/// Host-side `ToolCatalogSource` over the configured MCP tool provider.
/// 基于已配置 MCP 工具提供方的主机侧 `ToolCatalogSource`。
///
/// This is the agent-runtime host path: the package assembles the runtime
/// with a provider that actually speaks `tool.provider.v1`.
/// 这是 agent-runtime 的主机路径：软件包会使用真正实现 `tool.provider.v1` 的提供方组装运行时。
pub struct McpCatalogSource {
    provider: Arc<McpToolProvider>,
}

impl McpCatalogSource {
    /// Wrap one configured MCP tool provider.
    /// 包装一个已配置的 MCP 工具提供方。
    pub fn new(provider: Arc<McpToolProvider>) -> Self {
        Self { provider }
    }
}

#[async_trait::async_trait]
impl ToolCatalogSource for McpCatalogSource {
    async fn list_tools(
        &self,
        binding_id: Option<&str>,
    ) -> cyrene_plugin_contracts::tool_provider_v1::ListToolsResponse {
        self.provider.list_tools(binding_id).await
    }

    async fn call_tool(
        &self,
        request: &CallToolRequest,
    ) -> cyrene_plugin_contracts::tool_provider_v1::CallToolResponse {
        self.provider.call_tool(request).await
    }
}

impl Default for DirectPluginRuntimeServiceImpl {
    fn default() -> Self {
        Self::new()
    }
}

impl DirectPluginRuntimeServiceImpl {
    /// Builds the production host without simulated model, tool, or memory dependencies.
    ///
    /// Agent and Memory calls fail closed until the package launcher injects real
    /// capability bindings and a production memory backend. Computer operations
    /// remain available because they have no simulated upstream dependency.
    /// 在软件包启动器注入真实能力绑定和生产级记忆后端之前，Agent 和 Memory 调用会失败关闭。
    /// Computer 操作没有模拟上游依赖，因此仍可使用。
    pub fn new() -> Self {
        let current_dir = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));
        Self::with_runtime(
            Arc::new(ComputerRuntimeService::new(vec![current_dir])),
            ComputerRuntimeLimits::default(),
            None,
            false,
            None,
        )
    }

    /// Loads the isolated runner roots and limits from deployment configuration.
    /// 从部署配置加载隔离运行器的目录与资源限制。
    #[allow(dead_code)] // Integration tests embed this module without the package entrypoint.
    pub fn from_environment() -> Result<Self, String> {
        let workspace_root = required_directory("CYRENE_COMPUTER_WORKSPACE_ROOT")?;
        let artifact_root = required_directory("CYRENE_COMPUTER_ARTIFACT_ROOT")?;
        if workspace_root.starts_with(&artifact_root) || artifact_root.starts_with(&workspace_root)
        {
            return Err("workspace and artifact roots must be separate directories".into());
        }

        let defaults = ComputerRuntimeLimits::default();
        let limits = ComputerRuntimeLimits {
            command_default_timeout_ms: read_u64(
                "CYRENE_COMPUTER_COMMAND_DEFAULT_TIMEOUT_MS",
                defaults.command_default_timeout_ms,
            )?,
            command_max_timeout_ms: read_u64(
                "CYRENE_COMPUTER_COMMAND_MAX_TIMEOUT_MS",
                defaults.command_max_timeout_ms,
            )?,
            max_output_bytes: read_usize(
                "CYRENE_COMPUTER_MAX_OUTPUT_BYTES",
                defaults.max_output_bytes,
            )?,
            max_file_bytes: read_usize("CYRENE_COMPUTER_MAX_FILE_BYTES", defaults.max_file_bytes)?,
            max_directory_entries: read_usize(
                "CYRENE_COMPUTER_MAX_DIRECTORY_ENTRIES",
                defaults.max_directory_entries,
            )?,
            max_directory_depth: read_i32(
                "CYRENE_COMPUTER_MAX_DIRECTORY_DEPTH",
                defaults.max_directory_depth,
            )?,
            max_artifact_bytes: read_usize(
                "CYRENE_COMPUTER_MAX_ARTIFACT_BYTES",
                defaults.max_artifact_bytes,
            )?,
        };
        validate_limits(&limits)?;
        let backend = ComputerExecutionBackend::parse(
            std::env::var("CYRENE_COMPUTER_EXECUTION_BACKEND")
                .ok()
                .as_deref(),
        )?;
        let helper_path = if backend == ComputerExecutionBackend::MicrosoftMxc {
            let executable = std::env::current_exe()
                .map_err(|error| format!("cannot resolve plugin server executable: {error}"))?;
            let parent = executable
                .parent()
                .ok_or_else(|| "plugin server executable has no parent directory".to_string())?;
            Some(parent.join("cyrene-mxc-exec"))
        } else {
            None
        };
        let computer = Arc::new(ComputerRuntimeService::with_execution_backend(
            vec![workspace_root],
            artifact_root,
            limits.clone(),
            backend,
            helper_path,
        )?);
        if let Some(version) = computer.bubblewrap_version() {
            eprintln!(
                "[CYRENE_COMPUTER_BACKEND] backend=mxc isolation=microsoft-process-namespace mxc-sdk=1.0.1 bubblewrap={version}"
            );
        }
        Ok(Self::with_runtime(computer, limits, None, false, None))
    }

    /// Builds a host that serves `tool.provider.v1` over the configured MCP
    /// server bindings. The package launcher is expected to call this once
    /// bindings are resolved; without it, tool calls fail closed.
    /// 构建通过已配置 MCP server 绑定提供 `tool.provider.v1` 的主机。
    /// 软件包启动器应在解析绑定后调用此方法；否则工具调用会失败关闭。
    pub fn with_mcp_servers(servers: Vec<McpServerConfig>) -> Self {
        let current_dir = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));
        Self::with_runtime(
            Arc::new(ComputerRuntimeService::new(vec![current_dir])),
            ComputerRuntimeLimits::default(),
            Some(Arc::new(McpToolProvider::new(servers))),
            false,
            None,
        )
    }

    /// Builds a deterministic in-memory service exclusively for contract tests.
    /// 构建仅供契约测试使用的确定性内存服务。
    pub fn with_simulated_dependencies_for_tests() -> Self {
        let mem_backend = Arc::new(SqliteMemoryBackend::new_in_memory().expect("sqlite in-memory"));
        let mem_embed = Arc::new(ContractModelEmbeddingClient::new(4));
        let memory = Arc::new(CyreneMemoryService::new(mem_backend, Some(mem_embed), 4));

        let current_dir = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));
        Self::with_runtime(
            Arc::new(ComputerRuntimeService::new(vec![current_dir])),
            ComputerRuntimeLimits::default(),
            None,
            true,
            Some(memory),
        )
    }

    #[cfg(all(test, feature = "mxc", target_os = "linux"))]
    pub fn with_computer_runtime_for_tests(computer: Arc<ComputerRuntimeService>) -> Self {
        Self::with_runtime(
            computer,
            ComputerRuntimeLimits::default(),
            None,
            false,
            None,
        )
    }

    #[cfg(all(test, feature = "mxc", target_os = "linux"))]
    pub fn with_computer_runtime_and_limits_for_tests(
        computer: Arc<ComputerRuntimeService>,
        limits: ComputerRuntimeLimits,
    ) -> Self {
        Self::with_runtime(computer, limits, None, false, None)
    }

    fn with_runtime(
        computer: Arc<ComputerRuntimeService>,
        limits: ComputerRuntimeLimits,
        mcp: Option<Arc<McpToolProvider>>,
        simulated_agent_dependencies: bool,
        memory: Option<Arc<CyreneMemoryService>>,
    ) -> Self {
        let computer_tools = Arc::new(ComputerToolProvider::new(computer.clone(), limits.clone()));
        Self {
            memory,
            computer,
            computer_tools,
            mcp,
            simulated_agent_dependencies,
            command_default_timeout_ms: limits.command_default_timeout_ms,
            command_max_timeout_ms: limits.command_max_timeout_ms,
        }
    }

    fn fail_closed_error(
        code: Code,
        message: impl Into<String>,
        domain_code: impl Into<String>,
    ) -> DirectInvocationResponse {
        DirectInvocationResponse {
            result: Some(InvocationResult::Error(DirectInvocationError {
                code: code as i32,
                message: message.into(),
                retryable: false,
                domain_code: domain_code.into(),
            })),
        }
    }

    fn fail_closed_stream_error(
        code: Code,
        message: impl Into<String>,
        domain_code: impl Into<String>,
    ) -> DirectStreamItem {
        DirectStreamItem {
            event: Some(Event::Error(DirectInvocationError {
                code: code as i32,
                message: message.into(),
                retryable: false,
                domain_code: domain_code.into(),
            })),
        }
    }
}

fn computer_stream_error(error: ComputerError) -> DirectStreamItem {
    let (code, domain_code) = if error.code == ComputerErrorCode::CommandTimeout as i32 {
        (Code::DeadlineExceeded, "COMPUTER_STREAM_TIMEOUT")
    } else {
        (Code::ExecutionFailed, "COMPUTER_STREAM_EXECUTION_ERROR")
    };
    DirectPluginRuntimeServiceImpl::fail_closed_stream_error(code, error.message, domain_code)
}

#[allow(dead_code)]
fn required_directory(name: &str) -> Result<std::path::PathBuf, String> {
    let raw = std::env::var(name).map_err(|_| format!("required setting {name} is missing"))?;
    let configured = std::path::PathBuf::from(raw);
    if !configured.is_absolute() {
        return Err(format!("{name} must be an absolute path"));
    }
    let metadata = std::fs::metadata(&configured)
        .map_err(|error| format!("{name} is unavailable: {error}"))?;
    if !metadata.is_dir() {
        return Err(format!("{name} must name an existing directory"));
    }
    configured
        .canonicalize()
        .map_err(|error| format!("{name} cannot be resolved: {error}"))
}

#[allow(dead_code)]
fn read_u64(name: &str, default: u64) -> Result<u64, String> {
    match std::env::var(name) {
        Ok(raw) => raw
            .parse::<u64>()
            .map_err(|error| format!("{name} must be a positive integer: {error}")),
        Err(std::env::VarError::NotPresent) => Ok(default),
        Err(error) => Err(format!("{name} is unavailable: {error}")),
    }
}

#[allow(dead_code)]
fn read_usize(name: &str, default: usize) -> Result<usize, String> {
    match std::env::var(name) {
        Ok(raw) => raw
            .parse::<usize>()
            .map_err(|error| format!("{name} must be a positive integer: {error}")),
        Err(std::env::VarError::NotPresent) => Ok(default),
        Err(error) => Err(format!("{name} is unavailable: {error}")),
    }
}

#[allow(dead_code)]
fn read_i32(name: &str, default: i32) -> Result<i32, String> {
    match std::env::var(name) {
        Ok(raw) => raw
            .parse::<i32>()
            .map_err(|error| format!("{name} must be a positive integer: {error}")),
        Err(std::env::VarError::NotPresent) => Ok(default),
        Err(error) => Err(format!("{name} is unavailable: {error}")),
    }
}

#[allow(dead_code)]
fn validate_limits(limits: &ComputerRuntimeLimits) -> Result<(), String> {
    if limits.command_default_timeout_ms == 0
        || limits.command_default_timeout_ms > limits.command_max_timeout_ms
        || limits.command_max_timeout_ms > 900_000
    {
        return Err(
            "command timeout settings must satisfy 1 <= default <= maximum <= 900000 ms".into(),
        );
    }
    if limits.max_output_bytes == 0 || limits.max_output_bytes > 64 * 1024 {
        return Err("CYRENE_COMPUTER_MAX_OUTPUT_BYTES must be in [1, 65536]".into());
    }
    if limits.max_file_bytes == 0 || limits.max_file_bytes > 1024 * 1024 {
        return Err("CYRENE_COMPUTER_MAX_FILE_BYTES must be in [1, 1048576]".into());
    }
    if limits.max_directory_entries == 0 || limits.max_directory_entries > 10_000 {
        return Err("CYRENE_COMPUTER_MAX_DIRECTORY_ENTRIES must be in [1, 10000]".into());
    }
    if !(1..=64).contains(&limits.max_directory_depth) {
        return Err("CYRENE_COMPUTER_MAX_DIRECTORY_DEPTH must be in [1, 64]".into());
    }
    if limits.max_artifact_bytes == 0 || limits.max_artifact_bytes > 16 * 1024 * 1024 {
        return Err("CYRENE_COMPUTER_MAX_ARTIFACT_BYTES must be in [1, 16777216]".into());
    }
    Ok(())
}

fn unavailable_tool_catalog(binding_id: &str) -> ListToolsResponse {
    ListToolsResponse {
        result: Some(list_tools_response::Result::Error(ToolProviderError {
            code: ToolProviderErrorCode::InvalidArguments as i32,
            message: format!("binding '{binding_id}' is not configured"),
            retryable: false,
        })),
    }
}

fn combine_tool_catalogs(
    local: ListToolsResponse,
    external: ListToolsResponse,
) -> ListToolsResponse {
    let local_catalog = match local.result {
        Some(list_tools_response::Result::Catalog(catalog)) => catalog,
        Some(list_tools_response::Result::Error(error)) => {
            return ListToolsResponse {
                result: Some(list_tools_response::Result::Error(error)),
            };
        }
        None => {
            return ListToolsResponse {
                result: Some(list_tools_response::Result::Error(ToolProviderError {
                    code: ToolProviderErrorCode::ProviderError as i32,
                    message: "Computer tool provider returned no catalog result".into(),
                    retryable: true,
                })),
            };
        }
    };
    let external_catalog = match external.result {
        Some(list_tools_response::Result::Catalog(catalog)) => catalog,
        Some(list_tools_response::Result::Error(error)) => {
            return ListToolsResponse {
                result: Some(list_tools_response::Result::Error(error)),
            };
        }
        None => {
            return ListToolsResponse {
                result: Some(list_tools_response::Result::Error(ToolProviderError {
                    code: ToolProviderErrorCode::ProviderError as i32,
                    message: "Configured tool provider returned no catalog result".into(),
                    retryable: true,
                })),
            };
        }
    };

    let mut tools = local_catalog.tools;
    tools.extend(external_catalog.tools);
    tools.sort_by(|left, right| {
        (&left.binding_id, &left.provider_tool_id)
            .cmp(&(&right.binding_id, &right.provider_tool_id))
    });
    ListToolsResponse {
        result: Some(list_tools_response::Result::Catalog(ToolCatalog {
            catalog_version: format!(
                "{}+{}",
                local_catalog.catalog_version, external_catalog.catalog_version
            ),
            tools,
        })),
    }
}

#[tonic::async_trait]
impl DirectPluginRuntime for DirectPluginRuntimeServiceImpl {
    async fn health(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        let mut capabilities = vec![
            "computer.runtime.v1".to_string(),
            "tool.provider.v1".to_string(),
        ];
        if self.simulated_agent_dependencies {
            capabilities.splice(
                0..0,
                [
                    "agent.runtime.v1".to_string(),
                    "memory.provider.v1".to_string(),
                ],
            );
        }

        Ok(Response::new(HealthResponse {
            status: health_response::Status::Serving as i32,
            plugin_id: PLUGIN_ID.to_string(),
            plugin_version: "0.1.0".to_string(),
            capabilities,
        }))
    }

    async fn invoke(
        &self,
        request: Request<DirectInvocationRequest>,
    ) -> Result<Response<DirectInvocationResponse>, Status> {
        let req = request.into_inner();

        // 1. Fail-closed on interface version mismatch (R09)
        // 1. 接口版本不匹配时失败关闭（R09）
        if req.interface_version != "1" {
            return Ok(Response::new(Self::fail_closed_error(
                Code::InvalidRequest,
                format!(
                    "Unsupported interface version '{}', expected '1'",
                    req.interface_version
                ),
                "UNSUPPORTED_INTERFACE_VERSION",
            )));
        }

        // 2. Dispatch by capability; method ids are normalized to their
        //    canonical contract spelling first.
        // 2. 按能力分发；先将方法 ID 规范化为契约中的标准拼写。
        let method = canonical_method(req.capability.as_str(), req.method.as_str());
        match req.capability.as_str() {
            "agent.runtime.v1" => match method {
                "run" => {
                    const EXPECTED_URL: &str =
                        "type.cyrene.io/cyrene.agent.runtime.v1.AgentRunRequest";
                    if req.payload_type_url != EXPECTED_URL {
                        return Ok(Response::new(Self::fail_closed_error(
                            Code::InvalidRequest,
                            format!(
                                "Invalid payload_type_url '{}', expected '{}'",
                                req.payload_type_url, EXPECTED_URL
                            ),
                            "INVALID_TYPE_URL",
                        )));
                    }

                    let run_req = match AgentRunRequest::decode(&req.payload[..]) {
                        Ok(r) => r,
                        Err(e) => {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!("Failed to decode AgentRunRequest: {}", e),
                                "DECODE_ERROR",
                            )));
                        }
                    };

                    if !self.simulated_agent_dependencies {
                        return Ok(Response::new(Self::fail_closed_error(
                            Code::Unavailable,
                            "Agent Runtime requires injected model and tool capability bindings",
                            "DEPENDENCY_BINDINGS_UNAVAILABLE",
                        )));
                    }

                    let loop_engine = CyreneNativeAgentLoop::new();
                    let model = MockModelProvider::simple_text();
                    let tools = MockToolProvider::new();
                    let cancel = CancellationToken::new();

                    let run_resp = match loop_engine
                        .execute_run(run_req, &model, Some(&tools), cancel)
                        .await
                    {
                        Ok(resp) => resp,
                        Err(err) => AgentRunResponse {
                            result: Some(agent_run_response::Result::Error(err)),
                        },
                    };

                    let mut buf = Vec::new();
                    if let Err(e) = run_resp.encode(&mut buf) {
                        return Ok(Response::new(Self::fail_closed_error(
                            Code::ExecutionFailed,
                            format!("Failed to encode AgentRunResponse: {}", e),
                            "ENCODE_ERROR",
                        )));
                    }

                    Ok(Response::new(DirectInvocationResponse {
                        result: Some(InvocationResult::Payload(DirectPayload {
                            type_url: "type.cyrene.io/cyrene.agent.runtime.v1.AgentRunResponse"
                                .into(),
                            value: buf,
                            event_type: String::new(),
                        })),
                    }))
                }
                _ => Ok(Response::new(Self::fail_closed_error(
                    Code::MethodNotFound,
                    format!(
                        "Method '{}' not found in capability 'agent.runtime.v1'",
                        req.method
                    ),
                    "METHOD_NOT_FOUND",
                ))),
            },

            "memory.provider.v1" => {
                let Some(memory) = self.memory.as_ref() else {
                    return Ok(Response::new(Self::fail_closed_error(
                        Code::Unavailable,
                        "Memory provider requires an explicitly configured production backend",
                        "MEMORY_BACKEND_UNAVAILABLE",
                    )));
                };

                match method {
                    "store" => {
                        const EXPECTED_URL: &str =
                            "type.cyrene.io/cyrene.memory.provider.v1.StoreMemoryRequest";
                        if req.payload_type_url != EXPECTED_URL {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!(
                                    "Invalid payload_type_url '{}', expected '{}'",
                                    req.payload_type_url, EXPECTED_URL
                                ),
                                "INVALID_TYPE_URL",
                            )));
                        }
                        let store_req = match StoreMemoryRequest::decode(&req.payload[..]) {
                            Ok(r) => r,
                            Err(e) => {
                                return Ok(Response::new(Self::fail_closed_error(
                                    Code::InvalidRequest,
                                    format!("Failed to decode StoreMemoryRequest: {}", e),
                                    "DECODE_ERROR",
                                )));
                            }
                        };
                        let resp = memory.store(store_req).await;
                        let mut buf = Vec::new();
                        resp.encode(&mut buf).unwrap();
                        Ok(Response::new(DirectInvocationResponse {
                            result: Some(InvocationResult::Payload(DirectPayload {
                                type_url:
                                    "type.cyrene.io/cyrene.memory.provider.v1.StoreMemoryResponse"
                                        .into(),
                                value: buf,
                                event_type: String::new(),
                            })),
                        }))
                    }
                    "get" => {
                        const EXPECTED_URL: &str =
                            "type.cyrene.io/cyrene.memory.provider.v1.GetMemoryRequest";
                        if req.payload_type_url != EXPECTED_URL {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!(
                                    "Invalid payload_type_url '{}', expected '{}'",
                                    req.payload_type_url, EXPECTED_URL
                                ),
                                "INVALID_TYPE_URL",
                            )));
                        }
                        let get_req = match GetMemoryRequest::decode(&req.payload[..]) {
                            Ok(r) => r,
                            Err(e) => {
                                return Ok(Response::new(Self::fail_closed_error(
                                    Code::InvalidRequest,
                                    format!("Failed to decode GetMemoryRequest: {}", e),
                                    "DECODE_ERROR",
                                )));
                            }
                        };
                        let resp = memory.get(get_req).await;
                        let mut buf = Vec::new();
                        resp.encode(&mut buf).unwrap();
                        Ok(Response::new(DirectInvocationResponse {
                            result: Some(InvocationResult::Payload(DirectPayload {
                                type_url:
                                    "type.cyrene.io/cyrene.memory.provider.v1.GetMemoryResponse"
                                        .into(),
                                value: buf,
                                event_type: String::new(),
                            })),
                        }))
                    }
                    "recall" => {
                        const EXPECTED_URL: &str =
                            "type.cyrene.io/cyrene.memory.provider.v1.RecallMemoryRequest";
                        if req.payload_type_url != EXPECTED_URL {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!(
                                    "Invalid payload_type_url '{}', expected '{}'",
                                    req.payload_type_url, EXPECTED_URL
                                ),
                                "INVALID_TYPE_URL",
                            )));
                        }
                        let recall_req = match RecallMemoryRequest::decode(&req.payload[..]) {
                            Ok(r) => r,
                            Err(e) => {
                                return Ok(Response::new(Self::fail_closed_error(
                                    Code::InvalidRequest,
                                    format!("Failed to decode RecallMemoryRequest: {}", e),
                                    "DECODE_ERROR",
                                )));
                            }
                        };
                        let resp = memory.recall(recall_req).await;
                        let mut buf = Vec::new();
                        resp.encode(&mut buf).unwrap();
                        Ok(Response::new(DirectInvocationResponse {
                            result: Some(InvocationResult::Payload(DirectPayload {
                                type_url:
                                    "type.cyrene.io/cyrene.memory.provider.v1.RecallMemoryResponse"
                                        .into(),
                                value: buf,
                                event_type: String::new(),
                            })),
                        }))
                    }
                    "delete" => {
                        const EXPECTED_URL: &str =
                            "type.cyrene.io/cyrene.memory.provider.v1.DeleteMemoryRequest";
                        if req.payload_type_url != EXPECTED_URL {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!(
                                    "Invalid payload_type_url '{}', expected '{}'",
                                    req.payload_type_url, EXPECTED_URL
                                ),
                                "INVALID_TYPE_URL",
                            )));
                        }
                        let del_req = match DeleteMemoryRequest::decode(&req.payload[..]) {
                            Ok(r) => r,
                            Err(e) => {
                                return Ok(Response::new(Self::fail_closed_error(
                                    Code::InvalidRequest,
                                    format!("Failed to decode DeleteMemoryRequest: {}", e),
                                    "DECODE_ERROR",
                                )));
                            }
                        };
                        let resp = memory.delete(del_req).await;
                        let mut buf = Vec::new();
                        resp.encode(&mut buf).unwrap();
                        Ok(Response::new(DirectInvocationResponse {
                            result: Some(InvocationResult::Payload(DirectPayload {
                                type_url:
                                    "type.cyrene.io/cyrene.memory.provider.v1.DeleteMemoryResponse"
                                        .into(),
                                value: buf,
                                event_type: String::new(),
                            })),
                        }))
                    }
                    _ => Ok(Response::new(Self::fail_closed_error(
                        Code::MethodNotFound,
                        format!(
                            "Method '{}' not found in capability 'memory.provider.v1'",
                            req.method
                        ),
                        "METHOD_NOT_FOUND",
                    ))),
                }
            }

            "computer.runtime.v1" => match method {
                "execute_command" => {
                    const EXPECTED_URL: &str =
                        "type.cyrene.io/cyrene.computer.runtime.v1.CommandExecutionRequest";
                    if req.payload_type_url != EXPECTED_URL {
                        return Ok(Response::new(Self::fail_closed_error(
                            Code::InvalidRequest,
                            format!(
                                "Invalid payload_type_url '{}', expected '{}'",
                                req.payload_type_url, EXPECTED_URL
                            ),
                            "INVALID_TYPE_URL",
                        )));
                    }
                    let cmd_req = match CommandExecutionRequest::decode(&req.payload[..]) {
                        Ok(r) => r,
                        Err(e) => {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!("Failed to decode CommandExecutionRequest: {}", e),
                                "DECODE_ERROR",
                            )));
                        }
                    };
                    let resp = self.computer.execute_command(cmd_req, None).await;
                    let mut buf = Vec::new();
                    resp.encode(&mut buf).unwrap();
                    Ok(Response::new(DirectInvocationResponse {
                        result: Some(InvocationResult::Payload(DirectPayload {
                            type_url:
                                "type.cyrene.io/cyrene.computer.runtime.v1.CommandExecutionResponse"
                                    .into(),
                            value: buf,
                            event_type: String::new(),
                        })),
                    }))
                }
                "read_file" => {
                    const EXPECTED_URL: &str =
                        "type.cyrene.io/cyrene.computer.runtime.v1.ReadFileRequest";
                    if req.payload_type_url != EXPECTED_URL {
                        return Ok(Response::new(Self::fail_closed_error(
                            Code::InvalidRequest,
                            format!(
                                "Invalid payload_type_url '{}', expected '{}'",
                                req.payload_type_url, EXPECTED_URL
                            ),
                            "INVALID_TYPE_URL",
                        )));
                    }
                    let rf_req = match ReadFileRequest::decode(&req.payload[..]) {
                        Ok(r) => r,
                        Err(e) => {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!("Failed to decode ReadFileRequest: {}", e),
                                "DECODE_ERROR",
                            )));
                        }
                    };
                    let resp = self.computer.read_file(rf_req);
                    let mut buf = Vec::new();
                    resp.encode(&mut buf).unwrap();
                    Ok(Response::new(DirectInvocationResponse {
                        result: Some(InvocationResult::Payload(DirectPayload {
                            type_url: "type.cyrene.io/cyrene.computer.runtime.v1.ReadFileResponse"
                                .into(),
                            value: buf,
                            event_type: String::new(),
                        })),
                    }))
                }
                "write_file" => {
                    const EXPECTED_URL: &str =
                        "type.cyrene.io/cyrene.computer.runtime.v1.WriteFileRequest";
                    if req.payload_type_url != EXPECTED_URL {
                        return Ok(Response::new(Self::fail_closed_error(
                            Code::InvalidRequest,
                            format!(
                                "Invalid payload_type_url '{}', expected '{}'",
                                req.payload_type_url, EXPECTED_URL
                            ),
                            "INVALID_TYPE_URL",
                        )));
                    }
                    let wf_req = match WriteFileRequest::decode(&req.payload[..]) {
                        Ok(r) => r,
                        Err(e) => {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!("Failed to decode WriteFileRequest: {}", e),
                                "DECODE_ERROR",
                            )));
                        }
                    };
                    let resp = self.computer.write_file(wf_req);
                    let mut buf = Vec::new();
                    resp.encode(&mut buf).unwrap();
                    Ok(Response::new(DirectInvocationResponse {
                        result: Some(InvocationResult::Payload(DirectPayload {
                            type_url: "type.cyrene.io/cyrene.computer.runtime.v1.WriteFileResponse"
                                .into(),
                            value: buf,
                            event_type: String::new(),
                        })),
                    }))
                }
                "list_dir" => {
                    const EXPECTED_URL: &str =
                        "type.cyrene.io/cyrene.computer.runtime.v1.ListDirRequest";
                    if req.payload_type_url != EXPECTED_URL {
                        return Ok(Response::new(Self::fail_closed_error(
                            Code::InvalidRequest,
                            format!(
                                "Invalid payload_type_url '{}', expected '{}'",
                                req.payload_type_url, EXPECTED_URL
                            ),
                            "INVALID_TYPE_URL",
                        )));
                    }
                    let ld_req = match ListDirRequest::decode(&req.payload[..]) {
                        Ok(r) => r,
                        Err(e) => {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!("Failed to decode ListDirRequest: {}", e),
                                "DECODE_ERROR",
                            )));
                        }
                    };
                    let resp = self.computer.list_dir(ld_req);
                    let mut buf = Vec::new();
                    resp.encode(&mut buf).unwrap();
                    Ok(Response::new(DirectInvocationResponse {
                        result: Some(InvocationResult::Payload(DirectPayload {
                            type_url: "type.cyrene.io/cyrene.computer.runtime.v1.ListDirResponse"
                                .into(),
                            value: buf,
                            event_type: String::new(),
                        })),
                    }))
                }
                "create_artifact" => {
                    const EXPECTED_URL: &str =
                        "type.cyrene.io/cyrene.computer.runtime.v1.CreateArtifactRequest";
                    if req.payload_type_url != EXPECTED_URL {
                        return Ok(Response::new(Self::fail_closed_error(
                            Code::InvalidRequest,
                            format!(
                                "Invalid payload_type_url '{}', expected '{}'",
                                req.payload_type_url, EXPECTED_URL
                            ),
                            "INVALID_TYPE_URL",
                        )));
                    }
                    let ca_req = match CreateArtifactRequest::decode(&req.payload[..]) {
                        Ok(r) => r,
                        Err(e) => {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!("Failed to decode CreateArtifactRequest: {}", e),
                                "DECODE_ERROR",
                            )));
                        }
                    };
                    let resp = self.computer.create_artifact(ca_req);
                    let mut buf = Vec::new();
                    resp.encode(&mut buf).unwrap();
                    Ok(Response::new(DirectInvocationResponse {
                        result: Some(InvocationResult::Payload(DirectPayload {
                            type_url:
                                "type.cyrene.io/cyrene.computer.runtime.v1.CreateArtifactResponse"
                                    .into(),
                            value: buf,
                            event_type: String::new(),
                        })),
                    }))
                }
                "get_artifact" => {
                    const EXPECTED_URL: &str =
                        "type.cyrene.io/cyrene.computer.runtime.v1.GetArtifactRequest";
                    if req.payload_type_url != EXPECTED_URL {
                        return Ok(Response::new(Self::fail_closed_error(
                            Code::InvalidRequest,
                            format!(
                                "Invalid payload_type_url '{}', expected '{}'",
                                req.payload_type_url, EXPECTED_URL
                            ),
                            "INVALID_TYPE_URL",
                        )));
                    }
                    let ga_req = match GetArtifactRequest::decode(&req.payload[..]) {
                        Ok(r) => r,
                        Err(e) => {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!("Failed to decode GetArtifactRequest: {}", e),
                                "DECODE_ERROR",
                            )));
                        }
                    };
                    let resp = self.computer.get_artifact(ga_req);
                    let mut buf = Vec::new();
                    resp.encode(&mut buf).unwrap();
                    Ok(Response::new(DirectInvocationResponse {
                        result: Some(InvocationResult::Payload(DirectPayload {
                            type_url:
                                "type.cyrene.io/cyrene.computer.runtime.v1.GetArtifactResponse"
                                    .into(),
                            value: buf,
                            event_type: String::new(),
                        })),
                    }))
                }
                _ => Ok(Response::new(Self::fail_closed_error(
                    Code::MethodNotFound,
                    format!(
                        "Method '{}' not found in capability 'computer.runtime.v1'",
                        req.method
                    ),
                    "METHOD_NOT_FOUND",
                ))),
            },

            "tool.provider.v1" => match method {
                "list_tools" => {
                    const EXPECTED_URL: &str =
                        "type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest";
                    if req.payload_type_url != EXPECTED_URL {
                        return Ok(Response::new(Self::fail_closed_error(
                            Code::InvalidRequest,
                            format!(
                                "Invalid payload_type_url '{}', expected '{}'",
                                req.payload_type_url, EXPECTED_URL
                            ),
                            "INVALID_TYPE_URL",
                        )));
                    }
                    let list_req = match ListToolsRequest::decode(&req.payload[..]) {
                        Ok(r) => r,
                        Err(e) => {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!("Failed to decode ListToolsRequest: {}", e),
                                "DECODE_ERROR",
                            )));
                        }
                    };
                    let response = match list_req.binding_id.as_deref() {
                        Some(binding_id) if binding_id == COMPUTER_BINDING_ID => {
                            self.computer_tools.list_tools(Some(binding_id))
                        }
                        Some(binding_id) => match self.mcp.as_ref() {
                            Some(mcp) if mcp.is_configured() => {
                                mcp.list_tools(Some(binding_id)).await
                            }
                            _ => unavailable_tool_catalog(binding_id),
                        },
                        None => {
                            let local = self.computer_tools.list_tools(None);
                            match self.mcp.as_ref() {
                                Some(mcp) if mcp.is_configured() => {
                                    let external = mcp.list_tools(None).await;
                                    combine_tool_catalogs(local, external)
                                }
                                _ => local,
                            }
                        }
                    };
                    Ok(Response::new(DirectInvocationResponse {
                        result: Some(InvocationResult::Payload(DirectPayload {
                            type_url: "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse"
                                .into(),
                            value: response.encode_to_vec(),
                            event_type: String::new(),
                        })),
                    }))
                }
                "call_tool" => {
                    const EXPECTED_URL: &str =
                        "type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest";
                    if req.payload_type_url != EXPECTED_URL {
                        return Ok(Response::new(Self::fail_closed_error(
                            Code::InvalidRequest,
                            format!(
                                "Invalid payload_type_url '{}', expected '{}'",
                                req.payload_type_url, EXPECTED_URL
                            ),
                            "INVALID_TYPE_URL",
                        )));
                    }
                    let call_req = match CallToolRequest::decode(&req.payload[..]) {
                        Ok(r) => r,
                        Err(e) => {
                            return Ok(Response::new(Self::fail_closed_error(
                                Code::InvalidRequest,
                                format!("Failed to decode CallToolRequest: {}", e),
                                "DECODE_ERROR",
                            )));
                        }
                    };
                    let response = if call_req.binding_id == COMPUTER_BINDING_ID {
                        self.computer_tools.call_tool(&call_req).await
                    } else if let Some(mcp) = self.mcp.as_ref().filter(|mcp| mcp.is_configured()) {
                        mcp.call_tool(&call_req).await
                    } else {
                        CallToolResponse {
                            result: Some(call_tool_response::Result::Error(ToolProviderError {
                                code: ToolProviderErrorCode::InvalidArguments as i32,
                                message: format!(
                                    "binding '{}' is not configured",
                                    call_req.binding_id
                                ),
                                retryable: false,
                            })),
                        }
                    };
                    Ok(Response::new(DirectInvocationResponse {
                        result: Some(InvocationResult::Payload(DirectPayload {
                            type_url: "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse"
                                .into(),
                            value: response.encode_to_vec(),
                            event_type: String::new(),
                        })),
                    }))
                }
                _ => Ok(Response::new(Self::fail_closed_error(
                    Code::MethodNotFound,
                    format!(
                        "Method '{}' not found in capability 'tool.provider.v1'",
                        req.method
                    ),
                    "METHOD_NOT_FOUND",
                ))),
            },

            unknown => Ok(Response::new(Self::fail_closed_error(
                Code::MethodNotFound,
                format!("Capability '{}' not supported or unknown", unknown),
                "CAPABILITY_NOT_FOUND",
            ))),
        }
    }

    type InvokeStreamStream = Pin<
        Box<dyn tokio_stream::Stream<Item = Result<DirectStreamItem, Status>> + Send + 'static>,
    >;

    async fn invoke_stream(
        &self,
        request: Request<DirectInvocationRequest>,
    ) -> Result<Response<Self::InvokeStreamStream>, Status> {
        let req = request.into_inner();

        let stream_capacity = if req.capability == "computer.runtime.v1"
            && canonical_method(&req.capability, &req.method) == "execute_command_stream"
        {
            COMPUTER_STREAM_CHANNEL_CAPACITY
        } else {
            64
        };
        let (tx, rx) = mpsc::channel(stream_capacity);

        if req.interface_version != "1" {
            let _ = tx
                .send(Ok(Self::fail_closed_stream_error(
                    Code::InvalidRequest,
                    format!(
                        "Unsupported interface version '{}', expected '1'",
                        req.interface_version
                    ),
                    "UNSUPPORTED_INTERFACE_VERSION",
                )))
                .await;
            return Ok(Response::new(Box::pin(ReceiverStream::new(rx))));
        }

        let method = canonical_method(req.capability.as_str(), req.method.as_str());
        match (req.capability.as_str(), method) {
            ("agent.runtime.v1", "run_stream") => {
                const EXPECTED_URL: &str = "type.cyrene.io/cyrene.agent.runtime.v1.AgentRunRequest";
                if req.payload_type_url != EXPECTED_URL {
                    let _ = tx
                        .send(Ok(Self::fail_closed_stream_error(
                            Code::InvalidRequest,
                            format!(
                                "Invalid payload_type_url '{}', expected '{}'",
                                req.payload_type_url, EXPECTED_URL
                            ),
                            "INVALID_TYPE_URL",
                        )))
                        .await;
                    return Ok(Response::new(Box::pin(ReceiverStream::new(rx))));
                }

                let run_req = match AgentRunRequest::decode(&req.payload[..]) {
                    Ok(r) => r,
                    Err(e) => {
                        let _ = tx
                            .send(Ok(Self::fail_closed_stream_error(
                                Code::InvalidRequest,
                                format!("Failed to decode AgentRunRequest: {}", e),
                                "DECODE_ERROR",
                            )))
                            .await;
                        return Ok(Response::new(Box::pin(ReceiverStream::new(rx))));
                    }
                };

                if !self.simulated_agent_dependencies {
                    let _ = tx
                        .send(Ok(Self::fail_closed_stream_error(
                            Code::Unavailable,
                            "Agent Runtime requires injected model and tool capability bindings",
                            "DEPENDENCY_BINDINGS_UNAVAILABLE",
                        )))
                        .await;
                    return Ok(Response::new(Box::pin(ReceiverStream::new(rx))));
                }

                tokio::spawn(async move {
                    let loop_engine = CyreneNativeAgentLoop::new();
                    let model = MockModelProvider::simple_text();
                    let tools = MockToolProvider::new();
                    let cancel = CancellationToken::new();

                    let (event_tx, mut event_rx) = mpsc::channel::<AgentStreamEvent>(64);

                    tokio::spawn(async move {
                        let _ = loop_engine
                            .execute_stream(run_req, &model, Some(&tools), cancel, event_tx)
                            .await;
                    });

                    while let Some(event) = event_rx.recv().await {
                        let mut buf = Vec::new();
                        event.encode(&mut buf).unwrap();
                        let item = DirectStreamItem {
                            event: Some(Event::Payload(DirectPayload {
                                type_url: "type.cyrene.io/cyrene.agent.runtime.v1.AgentStreamEvent"
                                    .into(),
                                value: buf,
                                event_type: String::new(),
                            })),
                        };
                        if tx.send(Ok(item)).await.is_err() {
                            break;
                        }
                    }

                    let _ = tx
                        .send(Ok(DirectStreamItem {
                            event: Some(Event::End(DirectStreamEnd {})),
                        }))
                        .await;
                });

                Ok(Response::new(Box::pin(ReceiverStream::new(rx))))
            }

            ("computer.runtime.v1", "execute_command_stream") => {
                const EXPECTED_URL: &str =
                    "type.cyrene.io/cyrene.computer.runtime.v1.CommandExecutionRequest";
                if req.payload_type_url != EXPECTED_URL {
                    let _ = tx
                        .send(Ok(Self::fail_closed_stream_error(
                            Code::InvalidRequest,
                            format!(
                                "Invalid payload_type_url '{}', expected '{}'",
                                req.payload_type_url, EXPECTED_URL
                            ),
                            "INVALID_TYPE_URL",
                        )))
                        .await;
                    return Ok(Response::new(Box::pin(ReceiverStream::new(rx))));
                }

                let cmd_req = match CommandExecutionRequest::decode(&req.payload[..]) {
                    Ok(r) => r,
                    Err(e) => {
                        let _ = tx
                            .send(Ok(Self::fail_closed_stream_error(
                                Code::InvalidRequest,
                                format!("Failed to decode CommandExecutionRequest: {}", e),
                                "DECODE_ERROR",
                            )))
                            .await;
                        return Ok(Response::new(Box::pin(ReceiverStream::new(rx))));
                    }
                };

                let computer = self.computer.clone();
                let default_timeout_ms = self.command_default_timeout_ms;
                let maximum_timeout_ms = self.command_max_timeout_ms;
                tokio::spawn(async move {
                    // Keep two queue positions reserved for the terminal
                    // Error (when needed) and End records. Payload forwarding
                    // can fill the rest of the client queue, but cannot evict
                    // or starve the terminal outcome.
                    let error_permit = match tx.clone().reserve_owned().await {
                        Ok(permit) => permit,
                        Err(_) => return,
                    };
                    let end_permit = match tx.clone().reserve_owned().await {
                        Ok(permit) => permit,
                        Err(_) => return,
                    };
                    let (event_tx, mut event_rx) = mpsc::channel::<CommandStreamEvent>(64);
                    let cancel_flag = Arc::new(AtomicBool::new(false));
                    let timeout_ms = match cmd_req.timeout_ms {
                        Some(value) if value <= 0 => default_timeout_ms,
                        Some(value) => (value as u64).min(maximum_timeout_ms),
                        None => default_timeout_ms,
                    };
                    // The runtime enforces the command timeout. This bounded
                    // relay margin lets the host deliver its terminal error
                    // without extending workload execution.
                    let deadline = tokio::time::Instant::now()
                        + Duration::from_millis(timeout_ms.max(1))
                        + COMPUTER_STREAM_TERMINAL_GRACE;

                    let comp = computer.clone();
                    let command_cancel = Arc::clone(&cancel_flag);
                    let mut command_task = tokio::spawn(async move {
                        comp.execute_command_stream(cmd_req, Some(command_cancel), event_tx)
                            .await
                    });
                    let mut client_cancelled = false;
                    let mut stream_error_sent = false;
                    let mut relay_deadline_expired = false;

                    loop {
                        let event = tokio::select! {
                            event = event_rx.recv() => event,
                            _ = tokio::time::sleep_until(deadline) => {
                                relay_deadline_expired = true;
                                break;
                            }
                            _ = tx.closed() => {
                                cancel_flag.store(true, Ordering::SeqCst);
                                client_cancelled = true;
                                break;
                            }
                        };
                        let Some(event) = event else {
                            break;
                        };
                        if matches!(
                            event.event,
                            Some(cyrene_plugin_contracts::computer_runtime_v1::command_stream_event::Event::StreamError(_))
                        ) {
                            stream_error_sent = true;
                        }
                        let mut buf = Vec::new();
                        event.encode(&mut buf).unwrap();
                        let item = DirectStreamItem {
                            event: Some(Event::Payload(DirectPayload {
                                type_url:
                                    "type.cyrene.io/cyrene.computer.runtime.v1.CommandStreamEvent"
                                        .into(),
                                value: buf,
                                event_type: String::new(),
                            })),
                        };
                        let sent = tokio::select! {
                            result = tokio::time::timeout_at(deadline, tx.send(Ok(item))) => {
                                matches!(result, Ok(Ok(())))
                            }
                            _ = tx.closed() => false,
                        };
                        if !sent {
                            client_cancelled = tx.is_closed();
                            relay_deadline_expired = !client_cancelled;
                            cancel_flag.store(true, Ordering::SeqCst);
                            break;
                        }
                    }

                    if client_cancelled || relay_deadline_expired {
                        cancel_flag.store(true, Ordering::SeqCst);
                        command_task.abort();
                        let _ = command_task.await;
                        if relay_deadline_expired && !tx.is_closed() {
                            let timeout_item = Self::fail_closed_stream_error(
                                Code::DeadlineExceeded,
                                "Computer stream exceeded its deadline under downstream backpressure",
                                "COMPUTER_STREAM_TIMEOUT",
                            );
                            error_permit.send(Ok(timeout_item));
                            end_permit.send(Ok(DirectStreamItem {
                                event: Some(Event::End(DirectStreamEnd {})),
                            }));
                        }
                        return;
                    }

                    let execution_result = tokio::select! {
                        result = &mut command_task => Some(result),
                        _ = tokio::time::sleep_until(deadline) => None,
                        _ = tx.closed() => None,
                    };
                    if execution_result.is_none() {
                        cancel_flag.store(true, Ordering::SeqCst);
                        command_task.abort();
                        let _ = command_task.await;
                        if !tx.is_closed() {
                            let timeout_item = Self::fail_closed_stream_error(
                                Code::DeadlineExceeded,
                                "Computer command did not finish before its deadline",
                                "COMPUTER_STREAM_TIMEOUT",
                            );
                            error_permit.send(Ok(timeout_item));
                            end_permit.send(Ok(DirectStreamItem {
                                event: Some(Event::End(DirectStreamEnd {})),
                            }));
                        }
                        return;
                    }
                    if !stream_error_sent {
                        let error_item = match execution_result {
                            Some(Ok(Ok(()))) => None,
                            Some(Ok(Err(error))) => Some(computer_stream_error(error)),
                            Some(Err(error)) => Some(Self::fail_closed_stream_error(
                                Code::ExecutionFailed,
                                format!("Computer stream task failed: {error}"),
                                "COMPUTER_STREAM_TASK_FAILED",
                            )),
                            None => None,
                        };
                        if let Some(error) = error_item {
                            error_permit.send(Ok(error));
                        } else {
                            drop(error_permit);
                        }
                    } else {
                        drop(error_permit);
                    }

                    end_permit.send(Ok(DirectStreamItem {
                        event: Some(Event::End(DirectStreamEnd {})),
                    }));
                });

                Ok(Response::new(Box::pin(ReceiverStream::new(rx))))
            }

            _ => {
                let _ = tx
                    .send(Ok(Self::fail_closed_stream_error(
                        Code::MethodNotFound,
                        format!(
                            "Method '{}/{}' not supported for streaming",
                            req.capability, req.method
                        ),
                        "METHOD_NOT_FOUND",
                    )))
                    .await;
                Ok(Response::new(Box::pin(ReceiverStream::new(rx))))
            }
        }
    }
}
