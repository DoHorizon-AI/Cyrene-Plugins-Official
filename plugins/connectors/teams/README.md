# Cyrene Microsoft Teams Connector / Cyrene Microsoft Teams 连接器

The Teams connector provides a verified channel-message ingress and the
`message.connector.v1/send_message` transport used by Exchange. It does not
select users, decide authorization, create Navigator tasks, or call an LLM.

Teams 消息连接器负责验证频道消息入口，并提供 Exchange 调用的
`message.connector.v1/send_message` 传输能力。它不选择用户、不判定授权、不创建
Navigator 任务，也不调用 LLM。

## Components / 组成

| Path | Responsibility |
| --- | --- |
| `src/teams_connector/activity.py` | Normalize authenticated Teams activities and stable thread identity. |
| `src/teams_connector/navigator.py` | Sign and submit the normalized event to Navigator's durable admission endpoint. |
| `src/teams_connector/ingress.py` | Run the official Microsoft Teams SDK callback sidecar and health routes. |
| `src/teams_connector/connector.py` | Implement the canonical message capability over the official Teams SDK. |
| `teams-app-manifest.template.json` | Declare a team-scoped bot with channel-message RSC consent. |

## Runtime boundary / 运行边界

The ingress sidecar uses Microsoft's current Python Teams SDK for Bot Framework
JWT signature, issuer, audience, and expiry validation. It allows only the
configured tenant and Teams, forwards the stable AAD object ID and original
activity ID, and returns HTTP success to Teams only after Navigator confirms
durable admission. No actor identity or credential is persisted by the sidecar.
Only Navigator's authenticated terminal tenant/team refusal codes are
acknowledged without admission. HMAC/key-ID/timestamp failures, unknown 4xx,
and all 5xx responses remain retryable and never become a silent drop.

入口侧车使用微软当前 Python Teams SDK 校验 Bot Framework JWT 签名、issuer、audience
和有效期，只接收配置的租户与 Teams。它传递稳定 AAD object ID 和原始 activity ID，
只有 Navigator 确认持久接纳后才向 Teams 返回成功。侧车不持久化用户身份或凭据。
只有 Navigator 返回已认证的租户/团队终态拒绝码时才会在未接纳任务的情况下 ACK。
HMAC/key-ID/timestamp 错误、未知 4xx 和所有 5xx 均保持可重投，不能静默丢弃。

The app manifest uses Microsoft's [v1.28 schema](https://developer.microsoft.com/json-schemas/teams/v1.28/MicrosoftTeams.schema.json),
installs only in the `team` scope, and requests only `ChannelMessage.Read.Group`.
That resource-specific permission covers messages in the team where the app is
installed and consented. The runtime team allowlist further restricts accepted
activities. Navigator and Directory remain authoritative for the user's
connector role; the connector does not hard-code a person's identity. File
support stays disabled because this release omits attachment URLs and sends
text-only replies.

Team-scoped v1.28 app packages set `supportsChannelFeatures` to `tier1` because
the Teams app catalog rejects team-scoped packages that omit this declaration.
It does not add a Graph permission or an RSC permission.

App manifest 使用微软的 [v1.28 schema](https://developer.microsoft.com/json-schemas/teams/v1.28/MicrosoftTeams.schema.json)，
只安装到 `team` scope，并只申请 `ChannelMessage.Read.Group`。该资源特定权限仅覆盖
安装并同意应用的 Team 中的消息。运行时 team allowlist 会进一步限制接收范围。用户连接器
角色仍由 Navigator 与 Directory 权威判定；连接器不硬编码个人身份。当前版本不支持文件，
因为会省略附件 URL，且只发送文本回复。

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
`TEAMS_ALLOWED_TEAM_IDS`, `NAVIGATOR_INTERNAL_BASE_URL`, and
`TEAMS_NAVIGATOR_HMAC_KEY_FILE`. The HMAC key is read from the mounted file into
process memory; the key value is never placed in an environment variable or
event body. `TEAMS_NAVIGATOR_KEY_ID` defaults to `teams-v1`, the callback port
defaults to `3978`, and the host admission timeout defaults to five seconds.

The SDK-verified `activity.serviceUrl` is copied only to the private
`trust.service_url` routing envelope. Navigator carries it in the private
`conversation.serviceUrl` delivery route; Exchange binds the exact value into
the connector grant and passes it as the Teams provider's `service_url` fact.
The provider validates it again and preserves region paths and trailing slash.
It is never placed in the public message payload, model prompt, or logs.

Navigator owns the dynamic maintainer projection in the read-only file named by
`TEAMS_TASK_ACTOR_SUBJECTS_FILE`. It contains `{schemaVersion:1,workspaceId,issuer,subjects}`
and is re-read for each admission and reply. Teams ingress does not keep a
second actor allowlist, so adding or removing a Directory maintainer does not
require an ingress restart or code change.

入站环境变量为 `TEAMS_BOT_APP_ID`、`TEAMS_TENANT_ID`、`TEAMS_ALLOWED_TEAM_IDS`、
`NAVIGATOR_INTERNAL_BASE_URL` 和 `TEAMS_NAVIGATOR_HMAC_KEY_FILE`。HMAC key 从挂载文件
读入进程内存；key 值不会进入环境变量或 event body。`TEAMS_NAVIGATOR_KEY_ID` 默认
`teams-v1`，回调端口默认 `3978`，主机接纳超时默认五秒。

SDK 验证过的 `activity.serviceUrl` 只写入 HMAC 保护的私有 `trust.service_url` 路由
envelope。Navigator 将其保存在私有 `conversation.serviceUrl` delivery route；Exchange
将精确值绑定进 connector grant，并作为 Teams provider 的 `service_url` fact 传入。Provider
再次验证该值并保留区域路径和末尾斜线。该 URL 不进入公开消息载荷、模型 prompt 或日志。

Navigator 拥有由 `TEAMS_TASK_ACTOR_SUBJECTS_FILE` 指定的只读动态维护者投影文件，格式为
`{schemaVersion:1,workspaceId,issuer,subjects}`，并在每次接纳和回复前重新读取。Teams
入站不维护第二份 actor allowlist，因此新增或撤销 Directory 维护者无需重启侧车或改代码。

The Teams app package manifest uses `<BOT_APP_CLIENT_ID>` for both
`webApplicationInfo.id` and `bots[0].botId`; it does not contain a tenant ID.
The tenant ID is configured only in the private activation settings.

Teams 应用 manifest 的 `webApplicationInfo.id` 与 `bots[0].botId` 都使用
`<BOT_APP_CLIENT_ID>`；manifest 不包含 tenant ID。tenant ID 只写入私有 activation 设置。

## Local validation / 本地验证

Install the locked package dependencies and run the focused unit tests under
`tests/`. Tests use fabricated keys and activities and never contact Teams or
send messages.

安装锁定的依赖后运行 `tests/` 中的定向单元测试。测试使用伪造密钥和活动，不连接
Teams，也不发送消息。
