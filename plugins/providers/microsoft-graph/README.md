# Microsoft Graph Provider

This package exposes a small fixed Microsoft Graph v1.0 surface for delegated
Planner access and metadata-only reads from explicitly selected SharePoint
sites and lists. It obtains one-use user-profile leases from Account Center,
checks `/me` with the same token before accessing any resource, and never
accepts a Graph URL from a caller.

The signed Exchange caller must also carry the canonical original Entra anchor:
`issuer=https://login.microsoftonline.com/{tid}/v2.0` and `subject={oid}`.
The provider compares that identity with the Account Center profile and the
redeemed lease before calling `/me`. Missing mappings and actor/profile
mismatches fail before any Graph HTTP request.

Supported tools are exact-plan Planner reads, task create/update with bounded
arguments and ETag concurrency, and selected SharePoint site/list metadata and
list-item delta reads. SharePoint is unavailable until Account Center has an
explicit selected-site/list scope and its delegated selection grant; the
current user still needs access. A missing profile, scope, grant, or Graph right
fails closed.

The default delegated Graph scopes are `User.Read` and `Tasks.Read`. Planner
writes require an explicit `Tasks.ReadWrite` upgrade. SharePoint requires an
explicit `Sites.Selected` upgrade and selected target grant. Mail and Calendar
scopes and operations are not part of this provider. Planner observation uses
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

## Contents

| Path | Responsibility |
| --- | --- |
| `src/microsoft_graph_provider/` | Fixed Graph transport, Account Center lease handling, trust checks, and tool routing. |
| `tests/` | Offline tests using synthetic actors, scopes, IDs, and HTTP responses. |
| `plugin.manifest.json` | DirectPluginRuntime provider descriptor. |
| `configuration.schema.json` | Closed activation configuration and host-secret references. |

Suggested reading order: `settings.py`, `account_center.py`, `microsoft_graph_api.py`, then `plugin.py`.

## 内容

本包提供受限的 Microsoft Graph v1.0 委托式接口，用于 Planner 任务和明确选定的
SharePoint 站点/列表元数据。凭据通过 Account Center 一次性用户配置租约取得；访问任何
资源前，必须使用同一个令牌读取 `/me` 并核对身份。调用方不能提供 Graph URL。
Exchange 签名调用者还必须携带原始 Entra 锚点：`issuer` 为
`https://login.microsoftonline.com/{tid}/v2.0`，`subject` 为 `{oid}`。供应商会在调用
`/me` 前将该锚点与 Account Center profile 和兑换令牌身份进行比较；缺少映射或身份不一致时，
不会发起任何 Graph HTTP 请求。

支持精确计划范围内的 Planner 读取、带 ETag 并发保护的任务创建/更新，以及选定
SharePoint 站点/列表元数据和列表项 delta 读取。只有 Account Center 中存在明确的
selected 站点/列表授权、委托权限和当前用户权限时，SharePoint 才可用；缺少任一授权或
资源权限时均失败关闭。

默认委托 scope 为 `User.Read` 和 `Tasks.Read`。Planner 写入需要用户明确升级到
`Tasks.ReadWrite`；SharePoint 需要用户明确升级到 `Sites.Selected` 并完成选定目标授权。本
供应商不包含 Mail 或 Calendar scope/操作。Planner 观察使用有界完整快照，不调用 Planner
beta delta API。SharePoint 使用 v1.0 列表项 delta；只有整页链完整后，运行时才推进保存的
delta 游标。Graph 返回过期 delta token 时，供应商报告
`GRAPH_DELTA_RESYNC_REQUIRED`，不会跟随错误响应中的 `Location` URL。

创建请求只发送一次。写入结果不明确时，供应商执行有界回读并返回 `UNKNOWN` 及可能匹配项
元数据。即使近期只有一个任务标题和 bucket 都相同，也无法证明它属于哪一个并发请求。供应商
绝不将不明确创建报告为成功，也绝不重放该请求。包测试只使用合成身份、范围、ID 和 HTTP
响应，无需真实 Graph 凭据。

建议阅读顺序：`settings.py`、`account_center.py`、`microsoft_graph_api.py`、`plugin.py`。
