# GitHub Issues and Repository Provider

This binding exposes fixed `tool.provider.v1` operations over GitHub Cloud at
`api.github.com`: repository inventory and metadata, issue search, branch and
file readback, pull requests, Actions runs/jobs/log excerpts, and CI status. Its
bounded task operations can create and delete non-protected branches, create or
compare-and-update one file, open a draft pull request, and rerun failed jobs for
an exact task-branch CI attempt. Every repository target must match both a
stable numeric repository ID and the configured owner/name pair. The provider
accepts no arbitrary URL, command, or caller-supplied identity.

Issue search always pins the configured repository and excludes pull requests.
State, labels, update watermark, and page are structured inputs; free text does
not accept GitHub query qualifiers. Results report `complete`, `truncated`, and
`next_cursor`; GitHub's 1,000-result search ceiling is reported as incomplete.
The update watermark accepts UTC RFC3339 with `Z` or `+00:00` and is normalized
to the documented UTC offset syntax before the fixed search query is sent.
Workflow logs follow only GitHub's short-lived redirect to the documented
`productionresultssa*.blob.core.windows.net` hosts, omit the App token on that
request, and return a redacted excerpt of at most 200 lines and 20,000 bytes.

Writes require a task branch and exact SHA or file-blob precondition where
applicable. Hidden files, workflow/CI controls, permissions, deployment and
production configuration paths are denied. A file is limited to 500,000 bytes.
Create, delete, commit, draft-PR, and CI-rerun calls reconcile remote state
before reporting success and never retry an unknown write outcome.

New calls use a GitHub App installation profile and a one-use Account Center
lease bound to the verified actor, profile, operation, exact target, and
canonical argument digest. Account Center signs a repository-scoped installation
token only for the provider caller; the token stays in provider memory. Writes
retain readback or explicit uncertain outcomes, and the provider never retries
an ambiguous write. Legacy GitHub OAuth issue leases remain supported for
existing bindings during migration.

The host supplies the resolved Account Center connection reference and binding
in activation configuration. Local connections must be loopback; remote
connections require explicit `grpcs://` plus operator-provided mTLS material.
`hostSecretRefs` maps stable references to host-injected environment names and
never contains secret values. Exchange verification and provider signing use
different 32-byte HMAC keys.

`normalize_webhook` authenticates the original raw bytes before decoding JSON,
then checks the installation, event, action, and repository allowlists. It
emits normalized event facts and never invokes a model.

Run focused tests from this directory with
`uv run --project . --with pytest --with jsonschema pytest -q tests`.

## GitHub Issues 与仓库状态提供方

此 binding 通过 GitHub Cloud `api.github.com` 暴露固定的 `tool.provider.v1`
操作：仓库清单与 metadata、Issue 搜索、分支和文件回读、PR、Actions runs/jobs/日志摘录、
commit/CI 状态。受限任务操作可创建/删除非保护分支、创建或按 SHA 比对更新单个文件、
创建 draft PR，以及针对指定任务分支 CI attempt 重跑失败 job。每个仓库目标必须同时匹配
稳定数字 repository ID 与配置的 owner/name。provider 不接受任意 URL、命令或调用方自选身份。

Issue 搜索始终固定限定在配置仓库并排除 PR。state、labels、更新时间水位和 page 都是
结构化字段；自由文本不能传 GitHub 搜索限定符。结果明确返回 `complete`、`truncated`
和 `next_cursor`；达到 GitHub 搜索 API 的 1,000 条上限时会报告未完成。日志只跟随
GitHub 短时重定向到文档所列的 `productionresultssa*.blob.core.windows.net` 主机，
下载时不携带 App token，并只返回经过常见凭据格式脱敏的日志摘录（最多 200 行、
20,000 字节）。
更新时间水位接收 UTC RFC3339 的 `Z` 或 `+00:00` 格式，并在发送固定搜索查询前转换成
GitHub 文档说明的 UTC offset 形式。

写入要求使用任务分支，并在适用时绑定精确 commit SHA 或文件 blob SHA。隐藏文件、
工作流/CI 控制、权限、部署和生产配置路径会被拒绝；单个文件最多 500,000 字节。
创建分支、删除分支、提交文件、创建 draft PR 和重跑 CI 都会先核对远端状态再报告成功；
结果未知时不会自动重试写入。

新调用使用 GitHub App installation profile 和 Account Center 一次性租约，租约绑定
可信 actor、profile、操作、精确目标和规范化参数摘要。Account Center 只向 provider
caller 签发限定仓库的 installation token，token 只保留在 provider 内存。写操作保留
回读结果或明确的未知状态；结果不确定时不会自动重试。迁移期间仍支持旧 binding 的
GitHub OAuth Issue 租约。

宿主在激活配置中提供已解析的 Account Center 连接引用和 binding。本地连接必须指向
loopback；远程连接必须显式使用 `grpcs://` 和 operator 提供的 mTLS 资料。
`hostSecretRefs` 只将稳定引用映射到宿主注入的环境变量名，不包含密钥值。Exchange
验证和 provider 签名使用彼此独立的 32 字节 HMAC key。

`normalize_webhook` 会先验证原始字节，再解码 JSON，然后检查 installation、事件、操作
和仓库 allowlist。它只输出规范化事件事实，不会调用模型。
