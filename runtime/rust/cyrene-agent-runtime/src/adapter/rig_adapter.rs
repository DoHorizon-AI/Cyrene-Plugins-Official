// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 rig_adapter.rs                                                  │
// │  Package: cyrene-agent-runtime::adapter                             │
// │  Role: Internal third-party adapter (T58, T65).                     │
// │                                                                     │
// │  模块职责：将第三方 Agent 框架（如 Rig）封装在内部 Adapter 之后，      │
// │           对外保持严格纯粹的标准 Cyrene 契约，禁止泄露框架类型         │
// └─────────────────────────────────────────────────────────────────────┘

use async_trait::async_trait;
use cyrene_plugin_contracts::agent_runtime_v1::{
    AgentRunError, AgentRunRequest, AgentRunResponse, AgentStreamEvent,
};
use tokio::sync::mpsc;

use crate::adapter::model_provider::ModelProvider;
use crate::adapter::tool_provider::ToolProvider;
use crate::engine::cancel::CancellationToken;

/// Internal Driver trait behind which any framework (e.g. Rig) can be plugged.
/// Public consumers never interact with this trait directly (T58, T65).
#[async_trait]
pub trait AgentDriver: Send + Sync {
    async fn execute_run(
        &self,
        request: AgentRunRequest,
        model: &(dyn ModelProvider + 'static),
        tools: Option<&(dyn ToolProvider + 'static)>,
        cancel: CancellationToken,
    ) -> Result<AgentRunResponse, AgentRunError>;

    async fn execute_stream(
        &self,
        request: AgentRunRequest,
        model: &(dyn ModelProvider + 'static),
        tools: Option<&(dyn ToolProvider + 'static)>,
        cancel: CancellationToken,
        event_sender: mpsc::Sender<AgentStreamEvent>,
    ) -> Result<(), AgentRunError>;
}

/// Simulated Rig adapter demonstrating framework integration without public type exposure.
pub struct RigAgentAdapter {
    _framework_name: &'static str,
}

impl RigAgentAdapter {
    pub fn new() -> Self {
        Self {
            _framework_name: "rig-agent-internal-v0.4",
        }
    }
}

impl Default for RigAgentAdapter {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl AgentDriver for RigAgentAdapter {
    async fn execute_run(
        &self,
        request: AgentRunRequest,
        model: &(dyn ModelProvider + 'static),
        tools: Option<&(dyn ToolProvider + 'static)>,
        cancel: CancellationToken,
    ) -> Result<AgentRunResponse, AgentRunError> {
        let native = crate::engine::turn_loop::CyreneNativeAgentLoop::new();
        native.execute_run(request, model, tools, cancel).await
    }

    async fn execute_stream(
        &self,
        request: AgentRunRequest,
        model: &(dyn ModelProvider + 'static),
        tools: Option<&(dyn ToolProvider + 'static)>,
        cancel: CancellationToken,
        event_sender: mpsc::Sender<AgentStreamEvent>,
    ) -> Result<(), AgentRunError> {
        let native = crate::engine::turn_loop::CyreneNativeAgentLoop::new();
        native
            .execute_stream(request, model, tools, cancel, event_sender)
            .await
    }
}
