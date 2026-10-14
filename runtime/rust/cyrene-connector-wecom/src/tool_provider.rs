// SPDX-License-Identifier: Apache-2.0
//! Native Tool Provider for Enterprise WeChat (@wecom/cli) in Rust.

use serde_json::Value;
use std::sync::Arc;

use cyrene_plugin_contracts::tool_provider_v1::{
    call_tool_response, list_tools_response, CallToolRequest, CallToolResponse, ListToolsRequest,
    ListToolsResponse, ToolCallOutcome, ToolCatalog, ToolContentPart, ToolDescriptor,
    ToolProviderError, ToolProviderErrorCode, ToolTextContent,
};

use crate::cli::WeComCliClient;
use crate::error::WeComError;

pub const BINDING_ID: &str = "wecom.cli";

#[derive(Debug, Clone)]
pub struct WeComToolProvider {
    cli: Arc<WeComCliClient>,
}

impl WeComToolProvider {
    pub fn new(cli: Arc<WeComCliClient>) -> Self {
        Self { cli }
    }

    pub fn list_tools(&self, _req: &ListToolsRequest) -> ListToolsResponse {
        let tools = vec![
            ToolDescriptor {
                binding_id: BINDING_ID.to_string(),
                provider_tool_id: "wecom_auth_status".to_string(),
                display_name: "WeCom Auth Status Probe".to_string(),
                description: "Probes authorization status of local WeCom CLI toolchain".to_string(),
                input_schema_json: r#"{"type":"object","properties":{}}"#.to_string(),
                output_schema_json: None,
            },
            ToolDescriptor {
                binding_id: BINDING_ID.to_string(),
                provider_tool_id: "wecom_contact_search".to_string(),
                display_name: "Search WeCom Contacts".to_string(),
                description: "Searches enterprise contacts by keywords (name, alias, pinyin)".to_string(),
                input_schema_json: r#"{"type":"object","properties":{"keywords":{"type":"array","items":{"type":"string"}}},"required":["keywords"]}"#.to_string(),
                output_schema_json: None,
            },
            ToolDescriptor {
                binding_id: BINDING_ID.to_string(),
                provider_tool_id: "wecom_calendar_list".to_string(),
                display_name: "List WeCom Calendars".to_string(),
                description: "Lists schedules and calendar events".to_string(),
                input_schema_json: r#"{"type":"object","properties":{"limit":{"type":"integer"}}}"#.to_string(),
                output_schema_json: None,
            },
            ToolDescriptor {
                binding_id: BINDING_ID.to_string(),
                provider_tool_id: "wecom_calendar_create".to_string(),
                display_name: "Create WeCom Calendar Schedule".to_string(),
                description: "Creates a new calendar schedule for WeCom user".to_string(),
                input_schema_json: r#"{"type":"object","properties":{"summary":{"type":"string"},"start_time":{"type":"string"},"end_time":{"type":"string"}},"required":["summary"]}"#.to_string(),
                output_schema_json: None,
            },
            ToolDescriptor {
                binding_id: BINDING_ID.to_string(),
                provider_tool_id: "wecom_todo_list".to_string(),
                display_name: "List WeCom Todos".to_string(),
                description: "Lists active or finished todo items".to_string(),
                input_schema_json: r#"{"type":"object","properties":{"limit":{"type":"integer"}}}"#.to_string(),
                output_schema_json: None,
            },
            ToolDescriptor {
                binding_id: BINDING_ID.to_string(),
                provider_tool_id: "wecom_todo_create".to_string(),
                display_name: "Create WeCom Todo".to_string(),
                description: "Creates a new task or todo item in WeCom".to_string(),
                input_schema_json: r#"{"type":"object","properties":{"title":{"type":"string"}},"required":["title"]}"#.to_string(),
                output_schema_json: None,
            },
            ToolDescriptor {
                binding_id: BINDING_ID.to_string(),
                provider_tool_id: "wecom_doc_search".to_string(),
                display_name: "Search WeCom Docs".to_string(),
                description: "Searches documents and knowledge bases in WeCom".to_string(),
                input_schema_json: r#"{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}"#.to_string(),
                output_schema_json: None,
            },
            ToolDescriptor {
                binding_id: BINDING_ID.to_string(),
                provider_tool_id: "wecom_cli_exec".to_string(),
                display_name: "Execute WeCom CLI Command".to_string(),
                description: "Executes an authorized WeCom CLI subcommand".to_string(),
                input_schema_json: r#"{"type":"object","properties":{"args":{"type":"array","items":{"type":"string"}}},"required":["args"]}"#.to_string(),
                output_schema_json: None,
            },
        ];

        ListToolsResponse {
            result: Some(list_tools_response::Result::Catalog(ToolCatalog {
                catalog_version: "1.0.0".to_string(),
                tools,
            })),
        }
    }

    pub async fn call_tool(&self, req: &CallToolRequest) -> CallToolResponse {
        let tool_id = req.provider_tool_id.as_str();
        let args: Value = if req.arguments_json.trim().is_empty() {
            Value::Object(serde_json::Map::new())
        } else {
            match serde_json::from_str(&req.arguments_json) {
                Ok(v) => v,
                Err(e) => {
                    return CallToolResponse {
                        result: Some(call_tool_response::Result::Error(ToolProviderError {
                            code: ToolProviderErrorCode::InvalidArguments as i32,
                            message: format!("Malformed arguments JSON: {e}"),
                            retryable: false,
                        })),
                    };
                }
            }
        };

        let result = match tool_id {
            "wecom_auth_status" => self.handle_auth_status().await,
            "wecom_contact_search" => self.handle_contact_search(&args).await,
            "wecom_calendar_list" => self.handle_calendar_list(&args).await,
            "wecom_calendar_create" => self.handle_calendar_create(&args).await,
            "wecom_todo_list" => self.handle_todo_list(&args).await,
            "wecom_todo_create" => self.handle_todo_create(&args).await,
            "wecom_doc_search" => self.handle_doc_search(&args).await,
            "wecom_cli_exec" => self.handle_cli_exec(&args).await,
            _ => {
                return CallToolResponse {
                    result: Some(call_tool_response::Result::Error(ToolProviderError {
                        code: ToolProviderErrorCode::ToolNotFound as i32,
                        message: format!("Unknown tool id: {tool_id}"),
                        retryable: false,
                    })),
                };
            }
        };

        match result {
            Ok(output) => CallToolResponse {
                result: Some(call_tool_response::Result::Outcome(ToolCallOutcome {
                    content: vec![ToolContentPart {
                        content: Some(cyrene_plugin_contracts::tool_provider_v1::tool_content_part::Content::Text(
                            ToolTextContent { text: output },
                        )),
                    }],
                    is_error: false,
                })),
            },
            Err(e) => CallToolResponse {
                result: Some(call_tool_response::Result::Error(ToolProviderError {
                    code: ToolProviderErrorCode::ExecutionFailed as i32,
                    message: e.to_string(),
                    retryable: false,
                })),
            },
        }
    }

    async fn handle_auth_status(&self) -> Result<String, WeComError> {
        let is_auth = self.cli.check_auth_status().await?;
        let ver = self
            .cli
            .check_version()
            .await
            .unwrap_or_else(|_| "unknown".to_string());
        let res = serde_json::json!({
            "authorized": is_auth,
            "version": ver,
            "binary_path": self.cli.binary_path().to_string_lossy(),
        });
        Ok(res.to_string())
    }

    async fn handle_contact_search(&self, args: &Value) -> Result<String, WeComError> {
        let mut cmd_args = vec!["contact", "users", "search"];
        let mut keyword_strings = Vec::new();

        if let Some(arr) = args.get("keywords").and_then(Value::as_array) {
            for k in arr {
                if let Some(s) = k.as_str() {
                    keyword_strings.push(s.to_string());
                }
            }
        }

        if !keyword_strings.is_empty() {
            cmd_args.push("--keywords");
            for k in &keyword_strings {
                cmd_args.push(k.as_str());
            }
        }

        self.cli.run_args(&cmd_args).await
    }

    async fn handle_calendar_list(&self, args: &Value) -> Result<String, WeComError> {
        let mut cmd_args = vec!["calendar", "schedules", "list"];
        let limit_str;
        if let Some(limit) = args.get("limit").and_then(Value::as_i64) {
            cmd_args.push("--page-count");
            limit_str = limit.to_string();
            cmd_args.push(&limit_str);
        }
        self.cli.run_args(&cmd_args).await
    }

    async fn handle_calendar_create(&self, args: &Value) -> Result<String, WeComError> {
        let summary = args.get("summary").and_then(Value::as_str).ok_or_else(|| {
            WeComError::InvalidRequest("Missing required argument 'summary'".to_string())
        })?;

        let json_body = serde_json::json!({
            "schedule": {
                "summary": summary
            }
        });
        let json_str = json_body.to_string();
        let cmd_args = vec!["calendar", "schedules", "create", "--json", &json_str];
        self.cli.run_args(&cmd_args).await
    }

    async fn handle_todo_list(&self, _args: &Value) -> Result<String, WeComError> {
        self.cli.run_args(&["todo", "tasks", "list"]).await
    }

    async fn handle_todo_create(&self, args: &Value) -> Result<String, WeComError> {
        let title = args.get("title").and_then(Value::as_str).ok_or_else(|| {
            WeComError::InvalidRequest("Missing required argument 'title'".to_string())
        })?;

        let json_body = serde_json::json!({
            "task": {
                "title": title
            }
        });
        let json_str = json_body.to_string();
        let cmd_args = vec!["todo", "tasks", "create", "--json", &json_str];
        self.cli.run_args(&cmd_args).await
    }

    async fn handle_doc_search(&self, args: &Value) -> Result<String, WeComError> {
        let query = args.get("query").and_then(Value::as_str).ok_or_else(|| {
            WeComError::InvalidRequest("Missing required argument 'query'".to_string())
        })?;

        let cmd_args = vec!["doc", "documents", "search", "--query", query];
        self.cli.run_args(&cmd_args).await
    }

    async fn handle_cli_exec(&self, args: &Value) -> Result<String, WeComError> {
        let arr = args.get("args").and_then(Value::as_array).ok_or_else(|| {
            WeComError::InvalidRequest("Missing required 'args' array".to_string())
        })?;

        let mut str_args = Vec::new();
        for item in arr {
            if let Some(s) = item.as_str() {
                str_args.push(s);
            }
        }

        if str_args.is_empty() {
            return Err(WeComError::InvalidRequest("Empty args array".to_string()));
        }

        // Whitelist validation
        let root = str_args[0];
        let allowed = [
            "contact",
            "calendar",
            "todo",
            "doc",
            "meeting",
            "disk",
            "sheet",
            "smartpage",
            "mail",
            "media",
            "auth",
        ];
        if !allowed.contains(&root) {
            return Err(WeComError::InvalidRequest(format!(
                "Subcommand '{root}' not in allowed whitelist"
            )));
        }

        self.cli.run_args(&str_args).await
    }
}
