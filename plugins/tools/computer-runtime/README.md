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

`sha256_hash` is a domain-separated summary of the complete raw stdout and stderr streams. It hashes each stream independently and combines its byte length and SHA-256 digest in a fixed order; it therefore changes when bytes beyond the displayed 64 KiB limit differ, and invalid UTF-8 bytes are hashed before display conversion. The displayed text remains capped at the configured UTF-8 byte limit.

## Deployment boundary

The runner listens on h2c gRPC port `50051` and trusts the private container network. A separate mTLS proxy authenticates callers and forwards only approved Exchange traffic. The runner itself receives no proxy certificates, HMAC keys, model or connector credentials, Docker socket, or host administrative access. Mount only the workspace and artifact directory as writable paths; keep application source and runtime read-only, drop Linux capabilities, set `no-new-privileges`, and disable network egress.

Both roots must be existing absolute directories:

| Environment variable | Purpose |
| --- | --- |
| `CYRENE_COMPUTER_WORKSPACE_ROOT` | The root accepted by command cwd and file operations. |
| `CYRENE_COMPUTER_ARTIFACT_ROOT` | Artifact payload and metadata directory, separate from the workspace; retention follows the backing mount. |

The Teams Cloud v1 Compose profile mounts both roots as bounded tmpfs scratch. They are cleared when the runner container restarts or is replaced. Other profiles may provide a persistent backing mount, but the current v1 tool catalog does not expose the direct artifact RPCs.

### Optional MXC process namespace

The host may select `CYRENE_COMPUTER_EXECUTION_BACKEND=managed|mxc`; the default is `managed`. MXC is supported on Linux when the `mxc` Cargo feature is enabled for both Rust packages and the trusted `cyrene-mxc-exec` helper is installed beside `cyrene-plugin-server`. Package a Bubblewrap executable in the helper's directory or in one of its fixed system search directories. The helper ignores inherited and request-supplied PATH values for its own control tools.

MXC pins `mxc-sdk = 1.0.1`, selects the public V1 Bubblewrap containment explicitly, disables telemetry, denies network egress, ingress, and host loopback, and grants the configured workspace roots read/write access. The SDK's system runtime baseline remains read-only; `/tmp` and `/tmp/cyrene-home` are private to the workload. Only filtered request environment values enter the workload; HOME and TMPDIR are forced to the private temporary filesystem.

Selection runs a real startup containment probe with the configured workspace mounts and network policy. Each command also verifies the fixed Bubblewrap executable is at least 0.12.0 and requires a private startup marker plus a bounded terminal receipt before accepting an exit code. Package Bubblewrap 0.13.1; versions below 0.12.0 are rejected because they contain a setup-time symlink escape. A missing helper, unsupported namespace, outdated Bubblewrap, invalid receipt, or probe failure is reported explicitly and never switches execution to `managed`.

An operator can run `cyrene-mxc-exec --probe --expected-parent-pid "$$" --workspace-root <absolute-root>` from a shell for the same bounded containment check. The expected parent PID must identify the process that launches the helper; the helper compares it to its actual parent, installs a parent-death signal, then checks again before probing. In a parentless PID-namespace-init diagnostic, `--probe --standalone-probe` is also supported only when the helper itself is PID 1 with PPID 0. That mode has a 10-second SDK deadline and cannot run workloads. Probe output includes verified MXC SDK and Bubblewrap versions only after the workspace mount and network-deny probe actually starts inside Bubblewrap. The plugin server logs the selected backend and verified Bubblewrap version at startup.

MXC provides Linux process namespace isolation while sharing the host kernel; it is not a microVM. Some container hosts deny the namespace operations required by Bubblewrap. In that case the startup probe fails and the MXC-selected server does not start.

Build both optional binaries with the feature enabled, then place the helper and the pinned Bubblewrap executable beside the server:

```bash
cargo build --manifest-path runtime/rust/cyrene-computer-runtime/Cargo.toml --features mxc --bin cyrene-mxc-exec
cargo build --manifest-path runtime/rust/cyrene-plugin-server/Cargo.toml --features mxc --bin cyrene-plugin-server
```

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

#### 可选 MXC 进程命名空间

宿主可通过 `CYRENE_COMPUTER_EXECUTION_BACKEND=managed|mxc` 选择执行后端；默认值为 `managed`。MXC 仅支持 Linux，需要为两个 Rust package 启用 `mxc` Cargo feature，并将可信的 `cyrene-mxc-exec` helper 与 `cyrene-plugin-server` 放在同一目录。请把 Bubblewrap 可执行文件打包在 helper 同目录或固定系统搜索目录中。Helper 不会把继承的 PATH 或请求传入的 PATH 用于自身控制程序。

MXC 固定使用 `mxc-sdk = 1.0.1`，明确选择公开 V1 Bubblewrap containment，关闭遥测，拒绝网络出站、入站和宿主 loopback，并只给配置的工作区根目录读写权限。SDK 系统运行时基线保持只读；`/tmp` 与 `/tmp/cyrene-home` 对工作负载隔离。只有过滤后的请求环境变量会传入工作负载；HOME 和 TMPDIR 固定到私有临时文件系统。

选择 MXC 时会用实际工作区挂载和网络策略执行启动隔离探测。每条命令还会核验固定 Bubblewrap 可执行文件版本至少为 0.12.0，并要求可信启动标记和有界终端回执后才接受退出码。建议打包 Bubblewrap 0.13.1；低于 0.12.0 的版本存在 setup 阶段符号链接逃逸问题，会被拒绝。Helper 缺失、命名空间不可用、Bubblewrap 版本过旧、回执无效或探测失败都会明确报错，不会自动切换到 `managed`。

运维人员可在 shell 中运行 `cyrene-mxc-exec --probe --expected-parent-pid "$$" --workspace-root <绝对路径>` 执行同样的有界隔离检查。预期父 PID 必须是启动 helper 的进程；helper 会将它与实际父进程比较、安装父进程退出信号，并在启动探测前再次检查。在无父进程的 PID namespace init 诊断场景中，也支持 `--probe --standalone-probe`，但仅当 helper 自身是 PID 1 且 PPID 为 0 时可用；该模式具有 10 秒 SDK 截止时间且不能运行工作负载。只有工作区挂载和拒绝网络的探测任务确实在 Bubblewrap 内启动后，命令才会打印核验过的 MXC SDK 与 Bubblewrap 版本。Plugin server 启动日志也会记录实际选择的后端和核验过的 Bubblewrap 版本。

MXC 提供 Linux 进程命名空间隔离并共享宿主内核，不是 microVM。部分容器宿主会拒绝 Bubblewrap 需要的命名空间操作；此时启动探测失败，选择了 MXC 的服务不会启动。

启用 feature 构建两个可选二进制，再将 helper 与固定版本的 Bubblewrap 放到服务器同目录：

```bash
cargo build --manifest-path runtime/rust/cyrene-computer-runtime/Cargo.toml --features mxc --bin cyrene-mxc-exec
cargo build --manifest-path runtime/rust/cyrene-plugin-server/Cargo.toml --features mxc --bin cyrene-plugin-server
```

可选限制通过 `CYRENE_COMPUTER_COMMAND_DEFAULT_TIMEOUT_MS`、`CYRENE_COMPUTER_COMMAND_MAX_TIMEOUT_MS`、`CYRENE_COMPUTER_MAX_OUTPUT_BYTES`、`CYRENE_COMPUTER_MAX_FILE_BYTES`、`CYRENE_COMPUTER_MAX_DIRECTORY_ENTRIES`、`CYRENE_COMPUTER_MAX_DIRECTORY_DEPTH` 和 `CYRENE_COMPUTER_MAX_ARTIFACT_BYTES` 配置。默认单次命令 30 秒、最长 10 分钟、每个输出流 64 KiB、单个文件写入 1 MiB、工具每次读取 64 KiB、目录最多 2,048 项且深度 32、每个制品 16 MiB。为兼容传输封套，命令最多 15 分钟、每个输出流最多 64 KiB、单个文件写入最多 1 MiB、目录最多 10,000 项且深度 64。直接制品调用最多 16 MiB。

执行证据报告墙钟耗时和输出哈希；尚无可靠操作系统计数时，不填写 CPU 时间或峰值内存。这是在低权限容器中的有界受管执行，不宣称可以抵御恶意代码突破容器边界。

`sha256_hash` 是完整原始 stdout 和 stderr 的带域分隔摘要：分别计算两条流的字节长度和 SHA-256，再按固定顺序组合。因此，即使显示文本超过 64 KiB 而被截断，尾部字节变化仍会改变摘要；无效 UTF-8 也在转换为显示文本之前参与哈希。最终显示文本仍按配置的 UTF-8 字节上限截断。
