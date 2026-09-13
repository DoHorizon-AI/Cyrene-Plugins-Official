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
}

impl FilesystemService {
    pub fn new(validator: PathValidator) -> Self {
        Self { validator }
    }

    pub fn read_file(&self, req: ReadFileRequest) -> ReadFileResponse {
        let validated_path = match self.validator.validate_path(&req.path) {
            Ok(p) => p,
            Err(err) => {
                return ReadFileResponse {
                    result: Some(read_file_response::Result::Error(err)),
                };
            }
        };

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

        if let Some(offset) = req.offset {
            if offset > 0 {
                if let Err(e) = file.seek(SeekFrom::Start(offset as u64)) {
                    return ReadFileResponse {
                        result: Some(read_file_response::Result::Error(ComputerError {
                            code: ComputerErrorCode::ExecutionDenied as i32,
                            message: format!("Failed to seek offset {}: {}", offset, e),
                            retryable: false,
                        })),
                    };
                }
            }
        }

        let mut buffer = Vec::new();
        let read_result = if let Some(length) = req.length {
            if length > 0 {
                let mut take = file.take(length as u64);
                take.read_to_end(&mut buffer)
            } else {
                file.read_to_end(&mut buffer)
            }
        } else {
            file.read_to_end(&mut buffer)
        };

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
        let validated_path = match self.validator.validate_path(&req.path) {
            Ok(p) => p,
            Err(err) => {
                return WriteFileResponse {
                    result: Some(write_file_response::Result::Error(err)),
                };
            }
        };

        if validated_path.exists() && !req.overwrite {
            return WriteFileResponse {
                result: Some(write_file_response::Result::Error(ComputerError {
                    code: ComputerErrorCode::ExecutionDenied as i32,
                    message: format!("File '{}' already exists and overwrite is false", req.path),
                    retryable: false,
                })),
            };
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

        let max_depth = req.max_depth.unwrap_or(1).max(1);
        let mut entries = Vec::new();

        fn collect_entries(
            dir: &Path,
            current_depth: i32,
            max_depth: i32,
            entries: &mut Vec<DirEntry>,
        ) {
            if current_depth > max_depth {
                return;
            }

            if let Ok(read_dir) = fs::read_dir(dir) {
                for item in read_dir.flatten() {
                    let path = item.path();
                    let file_name = path
                        .file_name()
                        .map(|s| s.to_string_lossy().to_string())
                        .unwrap_or_default();
                    let is_directory = path.is_dir();
                    let metadata = path.metadata().ok();
                    let size_bytes = metadata.as_ref().map(|m| m.len() as i64).unwrap_or(0);
                    let modified_at_ms = metadata
                        .and_then(|m| m.modified().ok())
                        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                        .map(|d| d.as_millis() as i64)
                        .unwrap_or(0);

                    entries.push(DirEntry {
                        name: file_name,
                        is_directory,
                        size_bytes,
                        modified_at_ms,
                    });

                    if is_directory && current_depth < max_depth {
                        collect_entries(&path, current_depth + 1, max_depth, entries);
                    }
                }
            }
        }

        collect_entries(&validated_path, 1, max_depth, &mut entries);

        ListDirResponse {
            result: Some(list_dir_response::Result::Entries(DirEntries { entries })),
        }
    }
}
