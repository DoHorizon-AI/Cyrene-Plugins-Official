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
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::RwLock;
use std::time::{SystemTime, UNIX_EPOCH};

use cyrene_plugin_contracts::computer_runtime_v1::{
    create_artifact_response, get_artifact_response, ArtifactMetadata, ArtifactPayload,
    ComputerError, ComputerErrorCode, CreateArtifactRequest, CreateArtifactResponse,
    GetArtifactRequest, GetArtifactResponse,
};

/// Maximum inline payload limit for a single RPC artifact creation (16 MB).
/// 单次 RPC artifact 创建允许内联的最大 payload 大小（16 MB）。
pub const MAX_INLINE_PAYLOAD_BYTES: usize = 16 * 1024 * 1024;

/// In-memory index entry pointing to the externalized artifact file on disk.
/// Crucial invariant (R03): No payload byte buffers are retained in RAM!
/// 指向磁盘上外置 artifact 文件的内存索引项。
/// 关键不变量（R03）：RAM 中不会保留任何 payload 字节缓冲区！
#[derive(Clone, Debug)]
pub struct ArtifactIndexEntry {
    pub metadata: ArtifactMetadata,
    pub disk_path: PathBuf,
}

pub struct ArtifactStore {
    storage_dir: PathBuf,
    index: RwLock<HashMap<String, ArtifactIndexEntry>>,
    max_inline_payload_bytes: usize,
    initialization_error: Option<String>,
}

#[derive(serde::Serialize, serde::Deserialize)]
struct PersistedArtifactMetadata {
    artifact_id: String,
    name: String,
    mime_type: String,
    size_bytes: i64,
    sha256_hash: String,
    created_at_ms: i64,
}

static ARTIFACT_ID_COUNTER: AtomicU64 = AtomicU64::new(1);

impl Default for ArtifactStore {
    fn default() -> Self {
        Self::new()
    }
}

impl ArtifactStore {
    /// Creates an ArtifactStore using default ephemeral directory.
    /// 使用默认临时目录创建 ArtifactStore。
    pub fn new() -> Self {
        let default_dir = std::env::temp_dir().join("cyrene-artifacts");
        Self::with_storage_dir(default_dir)
    }

    /// Creates an ArtifactStore with an explicit storage directory on disk.
    /// 在磁盘上使用显式存储目录创建 ArtifactStore。
    pub fn with_storage_dir(dir: PathBuf) -> Self {
        Self::with_storage_dir_and_limit(dir, MAX_INLINE_PAYLOAD_BYTES)
    }

    /// Creates a disk-backed artifact store and restores metadata after restart.
    /// 创建磁盘制品存储，并在服务重启后恢复其元数据索引。
    pub fn with_storage_dir_and_limit(dir: PathBuf, max_payload_bytes: usize) -> Self {
        let max_inline_payload_bytes = max_payload_bytes.clamp(1, MAX_INLINE_PAYLOAD_BYTES);
        let (index, initialization_error) = match restore_index(&dir, max_inline_payload_bytes) {
            Ok(index) => (index, None),
            Err(error) => (HashMap::new(), Some(error)),
        };
        Self {
            storage_dir: dir,
            index: RwLock::new(index),
            max_inline_payload_bytes,
            initialization_error,
        }
    }

    /// Returns the active storage directory on disk.
    /// 返回当前使用的磁盘存储目录。
    pub fn storage_dir(&self) -> &PathBuf {
        &self.storage_dir
    }

    /// Resolves the filesystem path for an artifact by its opaque token,
    /// enabling external out-of-band transfer without passing bytes over RPC.
    /// 根据不透明 token 解析 artifact 的文件系统路径，
    /// 使调用方无需通过 RPC 传输字节即可执行带外传输。
    pub fn get_artifact_path(&self, artifact_id: &str) -> Option<PathBuf> {
        let map = self.index.read().unwrap();
        map.get(artifact_id).map(|e| e.disk_path.clone())
    }

    /// Creates an artifact: writes payload directly to disk, hashes with SHA-256,
    /// and indexes only the metadata and file path. Zero payload bytes retained in memory.
    /// 创建 artifact：将 payload 直接写入磁盘，计算 SHA-256，并仅索引 metadata 和文件路径。内存中不保留 payload 字节。
    pub fn create_artifact(&self, req: CreateArtifactRequest) -> CreateArtifactResponse {
        if let Some(error) = &self.initialization_error {
            return create_error(ComputerErrorCode::ExecutionDenied, error.clone());
        }
        if req.name.trim().is_empty() {
            return create_error(
                ComputerErrorCode::ExecutionDenied,
                "Artifact name cannot be empty".to_string(),
            );
        }
        if req.name.len() > 4 * 1024 || req.mime_type.len() > 256 {
            return create_error(
                ComputerErrorCode::OutputLimitExceeded,
                "Artifact name or MIME type exceeds its configured metadata limit".to_string(),
            );
        }

        if req.data.len() > self.max_inline_payload_bytes {
            return create_error(
                ComputerErrorCode::OutputLimitExceeded,
                format!(
                    "Artifact size {} bytes exceeds configured maximum of {} bytes",
                    req.data.len(),
                    self.max_inline_payload_bytes
                ),
            );
        }

        // Calculate SHA-256
        // 计算 SHA-256。
        let mut hasher = Sha256::new();
        hasher.update(&req.data);
        let sha256_hash = format!("{:x}", hasher.finalize());

        let created_at_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0);

        // Include nanoseconds plus a process counter so identical writes do
        // not silently replace one another within the same millisecond.
        let epoch_nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default();
        let counter = ARTIFACT_ID_COUNTER.fetch_add(1, Ordering::Relaxed);
        let artifact_id = format!("art-{sha256_hash}-{epoch_nanos}-{counter}");
        let size_bytes = req.data.len() as i64;
        let mime_type = if req.mime_type.is_empty() {
            "application/octet-stream".to_string()
        } else {
            req.mime_type
        };

        let target_file = self.storage_dir.join(format!("{}.bin", artifact_id));

        // Write directly to disk (externalization)
        // 直接写入磁盘（外置存储）。
        if let Err(error) = fs::create_dir_all(&self.storage_dir) {
            return create_error(
                ComputerErrorCode::ExecutionDenied,
                format!("Failed to create artifact storage directory: {error}"),
            );
        }

        if let Err(error) = write_new_file(&target_file, &req.data) {
            return create_error(
                ComputerErrorCode::ExecutionDenied,
                format!("Failed to persist artifact to disk: {error}"),
            );
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

        let sidecar_path = metadata_path(&self.storage_dir, &artifact_id);
        let sidecar = PersistedArtifactMetadata {
            artifact_id: metadata.artifact_id.clone(),
            name: metadata.name.clone(),
            mime_type: metadata.mime_type.clone(),
            size_bytes: metadata.size_bytes,
            sha256_hash: metadata.sha256_hash.clone(),
            created_at_ms: metadata.created_at_ms,
        };
        let sidecar_bytes = match serde_json::to_vec(&sidecar) {
            Ok(bytes) => bytes,
            Err(error) => {
                let _ = fs::remove_file(&entry.disk_path);
                return create_error(
                    ComputerErrorCode::ExecutionDenied,
                    format!("Failed to encode artifact metadata: {error}"),
                );
            }
        };
        if let Err(error) = write_new_file(&sidecar_path, &sidecar_bytes) {
            let _ = fs::remove_file(&entry.disk_path);
            return create_error(
                ComputerErrorCode::ExecutionDenied,
                format!("Failed to persist artifact metadata: {error}"),
            );
        }

        let mut map = self.index.write().unwrap();
        map.insert(artifact_id, entry);

        CreateArtifactResponse {
            result: Some(create_artifact_response::Result::Artifact(metadata)),
        }
    }

    /// Retrieves an artifact by its opaque token, reading bytes on-demand from external disk.
    /// 通过不透明 token 获取 artifact，按需从外部磁盘读取字节。
    pub fn get_artifact(&self, req: GetArtifactRequest) -> GetArtifactResponse {
        if let Some(error) = &self.initialization_error {
            return get_error(ComputerErrorCode::ExecutionDenied, error.clone());
        }
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

        let disk_metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) => {
                return get_error(
                    ComputerErrorCode::ArtifactNotFound,
                    format!("Artifact payload is unavailable: {error}"),
                );
            }
        };
        if !disk_metadata.file_type().is_file()
            || disk_metadata.len() as i64 != metadata.size_bytes
            || disk_metadata.len() as usize > self.max_inline_payload_bytes
        {
            return get_error(
                ComputerErrorCode::ExecutionDenied,
                format!(
                    "Artifact '{}' has an invalid file type or size",
                    req.artifact_id
                ),
            );
        }

        // Read bytes from disk on demand after checking type and size.
        // 校验文件类型和大小后，按需从磁盘读取字节。
        match std::fs::read(&path) {
            Ok(bytes) => {
                // Verify SHA-256 integrity on retrieval
                // 读取时验证 SHA-256 完整性。
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

fn create_error(code: ComputerErrorCode, message: String) -> CreateArtifactResponse {
    CreateArtifactResponse {
        result: Some(create_artifact_response::Result::Error(ComputerError {
            code: code as i32,
            message,
            retryable: false,
        })),
    }
}

fn get_error(code: ComputerErrorCode, message: String) -> GetArtifactResponse {
    GetArtifactResponse {
        result: Some(get_artifact_response::Result::Error(ComputerError {
            code: code as i32,
            message,
            retryable: false,
        })),
    }
}

fn metadata_path(directory: &std::path::Path, artifact_id: &str) -> PathBuf {
    directory.join(format!("{artifact_id}.meta.json"))
}

fn write_new_file(path: &std::path::Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write as _;
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)?;
    file.write_all(bytes)?;
    file.sync_all()
}

fn restore_index(
    directory: &std::path::Path,
    max_inline_payload_bytes: usize,
) -> Result<HashMap<String, ArtifactIndexEntry>, String> {
    fs::create_dir_all(directory)
        .map_err(|error| format!("Failed to initialize artifact directory: {error}"))?;
    let entries = fs::read_dir(directory)
        .map_err(|error| format!("Failed to read artifact directory: {error}"))?;
    let mut index = HashMap::new();
    for entry in entries {
        let entry =
            entry.map_err(|error| format!("Failed to read artifact index entry: {error}"))?;
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json")
            || !path
                .file_name()
                .and_then(|value| value.to_str())
                .is_some_and(|name| name.ends_with(".meta.json"))
        {
            continue;
        }

        let sidecar_metadata = fs::symlink_metadata(&path)
            .map_err(|error| format!("Failed to inspect artifact metadata: {error}"))?;
        if !sidecar_metadata.file_type().is_file() || sidecar_metadata.len() > 64 * 1024 {
            return Err("Artifact metadata file has an invalid type or size".into());
        }
        let bytes = fs::read(&path)
            .map_err(|error| format!("Failed to read artifact metadata: {error}"))?;
        let saved: PersistedArtifactMetadata = serde_json::from_slice(&bytes)
            .map_err(|error| format!("Invalid artifact metadata: {error}"))?;
        if !valid_artifact_id(&saved.artifact_id)
            || saved.size_bytes < 0
            || saved.size_bytes as usize > max_inline_payload_bytes
            || saved.name.len() > 4 * 1024
            || saved.mime_type.len() > 256
            || saved.sha256_hash.len() != 64
            || !saved
                .sha256_hash
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit())
        {
            return Err("Artifact metadata contains invalid identity or size".into());
        }
        let expected_name = format!("{}.meta.json", saved.artifact_id);
        if path.file_name().and_then(|value| value.to_str()) != Some(expected_name.as_str()) {
            return Err("Artifact metadata filename does not match its identity".into());
        }
        let disk_path = directory.join(format!("{}.bin", saved.artifact_id));
        let metadata = fs::symlink_metadata(&disk_path)
            .map_err(|error| format!("Artifact payload is unavailable: {error}"))?;
        if !metadata.file_type().is_file() || metadata.len() as i64 != saved.size_bytes {
            return Err("Artifact payload type or size does not match its metadata".into());
        }
        let artifact = ArtifactMetadata {
            artifact_id: saved.artifact_id.clone(),
            name: saved.name,
            mime_type: saved.mime_type,
            size_bytes: saved.size_bytes,
            sha256_hash: saved.sha256_hash,
            created_at_ms: saved.created_at_ms,
        };
        if index
            .insert(
                artifact.artifact_id.clone(),
                ArtifactIndexEntry {
                    metadata: artifact,
                    disk_path,
                },
            )
            .is_some()
        {
            return Err("Artifact metadata contains duplicate identities".into());
        }
    }
    Ok(index)
}

fn valid_artifact_id(artifact_id: &str) -> bool {
    let parts: Vec<&str> = artifact_id.split('-').collect();
    parts.len() == 4
        && parts[0] == "art"
        && parts[1].len() == 64
        && parts[1].bytes().all(|byte| byte.is_ascii_hexdigit())
        && !parts[2].is_empty()
        && parts[2].bytes().all(|byte| byte.is_ascii_digit())
        && !parts[3].is_empty()
        && parts[3].bytes().all(|byte| byte.is_ascii_digit())
}
