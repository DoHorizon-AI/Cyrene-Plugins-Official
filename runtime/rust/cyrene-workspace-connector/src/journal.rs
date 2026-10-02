//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 journal.rs                                                      │
//! │  Module: cyrene_workspace_connector::journal                        │
//! │  Role: Durable invocation receipt and lifecycle journal.           │
//! │                                                                     │
//! │  模块职责：将调用摘要、稳定回执和执行阶段持久化到本地追加日志。      │
//! └─────────────────────────────────────────────────────────────────────┘

use fs2::FileExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use thiserror::Error;

const MAX_RECORD_BYTES: usize = 16 * 1024 * 1024;
pub(crate) const JOURNAL_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Error)]
pub enum JournalError {
    #[error("journal I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("journal record is invalid: {0}")]
    Invalid(String),
    #[error("journal serialization failed: {0}")]
    Serialization(#[from] serde_json::Error),
}

/// Durable execution stages that determine safe restart behavior.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) enum InvocationStage {
    Claimed,
    Acknowledging,
    Acknowledged,
    Dispatching,
    ResultReady,
    UnknownResult,
    Submitted,
}

/// Latest durable state for one Authority invocation.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub(crate) struct JournalEntry {
    pub schema_version: u32,
    pub invocation_id: String,
    pub call_digest: String,
    pub receipt: Vec<u8>,
    pub credential: Vec<u8>,
    pub stage: InvocationStage,
    pub outcome_status: i32,
    pub response: Vec<u8>,
    pub error_message: String,
}

/// Append-only journal. Each state transition is framed, checksummed, and synced before return.
pub(crate) struct DurableJournal {
    file: File,
    latest: HashMap<String, JournalEntry>,
}

impl DurableJournal {
    /// Opens or creates a durable journal and restores the latest complete state for each call.
    pub(crate) fn open(path: impl AsRef<Path>) -> Result<Self, JournalError> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;
                let mut builder = std::fs::DirBuilder::new();
                builder.recursive(true).mode(0o700).create(parent)?;
            }
            #[cfg(not(unix))]
            std::fs::create_dir_all(parent)?;
        }
        let mut options = OpenOptions::new();
        options.create(true).read(true).append(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
            options.mode(0o600);
            if let Some(parent) = path.parent() {
                let metadata = std::fs::metadata(parent)?;
                if metadata.permissions().mode() & 0o077 != 0 {
                    return Err(JournalError::Invalid(
                        "journal directory must not be accessible to group or others".to_string(),
                    ));
                }
            }
        }
        let mut file = options.open(&path)?;
        file.try_lock_exclusive().map_err(|error| {
            if error.kind() == std::io::ErrorKind::WouldBlock {
                JournalError::Invalid("journal is already locked by another Connector".into())
            } else {
                JournalError::Io(error)
            }
        })?;
        if let Some(parent) = path.parent() {
            File::open(parent)?.sync_all()?;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if file.metadata()?.permissions().mode() & 0o077 != 0 {
                return Err(JournalError::Invalid(
                    "journal file must not be accessible to group or others".to_string(),
                ));
            }
        }
        let latest = recover(&mut file)?;
        Ok(Self { file, latest })
    }

    pub(crate) fn get(&self, invocation_id: &str) -> Option<&JournalEntry> {
        self.latest.get(invocation_id)
    }

    pub(crate) fn entries(&self) -> impl Iterator<Item = &JournalEntry> {
        self.latest.values()
    }

    /// Persists a complete snapshot before updating the in-memory view.
    pub(crate) fn persist(&mut self, entry: JournalEntry) -> Result<(), JournalError> {
        let payload = serde_json::to_vec(&entry)?;
        if payload.len() > MAX_RECORD_BYTES {
            return Err(JournalError::Invalid(
                "record exceeds size limit".to_string(),
            ));
        }
        let len = u32::try_from(payload.len())
            .map_err(|_| JournalError::Invalid("record length overflow".to_string()))?;
        let digest = Sha256::digest(&payload);
        self.file.write_all(&len.to_be_bytes())?;
        self.file.write_all(&payload)?;
        self.file.write_all(&digest)?;
        self.file.sync_all()?;
        self.latest.insert(entry.invocation_id.clone(), entry);
        Ok(())
    }
}

fn recover(file: &mut File) -> Result<HashMap<String, JournalEntry>, JournalError> {
    let mut latest = HashMap::new();
    file.seek(SeekFrom::Start(0))?;
    let mut offset = 0_u64;
    loop {
        let mut length_bytes = [0_u8; 4];
        let read = read_partial(file, &mut length_bytes)?;
        if read == 0 {
            break;
        }
        if read != length_bytes.len() {
            truncate_tail(file, offset)?;
            break;
        }
        let length = u32::from_be_bytes(length_bytes) as usize;
        if length == 0 || length > MAX_RECORD_BYTES {
            return Err(JournalError::Invalid(format!(
                "invalid frame length at byte {offset}"
            )));
        }
        let mut payload = vec![0_u8; length];
        let payload_read = read_partial(file, &mut payload)?;
        if payload_read != length {
            truncate_tail(file, offset)?;
            break;
        }
        let mut expected = [0_u8; 32];
        if read_partial(file, &mut expected)? != expected.len() {
            truncate_tail(file, offset)?;
            break;
        }
        let actual = Sha256::digest(&payload);
        if actual.as_slice() != expected {
            return Err(JournalError::Invalid(format!(
                "checksum mismatch at byte {offset}"
            )));
        }
        let entry: JournalEntry = serde_json::from_slice(&payload)?;
        if entry.schema_version != JOURNAL_SCHEMA_VERSION {
            return Err(JournalError::Invalid(format!(
                "unsupported journal schema version {}",
                entry.schema_version
            )));
        }
        latest.insert(entry.invocation_id.clone(), entry);
        offset += 4 + length as u64 + 32;
    }
    file.seek(SeekFrom::End(0))?;
    Ok(latest)
}

fn read_partial(reader: &mut impl Read, buffer: &mut [u8]) -> Result<usize, std::io::Error> {
    let mut total = 0;
    while total < buffer.len() {
        match reader.read(&mut buffer[total..])? {
            0 => break,
            count => total += count,
        }
    }
    Ok(total)
}

fn truncate_tail(file: &mut File, valid_len: u64) -> Result<(), std::io::Error> {
    file.set_len(valid_len)?;
    file.sync_all()
}
