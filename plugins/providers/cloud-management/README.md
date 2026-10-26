# Cloud Management Provider

This independently packaged provider exposes signed Azure, Google Cloud, and
Runpod Pods management tools. It keeps the read-only `cloud-resources` provider
separate, uses fixed HTTPS APIs for common operations, and offers each vendor's
native CLI through one bounded `argv[]` process runner.

Azure profiles use Managed Identity and bind a subscription plus resource-group
and resource-ID scope. Google Cloud profiles use a host-mounted Workload
Identity Federation credential configuration and a dedicated service account,
then bind a project, zones, and instance-name prefixes. Runpod profiles use a
host-injected API key and REST API v2, with explicit Pod IDs, name prefixes,
GPU types, data centers, and resource limits.

Each profile in `CYRENE_CAPABILITY_CONFIGURATION_JSON.profiles[]` accepts an
optional host-only `read_only` boolean (omitted means `false`). A read-only
profile permits only provider health, resource list/get,
and operation status/log reads. Mutations, cancellation, and the native vendor
CLI are rejected by the executor before it writes a replay receipt or contacts
the vendor. When every profile in a binding is read-only, `list_tools` hides the
mutating tools as well; mixed bindings retain a union catalog while the
per-profile execution gate remains authoritative.

`cloud_provider_health` reports the bound profile's authentication state,
authorization state, scope references, check time, and credential expiry when
the provider supplies one. Azure Managed Identity and GCP Workload Identity
Federation tokens are refreshed by their SDKs and report the resulting
short-lived token expiry. Runpod uses a static API key; the v2 API does not
provide a key-expiry timestamp, so `expires_at: null` means expiry is unknown,
not that the key is guaranteed to remain valid. A provider 401 is reported as
authentication failure, 403 as scope denial, and network/upstream failures as
unknown health. Response bodies and credential material are never included.

Every tool request selects a host-bound `profile_ref`; typed API calls also
carry a provider-specific resource `target`. The CLI call is granted the whole
host profile scope and signs the complete vendor `argv[]`, without claiming an
individual resource target. The Exchange-signed `TrustedCallerContext` binds
the immutable actor, workspace, provider binding, exact tool operation, and
SHA-256 of the complete arguments. The provider resolves credentials only from
host configuration. It never accepts tokens, credential paths, executable
paths, or shell text from tool arguments. Azure `az rest` is limited to the
pinned ARM host, subscription, and configured resource groups; arbitrary
endpoints and credential-bearing resource paths are rejected.

Writes are recorded by request ID before the vendor request. A repeated signed
request returns its recorded result, or `UNKNOWN_RESULT` after an interrupted
write; it never starts the same write again. Typed long operations and CLI
operations with a safely parsed native reference return a signed opaque
`operation_ref` that can be reconciled after process restart. A CLI write with
no native reference remains `REMOTE_STATE_UNVERIFIED`; it is never promoted
from exit code zero to verified remote success. Runpod results report expected
state, observed state, service readiness, and confirmed resource release
separately.

The SQLite receipt ledger is bounded by default to 8,192 rows and 128 MiB.
Known terminal receipts become purgeable after 90 days, longer than the
provider's 30-day operation-reference lifetime. Pending, unknown, and
unverified write tombstones are never purged. If protected receipts reach a
configured cap, new writes fail with `LEDGER_CAPACITY_REACHED` before provider
dispatch. Host deployments may tune `CYRENE_CLOUD_MANAGEMENT_LEDGER_MAX_ROWS`
(1–100,000), `CYRENE_CLOUD_MANAGEMENT_LEDGER_MAX_BYTES` (1–512 MiB), and
`CYRENE_CLOUD_MANAGEMENT_LEDGER_TERMINAL_RETENTION_DAYS` (31–3,650).

The CLI tool accepts a complete vendor command as an argument vector, launches
only the executable fixed by the profile, uses an isolated configuration
directory, enforces a deadline, and blocks credential enumeration/export,
interactive-auth commands, profile-escape flags, and out-of-scope endpoints.
Raw CLI stdout/stderr never reaches the model or Teams; the result reports
bounded execution metadata and whether output was suppressed or truncated.
`command_state=COMMAND_COMPLETED` describes only the local process. Mutating
commands report `remote_state=UNKNOWN` until native status read-back; captured
GCP and Runpod JSON operation/resource references can be reconciled through
the typed status tool. Cloud IAM and Runpod API-key permissions remain the
final vendor-side boundary.

## Local validation

Run tests with `uv run --project . --extra test pytest -q tests`. Tests use
semantic vendor fixtures and a local subprocess; they do not log in, contact a
cloud tenant, create provider resources, or incur cost.

## Official API references

- [Azure Resource Manager resource APIs](https://learn.microsoft.com/en-us/rest/api/resources/resources?view=rest-resources-2021-04-01)
- [Azure deployment cancel](https://learn.microsoft.com/en-us/rest/api/resources/deployments/cancel?view=rest-resources-2025-04-01)
- [Compute Engine instance REST API](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances)
- [Google Workload Identity Federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds)
- [Runpod REST API v2](https://docs.runpod.io/api-reference-v2/overview)
- [Runpod Pod API](https://docs.runpod.io/api-reference-v2/pods/list-pods)
- [Runpod API v1 migration](https://docs.runpod.io/api-reference-v2/migrate-from-v1)

<!-- 中文翻译 -->

## 云管理提供方

此独立 provider 提供带签名的 Azure、Google Cloud 和 Runpod Pods 管理工具。它与只读的
`cloud-resources` provider 分开；常用操作通过固定 HTTPS API 执行，各供应商原生 CLI
通过共享的有界 `argv[]` 进程执行器开放。

Azure profile 使用 Managed Identity，并限制到指定订阅、资源组和资源 ID 范围。Google
Cloud profile 使用主机挂载的 Workload Identity Federation 凭据配置和专用服务账号，并限制
到指定 project、zone 和实例名前缀。Runpod profile 使用仅由主机注入的 API key 和 REST API
v2，并限制 Pod ID、名称前缀、GPU 类型、数据中心和资源上限。

每个工具请求都选择主机绑定的 `profile_ref`；typed API 调用还会携带供应商专属资源
`target`。CLI 调用授权范围是整个主机 profile，并签署完整供应商 `argv[]`，不会宣称拥有某个
单独资源的精确授权。Exchange 签署的 `TrustedCallerContext` 会绑定不可变 actor、workspace、
provider binding、精确工具操作以及完整参数的 SHA-256。Provider 只从主机配置解析凭据；工具参数不能传
token、凭据路径、可执行路径或 shell 文本。Azure `az rest` 只允许使用固定 ARM host、订阅和已配置的
资源组；任意 endpoint 和携带凭据的资源路径都会被拒绝。

Provider 在请求供应商前按 request ID 记录写操作。重复的已签名请求返回原结果；写入中断后返回
`UNKNOWN_RESULT`，绝不再次启动同一写入。typed 长操作以及安全解析出原生引用的 CLI 操作会返回签名的
`operation_ref`，服务重启后可继续对账。没有原生引用的 CLI 写操作维持 `REMOTE_STATE_UNVERIFIED`，不会凭退出码
0 宣称远端写入成功。Runpod 分别报告期望状态、观察状态、服务就绪情况和已确认的资源释放。

CLI 工具接受完整供应商命令参数数组，只调用 profile 固定的可执行文件，使用隔离配置目录并限制
运行时间。它会阻止凭据枚举/导出、交互式认证命令、profile 越界参数和越界 endpoint。原始
stdout/stderr 不会送入模型或 Teams；结果只报告有界执行元数据以及输出是否被抑制或截断。
`command_state=COMMAND_COMPLETED` 只描述本地进程退出；写命令的 `remote_state=UNKNOWN` 会持续到原生状态回读。
安全解析的 GCP 和 Runpod JSON operation/resource 引用可通过 typed 状态工具对账。最终权限仍由云 IAM 和 Runpod
API key 权限控制。

`CYRENE_CAPABILITY_CONFIGURATION_JSON.profiles[]` 中的每个 profile 可配置宿主 `read_only` 布尔值
（省略时为 `false`）。只读 profile 只允许 provider
健康检查、资源列表/详情与操作状态/日志读取。写操作、取消和供应商 CLI 会在 executor 写入重放回执或访问供应商
前拒绝。当 binding 中所有 profile 都是只读时，`list_tools` 也会隐藏写工具；混合 binding 保留联合目录，最终权限
仍由逐 profile 的执行端门禁强制。

`cloud_provider_health` 会返回绑定 profile 的认证状态、授权状态、范围引用、检查时间，以及供应商
提供的凭据过期时间。Azure Managed Identity 和 GCP Workload Identity Federation token 由各自 SDK
刷新，并报告最新短期 token 的到期时间。Runpod 使用静态 API key；v2 API 不提供 key 过期时间字段，
因此 `expires_at: null` 表示过期时间未知，不表示 key 保证长期有效。供应商 401 会报告认证失败，403
报告范围授权不足，网络或上游故障报告为状态未知。响应正文和凭据不会进入结果。

SQLite 回执账本默认上限为 8,192 条、128 MiB。只有已知终态回执在 90 天后才可清理，该期限长于
30 天的 operation reference 有效期。Pending、unknown 和未验证写入 tombstone 永不清理；受保护回执达到上限时，
Provider 会在调用供应商前以 `LEDGER_CAPACITY_REACHED` 拒绝新写入。Host 可通过
`CYRENE_CLOUD_MANAGEMENT_LEDGER_MAX_ROWS`（1–100,000）、`CYRENE_CLOUD_MANAGEMENT_LEDGER_MAX_BYTES`
（1–512 MiB）以及 `CYRENE_CLOUD_MANAGEMENT_LEDGER_TERMINAL_RETENTION_DAYS`（31–3,650）调整限制。

## 本地验证

使用 `uv run --project . --extra test pytest -q tests` 运行测试。测试只使用供应商语义夹具和本地
子进程；不会登录、访问云租户、创建真实资源或产生费用。
