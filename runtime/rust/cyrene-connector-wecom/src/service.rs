// SPDX-License-Identifier: Apache-2.0
//! DirectPluginRuntime gRPC service implementation for WeCom in Rust.

use prost::Message;
use std::sync::Arc;
use tonic::Status;
use tracing::debug;

use cyrene_plugin_contracts::direct_plugin_runtime_v1::{
    direct_invocation_error::Code as ErrorCode,
    direct_invocation_response::Result as InvocationResult, DirectInvocationError,
    DirectInvocationRequest, DirectInvocationResponse, DirectPayload,
};
use cyrene_plugin_contracts::message_connector::{
    CAPABILITY_ID as MESSAGE_CONNECTOR_CAPABILITY, DELIVERY_RESULT_TYPE_URL, SEND_MESSAGE_METHOD,
};
use cyrene_plugin_contracts::message_connector_v1::{
    message_content_part, DeliveryResult, DeliveryStatus, SendMessageRequest,
};
use cyrene_plugin_contracts::tool_provider::{
    CALL_TOOL_RESPONSE_TYPE_URL, CAPABILITY_ID as TOOL_PROVIDER_CAPABILITY,
    LIST_TOOLS_RESPONSE_TYPE_URL, METHOD_CALL_TOOL, METHOD_LIST_TOOLS,
};
use cyrene_plugin_contracts::tool_provider_v1::{CallToolRequest, ListToolsRequest};

use crate::app::WeComAppClient;
use crate::bot::WeComBotClient;
use crate::config::{ConnectorMode, WeComConfig};
use crate::tool_provider::WeComToolProvider;

#[derive(Clone)]
pub struct WeComService {
    config: WeComConfig,
    app_client: Option<Arc<WeComAppClient>>,
    bot_client: Option<Arc<WeComBotClient>>,
    tool_provider: Option<Arc<WeComToolProvider>>,
}

impl WeComService {
    pub fn new(
        config: WeComConfig,
        app_client: Option<Arc<WeComAppClient>>,
        bot_client: Option<Arc<WeComBotClient>>,
        tool_provider: Option<Arc<WeComToolProvider>>,
    ) -> Self {
        Self {
            config,
            app_client,
            bot_client,
            tool_provider,
        }
    }

    pub async fn handle_invoke(
        &self,
        req: DirectInvocationRequest,
    ) -> Result<DirectInvocationResponse, Status> {
        let cap = req.capability.as_str();
        let method = req.method.as_str();

        debug!(
            "Handling DirectInvocationRequest: capability={}, method={}, req_id={}",
            cap, method, req.request_id
        );

        match (cap, method) {
            (MESSAGE_CONNECTOR_CAPABILITY, SEND_MESSAGE_METHOD) => {
                self.handle_send_message(&req.payload).await
            }
            (TOOL_PROVIDER_CAPABILITY, METHOD_LIST_TOOLS) => {
                self.handle_list_tools(&req.payload).await
            }
            (TOOL_PROVIDER_CAPABILITY, METHOD_CALL_TOOL) => {
                self.handle_call_tool(&req.payload).await
            }
            _ => Ok(DirectInvocationResponse {
                result: Some(InvocationResult::Error(DirectInvocationError {
                    code: ErrorCode::MethodNotFound as i32,
                    message: format!("Unsupported capability '{cap}' or method '{method}'"),
                    retryable: false,
                    domain_code: "METHOD_NOT_FOUND".to_string(),
                })),
            }),
        }
    }

    async fn handle_send_message(
        &self,
        payload_bytes: &[u8],
    ) -> Result<DirectInvocationResponse, Status> {
        let send_req = match SendMessageRequest::decode(payload_bytes) {
            Ok(r) => r,
            Err(e) => {
                return Ok(DirectInvocationResponse {
                    result: Some(InvocationResult::Error(DirectInvocationError {
                        code: ErrorCode::InvalidRequest as i32,
                        message: format!("Failed to decode SendMessageRequest: {e}"),
                        retryable: false,
                        domain_code: "INVALID_REQUEST".to_string(),
                    })),
                });
            }
        };

        let conv = send_req.conversation.as_ref().ok_or_else(|| {
            Status::invalid_argument("Missing conversation in SendMessageRequest")
        })?;

        // Extract message content text
        let mut text_parts = Vec::new();
        for part in &send_req.content {
            if let Some(message_content_part::Kind::Text(ref t)) = part.kind {
                text_parts.push(t.text.as_str());
            }
        }
        let full_text = text_parts.join("\n");

        let delivery_result = match self.config.mode {
            ConnectorMode::Bot => {
                let bot = self
                    .bot_client
                    .as_ref()
                    .ok_or_else(|| Status::failed_precondition("Bot client not initialized"))?;

                match bot
                    .send_message(&conv.conversation_id, &full_text, false)
                    .await
                {
                    Ok(_) => DeliveryResult {
                        status: DeliveryStatus::Accepted as i32,
                        vendor_message_id: format!(
                            "bot-{}",
                            std::time::SystemTime::now()
                                .elapsed()
                                .unwrap_or_default()
                                .as_millis()
                        ),
                        reason: "Accepted by WeCom Bot WebSocket gateway".to_string(),
                        retry_after: None,
                        vendor_extension: None,
                    },
                    Err(e) => DeliveryResult {
                        status: DeliveryStatus::Rejected as i32,
                        vendor_message_id: String::new(),
                        reason: e.to_string(),
                        retry_after: None,
                        vendor_extension: None,
                    },
                }
            }
            ConnectorMode::App | ConnectorMode::Hybrid => {
                let app = self
                    .app_client
                    .as_ref()
                    .ok_or_else(|| Status::failed_precondition("App client not initialized"))?;

                let to_user = if conv.kind == 1 {
                    // Private
                    Some(conv.conversation_id.as_str())
                } else {
                    None
                };
                let to_party = if conv.kind == 2 {
                    // Group / Party
                    Some(conv.conversation_id.as_str())
                } else {
                    None
                };

                match app
                    .send_text_message(to_user, to_party, None, &full_text, false)
                    .await
                {
                    Ok(msg_id) => DeliveryResult {
                        status: DeliveryStatus::Accepted as i32,
                        vendor_message_id: msg_id,
                        reason: "Accepted by WeCom App REST API".to_string(),
                        retry_after: None,
                        vendor_extension: None,
                    },
                    Err(e) => DeliveryResult {
                        status: DeliveryStatus::Rejected as i32,
                        vendor_message_id: String::new(),
                        reason: e.to_string(),
                        retry_after: None,
                        vendor_extension: None,
                    },
                }
            }
        };

        let mut out_bytes = Vec::new();
        delivery_result
            .encode(&mut out_bytes)
            .map_err(|e| Status::internal(format!("Failed to encode DeliveryResult: {e}")))?;

        Ok(DirectInvocationResponse {
            result: Some(InvocationResult::Payload(DirectPayload {
                type_url: DELIVERY_RESULT_TYPE_URL.to_string(),
                value: out_bytes,
                event_type: String::new(),
            })),
        })
    }

    async fn handle_list_tools(
        &self,
        payload_bytes: &[u8],
    ) -> Result<DirectInvocationResponse, Status> {
        let tp = match self.tool_provider.as_ref() {
            Some(t) => t,
            None => {
                return Ok(DirectInvocationResponse {
                    result: Some(InvocationResult::Error(DirectInvocationError {
                        code: ErrorCode::ExecutionFailed as i32,
                        message: "Tool provider is not enabled or available".to_string(),
                        retryable: false,
                        domain_code: "TOOLS_DISABLED".to_string(),
                    })),
                });
            }
        };

        let req = ListToolsRequest::decode(payload_bytes).unwrap_or_default();
        let resp = tp.list_tools(&req);

        let mut out_bytes = Vec::new();
        resp.encode(&mut out_bytes)
            .map_err(|e| Status::internal(format!("Failed to encode ListToolsResponse: {e}")))?;

        Ok(DirectInvocationResponse {
            result: Some(InvocationResult::Payload(DirectPayload {
                type_url: LIST_TOOLS_RESPONSE_TYPE_URL.to_string(),
                value: out_bytes,
                event_type: String::new(),
            })),
        })
    }

    async fn handle_call_tool(
        &self,
        payload_bytes: &[u8],
    ) -> Result<DirectInvocationResponse, Status> {
        let tp = match self.tool_provider.as_ref() {
            Some(t) => t,
            None => {
                return Ok(DirectInvocationResponse {
                    result: Some(InvocationResult::Error(DirectInvocationError {
                        code: ErrorCode::ExecutionFailed as i32,
                        message: "Tool provider is not enabled or available".to_string(),
                        retryable: false,
                        domain_code: "TOOLS_DISABLED".to_string(),
                    })),
                });
            }
        };

        let req = match CallToolRequest::decode(payload_bytes) {
            Ok(r) => r,
            Err(e) => {
                return Ok(DirectInvocationResponse {
                    result: Some(InvocationResult::Error(DirectInvocationError {
                        code: ErrorCode::InvalidRequest as i32,
                        message: format!("Failed to decode CallToolRequest: {e}"),
                        retryable: false,
                        domain_code: "INVALID_REQUEST".to_string(),
                    })),
                });
            }
        };

        let resp = tp.call_tool(&req).await;
        let mut out_bytes = Vec::new();
        resp.encode(&mut out_bytes)
            .map_err(|e| Status::internal(format!("Failed to encode CallToolResponse: {e}")))?;

        Ok(DirectInvocationResponse {
            result: Some(InvocationResult::Payload(DirectPayload {
                type_url: CALL_TOOL_RESPONSE_TYPE_URL.to_string(),
                value: out_bytes,
                event_type: String::new(),
            })),
        })
    }
}
