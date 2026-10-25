# Cyrene Computer Runtime

This package provides bounded command and file operations in one configured Cyrene workspace. `cyrene-plugin-server` keeps the existing `computer.runtime.v1` API and adds a fixed `tool.provider.v1` catalog that maps four approved tools to the same runtime service.

| File | Responsibility |
| --- | --- |
| `plugin.manifest.json` | Declares the direct runtime and tool-provider capabilities. |
| `../../../runtime/rust/cyrene-computer-runtime` | Executes bounded commands and validates workspace file operations. |
| `../../../runtime/rust/cyrene-plugin-server` | Serves h2c gRPC and maps fixed tool calls to the runtime. |

## Tool catalog

The Plugin binding ID is `computer-runtime`; the Exchange provider profile is `computer`.

| Tool | Required input | Optional input | Result |
| --- | --- | --- | --- |
| `computer.command.execute` | `command` string | Workspace-relative `cwd`, positive `timeout_ms` | Exit code, wall duration, up to 64 KiB each of stdout/stderr, truncation flags and output SHA-256. |
| `computer.file.read` | Workspace-relative `path`, `length` from 0 to 64 KiB | Non-negative `offset` | Byte count and UTF-8 text or standard-base64 bytes. |
| `computer.file.write` | Workspace-relative `path`, standard-base64 `content_base64`, `overwrite` boolean | — | Bytes written. |
| `computer.directory.list` | Workspace-relative `path` (`.` means root) | `max_depth` | Name, directory flag, size and modification time for each entry. |

The model-facing catalog exposes these four operations only; it does not expose the direct `CreateArtifact` or `GetArtifact` methods. Workspace files are scratch, not durable task state. In the Teams Cloud v1 profile they are cleared when the runner container restarts or is replaced. Navigator task, session, and operation records remain in their existing persistent service. Use GitHub tools for repository changes that must persist; this profile does not promise artifact-reference durability across a restart.

Inputs reject unknown properties. File and directory tools reject absolute paths and parent-directory components; filesystem validation also checks the resolved path against the configured root. Directory walks do not follow symlinks. Oversized reads, writes, directory results, and command output return explicit errors rather than silently truncating tool responses.

## Deployment boundary

The runner listens on h2c gRPC port `50051` and trusts the private container network. A separate mTLS proxy authenticates callers and forwards only approved Exchange traffic. The runner itself receives no proxy certificates, HMAC keys, model or connector credentials, Docker socket, or host administrative access. Mount only the workspace and artifact directory as writable paths; keep application source and runtime read-only, drop Linux capabilities, set `no-new-privileges`, and disable network egress.

Both roots must be existing absolute directories:

| Environment variable | Purpose |
| --- | --- |
| `CYRENE_COMPUTER_WORKSPACE_ROOT` | The root accepted by command cwd and file operations. |
| `CYRENE_COMPUTER_ARTIFACT_ROOT` | Artifact payload and metadata directory, separate from the workspace; retention follows the backing mount. |

The Teams Cloud v1 Compose profile mounts both roots as bounded tmpfs scratch. They are cleared when the runner container restarts or is replaced. Other profiles may provide a persistent backing mount, but the current v1 tool catalog does not expose the direct artifact RPCs.

Optional bounds are configured with `CYRENE_COMPUTER_COMMAND_DEFAULT_TIMEOUT_MS`, `CYRENE_COMPUTER_COMMAND_MAX_TIMEOUT_MS`, `CYRENE_COMPUTER_MAX_OUTPUT_BYTES`, `CYRENE_COMPUTER_MAX_FILE_BYTES`, `CYRENE_COMPUTER_MAX_DIRECTORY_ENTRIES`, `CYRENE_COMPUTER_MAX_DIRECTORY_DEPTH`, and `CYRENE_COMPUTER_MAX_ARTIFACT_BYTES`. Defaults are 30 seconds command time, 10 minutes maximum command time, 64 KiB per output stream, 1 MiB per file write, 64 KiB per tool file read, 2,048 directory entries, depth 32, and 16 MiB per artifact. Transport-safe maxima are 15 minutes per command, 64 KiB per output stream, 1 MiB per file write, 10,000 directory entries, and depth 64. Direct artifact calls allow up to 16 MiB.

Evidence reports wall duration and output hashes. It omits CPU time and peak-memory values until they are measured from operating-system counters. This is bounded managed execution in a low-privilege container; it does not claim hostile-code containment.

## Cyrene 电脑运行时

此 package 为已绑定的 Cyrene 工作区提供有界命令和文件操作。`cyrene-plugin-server` 保留现有 `computer.runtime.v1` API，并增加固定的 `tool.provider.v1` 工具目录；四个工具最终调用同一个运行时服务。

| 工具 | 必需输入 | 可选输入 | 返回结果 |
| --- | --- | --- | --- |
| `computer.command.execute` | `command` 字符串 | 工作区相对路径 `cwd`、正整数 `timeout_ms` | 退出码、墙钟耗时、每条最多 64 KiB 的 stdout/stderr、截断标记和输出 SHA-256。 |
| `computer.file.read` | 工作区相对路径 `path`、0 到 64 KiB 的 `length` | 非负 `offset` | 字节数，以及 UTF-8 文本或标准 base64 字节。 |
| `computer.file.write` | 工作区相对路径 `path`、标准 base64 `content_base64`、布尔值 `overwrite` | — | 写入字节数。 |
| `computer.directory.list` | 工作区相对路径 `path`（`.` 表示根目录） | `max_depth` | 每项的名称、目录标记、大小和修改时间。 |

面向模型的目录仅公开以上四个操作，不公开底层 `CreateArtifact` 或 `GetArtifact` 方法。工作区文件属于临时 scratch，不是持久任务状态。在 Teams Cloud v1 配置中，runner 容器重启或替换时会清除这些文件。Navigator 的任务、会话和操作回执仍由现有持久服务保存。需要保留的仓库改动应使用 GitHub 工具；该 profile 不承诺重启后仍可读取制品引用。

输入会拒绝未声明字段。文件和目录工具拒绝绝对路径及 `..` 路径分量；文件系统还会校验解析后的路径是否仍在配置的工作区根目录内。递归目录读取不会跟随符号链接。超过读取、写入、目录结果或命令输出限制时会明确报错，不会静默截断结果。

运行器监听私有容器网络上的 h2c gRPC `50051` 端口。独立 mTLS 代理验证调用方，并仅转发获准的 Exchange 请求。运行器本身不挂载代理证书、HMAC 密钥、模型或连接器凭据、Docker socket，也没有主机管理员权限。只将工作区和制品目录挂载为可写；应用源代码和运行时应只读，删除 Linux capabilities，开启 `no-new-privileges` 并关闭网络出口。

`CYRENE_COMPUTER_WORKSPACE_ROOT` 和 `CYRENE_COMPUTER_ARTIFACT_ROOT` 都必须指向已存在的绝对目录；制品目录必须与工作区分离。Teams Cloud v1 Compose profile 将两者挂载为有界 tmpfs scratch，runner 容器重启或替换后内容会清除。其他 profile 可以配置持久化挂载，但当前 v1 工具目录不会公开底层制品 RPC。

可选限制通过 `CYRENE_COMPUTER_COMMAND_DEFAULT_TIMEOUT_MS`、`CYRENE_COMPUTER_COMMAND_MAX_TIMEOUT_MS`、`CYRENE_COMPUTER_MAX_OUTPUT_BYTES`、`CYRENE_COMPUTER_MAX_FILE_BYTES`、`CYRENE_COMPUTER_MAX_DIRECTORY_ENTRIES`、`CYRENE_COMPUTER_MAX_DIRECTORY_DEPTH` 和 `CYRENE_COMPUTER_MAX_ARTIFACT_BYTES` 配置。默认单次命令 30 秒、最长 10 分钟、每个输出流 64 KiB、单个文件写入 1 MiB、工具每次读取 64 KiB、目录最多 2,048 项且深度 32、每个制品 16 MiB。为兼容传输封套，命令最多 15 分钟、每个输出流最多 64 KiB、单个文件写入最多 1 MiB、目录最多 10,000 项且深度 64。直接制品调用最多 16 MiB。

执行证据报告墙钟耗时和输出哈希；尚无可靠操作系统计数时，不填写 CPU 时间或峰值内存。这是在低权限容器中的有界受管执行，不宣称可以抵御恶意代码突破容器边界。
