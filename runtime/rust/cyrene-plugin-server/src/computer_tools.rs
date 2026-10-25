// SPDX-License-Identifier: Apache-2.0
//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 computer_tools.rs                                               │
//! │  Module: cyrene_plugin_server::computer_tools                       │
//! │  Role: Fixed tool.provider.v1 projection over Computer Runtime.      │
//! │                                                                     │
//! │  模块职责：将固定的工具目录映射到现有 ComputerRuntimeService，       │
//! │           不创建第二个任务执行器，也不接受任意服务端地址。           │
//! └─────────────────────────────────────────────────────────────────────┘

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use cyrene_computer_runtime::{
    computer_runtime_v1::{
        command_execution_response, list_dir_response, read_file_response, write_file_response,
        CommandExecutionRequest, ComputerErrorCode, ListDirRequest, ReadFileRequest,
        WriteFileRequest,
    },
    ComputerRuntimeLimits, ComputerRuntimeService,
};
use cyrene_plugin_contracts::tool_provider_v1::{
    call_tool_response, list_tools_response, CallToolRequest, CallToolResponse, ListToolsResponse,
    ToolCallOutcome, ToolCatalog, ToolContentPart, ToolDescriptor, ToolJsonContent,
    ToolProviderError, ToolProviderErrorCode,
};
use serde_json::{json, Map, Value};
use std::path::{Component, Path};
use std::sync::Arc;

/// Stable plugin binding that owns the bounded local runner catalog.
pub const COMPUTER_BINDING_ID: &str = "computer-runtime";

const CATALOG_VERSION: &str = "computer-runtime-v1-four-tools";
const COMMAND_EXECUTE: &str = "computer.command.execute";
const FILE_READ: &str = "computer.file.read";
const FILE_WRITE: &str = "computer.file.write";
const DIRECTORY_LIST: &str = "computer.directory.list";
const MAX_COMMAND_BYTES: usize = 32 * 1024;
const MAX_WORKSPACE_PATH_BYTES: usize = 4 * 1024;
const MAX_TOOL_READ_BYTES: usize = 64 * 1024;
// Keep JSON integer offsets exactly representable across host implementations.
const MAX_TOOL_READ_OFFSET: i64 = (1_i64 << 53) - 1;
const MAX_TOOL_RESULT_JSON_BYTES: usize = 1_450_000;
const SCRATCH_LIFETIME_NOTICE: &str = " Workspace files are scratch, not durable task state. In the Teams Cloud v1 profile they are cleared when the runner container restarts; use GitHub tools for repository changes that must persist.";

/// A fixed `tool.provider.v1` facade over the one configured Computer Runtime.
///
/// The facade exposes only the four host-bound operations approved for v1.
/// Calls map to existing computer runtime methods, preserving that runtime's
/// path validation, process group cleanup, timeouts, output caps and evidence.
///
/// 将批准的四个本机操作投影为固定工具目录，所有调用均复用现有 Computer Runtime。
pub struct ComputerToolProvider {
    computer: Arc<ComputerRuntimeService>,
    limits: ComputerRuntimeLimits,
}

impl ComputerToolProvider {
    /// Creates a catalog facade using the runtime's configured resource bounds.
    pub fn new(computer: Arc<ComputerRuntimeService>, limits: ComputerRuntimeLimits) -> Self {
        Self { computer, limits }
    }

    /// Returns the immutable v1 tool catalog for this runner binding.
    pub fn list_tools(&self, binding_id: Option<&str>) -> ListToolsResponse {
        if binding_id.is_some_and(|value| value != COMPUTER_BINDING_ID) {
            return list_error(
                ToolProviderErrorCode::InvalidArguments,
                format!("computer binding '{COMPUTER_BINDING_ID}' is not selected"),
                false,
            );
        }

        let command_schema = json!({
            "type": "object",
            "additionalProperties": false,
            "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 8192, "x-maxUtf8Bytes": MAX_COMMAND_BYTES},
                "cwd": {"type": "string", "maxLength": 1024, "x-maxUtf8Bytes": MAX_WORKSPACE_PATH_BYTES, "description": "Workspace-relative directory; defaults to the workspace root."},
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": self.limits.command_max_timeout_ms
                }
            },
            "required": ["command"]
        });
        let file_read_schema = json!({
            "type": "object",
            "additionalProperties": false,
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024, "x-maxUtf8Bytes": MAX_WORKSPACE_PATH_BYTES, "description": "Workspace-relative file path."},
                "offset": {"type": "integer", "minimum": 0, "maximum": MAX_TOOL_READ_OFFSET},
                "length": {"type": "integer", "minimum": 0, "maximum": MAX_TOOL_READ_BYTES}
            },
            "required": ["path", "length"]
        });
        let file_write_schema = json!({
            "type": "object",
            "additionalProperties": false,
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024, "x-maxUtf8Bytes": MAX_WORKSPACE_PATH_BYTES, "description": "Workspace-relative file path."},
                "content_base64": {
                    "type": "string",
                    "maxLength": self.limits.max_file_bytes.div_ceil(3) * 4,
                    "description": "File bytes encoded using standard base64."
                },
                "overwrite": {"type": "boolean"}
            },
            "required": ["path", "content_base64", "overwrite"]
        });
        let directory_schema = json!({
            "type": "object",
            "additionalProperties": false,
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024, "x-maxUtf8Bytes": MAX_WORKSPACE_PATH_BYTES, "description": "Workspace-relative directory path; use '.' for the root."},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": self.limits.max_directory_depth}
            },
            "required": ["path"]
        });

        let tools = vec![
            descriptor(
                COMMAND_EXECUTE,
                &format!(
                    "Execute a bounded shell command in the isolated workspace.{SCRATCH_LIFETIME_NOTICE}"
                ),
                command_schema,
                json!({
                    "type": "object",
                    "properties": {
                        "stdout": {"type": "string", "maxLength": self.limits.max_output_bytes, "x-maxUtf8Bytes": self.limits.max_output_bytes, "description": format!("At most {} output bytes.", self.limits.max_output_bytes)},
                        "stderr": {"type": "string", "maxLength": self.limits.max_output_bytes, "x-maxUtf8Bytes": self.limits.max_output_bytes, "description": format!("At most {} output bytes.", self.limits.max_output_bytes)},
                        "resource_usage": {"type": "null", "description": "Unavailable until measured from operating-system counters."}
                    }
                }),
            ),
            descriptor(
                FILE_READ,
                &format!(
                    "Read 0 to 65536 bytes from a workspace-relative file; length is required.{SCRATCH_LIFETIME_NOTICE}"
                ),
                file_read_schema,
                json!({
                    "type": "object",
                    "properties": {
                        "size_bytes": {"type": "integer", "maximum": MAX_TOOL_READ_BYTES},
                        "content_text": {"type": "string", "maxLength": MAX_TOOL_READ_BYTES, "description": "UTF-8 text when the encoded JSON result stays within the transport bound."},
                        "content_base64": {"type": "string", "maxLength": MAX_TOOL_READ_BYTES.div_ceil(3) * 4, "description": "Standard base64 bytes for binary or JSON-escaped content."}
                    },
                    "required": ["size_bytes"]
                }),
            ),
            descriptor(
                FILE_WRITE,
                &format!(
                    "Write bounded bytes to a workspace-relative file.{SCRATCH_LIFETIME_NOTICE}"
                ),
                file_write_schema,
                json!({"type": "object", "properties": {"bytes_written": {"type": "integer", "maximum": self.limits.max_file_bytes}}}),
            ),
            descriptor(
                DIRECTORY_LIST,
                &format!(
                    "List a bounded workspace-relative directory tree without following symlinks.{SCRATCH_LIFETIME_NOTICE}"
                ),
                directory_schema,
                json!({"type": "object", "properties": {"entries": {"type": "array", "maxItems": self.limits.max_directory_entries, "description": "Results exceeding the 1.45 MB transport envelope fail explicitly."}}}),
            ),
        ];

        ListToolsResponse {
            result: Some(list_tools_response::Result::Catalog(ToolCatalog {
                catalog_version: CATALOG_VERSION.to_string(),
                tools,
            })),
        }
    }

    /// Executes one fixed catalog operation through ComputerRuntimeService.
    pub async fn call_tool(&self, request: &CallToolRequest) -> CallToolResponse {
        if request.binding_id != COMPUTER_BINDING_ID {
            return call_error(
                ToolProviderErrorCode::InvalidArguments,
                format!(
                    "computer binding '{}' is not configured",
                    request.binding_id
                ),
                false,
            );
        }

        let arguments = match parse_arguments(&request.arguments_json) {
            Ok(arguments) => arguments,
            Err(message) => {
                return call_error(ToolProviderErrorCode::InvalidArguments, message, false);
            }
        };

        match request.provider_tool_id.as_str() {
            COMMAND_EXECUTE => self.execute_command(arguments).await,
            FILE_READ => self.read_file(arguments),
            FILE_WRITE => self.write_file(arguments),
            DIRECTORY_LIST => self.list_directory(arguments),
            _ => call_error(
                ToolProviderErrorCode::ToolNotFound,
                format!(
                    "computer tool '{}' is not in catalog {CATALOG_VERSION}",
                    request.provider_tool_id
                ),
                false,
            ),
        }
    }

    async fn execute_command(&self, arguments: Map<String, Value>) -> CallToolResponse {
        if let Err(message) = ensure_keys(&arguments, &["command", "cwd", "timeout_ms"]) {
            return call_error(ToolProviderErrorCode::InvalidArguments, message, false);
        }
        let command = match required_string(&arguments, "command") {
            Ok(value)
                if !value.trim().is_empty()
                    && value.len() <= MAX_COMMAND_BYTES
                    && !value.contains('\0') =>
            {
                value
            }
            Ok(_) => return call_error(
                ToolProviderErrorCode::InvalidArguments,
                "command must be non-empty, contain no NUL, and fit the configured command limit"
                    .into(),
                false,
            ),
            Err(message) => {
                return call_error(ToolProviderErrorCode::InvalidArguments, message, false)
            }
        };
        let cwd = match optional_workspace_path(&arguments, "cwd", true) {
            Ok(value) => value,
            Err(message) => {
                return call_error(ToolProviderErrorCode::InvalidArguments, message, false)
            }
        };
        let timeout_ms = match optional_i64(&arguments, "timeout_ms") {
            Ok(value) if value.is_none_or(|timeout| timeout > 0) => value,
            Ok(_) => {
                return call_error(
                    ToolProviderErrorCode::InvalidArguments,
                    "timeout_ms must be positive".into(),
                    false,
                )
            }
            Err(message) => {
                return call_error(ToolProviderErrorCode::InvalidArguments, message, false)
            }
        };

        let response = self
            .computer
            .execute_command(
                CommandExecutionRequest {
                    command,
                    cwd,
                    env: Default::default(),
                    timeout_ms,
                    max_output_bytes: Some(self.limits.max_output_bytes as i64),
                },
                None,
            )
            .await;
        match response.result {
            Some(command_execution_response::Result::Evidence(evidence)) => {
                let result = json!({
                    "exit_code": evidence.exit_code,
                    "duration_ms": evidence.duration_ms,
                    "stdout": evidence.stdout,
                    "stderr": evidence.stderr,
                    "stdout_truncated": evidence.stdout_truncated,
                    "stderr_truncated": evidence.stderr_truncated,
                    "sha256_hash": evidence.sha256_hash,
                    "resource_usage": Value::Null,
                });
                outcome(result, evidence.exit_code != 0)
            }
            Some(command_execution_response::Result::Error(error)) => {
                domain_error(error.code, error.message, error.retryable)
            }
            None => domain_error(
                0,
                "Computer Runtime returned no command result".into(),
                true,
            ),
        }
    }

    fn read_file(&self, arguments: Map<String, Value>) -> CallToolResponse {
        if let Err(message) = ensure_keys(&arguments, &["path", "offset", "length"]) {
            return call_error(ToolProviderErrorCode::InvalidArguments, message, false);
        }
        let path = match required_workspace_path(&arguments, "path", false) {
            Ok(value) => value,
            Err(message) => {
                return call_error(ToolProviderErrorCode::InvalidArguments, message, false)
            }
        };
        let offset = match optional_i64(&arguments, "offset") {
            Ok(value)
                if value.is_none_or(|offset| (0..=MAX_TOOL_READ_OFFSET).contains(&offset)) =>
            {
                value
            }
            Ok(_) => {
                return call_error(
                    ToolProviderErrorCode::InvalidArguments,
                    format!("offset must be between zero and {MAX_TOOL_READ_OFFSET}"),
                    false,
                )
            }
            Err(message) => {
                return call_error(ToolProviderErrorCode::InvalidArguments, message, false)
            }
        };
        let length = match optional_i64(&arguments, "length") {
            Ok(Some(length)) if (0..=MAX_TOOL_READ_BYTES as i64).contains(&length) => Some(length),
            Ok(None) => {
                return call_error(
                    ToolProviderErrorCode::InvalidArguments,
                    "length is required and must be between zero and 65536 bytes".into(),
                    false,
                )
            }
            Ok(Some(_)) => {
                return call_error(
                    ToolProviderErrorCode::InvalidArguments,
                    "length must be between zero and 65536 bytes".into(),
                    false,
                )
            }
            Err(message) => {
                return call_error(ToolProviderErrorCode::InvalidArguments, message, false)
            }
        };

        let response = self.computer.read_file(ReadFileRequest {
            path,
            offset,
            length,
        });
        match response.result {
            Some(read_file_response::Result::Content(content)) => {
                let size_bytes = content.len();
                let result = match String::from_utf8(content) {
                    Ok(text) => {
                        let text_result = json!({
                            "size_bytes": size_bytes,
                            "content_text": text,
                        });
                        if text_result.to_string().len() <= MAX_TOOL_RESULT_JSON_BYTES {
                            text_result
                        } else {
                            json!({
                                "size_bytes": size_bytes,
                                "content_base64": BASE64.encode(text.as_bytes()),
                            })
                        }
                    }
                    Err(error) => json!({
                        "size_bytes": size_bytes,
                        "content_base64": BASE64.encode(error.into_bytes()),
                    }),
                };
                outcome(result, false)
            }
            Some(read_file_response::Result::Error(error)) => {
                domain_error(error.code, error.message, error.retryable)
            }
            None => domain_error(0, "Computer Runtime returned no file result".into(), true),
        }
    }

    fn write_file(&self, arguments: Map<String, Value>) -> CallToolResponse {
        if let Err(message) = ensure_keys(&arguments, &["path", "content_base64", "overwrite"]) {
            return call_error(ToolProviderErrorCode::InvalidArguments, message, false);
        }
        let path = match required_workspace_path(&arguments, "path", false) {
            Ok(value) => value,
            Err(message) => {
                return call_error(ToolProviderErrorCode::InvalidArguments, message, false)
            }
        };
        let content_base64 = match required_string(&arguments, "content_base64") {
            Ok(value) => value,
            Err(message) => {
                return call_error(ToolProviderErrorCode::InvalidArguments, message, false)
            }
        };
        let max_encoded_bytes = self.limits.max_file_bytes.div_ceil(3) * 4;
        if content_base64.len() > max_encoded_bytes {
            return call_error(
                ToolProviderErrorCode::InvalidArguments,
                format!("encoded file content exceeds {max_encoded_bytes} bytes"),
                false,
            );
        }
        let content = match BASE64.decode(content_base64) {
            Ok(value) if value.len() <= self.limits.max_file_bytes => value,
            Ok(_) => {
                return call_error(
                    ToolProviderErrorCode::InvalidArguments,
                    format!("file content exceeds {} bytes", self.limits.max_file_bytes),
                    false,
                )
            }
            Err(error) => {
                return call_error(
                    ToolProviderErrorCode::InvalidArguments,
                    format!("content_base64 is invalid: {error}"),
                    false,
                )
            }
        };
        let overwrite = match arguments.get("overwrite").and_then(Value::as_bool) {
            Some(value) => value,
            None => {
                return call_error(
                    ToolProviderErrorCode::InvalidArguments,
                    "overwrite must be a boolean".into(),
                    false,
                )
            }
        };

        let response = self.computer.write_file(WriteFileRequest {
            path,
            content,
            overwrite,
        });
        match response.result {
            Some(write_file_response::Result::BytesWritten(bytes)) => {
                outcome(json!({"bytes_written": bytes}), false)
            }
            Some(write_file_response::Result::Error(error)) => {
                domain_error(error.code, error.message, error.retryable)
            }
            None => domain_error(0, "Computer Runtime returned no write result".into(), true),
        }
    }

    fn list_directory(&self, arguments: Map<String, Value>) -> CallToolResponse {
        if let Err(message) = ensure_keys(&arguments, &["path", "max_depth"]) {
            return call_error(ToolProviderErrorCode::InvalidArguments, message, false);
        }
        let path = match required_workspace_path(&arguments, "path", true) {
            Ok(value) => value,
            Err(message) => {
                return call_error(ToolProviderErrorCode::InvalidArguments, message, false)
            }
        };
        let max_depth = match optional_i64(&arguments, "max_depth") {
            Ok(value)
                if value.is_none_or(|depth| {
                    depth >= 1 && depth <= self.limits.max_directory_depth as i64
                }) =>
            {
                value.map(|depth| depth as i32)
            }
            Ok(_) => {
                return call_error(
                    ToolProviderErrorCode::InvalidArguments,
                    format!(
                        "max_depth must be between 1 and {}",
                        self.limits.max_directory_depth
                    ),
                    false,
                )
            }
            Err(message) => {
                return call_error(ToolProviderErrorCode::InvalidArguments, message, false)
            }
        };

        let response = self.computer.list_dir(ListDirRequest { path, max_depth });
        match response.result {
            Some(list_dir_response::Result::Entries(entries)) => {
                let values: Vec<Value> = entries
                    .entries
                    .into_iter()
                    .map(|entry| {
                        json!({
                            "name": entry.name,
                            "is_directory": entry.is_directory,
                            "size_bytes": entry.size_bytes,
                            "modified_at_ms": entry.modified_at_ms,
                        })
                    })
                    .collect();
                let result = json!({"entries": values});
                if result.to_string().len() > MAX_TOOL_RESULT_JSON_BYTES {
                    domain_error(
                        ComputerErrorCode::OutputLimitExceeded as i32,
                        "Directory result exceeds the 1.45 MB transport envelope; list a narrower path or reduce max_depth".into(),
                        false,
                    )
                } else {
                    outcome(result, false)
                }
            }
            Some(list_dir_response::Result::Error(error)) => {
                domain_error(error.code, error.message, error.retryable)
            }
            None => domain_error(
                0,
                "Computer Runtime returned no directory result".into(),
                true,
            ),
        }
    }
}

fn descriptor(
    tool_id: &str,
    description: &str,
    input_schema: Value,
    output_schema: Value,
) -> ToolDescriptor {
    ToolDescriptor {
        binding_id: COMPUTER_BINDING_ID.to_string(),
        provider_tool_id: tool_id.to_string(),
        display_name: tool_id.to_string(),
        description: description.to_string(),
        input_schema_json: input_schema.to_string(),
        output_schema_json: Some(output_schema.to_string()),
    }
}

fn parse_arguments(raw: &str) -> Result<Map<String, Value>, String> {
    if raw.trim().is_empty() {
        return Ok(Map::new());
    }
    match serde_json::from_str::<Value>(raw)
        .map_err(|error| format!("Invalid JSON arguments: {error}"))?
    {
        Value::Object(object) => Ok(object),
        _ => Err("Tool arguments must be a JSON object".into()),
    }
}

fn ensure_keys(arguments: &Map<String, Value>, allowed: &[&str]) -> Result<(), String> {
    if let Some(key) = arguments
        .keys()
        .find(|key| !allowed.contains(&key.as_str()))
    {
        return Err(format!("Unexpected argument '{key}'"));
    }
    Ok(())
}

fn required_string(arguments: &Map<String, Value>, key: &str) -> Result<String, String> {
    arguments
        .get(key)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| format!("'{key}' must be a string"))
}

fn optional_i64(arguments: &Map<String, Value>, key: &str) -> Result<Option<i64>, String> {
    arguments
        .get(key)
        .map(|value| {
            value
                .as_i64()
                .ok_or_else(|| format!("'{key}' must be an integer"))
        })
        .transpose()
}

fn optional_workspace_path(
    arguments: &Map<String, Value>,
    key: &str,
    allow_root: bool,
) -> Result<Option<String>, String> {
    arguments
        .get(key)
        .map(|_| required_workspace_path(arguments, key, allow_root))
        .transpose()
}

fn required_workspace_path(
    arguments: &Map<String, Value>,
    key: &str,
    allow_root: bool,
) -> Result<String, String> {
    let raw = required_string(arguments, key)?;
    if raw.trim().is_empty() || raw.contains('\0') {
        return Err(format!("'{key}' must be a non-empty path without NUL"));
    }
    if raw.len() > MAX_WORKSPACE_PATH_BYTES {
        return Err(format!(
            "'{key}' exceeds the {MAX_WORKSPACE_PATH_BYTES}-byte workspace path limit"
        ));
    }
    let path = Path::new(&raw);
    let is_root = allow_root && raw == ".";
    if path.is_absolute()
        || path.components().any(|component| {
            matches!(component, Component::ParentDir | Component::Prefix(_))
                || (matches!(component, Component::CurDir) && !is_root)
        })
    {
        return Err(format!("'{key}' must stay within the configured workspace"));
    }
    Ok(raw)
}

fn outcome(value: Value, is_error: bool) -> CallToolResponse {
    CallToolResponse {
        result: Some(call_tool_response::Result::Outcome(ToolCallOutcome {
            content: vec![ToolContentPart {
                content: Some(
                    cyrene_plugin_contracts::tool_provider_v1::tool_content_part::Content::Json(
                        ToolJsonContent {
                            json: value.to_string(),
                        },
                    ),
                ),
            }],
            is_error,
        })),
    }
}

fn list_error(code: ToolProviderErrorCode, message: String, retryable: bool) -> ListToolsResponse {
    ListToolsResponse {
        result: Some(list_tools_response::Result::Error(ToolProviderError {
            code: code as i32,
            message,
            retryable,
        })),
    }
}

fn call_error(code: ToolProviderErrorCode, message: String, retryable: bool) -> CallToolResponse {
    CallToolResponse {
        result: Some(call_tool_response::Result::Error(ToolProviderError {
            code: code as i32,
            message,
            retryable,
        })),
    }
}

fn domain_error(code: i32, message: String, retryable: bool) -> CallToolResponse {
    outcome(
        json!({
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
            }
        }),
        true,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use cyrene_plugin_contracts::tool_provider_v1::{call_tool_response, list_tools_response};
    use prost::Message;
    use std::fs;
    use tempfile::tempdir;

    fn provider(root: &std::path::Path) -> ComputerToolProvider {
        let artifact_root = tempdir().unwrap().keep();
        let limits = ComputerRuntimeLimits::default();
        let runtime = Arc::new(ComputerRuntimeService::with_limits_and_artifact_dir(
            vec![root.to_path_buf()],
            artifact_root,
            limits.clone(),
        ));
        ComputerToolProvider::new(runtime, limits)
    }

    #[test]
    fn catalog_contains_only_the_four_fixed_host_operations() {
        let workspace = tempdir().unwrap();
        let provider = provider(workspace.path());
        let response = provider.list_tools(None);
        let response = ListToolsResponse::decode(response.encode_to_vec().as_slice()).unwrap();
        let Some(list_tools_response::Result::Catalog(catalog)) = response.result else {
            panic!("expected fixed catalog");
        };
        assert_eq!(catalog.catalog_version, CATALOG_VERSION);
        assert_eq!(catalog.tools.len(), 4);
        assert_eq!(catalog.tools[0].provider_tool_id, COMMAND_EXECUTE);
        assert_eq!(catalog.tools[1].provider_tool_id, FILE_READ);
        assert_eq!(catalog.tools[2].provider_tool_id, FILE_WRITE);
        assert_eq!(catalog.tools[3].provider_tool_id, DIRECTORY_LIST);
        assert!(catalog
            .tools
            .iter()
            .all(|tool| tool.binding_id == COMPUTER_BINDING_ID));
        assert!(catalog.tools.iter().all(|tool| {
            tool.description.contains("scratch, not durable task state")
                && tool.description.contains("Teams Cloud v1 profile")
                && tool.description.contains("runner container restarts")
                && tool.description.contains("GitHub tools")
        }));
        let read_schema: Value = serde_json::from_str(&catalog.tools[1].input_schema_json).unwrap();
        assert_eq!(
            read_schema["properties"]["length"]["maximum"],
            MAX_TOOL_READ_BYTES
        );
        let maximum_offset = read_schema["properties"]["offset"]["maximum"]
            .as_i64()
            .expect(
                "offset maximum must fit the host's signed 64-bit bound after protobuf transport",
            );
        assert_eq!(maximum_offset, MAX_TOOL_READ_OFFSET);
        assert!(read_schema["required"]
            .as_array()
            .unwrap()
            .iter()
            .any(|value| value == "length"));
        let command_schema: Value =
            serde_json::from_str(&catalog.tools[0].input_schema_json).unwrap();
        assert_eq!(
            command_schema["properties"]["command"]["x-maxUtf8Bytes"],
            MAX_COMMAND_BYTES
        );
        let path_schema: Value = serde_json::from_str(&catalog.tools[1].input_schema_json).unwrap();
        assert_eq!(
            path_schema["properties"]["path"]["x-maxUtf8Bytes"],
            MAX_WORKSPACE_PATH_BYTES
        );
        let command_output: Value =
            serde_json::from_str(catalog.tools[0].output_schema_json.as_deref().unwrap()).unwrap();
        assert!(command_output["properties"]["stdout"]["description"]
            .as_str()
            .unwrap()
            .contains("65536"));
    }

    #[tokio::test]
    async fn fixed_operations_read_write_list_and_execute_in_the_scoped_root() {
        let workspace = tempdir().unwrap();
        let provider = provider(workspace.path());

        let write = provider
            .call_tool(&CallToolRequest {
                binding_id: COMPUTER_BINDING_ID.into(),
                provider_tool_id: FILE_WRITE.into(),
                arguments_json: json!({
                    "path": "note.txt",
                    "content_base64": BASE64.encode("hello workspace"),
                    "overwrite": false
                })
                .to_string(),
                catalog_version: Some(CATALOG_VERSION.into()),
            })
            .await;
        let Some(call_tool_response::Result::Outcome(write_outcome)) = write.result else {
            panic!("expected write outcome");
        };
        assert!(!write_outcome.is_error);
        assert_eq!(
            fs::read_to_string(workspace.path().join("note.txt")).unwrap(),
            "hello workspace"
        );

        let read = provider
            .call_tool(&CallToolRequest {
                binding_id: COMPUTER_BINDING_ID.into(),
                provider_tool_id: FILE_READ.into(),
                arguments_json: json!({"path": "note.txt", "length": 64}).to_string(),
                catalog_version: Some(CATALOG_VERSION.into()),
            })
            .await;
        let Some(call_tool_response::Result::Outcome(read_outcome)) = read.result else {
            panic!("expected read outcome");
        };
        assert!(!read_outcome.is_error);
        let Some(cyrene_plugin_contracts::tool_provider_v1::tool_content_part::Content::Json(json)) =
            read_outcome.content[0].content.as_ref()
        else {
            panic!("expected JSON content");
        };
        assert_eq!(
            serde_json::from_str::<Value>(&json.json).unwrap()["content_text"],
            "hello workspace"
        );

        let list = provider
            .call_tool(&CallToolRequest {
                binding_id: COMPUTER_BINDING_ID.into(),
                provider_tool_id: DIRECTORY_LIST.into(),
                arguments_json: json!({"path": "."}).to_string(),
                catalog_version: Some(CATALOG_VERSION.into()),
            })
            .await;
        let Some(call_tool_response::Result::Outcome(list_outcome)) = list.result else {
            panic!("expected directory outcome");
        };
        assert!(!list_outcome.is_error);

        let command = provider
            .call_tool(&CallToolRequest {
                binding_id: COMPUTER_BINDING_ID.into(),
                provider_tool_id: COMMAND_EXECUTE.into(),
                arguments_json: json!({"command": "printf runner-ok"}).to_string(),
                catalog_version: Some(CATALOG_VERSION.into()),
            })
            .await;
        let Some(call_tool_response::Result::Outcome(command_outcome)) = command.result else {
            panic!("expected command outcome");
        };
        assert!(!command_outcome.is_error);
        let Some(cyrene_plugin_contracts::tool_provider_v1::tool_content_part::Content::Json(json)) =
            command_outcome.content[0].content.as_ref()
        else {
            panic!("expected JSON content");
        };
        assert_eq!(
            serde_json::from_str::<Value>(&json.json).unwrap()["stdout"],
            "runner-ok"
        );
    }

    #[tokio::test]
    async fn unknown_bindings_tools_and_escaped_paths_fail_closed() {
        let workspace = tempdir().unwrap();
        let provider = provider(workspace.path());
        let missing_read_length = provider
            .call_tool(&CallToolRequest {
                binding_id: COMPUTER_BINDING_ID.into(),
                provider_tool_id: FILE_READ.into(),
                arguments_json: json!({"path": "note.txt"}).to_string(),
                catalog_version: None,
            })
            .await;
        assert!(matches!(
            missing_read_length.result,
            Some(call_tool_response::Result::Error(_))
        ));

        let oversized_read = provider
            .call_tool(&CallToolRequest {
                binding_id: COMPUTER_BINDING_ID.into(),
                provider_tool_id: FILE_READ.into(),
                arguments_json: json!({"path": "note.txt", "length": 65537}).to_string(),
                catalog_version: None,
            })
            .await;
        assert!(matches!(
            oversized_read.result,
            Some(call_tool_response::Result::Error(_))
        ));

        let escaped = provider
            .call_tool(&CallToolRequest {
                binding_id: COMPUTER_BINDING_ID.into(),
                provider_tool_id: FILE_READ.into(),
                arguments_json: json!({"path": "../secret"}).to_string(),
                catalog_version: None,
            })
            .await;
        assert!(matches!(
            escaped.result,
            Some(call_tool_response::Result::Error(_))
        ));

        let long_path = "x".repeat(MAX_WORKSPACE_PATH_BYTES + 1);
        let oversized_path = provider
            .call_tool(&CallToolRequest {
                binding_id: COMPUTER_BINDING_ID.into(),
                provider_tool_id: FILE_READ.into(),
                arguments_json: json!({"path": long_path, "length": 0}).to_string(),
                catalog_version: None,
            })
            .await;
        assert!(matches!(
            oversized_path.result,
            Some(call_tool_response::Result::Error(_))
        ));

        let long_command = "x".repeat(MAX_COMMAND_BYTES + 1);
        let oversized_command = provider
            .call_tool(&CallToolRequest {
                binding_id: COMPUTER_BINDING_ID.into(),
                provider_tool_id: COMMAND_EXECUTE.into(),
                arguments_json: json!({"command": long_command}).to_string(),
                catalog_version: None,
            })
            .await;
        assert!(matches!(
            oversized_command.result,
            Some(call_tool_response::Result::Error(_))
        ));

        let unknown = provider
            .call_tool(&CallToolRequest {
                binding_id: COMPUTER_BINDING_ID.into(),
                provider_tool_id: "computer.network.fetch".into(),
                arguments_json: "{}".into(),
                catalog_version: None,
            })
            .await;
        assert!(matches!(
            unknown.result,
            Some(call_tool_response::Result::Error(_))
        ));

        assert!(matches!(
            provider.list_tools(Some("another-binding")).result,
            Some(list_tools_response::Result::Error(_))
        ));
    }

    #[tokio::test]
    async fn file_read_rejects_offsets_above_the_json_safe_integer_bound() {
        let workspace = tempdir().unwrap();
        let provider = provider(workspace.path());
        fs::write(workspace.path().join("note.txt"), "content").unwrap();

        let response = provider
            .call_tool(&CallToolRequest {
                binding_id: COMPUTER_BINDING_ID.into(),
                provider_tool_id: FILE_READ.into(),
                arguments_json: json!({
                    "path": "note.txt",
                    "offset": MAX_TOOL_READ_OFFSET + 1,
                    "length": 0
                })
                .to_string(),
                catalog_version: Some(CATALOG_VERSION.into()),
            })
            .await;

        let Some(call_tool_response::Result::Error(error)) = response.result else {
            panic!("expected out-of-range offset to fail as invalid arguments");
        };
        assert_eq!(error.code, ToolProviderErrorCode::InvalidArguments as i32);
        assert!(error.message.contains(&MAX_TOOL_READ_OFFSET.to_string()));
    }
}
