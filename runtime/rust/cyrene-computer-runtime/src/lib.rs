// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 lib.rs                                                          │
// │  Package: cyrene-computer-runtime                                   │
// │  Role: Canonical Computer & Tool Runtime for Cyrene (M5C, R02-R04). │
// │                                                                     │
// │  模块职责：                                                         │
// │  1. Rust 原生实现 computer.runtime.v1 (T73)                         │
// │  2. 严格解耦 filesystem、shell、browser 和 artifact (T74)          │
// │  3. 强制路径白名单、环境脱敏、工作目录与输出上限控制 (T75, R02)      │
// │  4. 每次执行输出完整 Evidence (command, artifact, timing...) (T76)  │
// │  5. 严禁导入/调用 Platform SandboxBackend (T77)                      │
// │  6. 默认实现标记为【有界受管执行】，非恶意代码硬隔离 (T78)           │
// │  7. 预留 ExternalSandboxProviderAdapter 强隔离扩展 (T79)           │
// │  8. 进程组管理确保后代孤儿进程被清理 (T80)                          │
// │  9. Artifact 外部落盘存储与 Opaque Token 机制 (R03)                 │
// └─────────────────────────────────────────────────────────────────────┘

pub mod artifact;
pub mod browser;
pub mod filesystem;
pub mod sandbox;
pub mod security;
pub mod shell;

pub use cyrene_plugin_contracts::computer_runtime_v1;

use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use tokio::sync::mpsc;

pub use artifact::ArtifactStore;
pub use browser::BrowserService;
pub use filesystem::FilesystemService;
pub use sandbox::{
    BoundedManagedExecutionProvider, ExternalSandboxProviderAdapter, IsolationLevel,
};
pub use security::{EnvironmentFilter, OutputCollector, PathValidator};
pub use shell::ShellProcessRunner;

use computer_runtime_v1::{
    CommandExecutionRequest, CommandExecutionResponse, CommandStreamEvent, ComputerError,
    CreateArtifactRequest, CreateArtifactResponse, GetArtifactRequest, GetArtifactResponse,
    ListDirRequest, ListDirResponse, ReadFileRequest, ReadFileResponse, WriteFileRequest,
    WriteFileResponse,
};

/// ════════════════════════════════════════════════════════════════════════
/// Per-operation bounds shared by direct Computer Runtime calls and the
/// `tool.provider.v1` facade. They do not cap user turns or work duration.
///
/// 直接 Computer Runtime 与 `tool.provider.v1` 门面的单次操作边界，
/// 不限制用户对话轮数或工作的总时长。
/// ════════════════════════════════════════════════════════════════════════
#[derive(Clone, Debug)]
pub struct ComputerRuntimeLimits {
    pub command_default_timeout_ms: u64,
    pub command_max_timeout_ms: u64,
    pub max_output_bytes: usize,
    pub max_file_bytes: usize,
    pub max_directory_entries: usize,
    pub max_directory_depth: i32,
    pub max_artifact_bytes: usize,
}

impl Default for ComputerRuntimeLimits {
    fn default() -> Self {
        Self {
            command_default_timeout_ms: 30_000,
            command_max_timeout_ms: 600_000,
            max_output_bytes: security::DEFAULT_MAX_OUTPUT_BYTES,
            max_file_bytes: 1024 * 1024,
            max_directory_entries: 2_048,
            max_directory_depth: 32,
            max_artifact_bytes: artifact::MAX_INLINE_PAYLOAD_BYTES,
        }
    }
}

/// Unified high-level service facade for Computer Runtime v1.
/// Computer Runtime v1 的统一高层 service facade。
pub struct ComputerRuntimeService {
    shell: ShellProcessRunner,
    fs: FilesystemService,
    artifacts: ArtifactStore,
    browser: BrowserService,
    isolation_level: IsolationLevel,
}

impl ComputerRuntimeService {
    pub fn new(allowed_roots: Vec<PathBuf>) -> Self {
        Self::with_limits_and_artifact_dir(
            allowed_roots,
            std::env::temp_dir().join("cyrene-artifacts"),
            ComputerRuntimeLimits::default(),
        )
    }

    pub fn with_artifact_dir(allowed_roots: Vec<PathBuf>, artifact_dir: PathBuf) -> Self {
        Self::with_limits_and_artifact_dir(
            allowed_roots,
            artifact_dir,
            ComputerRuntimeLimits::default(),
        )
    }

    /// Creates a runtime scoped to explicit workspace and artifact roots.
    /// 创建一个明确绑定工作区和制品目录的运行时。
    pub fn with_limits_and_artifact_dir(
        allowed_roots: Vec<PathBuf>,
        artifact_dir: PathBuf,
        limits: ComputerRuntimeLimits,
    ) -> Self {
        let validator = PathValidator::new(allowed_roots);
        let env_filter = EnvironmentFilter::new();

        Self {
            shell: ShellProcessRunner::with_limits(
                validator.clone(),
                env_filter,
                limits.command_default_timeout_ms,
                limits.command_max_timeout_ms,
                limits.max_output_bytes,
            ),
            fs: FilesystemService::with_limits(
                validator,
                limits.max_file_bytes,
                limits.max_directory_entries,
                limits.max_directory_depth,
            ),
            artifacts: ArtifactStore::with_storage_dir_and_limit(
                artifact_dir,
                limits.max_artifact_bytes,
            ),
            browser: BrowserService::new(),
            isolation_level: IsolationLevel::BoundedManagedExecution,
        }
    }

    pub fn isolation_level(&self) -> IsolationLevel {
        self.isolation_level
    }

    // ── Shell Commands ─────────────────────────────────────────────────
    // ── Shell 命令
    pub async fn execute_command(
        &self,
        request: CommandExecutionRequest,
        cancel_flag: Option<Arc<AtomicBool>>,
    ) -> CommandExecutionResponse {
        self.shell.execute_command(request, cancel_flag).await
    }

    pub async fn execute_command_stream(
        &self,
        request: CommandExecutionRequest,
        cancel_flag: Option<Arc<AtomicBool>>,
        sender: mpsc::Sender<CommandStreamEvent>,
    ) -> Result<(), ComputerError> {
        self.shell
            .execute_command_stream(request, cancel_flag, sender)
            .await
    }

    // ── Filesystem ─────────────────────────────────────────────────────
    // ── 文件系统
    pub fn read_file(&self, req: ReadFileRequest) -> ReadFileResponse {
        self.fs.read_file(req)
    }

    pub fn write_file(&self, req: WriteFileRequest) -> WriteFileResponse {
        self.fs.write_file(req)
    }

    pub fn list_dir(&self, req: ListDirRequest) -> ListDirResponse {
        self.fs.list_dir(req)
    }

    // ── 制品s ──────────────────────────────────────────────────────
    // ── 制品
    pub fn create_artifact(&self, req: CreateArtifactRequest) -> CreateArtifactResponse {
        self.artifacts.create_artifact(req)
    }

    pub fn get_artifact(&self, req: GetArtifactRequest) -> GetArtifactResponse {
        self.artifacts.get_artifact(req)
    }

    pub fn get_artifact_path(&self, artifact_id: &str) -> Option<PathBuf> {
        self.artifacts.get_artifact_path(artifact_id)
    }

    // ── 浏览器 ────────────────────────────────────────────────────────
    // ── 浏览器
    pub async fn browser_navigate(&self, url: &str) -> Result<String, ComputerError> {
        self.browser.navigate(url).await
    }
}
