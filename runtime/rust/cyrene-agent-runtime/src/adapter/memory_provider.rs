// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 memory_provider.rs                                              │
// │  Package: cyrene-agent-runtime::adapter                             │
// │  Role: Injected memory provider adapter (T62).                      │
// │                                                                     │
// │  模块职责：Memory 作为显式注入能力，Agent Runtime 不拥有持久 session │
// └─────────────────────────────────────────────────────────────────────┘

use async_trait::async_trait;
use cyrene_plugin_contracts::agent_runtime_v1::AgentRunError;
use cyrene_plugin_contracts::memory_provider_v1::{RecallMemoryRequest, RecallMemoryResponse};

#[async_trait]
pub trait InjectedMemoryProvider: Send + Sync {
    /// Injected memory recall hook for grounding prompts on-demand.
    async fn recall(&self, req: RecallMemoryRequest)
        -> Result<RecallMemoryResponse, AgentRunError>;
}
