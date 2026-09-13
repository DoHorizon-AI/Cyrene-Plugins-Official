// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 browser_service.rs                                              │
// │  Package: cyrene-computer-runtime::browser                          │
// │  Role: Separated browser boundary operations (T74, R04).            │
// │                                                                     │
// │  模块职责：完全独立的浏览器操作边界（与 Shell、Filesystem 解耦）。    │
// │           严格进行环境可用性探测，在无真实无头环境时明确拒绝并报错，   │
// │           严禁空返回虚假的导航成功                                   │
// └─────────────────────────────────────────────────────────────────────┘

use cyrene_plugin_contracts::computer_runtime_v1::{ComputerError, ComputerErrorCode};
use std::path::PathBuf;
use std::time::Duration;
use tokio::process::Command;

#[derive(Clone, Debug)]
pub enum BrowserBackend {
    /// Real headless browser binary discovered or configured
    HeadlessBinary(PathBuf),
    /// Controlled test driver with explicit mock response
    MockDriver { response: String },
    /// No headless browser available in environment
    Unavailable { reason: String },
}

pub struct BrowserService {
    backend: BrowserBackend,
    timeout: Duration,
}

impl Default for BrowserService {
    fn default() -> Self {
        Self::new()
    }
}

impl BrowserService {
    /// Creates a BrowserService by actively probing the host system environment.
    pub fn new() -> Self {
        Self::with_backend(Self::probe_environment())
    }

    /// Creates a BrowserService with an explicit backend.
    pub fn with_backend(backend: BrowserBackend) -> Self {
        Self {
            backend,
            timeout: Duration::from_secs(15),
        }
    }

    /// Creates a BrowserService with a controlled mock driver for testing.
    pub fn with_mock_driver(response: impl Into<String>) -> Self {
        Self::with_backend(BrowserBackend::MockDriver {
            response: response.into(),
        })
    }

    /// Returns whether a functional browser backend is available.
    pub fn is_available(&self) -> bool {
        !matches!(self.backend, BrowserBackend::Unavailable { .. })
    }

    /// Returns the active backend description.
    pub fn backend(&self) -> &BrowserBackend {
        &self.backend
    }

    /// Probes the system for available headless browser binaries.
    pub fn probe_environment() -> BrowserBackend {
        // 1. Check explicit environment override
        if let Ok(bin_str) = std::env::var("CYRENE_HEADLESS_BROWSER_BIN") {
            let p = PathBuf::from(bin_str);
            if p.is_file() {
                return BrowserBackend::HeadlessBinary(p);
            }
        }

        // 2. Probe standard binary locations in PATH
        for candidate in &[
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
        ] {
            if let Ok(output) = std::process::Command::new("which").arg(candidate).output() {
                if output.status.success() {
                    let path_str = String::from_utf8_lossy(&output.stdout).trim().to_string();
                    if !path_str.is_empty() {
                        let p = PathBuf::from(path_str);
                        if p.exists() {
                            return BrowserBackend::HeadlessBinary(p);
                        }
                    }
                }
            }
        }

        BrowserBackend::Unavailable {
            reason: "Headless browser (chromium/chrome) is not installed on host and CYRENE_HEADLESS_BROWSER_BIN is not set".to_string(),
        }
    }

    /// Isolated browser navigate hook.
    /// Invariant (R04): Never returns fake success when no real browser backend exists!
    pub async fn navigate(&self, target_url: &str) -> Result<String, ComputerError> {
        // 1. Validate scheme strictly
        if !target_url.starts_with("http://") && !target_url.starts_with("https://") {
            return Err(ComputerError {
                code: ComputerErrorCode::ExecutionDenied as i32,
                message: format!("Invalid URL scheme for browser execution: {}", target_url),
                retryable: false,
            });
        }

        // 2. Dispatch according to backend
        match &self.backend {
            BrowserBackend::Unavailable { reason } => Err(ComputerError {
                code: ComputerErrorCode::ExecutionDenied as i32,
                message: format!("Browser execution denied: {}", reason),
                retryable: false,
            }),
            BrowserBackend::MockDriver { response } => Ok(format!(
                "[MockDriver] Navigated to {}: {}",
                target_url, response
            )),
            BrowserBackend::HeadlessBinary(bin_path) => {
                // Execute headless browser with sandboxed dump-dom
                let mut cmd = Command::new(bin_path);
                cmd.arg("--headless")
                    .arg("--disable-gpu")
                    .arg("--dump-dom")
                    .arg(target_url)
                    .env_clear();

                let output_res = tokio::time::timeout(self.timeout, cmd.output()).await;
                match output_res {
                    Ok(Ok(output)) => {
                        if output.status.success() {
                            let stdout = String::from_utf8_lossy(&output.stdout);
                            Ok(stdout.chars().take(2000).collect())
                        } else {
                            let stderr = String::from_utf8_lossy(&output.stderr);
                            Err(ComputerError {
                                code: ComputerErrorCode::ExecutionDenied as i32,
                                message: format!(
                                    "Headless browser process exited with error: {}",
                                    stderr
                                ),
                                retryable: false,
                            })
                        }
                    }
                    Ok(Err(e)) => Err(ComputerError {
                        code: ComputerErrorCode::ExecutionDenied as i32,
                        message: format!("Failed to spawn headless browser binary: {}", e),
                        retryable: false,
                    }),
                    Err(_) => Err(ComputerError {
                        code: ComputerErrorCode::CommandTimeout as i32,
                        message: format!("Browser navigation to '{}' timed out", target_url),
                        retryable: true,
                    }),
                }
            }
        }
    }
}
