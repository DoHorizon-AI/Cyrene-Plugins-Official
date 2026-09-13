// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 tool_provider.rs                                                │
// │  Package: cyrene-agent-runtime::adapter                             │
// │  Role: Tool & computer capability consumer (T61).                   │
// │                                                                     │
// │  模块职责：仅通过 tool.provider.v1 或 computer.runtime.v1 调用工具   │
// └─────────────────────────────────────────────────────────────────────┘

use async_trait::async_trait;
use cyrene_plugin_contracts::agent_runtime_v1::{AgentRunError, ToolCall, ToolCallResult};

#[async_trait]
pub trait ToolProvider: Send + Sync {
    /// Dispatches a tool execution to tool.provider.v1 or computer.runtime.v1.
    async fn execute(&self, call: &ToolCall) -> Result<ToolCallResult, AgentRunError>;
}
