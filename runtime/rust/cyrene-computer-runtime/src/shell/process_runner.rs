// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 process_runner.rs                                               │
// │  Package: cyrene-computer-runtime::shell                            │
// │  Role: Bounded managed shell execution engine (T74-T80).            │
// │                                                                     │
// │  模块职责：                                                         │
// │  1. 有界受管执行（Bounded Managed Execution），非恶意代码硬隔离      │
// │  2. 强制 cwd、环境脱敏、Deadline、输出截断与 Process Group 清理     │
// │  3. 产生完整 ExecutionEvidence（exit、timing、truncation、sha256） │
// │  4. 协作式取消与流式事件单调递增保证                                │
// └─────────────────────────────────────────────────────────────────────┘

use sha2::{Digest, Sha256};
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::sync::mpsc;
use tokio::time::timeout;

use cyrene_plugin_contracts::computer_runtime_v1::{
    command_execution_response, command_stream_event, CommandExecutionRequest,
    CommandExecutionResponse, CommandStreamEvent, ComputerError, ComputerErrorCode,
    ExecutionEvidence,
};

use crate::security::{
    EnvironmentFilter, OutputCollector, PathValidator, ABSOLUTE_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_OUTPUT_BYTES,
};

pub struct ShellProcessRunner {
    path_validator: PathValidator,
    env_filter: EnvironmentFilter,
    default_timeout_ms: u64,
    max_timeout_ms: u64,
    max_output_bytes: usize,
}

/// Kills a command process group when its request future is dropped.
/// 请求 future 被丢弃时终止该命令的整个进程组。
struct ProcessGroupGuard {
    pid: Option<u32>,
}

impl ProcessGroupGuard {
    fn new(pid: u32) -> Self {
        Self { pid: Some(pid) }
    }

    fn terminate(&self) {
        if let Some(pid) = self.pid {
            ShellProcessRunner::kill_process_group(pid);
        }
    }

    fn disarm(&mut self) {
        self.pid = None;
    }
}

impl Drop for ProcessGroupGuard {
    fn drop(&mut self) {
        self.terminate();
    }
}

impl ShellProcessRunner {
    pub fn new(path_validator: PathValidator, env_filter: EnvironmentFilter) -> Self {
        Self::with_limits(
            path_validator,
            env_filter,
            30_000,
            600_000,
            DEFAULT_MAX_OUTPUT_BYTES,
        )
    }

    /// Creates a runner with deployment-supplied per-command bounds.
    /// 使用部署提供的单次命令资源边界创建运行器。
    pub fn with_limits(
        path_validator: PathValidator,
        env_filter: EnvironmentFilter,
        default_timeout_ms: u64,
        max_timeout_ms: u64,
        max_output_bytes: usize,
    ) -> Self {
        Self {
            path_validator,
            env_filter,
            default_timeout_ms: default_timeout_ms.max(1),
            max_timeout_ms: max_timeout_ms.max(default_timeout_ms.max(1)),
            max_output_bytes: max_output_bytes.clamp(1, ABSOLUTE_MAX_OUTPUT_BYTES),
        }
    }

    fn current_epoch_ms() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or(Duration::from_secs(0))
            .as_millis() as i64
    }

    /// Kills the entire process group of a child process (T80 descendant cleanup).
    fn kill_process_group(pid: u32) {
        unsafe {
            let pgid = -(pid as i32);
            libc::kill(pgid, libc::SIGKILL);
        }
    }

    /// Unary blocking managed execution (T74, T75, T76, T78).
    /// 一元阻塞式托管执行（T74、T75、T76、T78）。
    pub async fn execute_command(
        &self,
        request: CommandExecutionRequest,
        cancel_flag: Option<Arc<AtomicBool>>,
    ) -> CommandExecutionResponse {
        let start_time = Instant::now();
        let timeout_duration = match self.requested_timeout(request.timeout_ms) {
            Ok(value) => value,
            Err(error) => {
                return CommandExecutionResponse {
                    result: Some(command_execution_response::Result::Error(error)),
                };
            }
        };

        // 1. Validate working directory (T75)
        // 1. 校验工作目录（T75）。
        let cwd = match self.path_validator.validate_cwd(request.cwd.as_deref()) {
            Ok(p) => p,
            Err(err) => {
                return CommandExecutionResponse {
                    result: Some(command_execution_response::Result::Error(err)),
                };
            }
        };

        // 2. Sanitize environment variables (T75, T80)
        // 2. 净化环境变量（T75、T80）。
        let sanitized_env = self.env_filter.sanitize_environment(&request.env);

        // 3. Configure command execution with process group (T80 descendant cleanup)
        // 3. 配置命令执行时使用进程组（T80 后代清理）。
        let mut cmd = Command::new("bash");
        cmd.arg("-c")
            .arg(&request.command)
            .current_dir(cwd)
            .env_clear() // Strict isolation: clear host environment to prevent token/secret leakage (R02, T75); 严格隔离：清除主机环境变量，防止令牌/密钥泄漏（R02、T75）
            .envs(sanitized_env)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0); // Sets new process group; 创建新的进程组

        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                return CommandExecutionResponse {
                    result: Some(command_execution_response::Result::Error(ComputerError {
                        code: ComputerErrorCode::ExecutionDenied as i32,
                        message: format!("Failed to spawn process: {}", e),
                        retryable: false,
                    })),
                };
            }
        };

        let child_pid = child.id().unwrap_or(0);
        let mut process_group = (child_pid > 0).then(|| ProcessGroupGuard::new(child_pid));

        let output_limit = self.requested_output_limit(request.max_output_bytes);
        let mut stdout_collector = OutputCollector::new(Some(output_limit as i64));
        let mut stderr_collector = OutputCollector::new(Some(output_limit as i64));

        let mut child_stdout = child.stdout.take().unwrap();
        let mut child_stderr = child.stderr.take().unwrap();

        // Read stdout and stderr concurrently with cancellation & timeout checks
        // 并发读取 stdout 和 stderr，同时检查取消状态与超时。
        let run_fut = async {
            let mut stdout_buf = [0u8; 4096];
            let mut stderr_buf = [0u8; 4096];
            let mut stdout_done = false;
            let mut stderr_done = false;

            while !stdout_done || !stderr_done {
                if let Some(ref cf) = cancel_flag {
                    if cf.load(Ordering::SeqCst) {
                        return Err(ComputerError {
                            code: ComputerErrorCode::CommandTimeout as i32,
                            message: "Command execution was cancelled".to_string(),
                            retryable: false,
                        });
                    }
                }

                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_millis(30)) => {
                        // Periodic wakeup to test cancel_flag even if process is silent
                        // 定期唤醒，以便在进程无输出时仍检查 cancel_flag。
                    }
                    res = child_stdout.read(&mut stdout_buf), if !stdout_done => {
                        match res {
                            Ok(0) => stdout_done = true,
                            Ok(n) => {
                                stdout_collector.append(&stdout_buf[..n]);
                            }
                            Err(_) => stdout_done = true,
                        }
                    }
                    res = child_stderr.read(&mut stderr_buf), if !stderr_done => {
                        match res {
                            Ok(0) => stderr_done = true,
                            Ok(n) => {
                                stderr_collector.append(&stderr_buf[..n]);
                            }
                            Err(_) => stderr_done = true,
                        }
                    }
                }
            }

            let status = child.wait().await.map_err(|e| ComputerError {
                code: ComputerErrorCode::ExecutionDenied as i32,
                message: format!("Failed waiting for child process: {}", e),
                retryable: false,
            })?;

            Ok(status.code().unwrap_or(-1))
        };

        let exit_code = match timeout(timeout_duration, run_fut).await {
            Ok(Ok(code)) => code,
            Ok(Err(err)) => {
                // Cancelled or errored -> clean process group
                if let Some(group) = process_group.as_ref() {
                    group.terminate();
                }
                let _ = child.wait().await;
                if let Some(group) = process_group.as_mut() {
                    group.disarm();
                }
                return CommandExecutionResponse {
                    result: Some(command_execution_response::Result::Error(err)),
                };
            }
            Err(_) => {
                // Timeout exceeded (T80)
                if let Some(group) = process_group.as_ref() {
                    group.terminate();
                }
                let _ = child.wait().await;
                if let Some(group) = process_group.as_mut() {
                    group.disarm();
                }
                return CommandExecutionResponse {
                    result: Some(command_execution_response::Result::Error(ComputerError {
                        code: ComputerErrorCode::CommandTimeout as i32,
                        message: format!(
                            "Command timed out after {} ms",
                            timeout_duration.as_millis()
                        ),
                        retryable: false,
                    })),
                };
            }
        };

        if let Some(group) = process_group.as_mut() {
            group.disarm();
        }

        let duration_ms = start_time.elapsed().as_millis() as i64;
        let (stdout_str, stdout_truncated) = stdout_collector.into_string_lossy();
        let (stderr_str, stderr_truncated) = stderr_collector.into_string_lossy();

        // Calculate SHA-256 evidence hash of output (T76)
        // 计算输出的 SHA-256 证据哈希（T76）。
        let mut hasher = Sha256::new();
        hasher.update(stdout_str.as_bytes());
        hasher.update(b"|");
        hasher.update(stderr_str.as_bytes());
        let sha256_hash = format!("{:x}", hasher.finalize());

        let evidence = ExecutionEvidence {
            exit_code,
            duration_ms,
            stdout: stdout_str,
            stderr: stderr_str,
            stdout_truncated,
            stderr_truncated,
            sha256_hash,
            // The runtime measures wall time and captured output, not CPU or
            // process memory. Keep those unavailable counters absent.
            resource_usage: None,
        };

        CommandExecutionResponse {
            result: Some(command_execution_response::Result::Evidence(evidence)),
        }
    }

    /// Streaming managed execution (T74, T80).
    /// 流式托管执行（T74、T80）。
    pub async fn execute_command_stream(
        &self,
        request: CommandExecutionRequest,
        cancel_flag: Option<Arc<AtomicBool>>,
        sender: mpsc::Sender<CommandStreamEvent>,
    ) -> Result<(), ComputerError> {
        let seq = AtomicI64::new(1);
        let start_time = Instant::now();
        let timeout_duration = self.requested_timeout(request.timeout_ms)?;

        let cwd = self.path_validator.validate_cwd(request.cwd.as_deref())?;
        let sanitized_env = self.env_filter.sanitize_environment(&request.env);

        let mut cmd = Command::new("bash");
        cmd.arg("-c")
            .arg(&request.command)
            .current_dir(cwd)
            .env_clear() // Strict isolation: clear host environment to prevent token/secret leakage (R02, T75); 严格隔离：清除主机环境变量，防止令牌/密钥泄漏（R02、T75）
            .envs(sanitized_env)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);

        let mut child = cmd.spawn().map_err(|e| ComputerError {
            code: ComputerErrorCode::ExecutionDenied as i32,
            message: format!("Failed to spawn stream process: {}", e),
            retryable: false,
        })?;

        let child_pid = child.id().unwrap_or(0);
        let mut process_group = (child_pid > 0).then(|| ProcessGroupGuard::new(child_pid));

        let mut child_stdout = child.stdout.take().unwrap();
        let mut child_stderr = child.stderr.take().unwrap();

        let output_limit = self.requested_output_limit(request.max_output_bytes);
        let mut stdout_accum = OutputCollector::new(Some(output_limit as i64));
        let mut stderr_accum = OutputCollector::new(Some(output_limit as i64));

        let stream_fut = async {
            let mut stdout_buf = [0u8; 4096];
            let mut stderr_buf = [0u8; 4096];
            let mut stdout_done = false;
            let mut stderr_done = false;

            while !stdout_done || !stderr_done {
                if let Some(ref cf) = cancel_flag {
                    if cf.load(Ordering::SeqCst) {
                        return Err(ComputerError {
                            code: ComputerErrorCode::CommandTimeout as i32,
                            message: "Streaming cancelled by client".to_string(),
                            retryable: false,
                        });
                    }
                }

                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_millis(30)) => {
                        // Periodic wakeup to test cancel_flag
                        // 定期唤醒，以便在进程无输出时仍检查 cancel_flag。
                    }
                    res = child_stdout.read(&mut stdout_buf), if !stdout_done => {
                        match res {
                            Ok(0) => stdout_done = true,
                            Ok(n) => {
                                let chunk = &stdout_buf[..n];
                                let retained = stdout_accum.append(chunk);
                                if retained > 0 {
                                    let text = String::from_utf8_lossy(&chunk[..retained]).to_string();
                                    let _ = sender.send(CommandStreamEvent {
                                        sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                                        timestamp_ms: Self::current_epoch_ms(),
                                        event: Some(command_stream_event::Event::StdoutChunk(text)),
                                    }).await;
                                }
                            }
                            Err(_) => stdout_done = true,
                        }
                    }
                    res = child_stderr.read(&mut stderr_buf), if !stderr_done => {
                        match res {
                            Ok(0) => stderr_done = true,
                            Ok(n) => {
                                let chunk = &stderr_buf[..n];
                                let retained = stderr_accum.append(chunk);
                                if retained > 0 {
                                    let text = String::from_utf8_lossy(&chunk[..retained]).to_string();
                                    let _ = sender.send(CommandStreamEvent {
                                        sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                                        timestamp_ms: Self::current_epoch_ms(),
                                        event: Some(command_stream_event::Event::StderrChunk(text)),
                                    }).await;
                                }
                            }
                            Err(_) => stderr_done = true,
                        }
                    }
                }
            }

            let status = child.wait().await.map_err(|e| ComputerError {
                code: ComputerErrorCode::ExecutionDenied as i32,
                message: format!("Process wait failed: {}", e),
                retryable: false,
            })?;

            Ok(status.code().unwrap_or(-1))
        };

        let exit_result = timeout(timeout_duration, stream_fut).await;

        match exit_result {
            Ok(Ok(code)) => {
                if let Some(group) = process_group.as_mut() {
                    group.disarm();
                }
                let duration_ms = start_time.elapsed().as_millis() as i64;
                let (stdout_str, stdout_truncated) = stdout_accum.into_string_lossy();
                let (stderr_str, stderr_truncated) = stderr_accum.into_string_lossy();

                let mut hasher = Sha256::new();
                hasher.update(stdout_str.as_bytes());
                hasher.update(b"|");
                hasher.update(stderr_str.as_bytes());
                let sha256_hash = format!("{:x}", hasher.finalize());

                let evidence = ExecutionEvidence {
                    exit_code: code,
                    duration_ms,
                    stdout: stdout_str,
                    stderr: stderr_str,
                    stdout_truncated,
                    stderr_truncated,
                    sha256_hash,
                    resource_usage: None,
                };

                let _ = sender
                    .send(CommandStreamEvent {
                        sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                        timestamp_ms: Self::current_epoch_ms(),
                        event: Some(command_stream_event::Event::ExitEvidence(evidence)),
                    })
                    .await;
                Ok(())
            }
            Ok(Err(err)) => {
                if let Some(group) = process_group.as_ref() {
                    group.terminate();
                }
                let _ = child.wait().await;
                if let Some(group) = process_group.as_mut() {
                    group.disarm();
                }
                let _ = sender
                    .send(CommandStreamEvent {
                        sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                        timestamp_ms: Self::current_epoch_ms(),
                        event: Some(command_stream_event::Event::StreamError(err)),
                    })
                    .await;
                Ok(())
            }
            Err(_) => {
                if let Some(group) = process_group.as_ref() {
                    group.terminate();
                }
                let _ = child.wait().await;
                if let Some(group) = process_group.as_mut() {
                    group.disarm();
                }
                let err = ComputerError {
                    code: ComputerErrorCode::CommandTimeout as i32,
                    message: "Stream command execution timed out".to_string(),
                    retryable: false,
                };
                let _ = sender
                    .send(CommandStreamEvent {
                        sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                        timestamp_ms: Self::current_epoch_ms(),
                        event: Some(command_stream_event::Event::StreamError(err)),
                    })
                    .await;
                Ok(())
            }
        }
    }

    fn requested_timeout(&self, requested_ms: Option<i64>) -> Result<Duration, ComputerError> {
        let timeout_ms = match requested_ms {
            Some(value) if value <= 0 => {
                return Err(ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: "Command timeout must be a positive number of milliseconds".into(),
                    retryable: false,
                });
            }
            Some(value) => (value as u64).min(self.max_timeout_ms),
            None => self.default_timeout_ms,
        };
        Ok(Duration::from_millis(timeout_ms))
    }

    fn requested_output_limit(&self, requested_bytes: Option<i64>) -> usize {
        match requested_bytes {
            Some(value) if value > 0 => (value as usize).min(self.max_output_bytes),
            _ => self.max_output_bytes,
        }
    }
}
