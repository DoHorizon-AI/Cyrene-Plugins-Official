// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 model_provider.rs                                               │
// │  Package: cyrene-agent-runtime::adapter                             │
// │  Role: Model capability consumer via model.provider.v1 (T60).       │
// │                                                                     │
// │  模块职责：仅通过 model.provider.v1 获取模型能力，严禁拥有厂商凭据    │
// └─────────────────────────────────────────────────────────────────────┘

use async_trait::async_trait;
use cyrene_plugin_contracts::agent_runtime_v1::AgentRunError;
use cyrene_plugin_contracts::model_provider_v1::{ChatCompletionRequest, ChatCompletionResponse};

#[async_trait]
pub trait ModelProvider: Send + Sync {
    /// Dispatches a typed chat completion request to model.provider.v1.
    /// Notice: No API keys, credentials, or vendor SDK types exist in this signature.
    async fn complete(
        &self,
        req: ChatCompletionRequest,
    ) -> Result<ChatCompletionResponse, AgentRunError>;
}
