// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 fs_service.rs                                                   │
// │  Package: cyrene-computer-runtime::filesystem                       │
// │  Role: Separated bounded filesystem service (T74, T75).             │
// │                                                                     │
// │  模块职责：独立的受管文件系统操作（Read、Write、ListDir），             │
// │           100% 遵守 PathValidator 白名单，严防路径穿越与符号逃逸          │
// └─────────────────────────────────────────────────────────────────────┘

use std::fs;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::time::UNIX_EPOCH;

use cyrene_plugin_contracts::computer_runtime_v1::{
    list_dir_response, read_file_response, write_file_response, ComputerError, ComputerErrorCode,
    DirEntries, DirEntry, ListDirRequest, ListDirResponse, ReadFileRequest, ReadFileResponse,
    WriteFileRequest, WriteFileResponse,
};

use crate::security::PathValidator;

pub struct FilesystemService {
    validator: PathValidator,
    max_file_bytes: usize,
    max_directory_entries: usize,
    max_directory_depth: i32,
}

impl FilesystemService {
    pub fn new(validator: PathValidator) -> Self {
        Self::with_limits(validator, 1024 * 1024, 2_048, 32)
    }

    /// Creates a filesystem facade with bounded response and write sizes.
    /// 使用受限的读取、写入和目录枚举额度创建文件系统门面。
    pub fn with_limits(
        validator: PathValidator,
        max_file_bytes: usize,
        max_directory_entries: usize,
        max_directory_depth: i32,
    ) -> Self {
        Self {
            validator,
            max_file_bytes: max_file_bytes.max(1),
            max_directory_entries: max_directory_entries.max(1),
            max_directory_depth: max_directory_depth.max(1),
        }
    }

    pub fn read_file(&self, req: ReadFileRequest) -> ReadFileResponse {
        if req.offset.is_some_and(|offset| offset < 0)
            || req.length.is_some_and(|length| length < 0)
        {
            return ReadFileResponse {
                result: Some(read_file_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: "File offset and length must be non-negative".into(),
                    retryable: false,
                })),
            };
        }

        let validated_path = match self.validator.validate_path(&req.path) {
            Ok(p) => p,
            Err(err) => {
                return ReadFileResponse {
                    result: Some(read_file_response::Result::Error(err)),
                };
            }
        };

        let metadata = match fs::metadata(&validated_path) {
            Ok(metadata) if metadata.is_file() => metadata,
            Ok(_) => {
                return ReadFileResponse {
                    result: Some(read_file_response::Result::Error(ComputerError {
                        code: ComputerErrorCode::ExecutionDenied as i32,
                        message: format!("Path '{}' is not a regular file", req.path),
                        retryable: false,
                    })),
                };
            }
            Err(error) => {
                return ReadFileResponse {
                    result: Some(read_file_response::Result::Error(ComputerError {
                        code: ComputerErrorCode::ExecutionDenied as i32,
                        message: format!("Failed to inspect file: {error}"),
                        retryable: false,
                    })),
                };
            }
        };

        let offset = req.offset.unwrap_or(0) as u64;
        let available = metadata.len().saturating_sub(offset);
        let requested = req.length.map(|length| length as u64).unwrap_or(available);
        if requested > self.max_file_bytes as u64 {
            return ReadFileResponse {
                result: Some(read_file_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::OutputLimitExceeded as i32,
                    message: format!(
                        "Requested file read is {requested} bytes; configured maximum is {} bytes",
                        self.max_file_bytes
                    ),
                    retryable: false,
                })),
            };
        }

        let mut file = match fs::File::open(&validated_path) {
            Ok(f) => f,
            Err(e) => {
                return ReadFileResponse {
                    result: Some(read_file_response::Result::Error(ComputerError {
                        code: ComputerErrorCode::ExecutionDenied as i32,
                        message: format!("Failed to open file: {}", e),
                        retryable: false,
                    })),
                };
            }
        };

        if offset > 0 {
            if let Err(e) = file.seek(SeekFrom::Start(offset)) {
                return ReadFileResponse {
                    result: Some(read_file_response::Result::Error(ComputerError {
                        code: ComputerErrorCode::ExecutionDenied as i32,
                        message: format!("Failed to seek offset {offset}: {e}"),
                        retryable: false,
                    })),
                };
            }
        }

        let mut buffer = Vec::new();
        let mut take = file.take(requested.min(available));
        let read_result = take.read_to_end(&mut buffer);

        match read_result {
            Ok(_) => ReadFileResponse {
                result: Some(read_file_response::Result::Content(buffer)),
            },
            Err(e) => ReadFileResponse {
                result: Some(read_file_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: format!("Failed to read file content: {}", e),
                    retryable: false,
                })),
            },
        }
    }

    pub fn write_file(&self, req: WriteFileRequest) -> WriteFileResponse {
        if req.content.len() > self.max_file_bytes {
            return WriteFileResponse {
                result: Some(write_file_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::OutputLimitExceeded as i32,
                    message: format!(
                        "File content is {} bytes; configured maximum is {} bytes",
                        req.content.len(),
                        self.max_file_bytes
                    ),
                    retryable: false,
                })),
            };
        }

        let validated_path = match self.validator.validate_path(&req.path) {
            Ok(p) => p,
            Err(err) => {
                return WriteFileResponse {
                    result: Some(write_file_response::Result::Error(err)),
                };
            }
        };

        match fs::symlink_metadata(&validated_path) {
            Ok(metadata) if !metadata.file_type().is_file() => {
                return WriteFileResponse {
                    result: Some(write_file_response::Result::Error(ComputerError {
                        code: ComputerErrorCode::ExecutionDenied as i32,
                        message: format!("Path '{}' is not a regular file", req.path),
                        retryable: false,
                    })),
                };
            }
            Ok(_) if !req.overwrite => {
                return WriteFileResponse {
                    result: Some(write_file_response::Result::Error(ComputerError {
                        code: ComputerErrorCode::ExecutionDenied as i32,
                        message: format!(
                            "File '{}' already exists and overwrite is false",
                            req.path
                        ),
                        retryable: false,
                    })),
                };
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return WriteFileResponse {
                    result: Some(write_file_response::Result::Error(ComputerError {
                        code: ComputerErrorCode::ExecutionDenied as i32,
                        message: format!("Failed to inspect file before writing: {error}"),
                        retryable: false,
                    })),
                };
            }
        }

        let mut file = match fs::OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&validated_path)
        {
            Ok(f) => f,
            Err(e) => {
                return WriteFileResponse {
                    result: Some(write_file_response::Result::Error(ComputerError {
                        code: ComputerErrorCode::ExecutionDenied as i32,
                        message: format!("Failed to create/open file for writing: {}", e),
                        retryable: false,
                    })),
                };
            }
        };

        match file.write_all(&req.content) {
            Ok(_) => WriteFileResponse {
                result: Some(write_file_response::Result::BytesWritten(
                    req.content.len() as i64
                )),
            },
            Err(e) => WriteFileResponse {
                result: Some(write_file_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: format!("Failed to write content: {}", e),
                    retryable: false,
                })),
            },
        }
    }

    pub fn list_dir(&self, req: ListDirRequest) -> ListDirResponse {
        let validated_path = match self.validator.validate_path(&req.path) {
            Ok(p) => p,
            Err(err) => {
                return ListDirResponse {
                    result: Some(list_dir_response::Result::Error(err)),
                };
            }
        };

        if !validated_path.is_dir() {
            return ListDirResponse {
                result: Some(list_dir_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: format!("Path '{}' is not a directory", req.path),
                    retryable: false,
                })),
            };
        }

        let max_depth = req.max_depth.unwrap_or(1);
        if max_depth < 1 || max_depth > self.max_directory_depth {
            return ListDirResponse {
                result: Some(list_dir_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: format!(
                        "Directory depth must be between 1 and {}",
                        self.max_directory_depth
                    ),
                    retryable: false,
                })),
            };
        }
        let mut entries = Vec::new();

        fn collect_entries(
            dir: &Path,
            current_depth: i32,
            max_depth: i32,
            max_entries: usize,
            entries: &mut Vec<DirEntry>,
        ) -> Result<(), ComputerError> {
            if current_depth > max_depth {
                return Ok(());
            }

            let read_dir = fs::read_dir(dir).map_err(|error| ComputerError {
                code: ComputerErrorCode::ExecutionDenied as i32,
                message: format!("Failed to list directory: {error}"),
                retryable: false,
            })?;
            for item in read_dir {
                let item = item.map_err(|error| ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: format!("Failed to read directory entry: {error}"),
                    retryable: false,
                })?;
                if entries.len() >= max_entries {
                    return Err(ComputerError {
                        code: ComputerErrorCode::OutputLimitExceeded as i32,
                        message: format!(
                            "Directory listing exceeds the configured maximum of {max_entries} entries"
                        ),
                        retryable: false,
                    });
                }

                let path = item.path();
                let file_type = item.file_type().map_err(|error| ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: format!("Failed to inspect directory entry: {error}"),
                    retryable: false,
                })?;
                let file_name = item.file_name().to_string_lossy().to_string();
                let metadata = fs::symlink_metadata(&path).ok();
                let size_bytes = metadata
                    .as_ref()
                    .map(|value| value.len() as i64)
                    .unwrap_or(0);
                let modified_at_ms = metadata
                    .and_then(|value| value.modified().ok())
                    .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
                    .map(|value| value.as_millis() as i64)
                    .unwrap_or(0);

                // Do not follow workspace symlinks during recursive discovery.
                let is_directory = file_type.is_dir();
                entries.push(DirEntry {
                    name: file_name,
                    is_directory,
                    size_bytes,
                    modified_at_ms,
                });

                if is_directory && current_depth < max_depth {
                    collect_entries(&path, current_depth + 1, max_depth, max_entries, entries)?;
                }
            }
            Ok(())
        }

        if let Err(error) = collect_entries(
            &validated_path,
            1,
            max_depth,
            self.max_directory_entries,
            &mut entries,
        ) {
            return ListDirResponse {
                result: Some(list_dir_response::Result::Error(error)),
            };
        }

        ListDirResponse {
            result: Some(list_dir_response::Result::Entries(DirEntries { entries })),
        }
    }
}
