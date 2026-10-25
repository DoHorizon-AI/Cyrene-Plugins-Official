//! Host-only protocol shared by the optional MXC adapter and its helper.
//!
//! The control request and receipt live in a private host temporary directory
//! outside the workspace. The MXC workload receives only the request's
//! filtered workload environment, never the helper's control environment.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

pub const MXC_CONTROL_SCHEMA_VERSION: u32 = 1;
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
            path.to_str()
                .map(str::to_string)
                .ok_or_else(|| "MXC control directory is not valid UTF-8".to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    if entries.is_empty() {
        return Err("no trusted MXC control directories are available".into());
    }
    Ok(entries.join(":"))
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
    pub command: String,
    pub start_marker: String,
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
