# Microsoft Graph Provider

This package exposes a small fixed Microsoft Graph v1.0 surface for delegated
Planner, To Do, calendar, selected SharePoint metadata, and explicitly enabled
Microsoft 365 Mail operations. Mail is off by default. It obtains one-use
user-profile leases from Account Center, checks `/me` with the same token
before accessing any resource, and never accepts a Graph URL from a caller.

The signed Exchange caller must also carry the canonical original Entra anchor:
`issuer=https://login.microsoftonline.com/{tid}/v2.0` and `subject={oid}`.
The provider compares that identity with the Account Center profile and the
redeemed lease before calling `/me`. Missing mappings and actor/profile
mismatches fail before any Graph HTTP request.

Supported tools are exact-plan Planner reads, task create/update with bounded
arguments and ETag concurrency, and selected SharePoint site/list metadata and
list-item delta reads. Office v2 adds To Do, calendar, and explicitly enabled
Mail operations. SharePoint is unavailable until Account Center has an explicit
selected-site/list scope and grant; the current user still needs access. Every
operation fails closed when its profile scope, target, grant, or Graph right is absent.

The v1 delegated Graph scopes are `User.Read` and `Tasks.Read`. Office v2
scopes derive from explicit feature flags and selected targets. Planner writes
require `Tasks.ReadWrite`; SharePoint requires `Sites.Selected` and a selected
target grant. Calendar keeps its existing read/write scope rules. Mail metadata
uses `Mail.ReadBasic`; draft operations use `Mail.ReadWrite`, which also covers
basic reads; sending adds `Mail.Send` and retains the minimum metadata scope
needed to preflight the draft. Send-only therefore uses `Mail.ReadBasic` plus
`Mail.Send`. Mail flags and folder targets default off/empty, `Mail.Read` is
never requested, and message body reads remain disabled. Planner observation uses
bounded full snapshots; the provider does not call Planner's beta delta API.
SharePoint uses the v1.0 list-item delta route, and the runtime must advance its
stored delta cursor only after the provider reports a complete page chain. An
expired Graph delta token is reported as `GRAPH_DELTA_RESYNC_REQUIRED`; the
provider does not follow an error `Location` URL.

The host may supply `sharepointTargets` in the provider activation config as an
exact `{siteId, listIds}` allowlist. When Account Center later enables the
separately consented SharePoint scope, its site and ordered list IDs must match
that binding tuple before lease redemption. Without the host tuple, SharePoint
stays unavailable even if a profile is mistakenly broader.

Create requests are sent once. If a write has an ambiguous result, the provider
performs bounded readback and returns `UNKNOWN` with possible-match metadata.
Even one recent task with the same title and bucket cannot prove which concurrent
request created it. The provider never reports an ambiguous create as confirmed
and never replays it. Reads and writes use synthetic test fixtures only; no real
Graph credentials are needed for package tests.

## Office v2 contract

The v2 contract is recorded in [`contracts/office-v2-operations.json`](contracts/office-v2-operations.json) and its exact Account Center target scope is validated by [`contracts/office-v2-target-scope.schema.json`](contracts/office-v2-target-scope.schema.json). Existing v1 Planner and SharePoint scopes remain exact and unchanged. V2 scopes bind the Entra tenant and Cyrene Workspace, with feature flags and resource IDs checked again at the provider. The activation `officeV2` flags only control catalog exposure; they do not replace actor-owned Account Center scope or Directory grants. Planner plan discovery, To Do, calendar, and SharePoint library metadata each have a separate host flag, disabled by default.

The v2 surface covers To Do task lists/tasks, calendar metadata/events/free-busy, selected SharePoint document-library/item metadata, and opt-in Mail. Calendar writes have no attendees and use UTC-normalized timestamps. SharePoint document contents are not read. Mail folder and message operations return metadata only, never body, body preview, or attachments. Draft create/update accept explicit plain-text `body_text`, but responses still contain metadata only. Draft mutation and send use the signed-in user's fixed Drafts folder; update and delete require the current ETag. Sending only accepts an existing draft with `isDraft=true`; Graph HTTP 202 is reported as `ACCEPTED`, not delivery. Ambiguous write outcomes are `UNKNOWN` and are never replayed. Mail scopes are requested only after explicit scope-upgrade consent; the previous profile remains available for non-Mail operations until the upgrade commits. Refresh and reauthentication state is metadata-only; token material never enters provider results.

### Office v2 合同

v2 合同记录在 [`contracts/office-v2-operations.json`](contracts/office-v2-operations.json)，Account Center 的精确目标范围定义在 [`contracts/office-v2-target-scope.schema.json`](contracts/office-v2-target-scope.schema.json)。既有 v1 Planner 与 SharePoint 范围保持原样。v2 范围绑定 Entra 租户和 Cyrene Workspace，并由 Provider 再次校验功能标志与资源 ID。部署配置中的 `officeV2` 仅决定目录是否发布工具，不能替代用户自己的 Account Center 授权或 Directory 目标授权。Planner 计划发现、To Do、日历和 SharePoint 文档库元数据分别有独立宿主开关，默认全部关闭。

v2 功能覆盖 To Do 任务清单/任务、日历元数据/事件/忙闲查询、选定 SharePoint 文档库/项目元数据，以及显式 opt-in 的邮件操作。日历写入不含参会者，时间统一转换为 UTC；SharePoint 正文不读取。Mail folder/message 操作只返回元数据，不返回正文、正文预览或附件。草稿创建/更新只接受明确提供的纯文本 `body_text`，响应仍仅含元数据。草稿变更与发送固定在当前用户的 Drafts 文件夹；更新和删除需匹配当前 ETag。发送只接受 `isDraft=true` 的既有草稿，Graph HTTP 202 仅报告 `ACCEPTED`，不代表投递成功。写入结果不明确时返回 `UNKNOWN` 且不重放。Mail scope 仅在用户明确同意 scope upgrade 后申请；升级提交前，旧 profile 仍可继续执行非 Mail 操作。令牌刷新和重新授权状态只公开元数据，不返回令牌内容。

## Contents

| Path | Responsibility |
| --- | --- |
| `src/microsoft_graph_provider/` | Fixed Graph transport, Account Center lease handling, trust checks, and tool routing. |
| `tests/` | Offline tests using synthetic actors, scopes, IDs, and HTTP responses. |
| `plugin.manifest.json` | DirectPluginRuntime provider descriptor. |
| `configuration.schema.json` | Closed activation configuration and host-secret references. |

Suggested reading order: `settings.py`, `account_center.py`, `microsoft_graph_api.py`, then `plugin.py`.

## 内容

本包提供受限的 Microsoft Graph v1.0 委托式接口，用于 Planner、To Do、日历、明确选定的
SharePoint 元数据，以及显式启用的 Microsoft 365 邮件操作。Mail 默认关闭。凭据通过
Account Center 一次性用户配置租约取得；访问任何资源前，必须使用同一个令牌读取 `/me` 并核对身份。调用方不能提供 Graph URL。
Exchange 签名调用者还必须携带原始 Entra 锚点：`issuer` 为
`https://login.microsoftonline.com/{tid}/v2.0`，`subject` 为 `{oid}`。供应商会在调用
`/me` 前将该锚点与 Account Center profile 和兑换令牌身份进行比较；缺少映射或身份不一致时，
不会发起任何 Graph HTTP 请求。

支持精确计划范围内的 Planner 读取、带 ETag 并发保护的任务创建/更新，以及选定
SharePoint 站点/列表元数据和列表项 delta 读取；Office v2 还支持 To Do、日历和显式启用的
Mail 操作。只有 Account Center 中存在明确的 selected 站点/列表授权、委托权限和当前用户权限时，
SharePoint 才可用；缺少任一授权或资源权限时均失败关闭。

v1 默认委托 scope 为 `User.Read` 和 `Tasks.Read`。Office v2 scope 根据显式功能开关和选定资源推导。
Planner 写入需要 `Tasks.ReadWrite`；SharePoint 需要 `Sites.Selected` 并完成选定目标授权。
Calendar 保持既有读写 scope 规则。邮件元数据使用 `Mail.ReadBasic`，草稿操作使用同时覆盖基础读取的
`Mail.ReadWrite`；发送添加 `Mail.Send`，并保留预检草稿所需的最小元数据读取权限。因此仅启用发送时使用
`Mail.ReadBasic` 与 `Mail.Send`。Mail 开关默认关闭、folder target 默认空；不会请求 `Mail.Read`，
邮件正文读取保持关闭。Planner 观察使用有界完整快照，不调用 Planner beta delta API。SharePoint 使用 v1.0
列表项 delta；只有整页链完整后，运行时才推进保存的 delta 游标。Graph 返回过期 delta token 时，供应商报告
`GRAPH_DELTA_RESYNC_REQUIRED`，不会跟随错误响应中的 `Location` URL。

创建请求只发送一次。写入结果不明确时，供应商执行有界回读并返回 `UNKNOWN` 及可能匹配项
元数据。即使近期只有一个任务标题和 bucket 都相同，也无法证明它属于哪一个并发请求。供应商
绝不将不明确创建报告为成功，也绝不重放该请求。包测试只使用合成身份、范围、ID 和 HTTP
响应，无需真实 Graph 凭据。

建议阅读顺序：`settings.py`、`account_center.py`、`microsoft_graph_api.py`、`plugin.py`。
