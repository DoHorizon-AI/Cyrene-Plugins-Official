// SPDX-License-Identifier: Apache-2.0
//! DirectPluginRuntime gRPC Service Implementation (R08, R09)
//!
//! Provides the canonical gRPC host for Rust native capabilities:
//! - agent.runtime.v1
//! - memory.provider.v1
//! - computer.runtime.v1
//!
//! Enforces strict fail-closed protection against:
//! - Mismatched interface versions (must be "1")
//! - Unknown capabilities and methods
//! - Invalid or spoofed Type URLs
//! - Corrupted or unparseable Protobuf payloads

use std::pin::Pin;
use std::sync::Arc;
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Request, Response, Status};

use prost::Message;

use cyrene_agent_runtime::adapter::tool_catalog::ToolCatalogSource;
use cyrene_agent_runtime::engine::turn_loop::CyreneNativeAgentLoop;
use cyrene_agent_runtime::tck::{MockModelProvider, MockToolProvider};
use cyrene_agent_runtime::CancellationToken;
use cyrene_computer_runtime::ComputerRuntimeService;
use cyrene_mcp_provider::{McpServerConfig, McpToolProvider};
use cyrene_memory_runtime::backend::sqlite::SqliteMemoryBackend;
use cyrene_memory_runtime::embedding::ContractModelEmbeddingClient;
use cyrene_memory_runtime::CyreneMemoryService;

use cyrene_plugin_contracts::agent_runtime_v1::{
    agent_run_response, AgentRunRequest, AgentRunResponse, AgentStreamEvent,
};
use cyrene_plugin_contracts::computer_runtime_v1::{
    CommandExecutionRequest, CommandStreamEvent, CreateArtifactRequest, GetArtifactRequest,
    ListDirRequest, ReadFileRequest, WriteFileRequest,
};
use cyrene_plugin_contracts::memory_provider_v1::{
    DeleteMemoryRequest, GetMemoryRequest, RecallMemoryRequest, StoreMemoryRequest,
};
use cyrene_plugin_contracts::tool_provider_v1::{CallToolRequest, ListToolsRequest};

use crate::proto::direct_invocation_error::Code;
use crate::proto::direct_invocation_response::Result as InvocationResult;
use crate::proto::direct_plugin_runtime_server::DirectPluginRuntime;
use crate::proto::direct_stream_item::Event;
use crate::proto::{
    health_response, DirectInvocationError, DirectInvocationRequest, DirectInvocationResponse,
    DirectPayload, DirectStreamEnd, DirectStreamItem, HealthRequest, HealthResponse,
};

pub struct DirectPluginRuntimeServiceImpl {
    memory: Option<Arc<CyreneMemoryService>>,
    computer: Arc<ComputerRuntimeService>,
    mcp: Option<Arc<McpToolProvider>>,
    simulated_agent_dependencies: bool,
}

/// Host-side `ToolCatalogSource` over the configured MCP tool provider.
///
/// This is the agent-runtime host path: the package assembles the runtime
/// with a provider that actually speaks `tool.provider.v1`.
pub struct McpCatalogSource {
    provider: Arc<McpToolProvider>,
}

impl McpCatalogSource {
    /// Wrap one configured MCP tool provider.
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
    pub fn new() -> Self {
        let current_dir = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));
        let computer = Arc::new(ComputerRuntimeService::new(vec![current_dir]));

        Self {
            memory: None,
            computer,
            mcp: None,
            simulated_agent_dependencies: false,
        }
    }

    /// Builds a host that serves `tool.provider.v1` over the configured MCP
    /// server bindings. The package launcher is expected to call this once
    /// bindings are resolved; without it, tool calls fail closed.
    pub fn with_mcp_servers(servers: Vec<McpServerConfig>) -> Self {
        let current_dir = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));
        let computer = Arc::new(ComputerRuntimeService::new(vec![current_dir]));

        Self {
            memory: None,
            computer,
            mcp: Some(Arc::new(McpToolProvider::new(servers))),
            simulated_agent_dependencies: false,
        }
    }

    /// Builds a deterministic in-memory service exclusively for contract tests.
    pub fn with_simulated_dependencies_for_tests() -> Self {
        let mem_backend = Arc::new(SqliteMemoryBackend::new_in_memory().expect("sqlite in-memory"));
        let mem_embed = Arc::new(ContractModelEmbeddingClient::new(4));
        let memory = Arc::new(CyreneMemoryService::new(mem_backend, Some(mem_embed), 4));

        let current_dir = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));
        let computer = Arc::new(ComputerRuntimeService::new(vec![current_dir]));

        Self {
            memory: Some(memory),
            computer,
            mcp: None,
            simulated_agent_dependencies: true,
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

#[tonic::async_trait]
impl DirectPluginRuntime for DirectPluginRuntimeServiceImpl {
    async fn health(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        let mut capabilities = vec!["computer.runtime.v1".to_string()];
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
            plugin_id: "cyrene.plugin.rust.server".to_string(),
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

        // 2. Dispatch by capability
        match req.capability.as_str() {
            "agent.runtime.v1" => match req.method.as_str() {
                "Run" => {
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

                match req.method.as_str() {
                    "StoreMemory" => {
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
                    "GetMemory" => {
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
                    "RecallMemory" => {
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
                    "DeleteMemory" => {
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

            "computer.runtime.v1" => match req.method.as_str() {
                "ExecuteCommand" => {
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
                "ReadFile" => {
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
                "WriteFile" => {
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
                "ListDir" => {
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
                "CreateArtifact" => {
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
                "GetArtifact" => {
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

            "tool.provider.v1" => {
                let Some(mcp) = self.mcp.clone() else {
                    return Ok(Response::new(Self::fail_closed_error(
                        Code::Unavailable,
                        "MCP tool provider binding is not configured",
                        "TOOL_PROVIDER_NOT_CONFIGURED",
                    )));
                };
                match req.method.as_str() {
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
                        let response = mcp.list_tools(list_req.binding_id.as_deref()).await;
                        let mut buf = Vec::new();
                        response.encode(&mut buf).unwrap();
                        Ok(Response::new(DirectInvocationResponse {
                            result: Some(InvocationResult::Payload(DirectPayload {
                                type_url:
                                    "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse"
                                        .into(),
                                value: buf,
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
                        let response = mcp.call_tool(&call_req).await;
                        let mut buf = Vec::new();
                        response.encode(&mut buf).unwrap();
                        Ok(Response::new(DirectInvocationResponse {
                            result: Some(InvocationResult::Payload(DirectPayload {
                                type_url: "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse"
                                    .into(),
                                value: buf,
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
                }
            }

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

        let (tx, rx) = mpsc::channel(64);

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

        match (req.capability.as_str(), req.method.as_str()) {
            ("agent.runtime.v1", "RunStream") => {
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

            ("computer.runtime.v1", "ExecuteCommandStream") => {
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
                tokio::spawn(async move {
                    let (event_tx, mut event_rx) = mpsc::channel::<CommandStreamEvent>(64);

                    let comp = computer.clone();
                    tokio::spawn(async move {
                        let _ = comp.execute_command_stream(cmd_req, None, event_tx).await;
                    });

                    while let Some(event) = event_rx.recv().await {
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
