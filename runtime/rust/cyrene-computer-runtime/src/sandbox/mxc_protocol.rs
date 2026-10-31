//! Host-only protocol shared by the optional MXC adapter and its helper.
//!
//! The control request and receipt live in a private host temporary directory
//! outside the workspace. The MXC workload receives only the request's
//! filtered workload environment, never the helper's control environment.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

pub const MXC_CONTROL_SCHEMA_VERSION: u32 = 2;
pub const MAX_MXC_CONTROL_REQUEST_BYTES: usize = 1024 * 1024;
pub const MAX_MXC_TERMINAL_RECEIPT_BYTES: usize = 4096;
pub const MAX_MXC_RECEIPT_MESSAGE_BYTES: usize = 1536;
pub const MAX_MXC_RECEIPT_WARNINGS: usize = 8;

/// Canonical fixed executable search directories used only by the host MXC SDK.
///
/// The helper's own directory comes first so an image may ship a pinned
/// Bubblewrap binary beside it. Remaining directories are fixed deployment
/// paths; no request, workload, or inherited user PATH participates.
pub fn trusted_control_directories(executable_path: &Path) -> Result<Vec<PathBuf>, String> {
    let helper_directory = executable_path
        .parent()
        .ok_or_else(|| "MXC helper executable has no parent directory".to_string())?
        .canonicalize()
        .map_err(|error| format!("cannot resolve MXC helper directory: {error}"))?;
    if helper_directory
        .to_str()
        .is_none_or(|value| value.is_empty() || value.contains(':'))
    {
        return Err("MXC helper directory cannot be represented safely in PATH".into());
    }
    let mut directories = Vec::new();
    for candidate in [
        helper_directory,
        PathBuf::from("/usr/local/sbin"),
        PathBuf::from("/usr/local/bin"),
        PathBuf::from("/usr/sbin"),
        PathBuf::from("/usr/bin"),
        PathBuf::from("/sbin"),
        PathBuf::from("/bin"),
    ] {
        if !candidate.is_dir() {
            continue;
        }
        let canonical = candidate
            .canonicalize()
            .map_err(|error| format!("cannot resolve fixed MXC control directory: {error}"))?;
        if !directories.contains(&canonical) {
            directories.push(canonical);
        }
    }
    Ok(directories)
}

/// Joins only the trusted directories returned by [`trusted_control_directories`].
pub fn trusted_control_path(directories: &[PathBuf]) -> Result<String, String> {
    let entries = directories
        .iter()
        .map(|path| {
            if !path.is_absolute() {
                return Err("MXC control directory must be absolute".to_string());
            }
            let value = path
                .to_str()
                .ok_or_else(|| "MXC control directory is not valid UTF-8".to_string())?;
            if value.is_empty() || value.contains(':') {
                return Err(
                    "MXC control directory contains a PATH separator or empty entry".into(),
                );
            }
            let canonical = path.canonicalize().map_err(|error| {
                format!(
                    "cannot resolve MXC control directory '{}': {error}",
                    path.display()
                )
            })?;
            if canonical.to_str() != Some(value) {
                return Err("MXC control directory must be canonical".into());
            }
            Ok(value.to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    if entries.is_empty() || entries.iter().any(String::is_empty) {
        return Err("no trusted MXC control directories are available".into());
    }
    Ok(entries.join(":"))
}

#[cfg(test)]
mod tests {
    use super::{trusted_control_path, MxcControlRequest, MXC_CONTROL_SCHEMA_VERSION};
    use std::collections::BTreeMap;
    use std::path::PathBuf;

    #[test]
    fn trusted_path_rejects_separator_injection_and_noncanonical_entries() {
        let temp = tempfile::tempdir().unwrap();
        let injected = temp.path().join("trusted:/writable");
        std::fs::create_dir_all(&injected).unwrap();
        let canonical = injected.canonicalize().unwrap();
        assert!(trusted_control_path(&[canonical]).is_err());

        let ordinary = temp.path().canonicalize().unwrap();
        let noncanonical = PathBuf::from(format!("{}/.", ordinary.display()));
        assert!(trusted_control_path(&[noncanonical]).is_err());
    }

    #[test]
    fn control_request_carries_an_explicit_workload_deadline() {
        let request = MxcControlRequest {
            schema_version: MXC_CONTROL_SCHEMA_VERSION,
            command: "true".into(),
            start_marker: "CYRENE_MXC_STARTED_0123456789abcdef0123456789abcdef".into(),
            timeout_ms: 321,
            supervisor_pid: 42,
            workspace_roots: vec!["/workspace".into()],
            working_directory: "/workspace".into(),
            environment: BTreeMap::new(),
        };
        let json = serde_json::to_string(&request).unwrap();
        let decoded: MxcControlRequest = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded.timeout_ms, 321);
        assert_eq!(decoded.supervisor_pid, 42);
    }
}

/// Rejects writable workspace roots that overlap the helper or its fixed PATH.
pub fn ensure_control_directories_outside_workspaces(
    directories: &[PathBuf],
    workspace_roots: &[PathBuf],
) -> Result<(), String> {
    for directory in directories {
        for root in workspace_roots {
            if directory.starts_with(root) || root.starts_with(directory) {
                return Err(format!(
                    "configured workspace overlaps trusted MXC control directory '{}'",
                    directory.display()
                ));
            }
        }
    }
    Ok(())
}

/// Strict host-to-helper policy envelope.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MxcControlRequest {
    pub schema_version: u32,
    /// Host process that owns this helper and is authorized to supervise it.
    pub supervisor_pid: u32,
    pub command: String,
    pub start_marker: String,
    pub timeout_ms: u32,
    pub workspace_roots: Vec<String>,
    pub working_directory: String,
    pub environment: BTreeMap<String, String>,
}

/// Strict terminal status written only after the workload and output readers end.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case", deny_unknown_fields)]
pub enum MxcTerminalReceipt {
    Exited {
        schema_version: u32,
        exit_code: i32,
        duration_ms: u64,
        warnings: Vec<String>,
    },
    TimedOut {
        schema_version: u32,
        duration_ms: u64,
        message: String,
    },
    BackendError {
        schema_version: u32,
        message: String,
    },
    BackendUnavailable {
        schema_version: u32,
        message: String,
    },
}
