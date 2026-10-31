//! Microsoft MXC process namespace adapter.
//!
//! A trusted sibling helper owns the synchronous MXC SDK lifecycle. The
//! existing shell runner owns streaming, output bounds, timeouts, and process
//! group cancellation for that helper. Only the strict policy JSON crosses
//! into the helper; model commands and filtered workload variables never enter
//! the helper's host environment.

use super::mxc_protocol::{
    ensure_control_directories_outside_workspaces, trusted_control_directories,
    trusted_control_path, MxcControlRequest, MxcTerminalReceipt, MAX_MXC_CONTROL_REQUEST_BYTES,
    MAX_MXC_TERMINAL_RECEIPT_BYTES, MXC_CONTROL_SCHEMA_VERSION,
};
use super::provider::{ExternalSandboxProviderAdapter, IsolationLevel};
use crate::computer_runtime_v1::{
    command_execution_response, command_stream_event, CommandExecutionRequest,
    CommandExecutionResponse, CommandStreamEvent, ComputerError, ComputerErrorCode,
    ExecutionEvidence,
};
use crate::security::{EnvironmentFilter, PathValidator};
use crate::ShellProcessRunner;
use async_trait::async_trait;
use std::collections::{BTreeMap, HashMap};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};
use tempfile::{Builder, TempDir};
use tokio::sync::mpsc;
use tokio::task::AbortHandle;

const MXC_LAUNCH_SCRIPT: &str =
    "exec \"$CYRENE_MXC_EXEC_HELPER\" --execute --expected-parent-pid \"$CYRENE_MXC_SUPERVISOR_PID\" --request \"$CYRENE_MXC_REQUEST\" --receipt \"$CYRENE_MXC_RECEIPT\"";
const DEFAULT_WORKLOAD_PATH: &str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
const MAX_MXC_COMMAND_BYTES: usize = 512 * 1024;
const MAX_MXC_ENV_ENTRIES: usize = 128;
const MAX_MXC_ENV_VALUE_BYTES: usize = 16 * 1024;
const MXC_STARTUP_PROBE_TIMEOUT: Duration = Duration::from_secs(10);
const MXC_STARTUP_PROBE_OUTPUT_LIMIT: usize = 4096;
const MXC_STARTUP_PROBE_PREFIX: &str =
    "CYRENE_MXC_PROBE_OK backend=mxc sdk=1.0.1 containment=bubblewrap bwrap=";

struct StartupProbeGroupGuard(Option<u32>);

impl StartupProbeGroupGuard {
    fn disarm(&mut self) {
        self.0 = None;
    }
}

impl Drop for StartupProbeGroupGuard {
    fn drop(&mut self) {
        if let Some(pid) = self.0 {
            kill_process_group(pid);
        }
    }
}

/// Microsoft MXC v1 Bubblewrap adapter routed through the shared shell runner.
pub struct MicrosoftMxcSandboxProviderAdapter {
    helper_path: PathBuf,
    control_path: String,
    bubblewrap_version: String,
    workspace_roots: Vec<PathBuf>,
    path_validator: PathValidator,
    runner: Arc<ShellProcessRunner>,
    env_filter: EnvironmentFilter,
    supervisor_pid: u32,
}

/// Per-call private control files and the fixed helper environment.
struct PreparedInvocation {
    _directory: TempDir,
    receipt_path: PathBuf,
    working_directory: String,
    control_environment: HashMap<String, String>,
    timeout_ms: u32,
}

/// Aborts the shared runner future if the adapter stream is abandoned.
struct AbortTaskOnDrop(Option<AbortHandle>);

impl Drop for AbortTaskOnDrop {
    fn drop(&mut self) {
        if let Some(handle) = self.0.take() {
            handle.abort();
        }
    }
}

impl MicrosoftMxcSandboxProviderAdapter {
    /// Creates the MXC adapter for one operator-configured set of workspace roots.
    pub fn new(
        helper_path: PathBuf,
        path_validator: PathValidator,
        runner: Arc<ShellProcessRunner>,
    ) -> Result<Self, ComputerError> {
        let helper_path = validate_helper_path(helper_path, path_validator.allowed_roots())?;
        let workspace_roots = path_validator.allowed_roots().to_vec();
        if workspace_roots.is_empty() {
            return Err(denied(
                "MXC requires at least one configured workspace root",
            ));
        }
        let control_directories = trusted_control_directories(&helper_path)
            .map_err(|error| denied(format!("cannot resolve trusted MXC PATH: {error}")))?;
        ensure_control_directories_outside_workspaces(&control_directories, &workspace_roots)
            .map_err(denied)?;
        let control_path = trusted_control_path(&control_directories)
            .map_err(|error| denied(format!("cannot build trusted MXC PATH: {error}")))?;
        let supervisor_pid = std::process::id();
        let bubblewrap_version = run_startup_probe(
            &helper_path,
            &workspace_roots,
            &control_path,
            supervisor_pid,
        )?;
        Ok(Self {
            helper_path,
            control_path,
            bubblewrap_version,
            workspace_roots,
            path_validator,
            runner,
            env_filter: EnvironmentFilter::new(),
            supervisor_pid,
        })
    }

    /// Returns the verified Bubblewrap version from the real startup probe.
    pub fn bubblewrap_version(&self) -> &str {
        &self.bubblewrap_version
    }

    fn prepare_invocation(
        &self,
        request: &CommandExecutionRequest,
    ) -> Result<PreparedInvocation, ComputerError> {
        if request.command.trim().is_empty()
            || request.command.len() > MAX_MXC_COMMAND_BYTES
            || request.command.contains('\0')
        {
            return Err(denied(
                "MXC command must be non-empty, contain no NUL, and fit the configured limit",
            ));
        }

        let working_directory = self.path_validator.validate_cwd(request.cwd.as_deref())?;
        let working_directory = path_to_utf8(&working_directory)?;
        let start_marker = new_start_marker()?;
        let timeout = self.runner.requested_timeout(request.timeout_ms)?;
        let timeout_ms = timeout.as_millis().min(u32::MAX as u128).max(1) as u32;
        let workspace_roots = self
            .workspace_roots
            .iter()
            .map(|path| path_to_utf8(path))
            .collect::<Result<Vec<_>, _>>()?;

        let mut workload_environment = self.env_filter.sanitize_requested_environment(&request.env);
        if workload_environment.len() > MAX_MXC_ENV_ENTRIES {
            return Err(denied("MXC request contains too many environment entries"));
        }
        for (key, value) in &workload_environment {
            if !valid_env_key(key) || value.contains('\0') || value.len() > MAX_MXC_ENV_VALUE_BYTES
            {
                return Err(denied(format!(
                    "MXC workload environment entry '{key}' is invalid"
                )));
            }
        }
        workload_environment
            .entry("PATH".into())
            .or_insert_with(|| DEFAULT_WORKLOAD_PATH.into());
        workload_environment.insert("HOME".into(), "/tmp/cyrene-home".into());
        workload_environment.insert("TMPDIR".into(), "/tmp".into());

        let control_request = MxcControlRequest {
            schema_version: MXC_CONTROL_SCHEMA_VERSION,
            supervisor_pid: self.supervisor_pid,
            command: request.command.clone(),
            start_marker,
            timeout_ms,
            workspace_roots,
            working_directory: working_directory.clone(),
            environment: workload_environment.into_iter().collect::<BTreeMap<_, _>>(),
        };
        let bytes = serde_json::to_vec(&control_request)
            .map_err(|error| denied(format!("cannot encode MXC control request: {error}")))?;
        if bytes.len() > MAX_MXC_CONTROL_REQUEST_BYTES {
            return Err(denied("MXC control request exceeds its size limit"));
        }

        let directory = Builder::new()
            .prefix("cyrene-mxc-control-")
            .tempdir_in("/tmp")
            .map_err(|error| {
                denied(format!(
                    "cannot create private MXC control directory: {error}"
                ))
            })?;
        let directory_path = directory
            .path()
            .canonicalize()
            .map_err(|error| denied(format!("cannot resolve MXC control directory: {error}")))?;
        if self
            .workspace_roots
            .iter()
            .any(|root| directory_path.starts_with(root))
        {
            return Err(denied(
                "MXC workspace overlaps the private control directory; choose a narrower workspace root",
            ));
        }

        let request_path = directory_path.join("request.json");
        let receipt_path = directory_path.join("terminal.json");
        let mut request_file = private_new_file(&request_path)
            .map_err(|error| denied(format!("cannot create private MXC request file: {error}")))?;
        request_file
            .write_all(&bytes)
            .and_then(|()| request_file.sync_all())
            .map_err(|error| denied(format!("cannot persist private MXC request: {error}")))?;
        drop(request_file);

        let host_home = directory_path.join("host-home");
        fs::create_dir(&host_home)
            .map_err(|error| denied(format!("cannot create private MXC helper home: {error}")))?;
        #[cfg(unix)]
        fs::set_permissions(&host_home, fs::Permissions::from_mode(0o700))
            .map_err(|error| denied(format!("cannot protect private MXC helper home: {error}")))?;

        let mut control_environment = HashMap::new();
        control_environment.insert(
            "CYRENE_MXC_EXEC_HELPER".into(),
            path_to_utf8(&self.helper_path)?,
        );
        control_environment.insert("CYRENE_MXC_REQUEST".into(), path_to_utf8(&request_path)?);
        control_environment.insert("CYRENE_MXC_RECEIPT".into(), path_to_utf8(&receipt_path)?);
        control_environment.insert(
            "CYRENE_MXC_SUPERVISOR_PID".into(),
            self.supervisor_pid.to_string(),
        );
        control_environment.insert("PATH".into(), self.control_path.clone());
        control_environment.insert("HOME".into(), path_to_utf8(&host_home)?);
        control_environment.insert("TMPDIR".into(), path_to_utf8(&directory_path)?);
        control_environment.insert("LC_ALL".into(), "C".into());

        Ok(PreparedInvocation {
            _directory: directory,
            receipt_path,
            working_directory,
            control_environment,
            timeout_ms,
        })
    }

    fn helper_request(
        &self,
        original: &CommandExecutionRequest,
        invocation: &PreparedInvocation,
    ) -> CommandExecutionRequest {
        CommandExecutionRequest {
            command: MXC_LAUNCH_SCRIPT.into(),
            cwd: Some(invocation.working_directory.clone()),
            env: invocation.control_environment.clone(),
            timeout_ms: Some(i64::from(invocation.timeout_ms)),
            max_output_bytes: original.max_output_bytes,
        }
    }

    async fn execute_with_cancel(
        &self,
        request: CommandExecutionRequest,
        cancel_flag: Option<Arc<AtomicBool>>,
    ) -> Result<CommandExecutionResponse, ComputerError> {
        let invocation = self.prepare_invocation(&request)?;
        let helper_request = self.helper_request(&request, &invocation);
        let response = self
            .runner
            .execute_command(helper_request, cancel_flag)
            .await;
        self.validate_terminal_response(response, &invocation)
    }

    fn validate_terminal_response(
        &self,
        response: CommandExecutionResponse,
        invocation: &PreparedInvocation,
    ) -> Result<CommandExecutionResponse, ComputerError> {
        let mut evidence = match response.result {
            Some(command_execution_response::Result::Evidence(evidence)) => evidence,
            Some(command_execution_response::Result::Error(error)) => return Err(error),
            None => return Err(denied("MXC helper returned no terminal response")),
        };
        let receipt = read_receipt(&invocation.receipt_path)?;
        match receipt {
            MxcTerminalReceipt::Exited {
                schema_version,
                exit_code,
                duration_ms,
                warnings,
            } => {
                if schema_version != MXC_CONTROL_SCHEMA_VERSION || evidence.exit_code != 0 {
                    return Err(denied(
                        "MXC helper completion receipt does not match its successful launcher exit",
                    ));
                }
                evidence.exit_code = exit_code;
                evidence.duration_ms = duration_ms.min(i64::MAX as u64) as i64;
                if !warnings.is_empty() {
                    return Err(denied(format!(
                        "MXC reported unexpected workload warnings: {}",
                        warnings.join("; ")
                    )));
                }
                Ok(CommandExecutionResponse {
                    result: Some(command_execution_response::Result::Evidence(evidence)),
                })
            }
            MxcTerminalReceipt::TimedOut {
                schema_version,
                message,
                ..
            } if schema_version == MXC_CONTROL_SCHEMA_VERSION => Err(timeout_error(message)),
            MxcTerminalReceipt::BackendError {
                schema_version,
                message,
            } if schema_version == MXC_CONTROL_SCHEMA_VERSION => Err(denied(message)),
            MxcTerminalReceipt::BackendUnavailable {
                schema_version,
                message,
            } if schema_version == MXC_CONTROL_SCHEMA_VERSION => Err(denied(format!(
                "MXC backend unavailable before workload start: {message}"
            ))),
            _ => Err(denied(
                "MXC helper terminal receipt has an unsupported schema",
            )),
        }
    }

    async fn execute_stream_with_cancel(
        &self,
        request: CommandExecutionRequest,
        cancel_flag: Option<Arc<AtomicBool>>,
        sender: mpsc::Sender<CommandStreamEvent>,
    ) -> Result<(), ComputerError> {
        let invocation = self.prepare_invocation(&request)?;
        let deadline =
            tokio::time::Instant::now() + Duration::from_millis(u64::from(invocation.timeout_ms));
        let helper_request = self.helper_request(&request, &invocation);
        let (internal_sender, mut internal_receiver) = mpsc::channel(16);
        let runner = Arc::clone(&self.runner);
        let command_cancel = cancel_flag.clone();
        let task = tokio::spawn(async move {
            runner
                .execute_command_stream(helper_request, command_cancel, internal_sender)
                .await
        });
        let mut abort_task = AbortTaskOnDrop(Some(task.abort_handle()));
        let mut sequence_number = 0i64;
        let mut launcher_evidence: Option<ExecutionEvidence> = None;
        let mut stream_error = None;

        loop {
            let event = tokio::select! {
                event = internal_receiver.recv() => event,
                _ = tokio::time::sleep_until(deadline) => {
                    return Err(timeout_error("MXC stream exceeded its absolute deadline"));
                }
                _ = wait_for_cancellation(cancel_flag.clone()) => {
                    return Err(timeout_error("MXC stream was cancelled"));
                }
                _ = sender.closed() => {
                    return Err(denied("MXC stream receiver closed before completion"));
                }
            };
            let Some(event) = event else { break };
            match event.event {
                Some(command_stream_event::Event::StdoutChunk(text)) => {
                    sequence_number += 1;
                    send_event_until(
                        &sender,
                        CommandStreamEvent {
                            sequence_number,
                            timestamp_ms: event.timestamp_ms,
                            event: Some(command_stream_event::Event::StdoutChunk(text)),
                        },
                        deadline,
                        cancel_flag.clone(),
                    )
                    .await?;
                }
                Some(command_stream_event::Event::StderrChunk(text)) => {
                    sequence_number += 1;
                    send_event_until(
                        &sender,
                        CommandStreamEvent {
                            sequence_number,
                            timestamp_ms: event.timestamp_ms,
                            event: Some(command_stream_event::Event::StderrChunk(text)),
                        },
                        deadline,
                        cancel_flag.clone(),
                    )
                    .await?;
                }
                Some(command_stream_event::Event::ExitEvidence(evidence)) => {
                    launcher_evidence = Some(evidence);
                }
                Some(command_stream_event::Event::StreamError(error)) => {
                    stream_error = Some(error);
                }
                None => {}
            }
        }
        let runner_result = tokio::select! {
            result = task => result,
            _ = tokio::time::sleep_until(deadline) => {
                return Err(timeout_error("MXC launcher exceeded its absolute deadline"));
            }
            _ = wait_for_cancellation(cancel_flag.clone()) => {
                return Err(timeout_error("MXC launcher was cancelled"));
            }
            _ = sender.closed() => {
                return Err(denied("MXC stream receiver closed before completion"));
            }
        };
        abort_task.0 = None;

        if let Some(error) = stream_error {
            return Err(error);
        }
        match runner_result {
            Err(error) => return Err(denied(format!("MXC launcher task failed: {error}"))),
            Ok(Err(error)) => return Err(error),
            Ok(Ok(())) => {}
        }

        let mut evidence = launcher_evidence
            .ok_or_else(|| denied("MXC launcher produced no terminal evidence"))?;
        let receipt = read_receipt(&invocation.receipt_path)?;
        match receipt {
            MxcTerminalReceipt::Exited {
                schema_version,
                exit_code,
                duration_ms,
                warnings,
            } if schema_version == MXC_CONTROL_SCHEMA_VERSION && evidence.exit_code == 0 => {
                evidence.exit_code = exit_code;
                evidence.duration_ms = duration_ms.min(i64::MAX as u64) as i64;
                if !warnings.is_empty() {
                    return Err(denied(format!(
                        "MXC reported unexpected workload warnings: {}",
                        warnings.join("; ")
                    )));
                }
                sequence_number += 1;
                send_event_until(
                    &sender,
                    CommandStreamEvent {
                        sequence_number,
                        timestamp_ms: current_epoch_ms(),
                        event: Some(command_stream_event::Event::ExitEvidence(evidence)),
                    },
                    deadline,
                    cancel_flag,
                )
                .await?;
                Ok(())
            }
            MxcTerminalReceipt::TimedOut {
                schema_version,
                message,
                ..
            } if schema_version == MXC_CONTROL_SCHEMA_VERSION => Err(timeout_error(message)),
            MxcTerminalReceipt::BackendError {
                schema_version,
                message,
            } if schema_version == MXC_CONTROL_SCHEMA_VERSION => Err(denied(message)),
            MxcTerminalReceipt::BackendUnavailable {
                schema_version,
                message,
            } if schema_version == MXC_CONTROL_SCHEMA_VERSION => Err(denied(format!(
                "MXC backend unavailable before workload start: {message}"
            ))),
            _ => Err(denied("MXC helper terminal receipt is missing or invalid")),
        }
    }
}

#[async_trait]
impl ExternalSandboxProviderAdapter for MicrosoftMxcSandboxProviderAdapter {
    fn isolation_level(&self) -> IsolationLevel {
        IsolationLevel::MicrosoftMxcProcessNamespace
    }

    async fn execute_isolated(
        &self,
        request: CommandExecutionRequest,
    ) -> Result<CommandExecutionResponse, ComputerError> {
        self.execute_with_cancel(request, None).await
    }

    async fn execute_isolated_cancellable(
        &self,
        request: CommandExecutionRequest,
        cancel_flag: Option<Arc<AtomicBool>>,
    ) -> Result<CommandExecutionResponse, ComputerError> {
        self.execute_with_cancel(request, cancel_flag).await
    }

    async fn execute_isolated_stream(
        &self,
        request: CommandExecutionRequest,
        cancel_flag: Option<Arc<AtomicBool>>,
        sender: mpsc::Sender<CommandStreamEvent>,
    ) -> Result<(), ComputerError> {
        self.execute_stream_with_cancel(request, cancel_flag, sender)
            .await
    }
}

fn validate_helper_path(
    helper_path: PathBuf,
    workspace_roots: &[PathBuf],
) -> Result<PathBuf, ComputerError> {
    let metadata = fs::symlink_metadata(&helper_path)
        .map_err(|error| denied(format!("MXC helper is unavailable: {error}")))?;
    if !metadata.file_type().is_file() || metadata.mode() & 0o111 == 0 {
        return Err(denied("MXC helper is not a regular executable file"));
    }
    let helper_path = helper_path
        .canonicalize()
        .map_err(|error| denied(format!("cannot resolve MXC helper: {error}")))?;
    if workspace_roots
        .iter()
        .any(|root| helper_path.starts_with(root))
    {
        return Err(denied(
            "MXC helper must be outside all writable workspace roots",
        ));
    }
    Ok(helper_path)
}

fn new_start_marker() -> Result<String, ComputerError> {
    let mut nonce = [0u8; 16];
    File::open("/dev/urandom")
        .and_then(|mut random| random.read_exact(&mut nonce))
        .map_err(|error| denied(format!("cannot generate an MXC start nonce: {error}")))?;
    Ok(format!("CYRENE_MXC_STARTED_{}", hex(&nonce)))
}

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(DIGITS[(byte >> 4) as usize] as char);
        output.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    output
}

fn run_startup_probe(
    helper_path: &Path,
    workspace_roots: &[PathBuf],
    control_path: &str,
    supervisor_pid: u32,
) -> Result<String, ComputerError> {
    use std::os::unix::process::CommandExt;

    let mut command = Command::new(helper_path);
    command
        .arg("--probe")
        .arg("--expected-parent-pid")
        .arg(supervisor_pid.to_string())
        .env_clear()
        .env("PATH", control_path)
        .env("HOME", "/tmp/cyrene-mxc-probe-home")
        .env("TMPDIR", "/tmp")
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    for root in workspace_roots {
        command.arg("--workspace-root").arg(root);
    }
    let mut child = command
        .spawn()
        .map_err(|error| denied(format!("cannot start MXC containment probe: {error}")))?;
    let child_id = child.id();
    let mut process_group = StartupProbeGroupGuard(Some(child_id));
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| denied("MXC containment probe stdout is unavailable"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| denied("MXC containment probe stderr is unavailable"))?;
    let stdout_reader = thread::spawn(move || read_bounded(stdout, MXC_STARTUP_PROBE_OUTPUT_LIMIT));
    let stderr_reader = thread::spawn(move || read_bounded(stderr, MXC_STARTUP_PROBE_OUTPUT_LIMIT));
    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                process_group.disarm();
                break Some(status);
            }
            Ok(None) if started.elapsed() < MXC_STARTUP_PROBE_TIMEOUT => {
                thread::sleep(Duration::from_millis(20));
            }
            Ok(None) => {
                kill_process_group(child_id);
                let _ = child.wait();
                process_group.disarm();
                break None;
            }
            Err(error) => {
                kill_process_group(child_id);
                let _ = child.wait();
                process_group.disarm();
                return Err(denied(format!(
                    "MXC containment probe wait failed: {error}"
                )));
            }
        }
    };
    let stdout = stdout_reader
        .join()
        .map_err(|_| denied("MXC containment probe stdout reader panicked"))?
        .map_err(|error| denied(format!("MXC containment probe stdout read failed: {error}")))?;
    let stderr = stderr_reader
        .join()
        .map_err(|_| denied("MXC containment probe stderr reader panicked"))?
        .map_err(|error| denied(format!("MXC containment probe stderr read failed: {error}")))?;
    let Some(status) = status else {
        return Err(denied("MXC Bubblewrap containment probe timed out"));
    };
    let report = std::str::from_utf8(&stdout.bytes)
        .map_err(|_| denied("MXC containment probe report is not UTF-8"))?;
    let report = report.strip_suffix('\n').unwrap_or(report);
    let bubblewrap_version = report
        .strip_prefix(MXC_STARTUP_PROBE_PREFIX)
        .ok_or_else(|| {
            denied(format!(
                "MXC containment probe reported an unexpected backend: {}",
                String::from_utf8_lossy(&stderr.bytes).trim()
            ))
        })?;
    let parsed_version = parse_bubblewrap_version_text(bubblewrap_version)?;
    if !status.success() || stdout.overflow || stderr.overflow || parsed_version < (0, 12, 0) {
        let diagnostic = String::from_utf8_lossy(&stderr.bytes);
        return Err(denied(format!(
            "MXC Bubblewrap containment probe did not verify its start marker (status {status}): {}",
            diagnostic.trim()
        )));
    }
    if !stderr.bytes.is_empty() {
        return Err(denied(format!(
            "MXC Bubblewrap containment probe emitted unexpected diagnostics: {}",
            String::from_utf8_lossy(&stderr.bytes).trim()
        )));
    }
    Ok(bubblewrap_version.to_string())
}

fn parse_bubblewrap_version_text(value: &str) -> Result<(u32, u32, u32), ComputerError> {
    let parts = value
        .split('.')
        .map(|part| part.parse::<u32>())
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| denied("MXC containment probe version is not numeric"))?;
    if parts.len() != 3 {
        return Err(denied(
            "MXC containment probe version must have three numeric parts",
        ));
    }
    Ok((parts[0], parts[1], parts[2]))
}

struct BoundedProbeOutput {
    bytes: Vec<u8>,
    overflow: bool,
}

fn read_bounded(mut reader: impl Read, limit: usize) -> std::io::Result<BoundedProbeOutput> {
    let mut bytes = Vec::with_capacity(limit.min(1024));
    let mut overflow = false;
    let mut buffer = [0u8; 1024];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        let retained = count.min(limit.saturating_sub(bytes.len()));
        bytes.extend_from_slice(&buffer[..retained]);
        overflow |= retained < count;
    }
    Ok(BoundedProbeOutput { bytes, overflow })
}

fn kill_process_group(pid: u32) {
    // SAFETY: negative pid addresses only the process group created by `process_group(0)`.
    unsafe {
        libc::kill(-(pid as i32), libc::SIGKILL);
    }
}

fn private_new_file(path: &Path) -> std::io::Result<File> {
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
        .open(path)
}

fn read_receipt(path: &Path) -> Result<MxcTerminalReceipt, ComputerError> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
        .open(path)
        .map_err(|error| {
            denied(format!(
                "MXC helper did not write its terminal receipt: {error}"
            ))
        })?;
    let metadata = file
        .metadata()
        .map_err(|error| denied(format!("cannot inspect MXC terminal receipt: {error}")))?;
    if !metadata.is_file()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.mode() & 0o077 != 0
        || metadata.nlink() != 1
    {
        return Err(denied(
            "MXC terminal receipt ownership or permissions are invalid",
        ));
    }
    let mut bytes =
        Vec::with_capacity(metadata.len().min(MAX_MXC_TERMINAL_RECEIPT_BYTES as u64) as usize);
    file.take(MAX_MXC_TERMINAL_RECEIPT_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| denied(format!("cannot read MXC terminal receipt: {error}")))?;
    if bytes.len() > MAX_MXC_TERMINAL_RECEIPT_BYTES {
        return Err(denied("MXC terminal receipt exceeds its size limit"));
    }
    serde_json::from_slice(&bytes)
        .map_err(|error| denied(format!("MXC terminal receipt is invalid: {error}")))
}

async fn wait_for_cancellation(cancel_flag: Option<Arc<AtomicBool>>) {
    loop {
        if cancel_flag
            .as_ref()
            .is_some_and(|flag| flag.load(Ordering::SeqCst))
        {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

async fn send_event_until(
    sender: &mpsc::Sender<CommandStreamEvent>,
    event: CommandStreamEvent,
    deadline: tokio::time::Instant,
    cancel_flag: Option<Arc<AtomicBool>>,
) -> Result<(), ComputerError> {
    tokio::select! {
        result = tokio::time::timeout_at(deadline, sender.send(event)) => {
            match result {
                Ok(Ok(())) => Ok(()),
                Ok(Err(_)) => Err(denied("MXC stream receiver closed before completion")),
                Err(_) => Err(timeout_error("MXC stream exceeded its deadline while forwarding output")),
            }
        }
        _ = wait_for_cancellation(cancel_flag) => {
            Err(timeout_error("MXC stream was cancelled while forwarding output"))
        }
        _ = sender.closed() => Err(denied("MXC stream receiver closed before completion")),
    }
}

fn valid_env_key(key: &str) -> bool {
    let mut bytes = key.bytes();
    matches!(bytes.next(), Some(b'A'..=b'Z' | b'a'..=b'z' | b'_'))
        && bytes.all(|byte| matches!(byte, b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_'))
}

fn path_to_utf8(path: &Path) -> Result<String, ComputerError> {
    path.to_str()
        .map(str::to_string)
        .ok_or_else(|| denied("MXC does not support non-UTF-8 workspace paths"))
}

fn denied(message: impl Into<String>) -> ComputerError {
    ComputerError {
        code: ComputerErrorCode::ExecutionDenied as i32,
        message: message.into(),
        retryable: false,
    }
}

fn timeout_error(message: impl Into<String>) -> ComputerError {
    ComputerError {
        code: ComputerErrorCode::CommandTimeout as i32,
        message: message.into(),
        retryable: false,
    }
}

fn current_epoch_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}
