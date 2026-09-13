// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 artifact_store.rs                                               │
// │  Package: cyrene-computer-runtime::artifact                         │
// │  Role: Separated artifact-only transfer store (T74, T75, R03).      │
// │                                                                     │
// │  模块职责：Artifact-only 数据传输存储。大对象/产物通过稳定 ID/token    │
// │           落盘持久化，内存仅保留元数据索引，严格校验 SHA-256 完整性      │
// └─────────────────────────────────────────────────────────────────────┘

use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::RwLock;
use std::time::{SystemTime, UNIX_EPOCH};

use cyrene_plugin_contracts::computer_runtime_v1::{
    create_artifact_response, get_artifact_response, ArtifactMetadata, ArtifactPayload,
    ComputerError, ComputerErrorCode, CreateArtifactRequest, CreateArtifactResponse,
    GetArtifactRequest, GetArtifactResponse,
};

/// Maximum inline payload limit for a single RPC artifact creation (16 MB).
pub const MAX_INLINE_PAYLOAD_BYTES: usize = 16 * 1024 * 1024;

/// In-memory index entry pointing to the externalized artifact file on disk.
/// Crucial invariant (R03): No payload byte buffers are retained in RAM!
#[derive(Clone, Debug)]
pub struct ArtifactIndexEntry {
    pub metadata: ArtifactMetadata,
    pub disk_path: PathBuf,
}

pub struct ArtifactStore {
    storage_dir: PathBuf,
    index: RwLock<HashMap<String, ArtifactIndexEntry>>,
}

impl Default for ArtifactStore {
    fn default() -> Self {
        Self::new()
    }
}

impl ArtifactStore {
    /// Creates an ArtifactStore using default ephemeral directory.
    pub fn new() -> Self {
        let default_dir = std::env::temp_dir().join("cyrene-artifacts");
        Self::with_storage_dir(default_dir)
    }

    /// Creates an ArtifactStore with an explicit storage directory on disk.
    pub fn with_storage_dir(dir: PathBuf) -> Self {
        let _ = std::fs::create_dir_all(&dir);
        Self {
            storage_dir: dir,
            index: RwLock::new(HashMap::new()),
        }
    }

    /// Returns the active storage directory on disk.
    pub fn storage_dir(&self) -> &PathBuf {
        &self.storage_dir
    }

    /// Resolves the filesystem path for an artifact by its opaque token,
    /// enabling external out-of-band transfer without passing bytes over RPC.
    pub fn get_artifact_path(&self, artifact_id: &str) -> Option<PathBuf> {
        let map = self.index.read().unwrap();
        map.get(artifact_id).map(|e| e.disk_path.clone())
    }

    /// Creates an artifact: writes payload directly to disk, hashes with SHA-256,
    /// and indexes only the metadata and file path. Zero payload bytes retained in memory.
    pub fn create_artifact(&self, req: CreateArtifactRequest) -> CreateArtifactResponse {
        if req.name.trim().is_empty() {
            return CreateArtifactResponse {
                result: Some(create_artifact_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: "Artifact name cannot be empty".to_string(),
                    retryable: false,
                })),
            };
        }

        if req.data.len() > MAX_INLINE_PAYLOAD_BYTES {
            return CreateArtifactResponse {
                result: Some(create_artifact_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::OutputLimitExceeded as i32,
                    message: format!(
                        "Artifact size {} bytes exceeds max inline limit of {} bytes",
                        req.data.len(),
                        MAX_INLINE_PAYLOAD_BYTES
                    ),
                    retryable: false,
                })),
            };
        }

        // Calculate SHA-256
        let mut hasher = Sha256::new();
        hasher.update(&req.data);
        let sha256_hash = format!("{:x}", hasher.finalize());

        let created_at_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0);

        // Opaque token format: art-{prefix}-{short_time}
        let artifact_id = format!("art-{}-{}", &sha256_hash[..12], created_at_ms % 100000);
        let size_bytes = req.data.len() as i64;
        let mime_type = if req.mime_type.is_empty() {
            "application/octet-stream".to_string()
        } else {
            req.mime_type
        };

        let target_file = self.storage_dir.join(format!("{}.bin", artifact_id));

        // Write directly to disk (externalization)
        if let Err(e) = std::fs::create_dir_all(&self.storage_dir) {
            return CreateArtifactResponse {
                result: Some(create_artifact_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: format!("Failed to create artifact storage directory: {}", e),
                    retryable: false,
                })),
            };
        }

        if let Err(e) = std::fs::write(&target_file, &req.data) {
            return CreateArtifactResponse {
                result: Some(create_artifact_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: format!("Failed to persist artifact to disk: {}", e),
                    retryable: false,
                })),
            };
        }

        let metadata = ArtifactMetadata {
            artifact_id: artifact_id.clone(),
            name: req.name,
            mime_type,
            size_bytes,
            created_at_ms,
            sha256_hash,
        };

        let entry = ArtifactIndexEntry {
            metadata: metadata.clone(),
            disk_path: target_file,
        };

        let mut map = self.index.write().unwrap();
        map.insert(artifact_id, entry);

        CreateArtifactResponse {
            result: Some(create_artifact_response::Result::Artifact(metadata)),
        }
    }

    /// Retrieves an artifact by its opaque token, reading bytes on-demand from external disk.
    pub fn get_artifact(&self, req: GetArtifactRequest) -> GetArtifactResponse {
        let (metadata, path) = {
            let map = self.index.read().unwrap();
            match map.get(&req.artifact_id) {
                Some(entry) => (entry.metadata.clone(), entry.disk_path.clone()),
                None => {
                    return GetArtifactResponse {
                        result: Some(get_artifact_response::Result::Error(ComputerError {
                            code: ComputerErrorCode::ArtifactNotFound as i32,
                            message: format!(
                                "Artifact '{}' not found in store index",
                                req.artifact_id
                            ),
                            retryable: false,
                        })),
                    };
                }
            }
        };

        // Read bytes from disk on demand
        match std::fs::read(&path) {
            Ok(bytes) => {
                // Verify SHA-256 integrity on retrieval
                let mut hasher = Sha256::new();
                hasher.update(&bytes);
                let actual_hash = format!("{:x}", hasher.finalize());
                if actual_hash != metadata.sha256_hash {
                    return GetArtifactResponse {
                        result: Some(get_artifact_response::Result::Error(ComputerError {
                            code: ComputerErrorCode::ExecutionDenied as i32,
                            message: format!(
                                "Corrupted artifact '{}': hash mismatch",
                                req.artifact_id
                            ),
                            retryable: false,
                        })),
                    };
                }

                GetArtifactResponse {
                    result: Some(get_artifact_response::Result::Payload(ArtifactPayload {
                        metadata: Some(metadata),
                        data: bytes,
                    })),
                }
            }
            Err(e) => GetArtifactResponse {
                result: Some(get_artifact_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::ArtifactNotFound as i32,
                    message: format!("Failed to read artifact file from disk: {}", e),
                    retryable: false,
                })),
            },
        }
    }
}
