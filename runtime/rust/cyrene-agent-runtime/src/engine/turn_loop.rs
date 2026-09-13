// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 turn_loop.rs                                                    │
// │  Package: cyrene-agent-runtime::engine                              │
// │  Role: Stateless agent reasoning turn loop (T58, T59, T64).         │
// │                                                                     │
// │  模块职责：Cyrene-native 生产级轻量 Rust 智能体循环：                  │
// │           1. 无状态单次调用执行                                     │
// │           2. 严格单调递增有序流式事件 (sequence_number >= 1)        │
// │           3. 工具执行循环 (Tool Loop) 与并发上限控制                 │
// │           4. 最大轮次限制 (max_turns) 与超时截止控制                 │
// │           5. 协作式取消与终态事件保证                               │
// └─────────────────────────────────────────────────────────────────────┘

use std::sync::atomic::{AtomicI64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::mpsc;

use cyrene_plugin_contracts::agent_runtime_v1::{
    agent_run_response, agent_stream_event, AgentErrorCode, AgentRunError, AgentRunRequest,
    AgentRunResponse, AgentRunSuccess, AgentStreamEvent, ContentDeltaEvent, RunCompletedEvent,
    RunFailedEvent, RunStartedEvent, ToolCall, ToolCallCompletedEvent, ToolCallStartedEvent,
    UsageStats,
};
use cyrene_plugin_contracts::model_provider_v1::{
    chat_message, ChatCompletionRequest, ChatMessage as ModelChatMessage, ChatToolCall,
    ChatToolCallFunction,
};

use crate::adapter::model_provider::ModelProvider;
use crate::adapter::tool_provider::ToolProvider;
use crate::engine::cancel::CancellationToken;
use crate::limits::RuntimeLimits;

#[derive(Default)]
pub struct CyreneNativeAgentLoop {}

impl CyreneNativeAgentLoop {
    pub fn new() -> Self {
        Self {}
    }

    fn current_timestamp_ms() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or(Duration::from_secs(0))
            .as_millis() as i64
    }

    pub async fn execute_run(
        &self,
        request: AgentRunRequest,
        model: &(dyn ModelProvider + 'static),
        tools: Option<&(dyn ToolProvider + 'static)>,
        cancel: CancellationToken,
    ) -> Result<AgentRunResponse, AgentRunError> {
        let limits = RuntimeLimits::from_config(request.config.as_ref())?;
        let start_time = Instant::now();

        let timeout_cancel = if let Some(t_ms) = limits.timeout_ms {
            CancellationToken::with_timeout(Duration::from_millis(t_ms))
        } else {
            cancel.clone()
        };

        let mut turn_count = 0;
        let mut model_call_count = 0;
        let mut tool_call_count = 0;
        let mut prompt_tokens = 0;
        let mut completion_tokens = 0;

        // Working message history for this stateless invocation
        let mut conversation_messages: Vec<ModelChatMessage> = Vec::new();

        // 1. Initial Prompt & messages from request
        if !request.prompt.is_empty() {
            conversation_messages.push(ModelChatMessage {
                role: chat_message::Role::User as i32,
                content: request.prompt.clone(),
                name: None,
                tool_calls: Vec::new(),
                tool_call_id: None,
            });
        }

        for m in &request.messages {
            let role_enum = match m.role.to_lowercase().as_str() {
                "system" => chat_message::Role::System,
                "assistant" => chat_message::Role::Assistant,
                "tool" => chat_message::Role::Tool,
                _ => chat_message::Role::User,
            };
            conversation_messages.push(ModelChatMessage {
                role: role_enum as i32,
                content: m.content.clone(),
                name: m.name.clone(),
                tool_calls: Vec::new(),
                tool_call_id: m.tool_call_id.clone(),
            });
        }

        loop {
            // Check cancellation / deadline
            if cancel.is_cancelled() || timeout_cancel.is_cancelled() {
                let code = if timeout_cancel.is_deadline_expired() {
                    AgentErrorCode::DeadlineExceeded
                } else {
                    AgentErrorCode::Cancelled
                };
                return Ok(AgentRunResponse {
                    result: Some(agent_run_response::Result::Error(AgentRunError {
                        code: code as i32,
                        message: "Agent run was cancelled or timed out".to_string(),
                        retryable: false,
                        domain_details: "turn_loop.interrupted".to_string(),
                    })),
                });
            }

            // Check maximum turns limit (T59, T64)
            if turn_count >= limits.max_turns {
                return Ok(AgentRunResponse {
                    result: Some(agent_run_response::Result::Error(AgentRunError {
                        code: AgentErrorCode::TurnLimitExceeded as i32,
                        message: format!(
                            "Execution stopped: maximum turns limit ({}) reached",
                            limits.max_turns
                        ),
                        retryable: false,
                        domain_details: "turn_loop.turn_limit".to_string(),
                    })),
                });
            }

            turn_count += 1;
            model_call_count += 1;

            // Prepare completion request to model.provider.v1 (T60)
            let completion_req = ChatCompletionRequest {
                messages: conversation_messages.clone(),
                model: request
                    .config
                    .as_ref()
                    .and_then(|c| c.model_selector.clone()),
                stream: false,
                temperature: request
                    .config
                    .as_ref()
                    .and_then(|c| c.temperature.map(|t| t as f64)),
                max_tokens: None,
                tools: Vec::new(),
                tool_choice: None,
                parallel_tool_calls: None,
                include_usage: Some(true),
            };

            let completion_resp = match model.complete(completion_req).await {
                Ok(resp) => resp,
                Err(err) => {
                    return Ok(AgentRunResponse {
                        result: Some(agent_run_response::Result::Error(err)),
                    });
                }
            };

            let first_chunk = match completion_resp.chunks.into_iter().next() {
                Some(c) => c,
                None => {
                    return Ok(AgentRunResponse {
                        result: Some(agent_run_response::Result::Error(AgentRunError {
                            code: AgentErrorCode::ModelUnavailable as i32,
                            message: "Model returned empty completion chunks".to_string(),
                            retryable: true,
                            domain_details: "model.chunks_empty".to_string(),
                        })),
                    });
                }
            };

            if let Some(pt) = first_chunk.prompt_tokens {
                prompt_tokens += pt as i32;
            }
            if let Some(ct) = first_chunk.completion_tokens {
                completion_tokens += ct as i32;
            }

            let delta_text = first_chunk.delta;
            let finish_reason = first_chunk
                .finish_reason
                .unwrap_or_else(|| "stop".to_string());

            // Check if model requested tool calls
            if !first_chunk.tool_calls.is_empty() {
                let tool_provider = match tools {
                    Some(tp) => tp,
                    None => {
                        return Ok(AgentRunResponse {
                            result: Some(agent_run_response::Result::Error(AgentRunError {
                                code: AgentErrorCode::ToolExecutionFailed as i32,
                                message:
                                    "Model requested tool calls but no ToolProvider was configured"
                                        .to_string(),
                                retryable: false,
                                domain_details: "tools.missing_provider".to_string(),
                            })),
                        });
                    }
                };

                // Build Assistant message with tool calls
                let model_tool_calls: Vec<ChatToolCall> = first_chunk
                    .tool_calls
                    .iter()
                    .map(|tc| ChatToolCall {
                        id: tc.id.clone().unwrap_or_default(),
                        r#type: tc.r#type.clone().unwrap_or_else(|| "function".to_string()),
                        function: Some(ChatToolCallFunction {
                            name: tc.function_name.clone().unwrap_or_default(),
                            arguments: tc.function_arguments.clone().unwrap_or_default(),
                        }),
                    })
                    .collect();

                conversation_messages.push(ModelChatMessage {
                    role: chat_message::Role::Assistant as i32,
                    content: delta_text.clone(),
                    name: None,
                    tool_calls: model_tool_calls,
                    tool_call_id: None,
                });

                // Execute tools (respecting concurrency limit, T64)
                let tool_calls_slice = if first_chunk.tool_calls.len() > limits.max_tool_concurrency
                {
                    &first_chunk.tool_calls[..limits.max_tool_concurrency]
                } else {
                    &first_chunk.tool_calls[..]
                };

                for tc in tool_calls_slice {
                    let tool_call = ToolCall {
                        call_id: tc.id.clone().unwrap_or_default(),
                        tool_name: tc.function_name.clone().unwrap_or_default(),
                        arguments_json: tc.function_arguments.clone().unwrap_or_default(),
                    };

                    tool_call_count += 1;
                    let result = match tool_provider.execute(&tool_call).await {
                        Ok(res) => res,
                        Err(err) => {
                            return Ok(AgentRunResponse {
                                result: Some(agent_run_response::Result::Error(err)),
                            });
                        }
                    };

                    // Feed tool result back into conversation history
                    conversation_messages.push(ModelChatMessage {
                        role: chat_message::Role::Tool as i32,
                        content: result.output_json,
                        name: Some(result.tool_name),
                        tool_calls: Vec::new(),
                        tool_call_id: Some(result.call_id),
                    });
                }

                // Continue loop with updated history
                continue;
            }

            // Model completed with text response
            let duration_ms = start_time.elapsed().as_millis() as i64;
            return Ok(AgentRunResponse {
                result: Some(agent_run_response::Result::Success(AgentRunSuccess {
                    run_id: request.run_id.clone(),
                    output_text: delta_text,
                    total_turns: turn_count,
                    finish_reason,
                    usage: Some(UsageStats {
                        prompt_tokens,
                        completion_tokens,
                        total_tokens: prompt_tokens + completion_tokens,
                        model_call_count,
                        tool_call_count,
                        execution_duration_ms: duration_ms,
                    }),
                })),
            });
        }
    }

    pub async fn execute_stream(
        &self,
        request: AgentRunRequest,
        model: &(dyn ModelProvider + 'static),
        tools: Option<&(dyn ToolProvider + 'static)>,
        cancel: CancellationToken,
        sender: mpsc::Sender<AgentStreamEvent>,
    ) -> Result<(), AgentRunError> {
        let limits = RuntimeLimits::from_config(request.config.as_ref())?;
        let seq = AtomicI64::new(1);
        let start_time = Instant::now();

        let timeout_cancel = if let Some(t_ms) = limits.timeout_ms {
            CancellationToken::with_timeout(Duration::from_millis(t_ms))
        } else {
            cancel.clone()
        };

        // 1. Emit RunStartedEvent (seq = 1)
        let resolved_model = request
            .config
            .as_ref()
            .and_then(|c| c.model_selector.clone())
            .unwrap_or_else(|| "default-model".to_string());

        let _ = sender
            .send(AgentStreamEvent {
                sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                timestamp_ms: Self::current_timestamp_ms(),
                event: Some(agent_stream_event::Event::RunStarted(RunStartedEvent {
                    run_id: request.run_id.clone(),
                    resolved_model,
                })),
            })
            .await;

        let mut turn_count = 0;
        let mut model_call_count = 0;
        let mut tool_call_count = 0;
        let mut prompt_tokens = 0;
        let mut completion_tokens = 0;
        let mut accumulated_output = String::new();

        let mut conversation_messages: Vec<ModelChatMessage> = Vec::new();
        if !request.prompt.is_empty() {
            conversation_messages.push(ModelChatMessage {
                role: chat_message::Role::User as i32,
                content: request.prompt.clone(),
                name: None,
                tool_calls: Vec::new(),
                tool_call_id: None,
            });
        }

        loop {
            // Check cancellation / timeout
            if cancel.is_cancelled() || timeout_cancel.is_cancelled() {
                let code = if timeout_cancel.is_deadline_expired() {
                    AgentErrorCode::DeadlineExceeded
                } else {
                    AgentErrorCode::Cancelled
                };
                let _ = sender
                    .send(AgentStreamEvent {
                        sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                        timestamp_ms: Self::current_timestamp_ms(),
                        event: Some(agent_stream_event::Event::RunFailed(RunFailedEvent {
                            code: code as i32,
                            message: "Agent stream run was cancelled or timed out".to_string(),
                            retryable: false,
                        })),
                    })
                    .await;
                return Ok(());
            }

            // Check maximum turns limit (T59, T64)
            if turn_count >= limits.max_turns {
                let _ = sender
                    .send(AgentStreamEvent {
                        sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                        timestamp_ms: Self::current_timestamp_ms(),
                        event: Some(agent_stream_event::Event::RunFailed(RunFailedEvent {
                            code: AgentErrorCode::TurnLimitExceeded as i32,
                            message: format!("Turn limit of {} exceeded", limits.max_turns),
                            retryable: false,
                        })),
                    })
                    .await;
                return Ok(());
            }

            turn_count += 1;
            model_call_count += 1;

            let completion_req = ChatCompletionRequest {
                messages: conversation_messages.clone(),
                model: request
                    .config
                    .as_ref()
                    .and_then(|c| c.model_selector.clone()),
                stream: false,
                temperature: request
                    .config
                    .as_ref()
                    .and_then(|c| c.temperature.map(|t| t as f64)),
                max_tokens: None,
                tools: Vec::new(),
                tool_choice: None,
                parallel_tool_calls: None,
                include_usage: Some(true),
            };

            let completion_resp = match model.complete(completion_req).await {
                Ok(resp) => resp,
                Err(err) => {
                    let _ = sender
                        .send(AgentStreamEvent {
                            sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                            timestamp_ms: Self::current_timestamp_ms(),
                            event: Some(agent_stream_event::Event::RunFailed(RunFailedEvent {
                                code: err.code,
                                message: err.message,
                                retryable: err.retryable,
                            })),
                        })
                        .await;
                    return Ok(());
                }
            };

            let first_chunk = match completion_resp.chunks.into_iter().next() {
                Some(c) => c,
                None => {
                    let _ = sender
                        .send(AgentStreamEvent {
                            sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                            timestamp_ms: Self::current_timestamp_ms(),
                            event: Some(agent_stream_event::Event::RunFailed(RunFailedEvent {
                                code: AgentErrorCode::ModelUnavailable as i32,
                                message: "Model chunks empty".to_string(),
                                retryable: true,
                            })),
                        })
                        .await;
                    return Ok(());
                }
            };

            if let Some(pt) = first_chunk.prompt_tokens {
                prompt_tokens += pt as i32;
            }
            if let Some(ct) = first_chunk.completion_tokens {
                completion_tokens += ct as i32;
            }

            let delta_text = first_chunk.delta;
            let finish_reason = first_chunk
                .finish_reason
                .unwrap_or_else(|| "stop".to_string());

            if !first_chunk.tool_calls.is_empty() {
                let tool_provider = match tools {
                    Some(tp) => tp,
                    None => {
                        let _ = sender
                            .send(AgentStreamEvent {
                                sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                                timestamp_ms: Self::current_timestamp_ms(),
                                event: Some(agent_stream_event::Event::RunFailed(RunFailedEvent {
                                    code: AgentErrorCode::ToolExecutionFailed as i32,
                                    message: "Missing tool provider for tool calls".to_string(),
                                    retryable: false,
                                })),
                            })
                            .await;
                        return Ok(());
                    }
                };

                let model_tool_calls: Vec<ChatToolCall> = first_chunk
                    .tool_calls
                    .iter()
                    .map(|tc| ChatToolCall {
                        id: tc.id.clone().unwrap_or_default(),
                        r#type: tc.r#type.clone().unwrap_or_else(|| "function".to_string()),
                        function: Some(ChatToolCallFunction {
                            name: tc.function_name.clone().unwrap_or_default(),
                            arguments: tc.function_arguments.clone().unwrap_or_default(),
                        }),
                    })
                    .collect();

                conversation_messages.push(ModelChatMessage {
                    role: chat_message::Role::Assistant as i32,
                    content: delta_text.clone(),
                    name: None,
                    tool_calls: model_tool_calls,
                    tool_call_id: None,
                });

                let tool_calls_slice = if first_chunk.tool_calls.len() > limits.max_tool_concurrency
                {
                    &first_chunk.tool_calls[..limits.max_tool_concurrency]
                } else {
                    &first_chunk.tool_calls[..]
                };

                for tc in tool_calls_slice {
                    let tool_call = ToolCall {
                        call_id: tc.id.clone().unwrap_or_default(),
                        tool_name: tc.function_name.clone().unwrap_or_default(),
                        arguments_json: tc.function_arguments.clone().unwrap_or_default(),
                    };

                    // Emit ToolCallStartedEvent
                    let _ = sender
                        .send(AgentStreamEvent {
                            sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                            timestamp_ms: Self::current_timestamp_ms(),
                            event: Some(agent_stream_event::Event::ToolCallStarted(
                                ToolCallStartedEvent {
                                    tool_call: Some(tool_call.clone()),
                                },
                            )),
                        })
                        .await;

                    tool_call_count += 1;
                    let result = match tool_provider.execute(&tool_call).await {
                        Ok(res) => res,
                        Err(err) => {
                            let _ = sender
                                .send(AgentStreamEvent {
                                    sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                                    timestamp_ms: Self::current_timestamp_ms(),
                                    event: Some(agent_stream_event::Event::RunFailed(
                                        RunFailedEvent {
                                            code: err.code,
                                            message: err.message,
                                            retryable: err.retryable,
                                        },
                                    )),
                                })
                                .await;
                            return Ok(());
                        }
                    };

                    // Emit ToolCallCompletedEvent
                    let _ = sender
                        .send(AgentStreamEvent {
                            sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                            timestamp_ms: Self::current_timestamp_ms(),
                            event: Some(agent_stream_event::Event::ToolCallCompleted(
                                ToolCallCompletedEvent {
                                    result: Some(result.clone()),
                                },
                            )),
                        })
                        .await;

                    conversation_messages.push(ModelChatMessage {
                        role: chat_message::Role::Tool as i32,
                        content: result.output_json,
                        name: Some(result.tool_name),
                        tool_calls: Vec::new(),
                        tool_call_id: Some(result.call_id),
                    });
                }

                continue;
            }

            // Emit ContentDeltaEvent for text output
            accumulated_output.push_str(&delta_text);
            let _ = sender
                .send(AgentStreamEvent {
                    sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                    timestamp_ms: Self::current_timestamp_ms(),
                    event: Some(agent_stream_event::Event::ContentDelta(ContentDeltaEvent {
                        text_delta: delta_text.clone(),
                    })),
                })
                .await;

            // Emit terminal RunCompletedEvent
            let duration_ms = start_time.elapsed().as_millis() as i64;
            let _ = sender
                .send(AgentStreamEvent {
                    sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                    timestamp_ms: Self::current_timestamp_ms(),
                    event: Some(agent_stream_event::Event::RunCompleted(RunCompletedEvent {
                        finish_reason,
                        output_text: accumulated_output,
                        total_turns: turn_count,
                        usage: Some(UsageStats {
                            prompt_tokens,
                            completion_tokens,
                            total_tokens: prompt_tokens + completion_tokens,
                            model_call_count,
                            tool_call_count,
                            execution_duration_ms: duration_ms,
                        }),
                    })),
                })
                .await;

            return Ok(());
        }
    }
}

#[async_trait::async_trait]
impl crate::adapter::rig_adapter::AgentDriver for CyreneNativeAgentLoop {
    async fn execute_run(
        &self,
        request: AgentRunRequest,
        model: &(dyn ModelProvider + 'static),
        tools: Option<&(dyn ToolProvider + 'static)>,
        cancel: CancellationToken,
    ) -> Result<AgentRunResponse, AgentRunError> {
        self.execute_run(request, model, tools, cancel).await
    }

    async fn execute_stream(
        &self,
        request: AgentRunRequest,
        model: &(dyn ModelProvider + 'static),
        tools: Option<&(dyn ToolProvider + 'static)>,
        cancel: CancellationToken,
        event_sender: mpsc::Sender<AgentStreamEvent>,
    ) -> Result<(), AgentRunError> {
        self.execute_stream(request, model, tools, cancel, event_sender)
            .await
    }
}
