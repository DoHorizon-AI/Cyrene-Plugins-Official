// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 limits.rs                                                       │
// │  Package: cyrene-agent-runtime                                      │
// │  Role: Execution boundary & limit enforcement (T64).                │
// │                                                                     │
// │  模块职责：严格强制执行轮次、字节、并行工具、事件数与时钟上限        │
// └─────────────────────────────────────────────────────────────────────┘

use cyrene_plugin_contracts::agent_runtime_v1::{AgentConfig, AgentErrorCode, AgentRunError};

pub const DEFAULT_MAX_TURNS: i32 = 10;
pub const ABSOLUTE_MAX_TURNS: i32 = 50;

pub const DEFAULT_MAX_TOOL_CONCURRENCY: i32 = 4;
pub const ABSOLUTE_MAX_TOOL_CONCURRENCY: i32 = 16;

pub const MAX_REQUEST_PAYLOAD_BYTES: usize = 1_048_576; // 1 MB
pub const MAX_STREAMING_EVENTS: usize = 10_000;

#[derive(Debug, Clone)]
pub struct RuntimeLimits {
    pub max_turns: i32,
    pub max_tool_concurrency: usize,
    pub timeout_ms: Option<u64>,
}

impl RuntimeLimits {
    pub fn from_config(config: Option<&AgentConfig>) -> Result<Self, AgentRunError> {
        let mut max_turns = DEFAULT_MAX_TURNS;
        let mut max_tool_concurrency = DEFAULT_MAX_TOOL_CONCURRENCY as usize;
        let mut timeout_ms = None;

        if let Some(cfg) = config {
            if let Some(turns) = cfg.max_turns {
                if turns <= 0 {
                    return Err(AgentRunError {
                        code: AgentErrorCode::InvalidRequest as i32,
                        message: "max_turns must be greater than 0".to_string(),
                        retryable: false,
                        domain_details: "limits.validation".to_string(),
                    });
                }
                max_turns = turns.min(ABSOLUTE_MAX_TURNS);
            }

            if let Some(concurrency) = cfg.max_tool_concurrency {
                if concurrency <= 0 {
                    return Err(AgentRunError {
                        code: AgentErrorCode::InvalidRequest as i32,
                        message: "max_tool_concurrency must be greater than 0".to_string(),
                        retryable: false,
                        domain_details: "limits.validation".to_string(),
                    });
                }
                max_tool_concurrency =
                    (concurrency as usize).min(ABSOLUTE_MAX_TOOL_CONCURRENCY as usize);
            }

            if let Some(timeout) = cfg.timeout_ms {
                if timeout > 0 {
                    timeout_ms = Some(timeout as u64);
                }
            }
        }

        Ok(Self {
            max_turns,
            max_tool_concurrency,
            timeout_ms,
        })
    }

    pub fn validate_payload_size(bytes_len: usize) -> Result<(), AgentRunError> {
        if bytes_len > MAX_REQUEST_PAYLOAD_BYTES {
            return Err(AgentRunError {
                code: AgentErrorCode::InvalidRequest as i32,
                message: format!(
                    "Request payload size ({} bytes) exceeds maximum limit of {} bytes",
                    bytes_len, MAX_REQUEST_PAYLOAD_BYTES
                ),
                retryable: false,
                domain_details: "limits.payload_size".to_string(),
            });
        }
        Ok(())
    }
}
