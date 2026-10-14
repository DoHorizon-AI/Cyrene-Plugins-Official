// SPDX-License-Identifier: Apache-2.0
//! Native subprocess wrapper for official @wecom/cli in Rust.

use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use tokio::process::Command;
use tracing::{debug, error, info};

use crate::error::WeComError;

#[derive(Debug, Clone)]
pub struct WeComCliClient {
    binary_path: PathBuf,
}

impl WeComCliClient {
    pub fn new(custom_path: Option<&str>) -> Result<Self, WeComError> {
        let bin = Self::resolve_binary(custom_path)?;
        Ok(Self { binary_path: bin })
    }

    fn resolve_binary(custom_path: Option<&str>) -> Result<PathBuf, WeComError> {
        if let Some(p) = custom_path {
            let path = PathBuf::from(p);
            if path.exists() {
                return Ok(path);
            }
            return Err(WeComError::Cli(format!(
                "Specified CLI path does not exist: {p}"
            )));
        }

        // 1. Check relative ./bin/wecom-cli or current exe dir
        if let Ok(exe_path) = std::env::current_exe() {
            if let Some(parent) = exe_path.parent() {
                let candidate = parent.join("wecom-cli");
                if candidate.exists() {
                    return Ok(candidate);
                }
            }
        }

        let local_bin = PathBuf::from("bin/wecom-cli");
        if local_bin.exists() {
            return Ok(local_bin);
        }

        // 2. Check PATH by running `which wecom-cli` or probing
        if let Ok(path) = which::which("wecom-cli") {
            return Ok(path);
        }

        // Default to command name in PATH
        Ok(PathBuf::from("wecom-cli"))
    }

    pub fn binary_path(&self) -> &Path {
        &self.binary_path
    }

    /// Execute raw CLI command with arguments.
    pub async fn run_args(&self, args: &[&str]) -> Result<String, WeComError> {
        debug!("Executing WeCom CLI: {:?} {:?}", self.binary_path, args);

        let mut cmd = Command::new(&self.binary_path);
        cmd.args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        let output = cmd.output().await.map_err(|e| {
            WeComError::Cli(format!("Failed to execute {:?}: {e}", self.binary_path))
        })?;

        let stdout = String::from_utf8_lossy(&output.stdout).to_string();
        let stderr = String::from_utf8_lossy(&output.stderr).to_string();

        if !output.status.success() {
            let code = output.status.code().unwrap_or(-1);
            error!("WeCom CLI error (exit {}): {}", code, stderr.trim());
            return Err(WeComError::Cli(format!(
                "CLI failed with exit code {}: {}",
                code,
                if !stderr.trim().is_empty() {
                    stderr.trim()
                } else {
                    stdout.trim()
                }
            )));
        }

        Ok(stdout)
    }

    /// Check CLI version (requires >= 1.2.1).
    pub async fn check_version(&self) -> Result<String, WeComError> {
        let out = self.run_args(&["--version"]).await?;
        let ver = out.trim().trim_start_matches("wecom-cli").trim();
        info!("Detected WeCom CLI version: {}", ver);
        Ok(ver.to_string())
    }

    /// Check if CLI credentials are authorized.
    pub async fn check_auth_status(&self) -> Result<bool, WeComError> {
        match self.run_args(&["auth", "show", "--status"]).await {
            Ok(out) => {
                let status = out.trim();
                Ok(status.contains("authorized"))
            }
            Err(e) => {
                error!("Auth status check failed: {e}");
                Ok(false)
            }
        }
    }

    /// Fetch dynamic JSON schema for a service or method.
    pub async fn get_schema(&self, service: &str) -> Result<Value, WeComError> {
        let out = self.run_args(&[service, "--schema"]).await?;
        let val: Value = serde_json::from_str(&out)?;
        Ok(val)
    }
}

// Minimal which probe without requiring the which crate
mod which {
    use std::env;
    use std::path::PathBuf;

    pub fn which(name: &str) -> Result<PathBuf, ()> {
        if let Ok(paths) = env::var("PATH") {
            for dir in env::split_paths(&paths) {
                let full = dir.join(name);
                if full.is_file() {
                    return Ok(full);
                }
            }
        }
        Err(())
    }
}
