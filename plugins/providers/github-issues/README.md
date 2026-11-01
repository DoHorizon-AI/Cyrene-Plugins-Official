# GitHub Issues and Repository Provider

This binding exposes fixed `tool.provider.v1` operations over GitHub Cloud at
`api.github.com`: repository inventory and metadata, issue search, branch and
file readback, pull requests, Actions runs/jobs/log excerpts, and CI status. Its
bounded task operations can create and delete non-protected branches, create or
compare-and-update one file, open a draft pull request, and rerun failed jobs for
an exact task-branch CI attempt. Writable branch refs must use
`cyrene/assistant/<task>`; the repository default branch is always protected.
Branch deletion is refused while an open same-repository pull request uses it.
Every repository target must match both a
stable numeric repository ID and the configured owner/name pair. The provider
accepts no arbitrary URL, command, or caller-supplied identity.

Issue search always pins the configured repository and excludes pull requests.
State, labels, lower and upper update-window bounds, and page are structured
inputs; free text does not accept GitHub query qualifiers. Results echo
`page_cursor` and report `complete`, `truncated`, `incomplete_results`,
`result_limit_reached`, and `next_cursor`. `complete` means this response
alone contains all results, so page 2 can never claim the earlier page was
included. The workflow must reconcile the ordered page receipts before moving
its watermark. GitHub's 1,000-result search ceiling is reported explicitly.
The update window accepts UTC RFC3339 with `Z` or `+00:00`; the provider
normalizes both bounds to the documented UTC offset syntax before sending the
fixed search query.
CI status returns context and check-run counts with `complete`/`truncated`;
responses over the first 100 items are explicitly incomplete and cannot be
treated as a green result.
Actions run and job lists also return `total_count`, `complete`, and
`truncated`; consumers can distinguish the bounded first page from the full
set.
Pull-request listing requests at most 20 records per page to stay below the
fixed 1 MB upstream response limit. Calls without `page_cursor` read up to five
pages sequentially and return at most 100 recent PRs, preserving the older
state-only call shape. A call with an explicit `page_cursor` reads one page.
The response reports `complete`, `truncated`, `has_more`, and `next_cursor`.
Continue only when `has_more` is true and `next_cursor` is non-null; stop when
the cursor is null and report `result_limit_reached` when true. `complete` means
the current response alone contains the full list; later single pages remain
`truncated` even when they have no next page. A full page without a valid next
link is treated as ambiguous and gets a conservative next cursor rather than
being called complete. The endpoint must return HTTP 200; unexpected success
statuses, oversized or malformed pages remain explicit errors, never empty
lists. The former 100-record request exceeded the limit for this repository
(about 1.85 MB).
CI status returns the raw combined commit-status state and separate status
context/check-run counts. `required_contexts_verified` is always false because
this operation does not read branch protection. A raw `PENDING` value with no
status contexts and terminal check runs is called out in `state_note`; it is
not evidence that a required context is missing.
Responses over the first 100 status or check-run items are explicitly
incomplete and cannot be treated as a green result.
Failed-job reruns require a repository-scoped `source_workflow_ids` allowlist;
an absent or empty entry denies every rerun. The request binds the expected
`expected_workflow_id` and task branch, and the provider rereads the run before
retrying.
Workflow logs follow only GitHub's short-lived redirect to the documented
`productionresultssa*.blob.core.windows.net` hosts, omit the App token on that
request, and return a best-effort excerpt with common credential patterns
redacted, bounded to 200 lines and 20,000 bytes. This filter cannot identify
every possible secret format.

Pull-request projections keep GitHub's `state` and add `merge_status` with
`OPEN`, `CLOSED_UNMERGED`, `MERGED`, or `UNKNOWN`. `merged`, `merged_at`, and
`merge_commit_sha` are included only when GitHub provides valid non-null values;
missing or null fields are omitted rather than replaced with guessed false/null
values. A closed response without enough merge facts is `UNKNOWN`. A merge SHA
must contain exactly 40 hexadecimal characters. GitHub notes that
`merge_commit_sha` may be a test merge commit before the PR is merged, so this
field never establishes merge status by itself. See the [GitHub pull request
REST API](https://docs.github.com/en/rest/pulls/pulls).

Writes require the dedicated assistant task namespace and exact SHA preconditions
where the GitHub API supports them. Branch deletion checks the expected SHA
and current default branch immediately before deletion, but GitHub exposes no
atomic conditional delete. The host must hold the same repository-and-branch
lifecycle lock across ref writes, draft-PR creation or closure, and deletion of
that unique task branch. This lock cannot serialize changes made directly in
GitHub, so a concurrent external edit or PR remains outside the guarantee.
Hidden files, workflow/CI controls, permissions, deployment and production
configuration paths are denied. A file is limited to 500,000 bytes.
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
commit/CI 状态。受限任务操作仅可在 `cyrene/assistant/<task>` 命名空间创建/删除分支、
创建或按 SHA 比对更新单个文件、创建 draft PR，以及针对指定任务分支 CI attempt 重跑失败 job；
仓库默认分支始终受保护，若同仓库的开放 PR 正在使用某分支则拒绝删除。每个仓库目标必须同时匹配
稳定数字 repository ID 与配置的 owner/name。provider 不接受任意 URL、命令或调用方自选身份。

Issue 搜索始终固定限定在配置仓库并排除 PR。state、labels、更新时间窗口上下界和 page 都是
结构化字段；自由文本不能传 GitHub 搜索限定符。结果回显 `page_cursor`，并返回
`complete`、`truncated`、`incomplete_results`、`result_limit_reached` 和 `next_cursor`。
`complete` 表示本次响应自身包含全部结果，因此第 2 页不会声称包含了第 1 页；workflow
须按顺序核对每页真实回执后再推进水位。GitHub 搜索 API 的 1,000 条上限会明确报告。
更新时间窗口接收 UTC RFC3339 的 `Z` 或 `+00:00` 格式，provider 会在发送固定搜索查询前将
上下界都规范为 GitHub 文档说明的 UTC offset 形式。日志只跟随
GitHub 短时重定向到文档所列的 `productionresultssa*.blob.core.windows.net` 主机，
下载时不携带 App token，日志摘录会尽力过滤常见凭据格式（最多 200 行、20,000 字节）；
此过滤无法识别所有可能的密钥格式。
CI 状态附带 status context/check-run 计数与 `complete`/`truncated`；超过首批 100 条时会
明确标为不完整，不能当作 CI 全绿结果。
Actions run 和 job 列表也会返回 `total_count`、`complete` 与 `truncated`，调用方可以区分有界
首批结果与完整集合。
PR 列表每页最多请求 20 条，避免超过固定的 1 MB 上游响应上限。不带 `page_cursor` 的调用会按顺序
最多读取 5 页、返回最近最多 100 条 PR，兼容旧的 state-only 调用格式。显式传入 `page_cursor` 时只读一页。
响应明确返回 `complete`、`truncated`、`has_more` 和 `next_cursor`；仅当 `has_more=true` 且
`next_cursor` 非空时继续，cursor 为空时停止，并在 `result_limit_reached=true` 时明确报告结果上限。
`complete` 表示当前这一条响应本身已包含全部 PR；显式读取后续页时，即使该页是最后一页也仍为 `truncated`，
因为它不包含前面页的数据。若满页响应没有可验证的 next link，会保守提供下一页 cursor，避免误报列表完整。
接口只接受 HTTP 200；非预期的成功状态、上游单页超限或格式异常都会明确失败，绝不伪装成空列表。
该仓库原先每次请求 100 条时响应约 1.85 MB，会超过限制。
CI 状态返回 GitHub combined commit-status 原始状态及 status context/check-run 独立计数。
由于此操作不会读取分支保护规则，`required_contexts_verified` 始终为 false。当原始状态为
`PENDING`、没有 status context 且所有 check run 均已终态时，`state_note` 会明确解释这些事实；
这不能证明存在缺失的必需 context。status/check-run 超过首批 100 条仍会明确标为不完整，不能当作
CI 全绿结果。
失败 job 重跑还需要按仓库配置的 `source_workflow_ids` 正向 allowlist；缺失或空列表时一律拒绝。
请求会绑定 `expected_workflow_id` 和任务分支，provider 在重跑前重新读取并核对上游 run。

PR projection 保留 GitHub 的 `state`，并增加 `merge_status`：`OPEN`、`CLOSED_UNMERGED`、
`MERGED` 或 `UNKNOWN`。只有 GitHub 提供有效非 null 值时才输出 `merged`、`merged_at` 和
`merge_commit_sha`；字段缺失或为 null 时省略，不猜测 `false` 或补 null。closed 响应缺少
足够合并事实时为 `UNKNOWN`。merge SHA 必须恰好是 40 位十六进制。GitHub 说明 PR 合并前
`merge_commit_sha` 可能指向测试 merge commit，因此不能单独据此判断是否已合并。详见
[GitHub Pull Request REST API](https://docs.github.com/en/rest/pulls/pulls)。

写入要求使用专属 assistant 任务分支，并在 API 支持时绑定精确 SHA。删除分支前会重新读取并
核对 expected SHA 和当前默认分支，但 GitHub 没有原子化的条件删除 API；宿主必须对同一仓库和
唯一任务分支使用同一生命周期锁，覆盖 ref 写入、draft PR 创建或关闭以及分支删除。此锁无法
串行化直接在 GitHub 上进行的更改，因此并发外部编辑或 PR 不在保证范围内。隐藏文件、
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
