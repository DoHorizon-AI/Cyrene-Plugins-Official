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
    command_execution_response, command_stream_event, CommandExecutionRequest,
    CommandExecutionResponse, CommandStreamEvent, ComputerError, ComputerErrorCode,
};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::mpsc;

/// Label indicating the isolation guarantee level (T78).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IsolationLevel {
    /// Standard bounded managed execution: path allowlist, env filtering, timeout, output limits.
    /// Explicitly NOT hostile-code containment.
    BoundedManagedExecution,
    /// Hard isolation via external microVM/container provider adapter (T79).
    ExternalHardIsolation,
    /// Microsoft MXC's Linux process namespace containment.
    ///
    /// This identifies the MXC Bubblewrap process namespace backend. It does
    /// not claim microVM or hardware virtualization isolation.
    MicrosoftMxcProcessNamespace,
}

/// Host-selected computer execution backend.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ComputerExecutionBackend {
    /// Existing bounded shell runner, selected unless explicitly overridden.
    #[default]
    Managed,
    /// Optional Microsoft MXC Bubblewrap process namespace backend.
    MicrosoftMxc,
}

impl ComputerExecutionBackend {
    /// Parses the deployment-only backend setting.
    pub fn parse(value: Option<&str>) -> Result<Self, String> {
        match value.unwrap_or("managed") {
            "managed" => Ok(Self::Managed),
            "mxc" => Ok(Self::MicrosoftMxc),
            other => Err(format!(
                "unsupported computer execution backend '{other}'; expected 'managed' or 'mxc'"
            )),
        }
    }
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

    /// Executes an isolated request while observing runtime cancellation.
    ///
    /// Adapters that own an external process should override this method so
    /// cancellation tears down that process. The default preserves adapters
    /// whose implementation has no separate cancellable lifecycle.
    async fn execute_isolated_cancellable(
        &self,
        request: CommandExecutionRequest,
        cancel_flag: Option<Arc<AtomicBool>>,
    ) -> Result<CommandExecutionResponse, ComputerError> {
        if cancel_flag
            .as_ref()
            .is_some_and(|flag| flag.load(Ordering::SeqCst))
        {
            return Err(ComputerError {
                code: ComputerErrorCode::CommandTimeout as i32,
                message: "Command execution was cancelled".to_string(),
                retryable: false,
            });
        }
        self.execute_isolated(request).await
    }

    /// Executes an isolated request and forwards its output through the
    /// existing command-stream contract.
    async fn execute_isolated_stream(
        &self,
        request: CommandExecutionRequest,
        cancel_flag: Option<Arc<AtomicBool>>,
        sender: mpsc::Sender<CommandStreamEvent>,
    ) -> Result<(), ComputerError> {
        let response = self
            .execute_isolated_cancellable(request, cancel_flag)
            .await?;
        let timestamp_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as i64;
        match response.result {
            Some(command_execution_response::Result::Evidence(evidence)) => {
                let mut sequence_number = 1;
                if !evidence.stdout.is_empty() {
                    let _ = sender
                        .send(CommandStreamEvent {
                            sequence_number,
                            timestamp_ms,
                            event: Some(command_stream_event::Event::StdoutChunk(
                                evidence.stdout.clone(),
                            )),
                        })
                        .await;
                    sequence_number += 1;
                }
                if !evidence.stderr.is_empty() {
                    let _ = sender
                        .send(CommandStreamEvent {
                            sequence_number,
                            timestamp_ms,
                            event: Some(command_stream_event::Event::StderrChunk(
                                evidence.stderr.clone(),
                            )),
                        })
                        .await;
                    sequence_number += 1;
                }
                let _ = sender
                    .send(CommandStreamEvent {
                        sequence_number,
                        timestamp_ms,
                        event: Some(command_stream_event::Event::ExitEvidence(evidence)),
                    })
                    .await;
                Ok(())
            }
            Some(command_execution_response::Result::Error(error)) => {
                let _ = sender
                    .send(CommandStreamEvent {
                        sequence_number: 1,
                        timestamp_ms,
                        event: Some(command_stream_event::Event::StreamError(error.clone())),
                    })
                    .await;
                Ok(())
            }
            None => Err(ComputerError {
                code: ComputerErrorCode::ExecutionDenied as i32,
                message: "Sandbox adapter returned no terminal result".to_string(),
                retryable: false,
            }),
        }
    }
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
