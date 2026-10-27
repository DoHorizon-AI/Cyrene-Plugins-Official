# Cyrene Microsoft Teams Connector / Cyrene Microsoft Teams 连接器

The Teams connector provides verified channel-message ingress, opt-in
one-person bot conversations, and the `message.connector.v1/send_message`
transport used by Exchange. It does not select users, decide authorization,
create Navigator tasks, or call an LLM.

Teams 消息连接器负责验证频道消息入口、可选的一对一 bot 会话，并提供 Exchange 调用的
`message.connector.v1/send_message` 传输能力。它不选择用户、不判定授权、不创建
Navigator 任务，也不调用 LLM。

## Components / 组成

| Path | Responsibility |
| --- | --- |
| `src/teams_connector/activity.py` | Normalize authenticated Teams activities and stable thread identity. |
| `src/teams_connector/navigator.py` | Sign and submit the normalized event to Navigator's durable admission endpoint. |
| `src/teams_connector/delivery_spool.py` | Persist verified event bodies and run one leased Navigator delivery worker. |
| `src/teams_connector/ingress.py` | Run the official Microsoft Teams SDK callback sidecar and health routes. |
| `src/teams_connector/connector.py` | Implement the canonical message capability over the official Teams SDK. |
| `teams-app-manifest.template.json` | Declare personal and team bot scopes with channel-message RSC consent. |

## Runtime boundary / 运行边界

The ingress sidecar uses Microsoft's current Python Teams SDK for Bot Framework
JWT signature, issuer, audience, and expiry validation. It allows only the
configured tenant and Teams for channel messages. Personal messages and
personal installation events are accepted only when
`TEAMS_PERSONAL_CHAT_ENABLED` is explicitly true and the signed activity
reports a one-person conversation. It forwards the stable AAD object ID and
original activity ID, and never persists Bot Framework authorization headers. With
`TEAMS_NAVIGATOR_DELIVERY_STATE_DIR` unset it retains the existing direct mode:
the callback waits for Navigator admission. When durable mode is enabled, the
callback writes the exact canonical Navigator body and stable activity identity
to SQLite, commits with `synchronous=FULL`, then acknowledges Teams. A single
background worker sends the saved bytes to Navigator; every retry keeps the
same body and refreshes only the timestamp and HMAC headers. Navigator's
existing connector-event idempotency prevents another Task after an ambiguous
timeout. The spool does not execute provider, model, Plugin, or Task logic.
Within one verified Teams thread, a persisted monotonic enqueue sequence keeps
later replies behind earlier unconfirmed events; independent threads may progress.

Transient network/timeouts, HTTP 408/425/429, 5xx, and source-authentication
401/403 responses retry with an exponential delay capped at five minutes. These
retries do not exhaust into message deletion; the SQLite row/byte limits bound
storage, and a full queue makes new callbacks return non-2xx. Other 4xx responses
become unresolved dead letters whose bodies remain until an owner explicitly
requeues them or intentionally retires the spool. Only Navigator's authenticated,
code-allowlisted tenant/team or owner-private binding refusal is counted as a
terminal business rejection.
Delivered and terminally rejected bodies are erased. The canonical
body includes normalized message text and verified actor/thread identity and is
stored only in the owner-only SQLite directory. Raw SDK activities and Bot
Framework or Navigator Authorization headers are never stored.

Personal installation events carry actor, tenant, bot, conversation, activity,
and validated service URL only in the HMAC-protected `trust` envelope. The Work
host authorizes the actor against its live owner projection and owns the active
route record and revocation. Channel thread identity stays shared; Navigator
must keep each actor's native session separate within that thread. Shared
channels receive approved shared metadata only. A private source query from a
shared channel fails until an approved personal binding exists. The model and
workflow notice contain no personal conversation ID or recipient ID; V3
notifications select `owner_private`, and the host resolves the route from the
verified owner task.

入口侧车使用微软当前 Python Teams SDK 校验 Bot Framework JWT 签名、issuer、audience
和有效期。它只接收配置的租户与 Teams，传递稳定 AAD object ID 与原始 activity ID，且
不持久化 Bot Framework authorization header。未设置
`TEAMS_NAVIGATOR_DELIVERY_STATE_DIR` 时保留原 direct 模式：回调等待 Navigator 接纳。
启用持久模式后，回调先把精确规范 Navigator body 和稳定 activity 身份写入 SQLite，使用
`synchronous=FULL` 提交后再向 Teams ACK。单一后台 worker 向 Navigator 投递保存的字节；每次
重试正文不变，只刷新 timestamp 与 HMAC headers。Navigator 已有的 connector event 幂等
确保网络超时不重复创建 Task。spool 不执行 provider、model、Plugin 或 Task 逻辑。
同一已验证 Teams thread 使用持久单调入队序号；较早事件未确认时，后续 reply 不会越过，
其他 thread 仍可继续投递。

网络/timeout、HTTP 408/425/429、5xx 以及 source-authentication 401/403 使用指数退避，
最长间隔五分钟。不会因为重试次数耗尽而删除消息；SQLite 行数/字节上限负责限制存储，队列满时
新 callback 返回非 2xx。其他 4xx 转为未解决 dead-letter，正文保留到 owner 显式恢复或有意退役
spool；只有 Navigator 已认证且 code 在 allowlist 中的租户/团队或 owner-private binding 拒绝
计为业务终态拒绝。已投递及业务
终态拒绝会清除正文。规范
body 包含归一化消息和已验证 actor/thread identity，只写入 owner-only SQLite 目录。原始 SDK
activity 以及 Bot Framework 或 Navigator Authorization headers 都不会保存。

持久模式仅保存规范事件中的消息文本、已验证 actor/thread identity 与必要路由字段，并且
只写入 owner-only SQLite 目录。原始 SDK activity 以及 Bot Framework 或 Navigator
Authorization headers 都不会保存。

The app manifest uses Microsoft's [v1.28 schema](https://developer.microsoft.com/json-schemas/teams/v1.28/MicrosoftTeams.schema.json)
and declares `personal` and `team` bot scopes. It requests only
`ChannelMessage.Read.Group`; that resource-specific permission covers messages
in the team where the app is installed and consented. Personal one-to-one bot
activities do not add a Graph or chat RSC permission. The runtime team allowlist
further restricts channel activities. Navigator and Directory remain
authoritative for the user's connector role; the connector does not hard-code a
person's identity. Personal scope stays disabled in activation config by
default. Updating the installed app package and obtaining each intended user's
installation/consent remain external acceptance steps. No new pilot user is
enabled by this source change. File support stays disabled because this release
omits attachment URLs and sends text-only replies.

Team-scoped v1.28 app packages set `supportsChannelFeatures` to `tier1` because
the Teams app catalog rejects team-scoped packages that omit this declaration.
It does not add a Graph permission or an RSC permission.

App manifest 使用微软的 [v1.28 schema](https://developer.microsoft.com/json-schemas/teams/v1.28/MicrosoftTeams.schema.json)，
声明 `personal` 与 `team` bot scope，并且只申请 `ChannelMessage.Read.Group`。该资源特定
权限仅覆盖安装并同意应用的 Team 中的消息。个人一对一 bot activity 不增加 Graph 或 chat RSC
权限。运行时 team allowlist 会进一步限制频道 activity。用户连接器角色仍由 Navigator 与
Directory 权威判定；连接器不硬编码个人身份。activation 配置默认关闭 personal scope。更新已安装
应用包并取得每位目标用户的安装/同意仍属于外部验收。本源码变更不会启用新的 pilot 用户。当前
版本不支持文件，因为会省略附件 URL，且只发送文本回复。

`team` scope 的 v1.28 应用包将 `supportsChannelFeatures` 设为 `tier1`，因为 Teams 应用
目录会拒绝缺少此声明的 team-scoped package。该声明不会增加 Graph 或 RSC 权限。

The ingress uses the [Microsoft Teams SDK for Python](https://microsoft.github.io/teams-sdk/python/)
and keeps its built-in request authentication enabled. The app manifest's
`ChannelMessage.Read.Group` declaration follows Microsoft's
[RSC guidance for receiving channel messages](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/channel-messages-for-bots-and-agents).

入站使用 [Microsoft Teams Python SDK](https://microsoft.github.io/teams-sdk/python/)，并保留
其内置请求鉴权。应用 manifest 中的 `ChannelMessage.Read.Group` 声明遵循微软的
[频道消息 RSC 指引](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/channel-messages-for-bots-and-agents)。

## Configuration / 配置

Start from `activation.example.json`. Replace the application and tenant
placeholders from the private host configuration process. Keep the bot secret
in the host secret store and expose only its reference name in configuration.
Do not put a client secret, callback token, JWT, service URL from an activity,
or signed attachment URL in configuration or logs.

从 `activation.example.json` 开始，在私有主机配置流程中填写应用和租户标识。Bot
secret 存入主机 secret store，配置中只保存引用名。不要把 client secret、回调 token、
JWT、activity 中的 service URL 或签名附件 URL 放入配置或日志。

Ingress environment names are `TEAMS_BOT_APP_ID`, `TEAMS_TENANT_ID`,
`TEAMS_ALLOWED_TEAM_IDS`, `TEAMS_PERSONAL_CHAT_ENABLED`,
`NAVIGATOR_INTERNAL_BASE_URL`, and
`TEAMS_NAVIGATOR_HMAC_KEY_FILE`. The HMAC key is read from the mounted file into
process memory; the key value is never placed in an environment variable or
event body. `TEAMS_NAVIGATOR_KEY_ID` defaults to `teams-v1`, the callback port
defaults to `3978`, and the host admission timeout defaults to five seconds.
`TEAMS_PERSONAL_CHAT_ENABLED` accepts only `true` or `false` and defaults to
`false`. The package activation setting `personal_chat_enabled` uses the same
default. Enabling either side alone is insufficient: ingress and outbound
transport must both be explicitly configured, and the Work host must have an
active owner-private installation binding.

Durable mode is enabled only when both `TEAMS_NAVIGATOR_DELIVERY_STATE_DIR` and
`TEAMS_DELIVERY_ADMIN_TOKEN_FILE` are set. Mount a writable owner-only persistent
directory at `/var/lib/cyrene/teams-delivery` and set it as the state directory;
SQLite uses `delivery-spool.sqlite3` inside it. Mount an independent printable
32-256 byte admin token at `/run/secrets/teams-delivery-admin-token`, readable by
the ingress UID with mode `0400` or `0600`. The admin token is separate from the
Navigator HMAC key. Partial configuration fails startup instead of silently
falling back to direct mode. The default bounds are 10,000 rows, 64 MiB of stored
bodies, 256 KiB per event, a five-minute maximum transient retry interval,
24-hour delivered-row retention, and seven-day rejected-row retention. Transient
retries continue until Navigator confirms admission; permanent dead letters stay
recoverable until an owner explicitly requeues them. The row/byte cap causes new
webhooks to receive non-2xx if the backlog fills.

The internal admin routes are `GET /_internal/teams/delivery/status`,
`POST /_internal/teams/delivery/pause`,
`POST /_internal/teams/delivery/retry-dead`, and
`POST /_internal/teams/delivery/resume`. They require
`Authorization: Bearer <mounted admin token>` and must remain outside public
proxy routes. Pause/resume JSON is `{"holder":"<operation-id>",
"expected_revision":0}`. A successful pause gates new claims but continues
accepting authenticated webhooks after their SQLite commit; it waits for active
forwards to finish and returns metadata with `active_delivery_count: 0`. The
first pause increments `revision`; repeats by the same holder are idempotent.
Resume and dead-letter recovery must match both the current holder and revision;
dead-letter recovery also requires zero active deliveries. A stale or different
holder receives HTTP 409. `retry-dead` requeues retained bodies without returning
them. Status reports `pending_bytes` and `retained_body_bytes` but does not return
the holder, delivery IDs, event body, HMAC material, bearer token, or headers.

The SDK-verified `activity.serviceUrl` is copied only to the private
`trust.service_url` routing envelope. Navigator carries it in the private
`conversation.serviceUrl` delivery route; Exchange binds the exact value into
the connector grant and passes it as the Teams provider's `service_url` fact.
The provider validates it again and preserves region paths and trailing slash.
It is never placed in the public message payload, model prompt, or logs.

Navigator owns the dynamic maintainer projection in the read-only file named by
`TEAMS_TASK_ACTOR_SUBJECTS_FILE`. It contains `{schemaVersion:1,workspaceId,issuer,subjects}`
and is re-read for each admission and reply. Work reads the same projection
when registering, revoking, or resolving an owner-private installation.
Teams ingress does not keep a second actor allowlist, so adding or removing a
Directory maintainer does not require an ingress restart or code change.

入站环境变量为 `TEAMS_BOT_APP_ID`、`TEAMS_TENANT_ID`、`TEAMS_ALLOWED_TEAM_IDS`、
`TEAMS_PERSONAL_CHAT_ENABLED`、`NAVIGATOR_INTERNAL_BASE_URL` 和
`TEAMS_NAVIGATOR_HMAC_KEY_FILE`。HMAC key 从挂载文件
读入进程内存；key 值不会进入环境变量或 event body。`TEAMS_NAVIGATOR_KEY_ID` 默认
`teams-v1`，回调端口默认 `3978`，主机接纳超时默认五秒。
`TEAMS_PERSONAL_CHAT_ENABLED` 只接受 `true` 或 `false`，默认 `false`。package activation
设置 `personal_chat_enabled` 也默认 `false`。仅启用其中一侧仍不能收发个人消息：入站与出站
transport 都必须显式配置，且 Work host 必须存在有效的 owner-private installation binding。

持久模式仅在同时设置 `TEAMS_NAVIGATOR_DELIVERY_STATE_DIR` 与
`TEAMS_DELIVERY_ADMIN_TOKEN_FILE` 时启用。将可写、owner-only 持久目录挂载到
`/var/lib/cyrene/teams-delivery` 并作为 state 路径；SQLite 文件名固定为
`delivery-spool.sqlite3`。将独立、可打印的 32-256 字节 admin token 挂载到
`/run/secrets/teams-delivery-admin-token`，由 ingress UID 以 `0400` 或 `0600` 读取。
它与 Navigator HMAC key 分开保存。只配置其中一项会导致启动失败，不会静默退回 direct
模式。默认上限为 10,000 行、64 MiB 正文、单事件 256 KiB、最多 5 分钟退避间隔、
已投递记录保留 24 小时、业务终态拒绝记录保留 7 天。未解决 dead-letter 正文保留在有界
spool，直到 owner 显式恢复。

临时错误会持续重试直至 Navigator 确认；达到行数或字节上限后，新 webhook 收到非 2xx，
等待 Teams 重投，不会对已 ACK 的队列消息静默删正文。

内部 admin 路由为 `GET /_internal/teams/delivery/status`、
`POST /_internal/teams/delivery/pause`、`POST /_internal/teams/delivery/retry-dead` 和
`POST /_internal/teams/delivery/resume`。它们
要求 `Authorization: Bearer <挂载的 admin token>`，不得加入公网代理路由。pause/resume
JSON 为 `{"holder":"<operation-id>","expected_revision":0}`。pause 成功后禁止新 claim，
但仍接收经过认证的 webhook 并在 SQLite commit 后 ACK；所有当前转发结束后才返回，且
`active_delivery_count` 为 0。首次 pause 增加 `revision`，同一 holder 重放幂等。resume
必须同时匹配当前 holder 和 revision；dead-letter 恢复还要求 active count 为 0。旧 revision
或其他 holder 返回 HTTP 409。`retry-dead` 只重排保存的 event，不返回正文。status 返回
`pending_bytes` 与 `retained_body_bytes`，但不返回 holder、delivery ID、正文、HMAC 材料、
bearer token 或请求 headers。

SDK 验证过的 `activity.serviceUrl` 只写入 HMAC 保护的私有 `trust.service_url` 路由
envelope。Navigator 将其保存在私有 `conversation.serviceUrl` delivery route；Exchange
将精确值绑定进 connector grant，并作为 Teams provider 的 `service_url` fact 传入。Provider
再次验证该值并保留区域路径和末尾斜线。该 URL 不进入公开消息载荷、模型 prompt 或日志。

Navigator 拥有由 `TEAMS_TASK_ACTOR_SUBJECTS_FILE` 指定的只读动态维护者投影文件，格式为
`{schemaVersion:1,workspaceId,issuer,subjects}`，并在每次接纳和回复前重新读取。Work 在注册、撤销
或解析 owner-private installation 时读取相同投影。Teams 入站不维护第二份 actor allowlist，因此
新增或撤销 Directory 维护者无需重启侧车或改代码。

The Teams app package manifest uses `<BOT_APP_CLIENT_ID>` for both
`webApplicationInfo.id` and `bots[0].botId`; it does not contain a tenant ID.
The tenant ID is configured only in the private activation settings.

The private send transport accepts only `ConversationKind.PRIVATE`, a host
resolved `target_kind=owner_private`, the verified actor UUID, and the SDK
validated service URL. It calls the SDK's direct conversation send path and
rejects channel reply references. The model-facing notification contract does
not carry a conversation ID or recipient identifier.

Teams 应用 manifest 的 `webApplicationInfo.id` 与 `bots[0].botId` 都使用
`<BOT_APP_CLIENT_ID>`；manifest 不包含 tenant ID。tenant ID 只写入私有 activation 设置。

个人消息 transport 只接受 `ConversationKind.PRIVATE`、宿主解析的
`target_kind=owner_private`、已验证 actor UUID 与 SDK 校验过的 service URL。它调用 SDK 的
conversation send 路径，并拒绝频道 reply reference。模型可见的 notification contract 不包含
conversation ID 或 recipient 标识。

## Local validation / 本地验证

Install the locked package dependencies and run the focused unit tests under
`tests/`. Tests use fabricated keys and activities and never contact Teams or
send messages.

安装锁定的依赖后运行 `tests/` 中的定向单元测试。测试使用伪造密钥和活动，不连接
Teams，也不发送消息。
