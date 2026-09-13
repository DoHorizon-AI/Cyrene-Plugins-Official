// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 path_validator.rs                                               │
// │  Package: cyrene-computer-runtime::security                         │
// │  Role: Path allowlist and traversal protection (T75, T80).          │
// │                                                                     │
// │  模块职责：路径白名单校验，杜绝相对路径穿越与软链接逃逸                │
// └─────────────────────────────────────────────────────────────────────┘

use cyrene_plugin_contracts::computer_runtime_v1::{ComputerError, ComputerErrorCode};
use std::path::{Component, Path, PathBuf};

#[derive(Clone, Debug)]
pub struct PathValidator {
    allowed_roots: Vec<PathBuf>,
}

impl PathValidator {
    pub fn new(allowed_roots: Vec<PathBuf>) -> Self {
        let canonical_roots: Vec<PathBuf> = allowed_roots
            .into_iter()
            .filter_map(|p| p.canonicalize().ok())
            .collect();
        Self {
            allowed_roots: canonical_roots,
        }
    }

    /// Validates and resolves a path against allowed roots.
    /// Rejects path traversal (`..`), empty paths, and symlinks escaping the roots.
    pub fn validate_path(&self, raw_path: &str) -> Result<PathBuf, ComputerError> {
        if raw_path.trim().is_empty() {
            return Err(ComputerError {
                code: ComputerErrorCode::PathTraversalDenied as i32,
                message: "Path cannot be empty".to_string(),
                retryable: false,
            });
        }

        let input_path = Path::new(raw_path);

        // Disallow suspicious components in non-canonical input
        for comp in input_path.components() {
            if matches!(comp, Component::ParentDir) {
                // If path contains `..`, verify it strictly after canonicalization or relative resolution
            }
        }

        // If path is relative, resolve it relative to the first allowed root
        let full_path = if input_path.is_absolute() {
            input_path.to_path_buf()
        } else {
            if let Some(first_root) = self.allowed_roots.first() {
                first_root.join(input_path)
            } else {
                return Err(ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: "No allowed filesystem roots configured".to_string(),
                    retryable: false,
                });
            }
        };

        // Canonicalize to resolve symlinks and `..`
        let resolved = if full_path.exists() {
            full_path.canonicalize().map_err(|e| ComputerError {
                code: ComputerErrorCode::PathTraversalDenied as i32,
                message: format!("Failed to canonicalize path: {}", e),
                retryable: false,
            })?
        } else {
            // For write/create operations, validate the existing parent directory
            let parent = full_path.parent().ok_or_else(|| ComputerError {
                code: ComputerErrorCode::PathTraversalDenied as i32,
                message: "Path has no parent directory".to_string(),
                retryable: false,
            })?;

            let parent_canonical = parent.canonicalize().map_err(|e| ComputerError {
                code: ComputerErrorCode::PathTraversalDenied as i32,
                message: format!("Parent directory does not exist or inaccessible: {}", e),
                retryable: false,
            })?;

            if let Some(file_name) = full_path.file_name() {
                parent_canonical.join(file_name)
            } else {
                return Err(ComputerError {
                    code: ComputerErrorCode::PathTraversalDenied as i32,
                    message: "Invalid file name".to_string(),
                    retryable: false,
                });
            }
        };

        // Check if resolved path is prefixed by any allowed root
        let is_allowed = self
            .allowed_roots
            .iter()
            .any(|root| resolved.starts_with(root));

        if !is_allowed {
            return Err(ComputerError {
                code: ComputerErrorCode::PathTraversalDenied as i32,
                message: format!("Path '{}' escapes all allowed root boundaries", raw_path),
                retryable: false,
            });
        }

        Ok(resolved)
    }

    /// Validates working directory for shell execution.
    pub fn validate_cwd(&self, cwd: Option<&str>) -> Result<PathBuf, ComputerError> {
        match cwd {
            Some(p) => {
                let resolved = self.validate_path(p)?;
                if !resolved.is_dir() {
                    return Err(ComputerError {
                        code: ComputerErrorCode::PathTraversalDenied as i32,
                        message: format!("Working directory '{}' is not a directory", p),
                        retryable: false,
                    });
                }
                Ok(resolved)
            }
            None => {
                if let Some(root) = self.allowed_roots.first() {
                    Ok(root.clone())
                } else {
                    Err(ComputerError {
                        code: ComputerErrorCode::ExecutionDenied as i32,
                        message: "No default working directory available".to_string(),
                        retryable: false,
                    })
                }
            }
        }
    }
}
