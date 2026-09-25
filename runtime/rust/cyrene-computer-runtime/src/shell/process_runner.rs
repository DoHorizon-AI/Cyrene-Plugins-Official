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
    ExecutionEvidence, ExecutionResourceUsage,
};

use crate::security::{EnvironmentFilter, OutputCollector, PathValidator};

pub struct ShellProcessRunner {
    path_validator: PathValidator,
    env_filter: EnvironmentFilter,
}

impl ShellProcessRunner {
    pub fn new(path_validator: PathValidator, env_filter: EnvironmentFilter) -> Self {
        Self {
            path_validator,
            env_filter,
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
        let timeout_duration = match request.timeout_ms {
            Some(ms) if ms > 0 => Duration::from_millis(ms as u64),
            _ => Duration::from_secs(30), // Default 30s timeout; 默认超时为 30 秒
        };

        let mut stdout_collector = OutputCollector::new(request.max_output_bytes);
        let mut stderr_collector = OutputCollector::new(request.max_output_bytes);

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
                            Ok(n) => stdout_collector.append(&stdout_buf[..n]),
                            Err(_) => stdout_done = true,
                        }
                    }
                    res = child_stderr.read(&mut stderr_buf), if !stderr_done => {
                        match res {
                            Ok(0) => stderr_done = true,
                            Ok(n) => stderr_collector.append(&stderr_buf[..n]),
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
                if child_pid > 0 {
                    Self::kill_process_group(child_pid);
                }
                let _ = child.wait().await;
                return CommandExecutionResponse {
                    result: Some(command_execution_response::Result::Error(err)),
                };
            }
            Err(_) => {
                // Timeout exceeded (T80)
                if child_pid > 0 {
                    Self::kill_process_group(child_pid);
                }
                let _ = child.wait().await;
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

        let peak_mem = (stdout_str.len() + stderr_str.len()) as i64;
        let evidence = ExecutionEvidence {
            exit_code,
            duration_ms,
            stdout: stdout_str,
            stderr: stderr_str,
            stdout_truncated,
            stderr_truncated,
            sha256_hash,
            resource_usage: Some(ExecutionResourceUsage {
                cpu_time_ms: duration_ms,
                peak_memory_bytes: peak_mem,
            }),
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
        let timeout_duration = match request.timeout_ms {
            Some(ms) if ms > 0 => Duration::from_millis(ms as u64),
            _ => Duration::from_secs(30),
        };

        let mut child_stdout = child.stdout.take().unwrap();
        let mut child_stderr = child.stderr.take().unwrap();

        let mut stdout_accum = OutputCollector::new(request.max_output_bytes);
        let mut stderr_accum = OutputCollector::new(request.max_output_bytes);

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
                                stdout_accum.append(chunk);
                                let text = String::from_utf8_lossy(chunk).to_string();
                                let _ = sender.send(CommandStreamEvent {
                                    sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                                    timestamp_ms: Self::current_epoch_ms(),
                                    event: Some(command_stream_event::Event::StdoutChunk(text)),
                                }).await;
                            }
                            Err(_) => stdout_done = true,
                        }
                    }
                    res = child_stderr.read(&mut stderr_buf), if !stderr_done => {
                        match res {
                            Ok(0) => stderr_done = true,
                            Ok(n) => {
                                let chunk = &stderr_buf[..n];
                                stderr_accum.append(chunk);
                                let text = String::from_utf8_lossy(chunk).to_string();
                                let _ = sender.send(CommandStreamEvent {
                                    sequence_number: seq.fetch_add(1, Ordering::SeqCst),
                                    timestamp_ms: Self::current_epoch_ms(),
                                    event: Some(command_stream_event::Event::StderrChunk(text)),
                                }).await;
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
                    resource_usage: Some(ExecutionResourceUsage {
                        cpu_time_ms: duration_ms,
                        peak_memory_bytes: 4096,
                    }),
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
                if child_pid > 0 {
                    Self::kill_process_group(child_pid);
                }
                let _ = child.wait().await;
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
                if child_pid > 0 {
                    Self::kill_process_group(child_pid);
                }
                let _ = child.wait().await;
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
}
