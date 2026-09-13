//! `tool.provider.v1` implementation over MCP stdio servers.
//!
//! v1 exposes `tools/list` and `tools/call` only (no resources or prompts).
//! Tool identity is the `(binding_id, provider_tool_id)` pair; `display_name`
//! is a presentation fact. `list_tools` records a per-binding snapshot that
//! `call_tool` enforces, which is what lets an Agent Runtime take one catalog
//! snapshot per run.
//!
//! Process lifecycle: every operation opens a short-lived MCP session and
//! closes it deterministically; a timeout or a dropped future kills the child
//! process (`kill_on_drop`). The provider is not a process supervisor — the
//! production path is expected to reuse the launcher's managed execution
//! primitives (capability expansion plan, decision C).

pub mod client;

pub use client::{McpClientError, McpServerConfig, McpStdioClient, MCP_PROTOCOL_VERSION};

use std::collections::HashMap;
use std::fmt::Write as _;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tokio::sync::Mutex;

use cyrene_plugin_contracts::tool_provider_v1::{
    call_tool_response, list_tools_response, tool_content_part, CallToolRequest, CallToolResponse,
    ListToolsResponse, ToolCallOutcome, ToolCatalog, ToolContentPart, ToolDescriptor,
    ToolJsonContent, ToolProviderError, ToolProviderErrorCode, ToolTextContent,
};

/// Capability identifier owned by this implementation.
pub const CAPABILITY_ID: &str = "tool.provider.v1";
/// Interface version owned by this implementation.
pub const INTERFACE_VERSION: &str = "1";

/// MCP stdio tool provider implementing `tool.provider.v1`.
pub struct McpToolProvider {
    servers: Vec<McpServerConfig>,
    snapshots: Mutex<HashMap<String, Vec<String>>>,
}

impl std::fmt::Debug for McpToolProvider {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("McpToolProvider")
            .field("bindings", &self.bindings())
            .finish()
    }
}

impl McpToolProvider {
    /// Build a provider over the configured server bindings.
    pub fn new(servers: Vec<McpServerConfig>) -> Self {
        Self {
            servers,
            snapshots: Mutex::new(HashMap::new()),
        }
    }

    /// True when at least one server binding is configured.
    pub fn is_configured(&self) -> bool {
        !self.servers.is_empty()
    }

    /// Binding identifiers in configuration order.
    pub fn bindings(&self) -> Vec<String> {
        self.servers
            .iter()
            .map(|server| server.binding_id.clone())
            .collect()
    }

    /// Return one catalog snapshot for the selected bindings and record it.
    pub async fn list_tools(&self, binding_id: Option<&str>) -> ListToolsResponse {
        let selected: Vec<&McpServerConfig> = match binding_id {
            Some(selected_id) => self
                .servers
                .iter()
                .filter(|server| server.binding_id == selected_id)
                .collect(),
            None => self.servers.iter().collect(),
        };
        if selected.is_empty() {
            return ListToolsResponse {
                result: Some(list_tools_response::Result::Error(error_fact(
                    ToolProviderErrorCode::InvalidArguments,
                    match binding_id {
                        Some(selected_id) => {
                            format!("binding '{selected_id}' is not configured")
                        }
                        None => "no MCP server bindings are configured".to_string(),
                    },
                    false,
                ))),
            };
        }

        let mut descriptors = Vec::new();
        let mut snapshot = HashMap::new();
        for server in selected.iter() {
            let tools = match fetch_tools(server).await {
                Ok(tools) => tools,
                Err(error) => {
                    return ListToolsResponse {
                        result: Some(list_tools_response::Result::Error(map_client_error(error))),
                    }
                }
            };
            snapshot.insert(
                server.binding_id.clone(),
                tools
                    .iter()
                    .map(|descriptor| descriptor.provider_tool_id.clone())
                    .collect::<Vec<_>>(),
            );
            descriptors.extend(tools);
        }
        descriptors.sort_by(|left, right| {
            (&left.binding_id, &left.provider_tool_id)
                .cmp(&(&right.binding_id, &right.provider_tool_id))
        });
        let catalog_version = catalog_digest(&descriptors);

        let mut recorded = self.snapshots.lock().await;
        for (binding, tools) in snapshot {
            recorded.insert(binding, tools);
        }

        ListToolsResponse {
            result: Some(list_tools_response::Result::Catalog(ToolCatalog {
                catalog_version,
                tools: descriptors,
            })),
        }
    }

    /// Execute one tool call against the configured binding.
    pub async fn call_tool(&self, request: &CallToolRequest) -> CallToolResponse {
        let Some(server) = self
            .servers
            .iter()
            .find(|server| server.binding_id == request.binding_id)
        else {
            return CallToolResponse {
                result: Some(call_tool_response::Result::Error(error_fact(
                    ToolProviderErrorCode::InvalidArguments,
                    format!("binding '{}' is not configured", request.binding_id),
                    false,
                ))),
            };
        };

        {
            let recorded = self.snapshots.lock().await;
            if let Some(known) = recorded.get(&request.binding_id) {
                if !known.contains(&request.provider_tool_id) {
                    return CallToolResponse {
                        result: Some(call_tool_response::Result::Error(error_fact(
                            ToolProviderErrorCode::ToolNotFound,
                            format!(
                                "tool '{}' is not part of the recorded '{}' snapshot",
                                request.provider_tool_id, request.binding_id
                            ),
                            false,
                        ))),
                    };
                }
            }
        }

        let arguments = match parse_arguments(&request.arguments_json) {
            Ok(arguments) => arguments,
            Err(message) => {
                return CallToolResponse {
                    result: Some(call_tool_response::Result::Error(error_fact(
                        ToolProviderErrorCode::InvalidArguments,
                        message,
                        false,
                    ))),
                }
            }
        };

        match invoke_tool(server, &request.provider_tool_id, arguments).await {
            Ok(outcome) => CallToolResponse {
                result: Some(call_tool_response::Result::Outcome(outcome)),
            },
            Err(error) => CallToolResponse {
                result: Some(call_tool_response::Result::Error(map_client_error(error))),
            },
        }
    }
}

async fn fetch_tools(server: &McpServerConfig) -> Result<Vec<ToolDescriptor>, McpClientError> {
    let mut session = McpStdioClient::start(server).await?;
    let result = session
        .request("tools/list", json!({}), server.timeout)
        .await;
    let mapped = result.map(|value| map_tool_list(server, &value));
    session.shutdown().await;
    mapped
}

async fn invoke_tool(
    server: &McpServerConfig,
    provider_tool_id: &str,
    arguments: Value,
) -> Result<ToolCallOutcome, McpClientError> {
    let mut session = McpStdioClient::start(server).await?;
    let params = json!({ "name": provider_tool_id, "arguments": arguments });
    let result = session.request("tools/call", params, server.timeout).await;
    let mapped = result.map(|value| map_tool_outcome(&value));
    session.shutdown().await;
    mapped
}

fn map_tool_list(server: &McpServerConfig, result: &Value) -> Vec<ToolDescriptor> {
    let mut tools = Vec::new();
    let Some(items) = result.get("tools").and_then(Value::as_array) else {
        return tools;
    };
    for item in items {
        let Some(name) = item.get("name").and_then(Value::as_str) else {
            continue;
        };
        let display_name = item
            .get("annotations")
            .and_then(|annotations| annotations.get("title"))
            .and_then(Value::as_str)
            .unwrap_or(name);
        tools.push(ToolDescriptor {
            binding_id: server.binding_id.clone(),
            provider_tool_id: name.to_string(),
            display_name: display_name.to_string(),
            description: item
                .get("description")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            input_schema_json: item
                .get("inputSchema")
                .map(Value::to_string)
                .unwrap_or_else(|| "{}".to_string()),
            output_schema_json: item
                .get("outputSchema")
                .filter(|schema| !schema.is_null())
                .map(Value::to_string),
        });
    }
    tools
}

fn map_tool_outcome(result: &Value) -> ToolCallOutcome {
    let mut content = Vec::new();
    if let Some(items) = result.get("content").and_then(Value::as_array) {
        for item in items {
            if item.get("type").and_then(Value::as_str) == Some("text") {
                let text = item
                    .get("text")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                content.push(ToolContentPart {
                    content: Some(tool_content_part::Content::Text(ToolTextContent { text })),
                });
            } else {
                // Images, audio, and embedded resources stay opaque JSON in v1.
                content.push(ToolContentPart {
                    content: Some(tool_content_part::Content::Json(ToolJsonContent {
                        json: item.to_string(),
                    })),
                });
            }
        }
    }
    ToolCallOutcome {
        content,
        is_error: result
            .get("isError")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    }
}

fn parse_arguments(arguments_json: &str) -> Result<Value, String> {
    if arguments_json.trim().is_empty() {
        return Ok(json!({}));
    }
    match serde_json::from_str::<Value>(arguments_json) {
        Ok(value) if value.is_object() => Ok(value),
        Ok(_) => Err("arguments_json must be a JSON object".to_string()),
        Err(error) => Err(format!("arguments_json is not valid JSON: {error}")),
    }
}

fn catalog_digest(descriptors: &[ToolDescriptor]) -> String {
    let canonical: Vec<Value> = descriptors
        .iter()
        .map(|descriptor| {
            json!({
                "binding_id": descriptor.binding_id,
                "provider_tool_id": descriptor.provider_tool_id,
                "display_name": descriptor.display_name,
                "description": descriptor.description,
                "input_schema_json": descriptor.input_schema_json,
                "output_schema_json": descriptor.output_schema_json,
            })
        })
        .collect();
    let payload = serde_json::to_string(&canonical).unwrap_or_default();
    let digest = Sha256::digest(payload.as_bytes());
    let mut encoded = String::with_capacity(digest.len() * 2 + 7);
    encoded.push_str("sha256:");
    for byte in digest {
        let _ = write!(encoded, "{byte:02x}");
    }
    encoded
}

fn error_fact(
    code: ToolProviderErrorCode,
    message: impl Into<String>,
    retryable: bool,
) -> ToolProviderError {
    ToolProviderError {
        code: code as i32,
        message: message.into(),
        retryable,
    }
}

fn map_client_error(error: McpClientError) -> ToolProviderError {
    let (code, retryable) = match &error {
        McpClientError::Timeout { .. } => (ToolProviderErrorCode::Timeout, true),
        McpClientError::Spawn(_) => (ToolProviderErrorCode::ProviderError, false),
        McpClientError::ProcessExited { .. } => (ToolProviderErrorCode::ProviderError, true),
        McpClientError::Io(_) => (ToolProviderErrorCode::ProviderError, true),
        McpClientError::Malformed(_) => (ToolProviderErrorCode::ProviderError, false),
        McpClientError::Protocol { code, .. } => match code {
            -32602 => (ToolProviderErrorCode::InvalidArguments, false),
            _ => (ToolProviderErrorCode::ProviderError, false),
        },
    };
    error_fact(code, error.to_string(), retryable)
}
