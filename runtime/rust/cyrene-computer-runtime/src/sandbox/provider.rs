// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 provider.rs                                                     │
// │  Package: cyrene-computer-runtime::sandbox                          │
// │  Role: Separately reviewed external sandbox adapter (T77, T78, T79).│
// │                                                                     │
// │  模块职责：                                                         │
// │  1. 默认实现为【有界受管执行（Bounded Managed Execution）】，        │
// │     绝不标榜为恶意代码硬隔离（Hostile-Code Containment）（T78）     │
// │  2. 严禁导入、实现或调用 Platform SandboxBackend（T77）             │
// │  3. 如需硬件/虚拟化强隔离，通过此外部 Sandbox Adapter 接入（T79）   │
// └─────────────────────────────────────────────────────────────────────┘

use async_trait::async_trait;
use cyrene_plugin_contracts::computer_runtime_v1::{
    CommandExecutionRequest, CommandExecutionResponse, ComputerError,
};

/// Label indicating the isolation guarantee level (T78).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IsolationLevel {
    /// Standard bounded managed execution: path allowlist, env filtering, timeout, output limits.
    /// Explicitly NOT hostile-code containment.
    BoundedManagedExecution,
    /// Hard isolation via external microVM/container provider adapter (T79).
    ExternalHardIsolation,
}

#[async_trait]
pub trait ExternalSandboxProviderAdapter: Send + Sync {
    /// Returns the active isolation level.
    fn isolation_level(&self) -> IsolationLevel;

    /// Executes command inside external hard-isolated sandbox.
    async fn execute_isolated(
        &self,
        request: CommandExecutionRequest,
    ) -> Result<CommandExecutionResponse, ComputerError>;
}

/// Default managed execution marker (T78).
pub struct BoundedManagedExecutionProvider {
    pub isolation_level: IsolationLevel,
}

impl Default for BoundedManagedExecutionProvider {
    fn default() -> Self {
        Self {
            isolation_level: IsolationLevel::BoundedManagedExecution,
        }
    }
}
