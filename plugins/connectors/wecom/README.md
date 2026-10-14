# Official WeCom Unified Connector / 企业微信统一连接器

The package implements `message.connector.v1` for WeCom application messages
and the OpenWS smart-bot connection. Production bot connections use the
[official WeCom Python SDK](https://github.com/WecomTeam/wecom-aibot-python-sdk/tree/6bcb59a9a636c566f4c6ea5268b228e3def1611a)
pinned to commit `6bcb59a9a636c566f4c6ea5268b228e3def1611a` (package version
1.0.1, MIT).
The bot path emits canonical inbound `InboundMessagePayload` events and sends
notifications only after a correlated OpenWS acknowledgement with `errcode=0`.
An ACK confirms vendor acceptance only; it does not prove delivery or reading.
The OpenWS request ID is retained in `vendor_extension`, and an absent vendor
message ID remains empty. Unknown send outcomes are typed as `UNKNOWN` and are
not retried automatically. With same-host Navigator enabled, trusted
inbound tasks carry an exact notification route and terminal task results
return through a leased, receipt-aware outbox worker. The optional
`tool.provider.v1` surface wraps a bounded set of `@wecom/cli` operations.

本软件包通过 WeCom 应用消息和 OpenWS 智能机器人连接实现
`message.connector.v1`。bot 路径会发出规范的 `InboundMessagePayload` 入站事件，
生产 bot 长连接使用固定到 commit
`6bcb59a9a636c566f4c6ea5268b228e3def1611a` 的 WeCom 官方 Python SDK
（版本 1.0.1，MIT）。bot 路径会发出规范的 `InboundMessagePayload` 入站事件，并且只在收到
包含 `errcode=0` 的匹配 OpenWS 确认后报告厂商已接受。该确认不证明消息已送达或已读。
OpenWS 请求 ID 保存在 `vendor_extension`；厂商未返回消息 ID 时保持为空。未知发送结果以
类型化 `UNKNOWN` 返回，且不会自动重试。启用同宿主 Navigator 集成后，可信入站
task 会携带精确通知 route；终态 task 结果通过带 lease 和回执语义的 outbox worker 返回。
可选的 `tool.provider.v1` 能力包装一组受限的 `@wecom/cli` 操作。

| Surface | Status |
| --- | --- |
| `send_message` / application | Implemented: text, markdown, image and file; vendor receipt maps to accepted, rate limited or rejected. |
| `send_message` / bot | Implemented with the official pinned SDK: text response or proactive send; waits for the correlated ACK with an explicit zero error code. ACK means accepted only. Missing or ambiguous ACK returns typed `UNKNOWN`, not accepted. |
| `inbound_message` / bot | Implemented: `on_subscribe` starts the WebSocket listener and emits canonical protobuf events. App mode does not provide inbound callbacks. |
| Bot image/file inbound | Canonical image and file parts accept binding-owned WeCom media IDs or validated absolute HTTP(S) URLs. Unknown attachment shapes fail closed. |
| Navigator intake | Optional, same-host loopback only. Exact account, conversation kind, conversation ID, sender, owner and session mapping gates task admission. The connector-events endpoint deduplicates by vendor message ID and atomically links the task with a fixed notification route. |
| Navigator terminal reply | With Navigator configured, the subscribed bot binding claims only `wecom.message` rows for its exact connector and trusted recipient. It persists `started` before canonical `send_message`. A vendor ACK is recorded as `uncertain` under Navigator's legacy finish contract, with `submission_status=accepted`, the transport request ID and optional vendor message reference; this does not claim delivery and is not retried. |
| `tool.provider.v1` / human CLI | Optional, isolated `cli.config_dir` profile. It exposes specific read operations; writes require the exact operation in binding configuration and the same `write_approval` value in the invocation. There is no general CLI executor. |
| Signed CLI authorization | Optional `authorization.enabled` profile health and renewal tools. A fixed read-only `message aibot sessions list` probe proves only that capability; expiry stays null unless the provider supplies it. |

Real WeCom credentials, external message sends and a real Navigator runtime were
not exercised by the offline tests (`NOT_RUN`).

## Configuration

Activation uses `CYRENE_CAPABILITY_BINDING_ID` and
`CYRENE_CAPABILITY_CONFIGURATION_JSON`. Application and bot credentials are
resolved separately from `WECOM_CORP_SECRET` and `WECOM_BOT_SECRET` by default;
the configuration may select other uppercase environment variable names with
`corp_secret_env_ref` and `bot_secret_env_ref`. The legacy direct secret fields
remain accepted for compatibility. The human `@wecom/cli` identity is separate:
enable it explicitly and provide its dedicated profile directory. Its login
state does not share application or bot credentials.

An explicit CLI-only binding uses `"mode": "cli"` with `cli_enabled: true` and
`cli_config_dir`; it needs no application or WebSocket bot secret. Signed
authorization tools additionally require a binding-owned `authorization`
configuration with the opaque `authorization_ref`, trusted workload, audience,
issuer, organization, workspace, operator subject allowlist, and a 32-byte hex HMAC key read through
`caller_context_key_env_ref` (default `WECOM_AUTHORIZATION_CONTEXT_KEY`). The
new tools accept only the fixed signed `{caller_context, arguments}` envelope.
Their arguments name this binding's opaque `authorization_ref`, or one
actor-owned `session_ref`; callers cannot select a profile, executable, path,
URL, or CLI command.
If the start response is lost, status can also resolve the original signed
start request ID as a read-only alias under the same actor and binding.

The health result covers only `wecom.message.aibot.sessions.list`. It does not
infer authorization expiry from local CLI login status, and a network or
unclassified vendor error remains `UNKNOWN`. Expiry fields stay null when the
official provider does not return a verified expiry. Renewal sessions persist
inside a private binding-specific directory below the selected CLI profile.
For an already-authorized profile, the connector never reruns `auth init`: it
accepts only a matching official grant URL returned by the failed capability
probe and rechecks the same CLI Bot ID and read grant before success. When the
profile is missing, a signed operator may start the official
`auth init --noninteractive --no-browser` onboarding flow. The upstream CLI
documents that this QR onboarding can create a new bot, so it is restricted to
the missing-profile path and remains separate from the connector's app and bot
identities. Unsupported vendor renewal URLs fail with a scoped error.

`cli.config_dir` is passed to the official CLI through the child process's
`WECOM_CLI_CONFIG_DIR` environment variable, not a `--config` argument.
Configured profiles ignore inherited `WECOM_CLI_ACCESS_TOKEN` and
`WECOM_CLI_ADDITIONAL_HEADERS` (including suffixed header variables), so ambient
credentials cannot replace that profile's identity. Other host environment
settings, including proxy configuration, remain available. Authorize the
dedicated profile with the same environment variable when running the official
CLI login flow. See the [official CLI reference](https://github.com/WecomTeam/wecom-cli/blob/f9b2815/docs/cli-reference.md).

```json
{
  "mode": "hybrid",
  "corp_id": "ww...",
  "agent_id": 1000002,
  "bot_id": "bot...",
  "cli": {
    "enabled": true,
    "config_dir": "/var/lib/cyrene/wecom-human-cli"
  },
  "approved_write_operations": [],
  "navigator": {
    "enabled": true,
    "base_url": "http://127.0.0.1:8080",
    "workspace_id": "workspace-id",
    "secret_env_ref": "WECOM_NAVIGATOR_BEARER_TOKEN",
    "trusted_conversations": [
      {
        "account_id": "bot:bot...",
        "conversation_id": "trusted-chat-id",
        "kind": "private",
        "owner_id": "navigator-owner-id",
        "session_id": "navigator-session-id",
        "trusted_senders": ["wecom-member-id"]
      }
    ]
  }
}
```

CLI-only profile and signed authorization settings can be configured without
application or bot credentials:

```json
{
  "binding_id": "wecom-cli-tools",
  "mode": "cli",
  "cli_enabled": true,
  "cli_config_dir": "/var/lib/cyrene/wecom-cli-profile",
  "authorization": {
    "enabled": true,
    "authorization_ref": "configured-authorization-ref",
    "caller_context_key_env_ref": "WECOM_AUTHORIZATION_CONTEXT_KEY",
    "caller_workload": "cyrene-exchange",
    "audience": "cyrene.connectors.wecom",
    "issuer": "cyrene-exchange",
    "organization_id": "configured-organization",
    "workspace_id": "configured-workspace",
    "operator_subjects": ["configured-operator"]
  }
}
```

Navigator URLs must use `localhost`, `127.0.0.1` or `::1`, with no URL
credentials, query or path prefix. The token is read from the named environment
variable and is never returned by health diagnostics. Navigator task intake
uses the workspace bearer and a same-host request to
`POST /api/v1/workspaces/{workspace}/work/connectors/{connector}/events`; the
`connector` is the activation `binding_id` and cannot be overridden. The
`task` is admitted only when the incoming message exactly matches a configured
trusted sender map. Messages outside that map may still be delivered to an
authorized canonical event subscriber, but they do not create Navigator work.
Integration bindings set `navigator.forward_inbound` to `false` to keep
inbound admission on the Exchange zero-LLM path; the default remains `true` for
existing bindings.

Trusted task metadata includes a notification descriptor for the same configured
account, conversation, chat type and sender. When Navigator terminal state
enqueues a task result, the background worker claims only rows filtered by
`type=wecom.message`, the configured connector ID, and a stable recipient key
derived from that trust map. It marks each row started before calling the
canonical message connector. A matching vendor ACK is stored with
`submission_status=accepted` while the legacy Navigator finish outcome remains
`uncertain`; this does not claim delivery. An explicit vendor rejection is
`failed`; transport errors or missing acknowledgements are `uncertain`. Nothing
is retried automatically. It starts
alongside the subscribed or explicitly started bot runtime when Navigator is
enabled, stays active while Navigator owns that intake session, and stops on
runtime shutdown or close. It is not created when Navigator is disabled.

`create_navigator_wecom_bridge(config)` also provides a same-host bridge with
`health()`, `list_tools()`, `call_tool(tool_id, arguments)`, `start()` and
`close()`. Its catalog and calls use the canonical `tool.provider.v1`
protobuf operations and existing write-approval checks. Construction and
health checks do not start the bot; the host starts it explicitly when its
Work API lifecycle owns the bridge.

`approved_write_operations` is empty by default. Supported approval IDs are
`calendar.schedules.create`, `todo.create`, and `todo.finish`. A write is
rejected unless its operation is pre-approved in the binding and its invocation
contains the exact matching `write_approval` value. The operation catalog lists
the exposed read operations and approval requirements. The connector never
passes a caller-supplied command string, service name or subcommand to a general
CLI executor.

## Delivery and lifecycle

- Bot mode and hybrid mode use the OpenWS smart-bot identity through the pinned
  official SDK, which manages authentication, heartbeat and reconnect. Runtime
  shutdown disconnects the SDK client.
- Application mode and hybrid mode use the configured application agent and
  cached access token. Invalid-token vendor responses refresh once before retry.
- Bot and application receipts are distinct. Application `errcode=0` returns
  the vendor `msgid`; bot `send_message` returns accepted only when the matching
  OpenWS request ID receives an explicit `errcode=0`. Missing vendor message
  IDs are not replaced with the request ID. A timeout or broken transport is a
  typed unknown outcome because acceptance is unknown.
- Navigator terminal replies use the same canonical `send_message` path and
  OpenWS receipt. The outbox worker persists `started` before sending; an ACK
  records `submission_status=accepted` inside the legacy `uncertain` finish
  outcome, while unknown results also remain `uncertain`. Nothing is resent
  automatically.
- Image and file parts on the application send path accept a binding-owned
  `vendor_media` reference or a remote HTTP(S) URI. The connector enforces
  WeCom's 2 MiB image and 20 MiB file limits and allows one media part per
  application message.
- `wecom_connection_health` reports application configuration, bot connection
  and reconnect state, human CLI profile availability/auth state, and local
  Navigator configuration separately. It does not reveal secret values.
- Inbound event subscriptions accept filters for event type, account ID,
  conversation ID, kind and sender ID. A duplicate vendor event ID is emitted
  only once during the connector process lifetime; durable Navigator
  deduplication uses the configured connector and workspace scope.

## Offline checks

```bash
python3 -m pytest plugins/connectors/wecom/tests \
  --ignore=plugins/connectors/wecom/tests/test_wecom_live_smoke.py
python3 plugins/connectors/wecom/tools/generate_message_connector_bindings.py
```

The separate `tests/test_wecom_live_smoke.py` suite runs installed CLI status,
service-schema and canonical tool-provider checks. Set
`WECOM_LIVE_CLI_CONFIG_DIR` to select an existing authorized profile. It also
verifies that an empty profile stays unauthorized despite an inherited token.
It does not send messages, create business data, or validate bot login/inbound
delivery; those require a separately approved tenant smoke test.

---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# 企业微信统一连接器

本软件包通过 WeCom 应用消息和 OpenWS 智能机器人连接实现
`message.connector.v1`。bot 路径发出规范的 `InboundMessagePayload` 入站事件，
并且只有收到匹配的 OpenWS 确认后才报告通知已接受。启用同宿主 Navigator 集成后，可信入站
task 会携带精确通知 route；终态 task 结果通过带 lease 和回执语义的 outbox worker 返回。
可选的 `tool.provider.v1` 能力包装受限的 `@wecom/cli` 操作。

| 能力 | 状态 |
| --- | --- |
| `send_message` / application | 已实现 text、markdown、image、file；根据厂商回执映射 accepted、rate limited 或 rejected。 |
| `send_message` / bot | 使用固定版本的官方 SDK 实现回复和主动文本发送；必须收到 request ID 匹配且明确 `errcode=0` 的确认才报告厂商 accepted。该状态不证明送达或已读；缺失或不确定的回执返回 typed `UNKNOWN`。 |
| `inbound_message` / bot | 已实现。`on_subscribe` 启动 WebSocket listener 并发出规范 protobuf event。app 模式没有入站 callback。 |
| bot 图片和文件 | 规范 image/file part 支持 binding 持有的 WeCom media ID 或经过校验的绝对 HTTP(S) URL。无法识别的附件格式会失败关闭。 |
| Navigator intake | 可选，仅限同宿主 loopback。必须精确匹配 account、conversation kind、conversation ID、sender、owner 和 session map 才会 admission task。connector-events endpoint 按厂商 message ID 去重，并原子关联 task。 |
| Navigator 终态通知 | 配置 Navigator 后，订阅中的 bot binding 只 claim `wecom.message`、当前 connector ID 和可信 recipient 的通知。通过规范 `send_message` 发送前先持久化 `started`。因 Navigator 旧 finish contract 不接受 `accepted`，收到厂商 ACK 后记录 `uncertain`，并在 result 中保存 `submission_status=accepted`、transport request ID 和可选 vendor message reference；这不代表已送达，也不会自动重发。 |
| `tool.provider.v1` / human CLI | 可选，必须配置独立的 `cli.config_dir` profile。只暴露指定读取操作；写操作需要在 binding 配置中批准确切 operation，并在 invocation 中提供相同 `write_approval` 值。不提供通用 CLI executor。 |
| 签名 CLI 授权 | 可选的 `authorization.enabled` 健康检查与续期工具。只读 `message aibot sessions list` 探针仅证明该项能力；除非厂商返回有效期，否则到期时间为空。 |

离线测试未使用真实 WeCom credentials、未发送外部消息、未连接真实
Navigator 运行时（`NOT_RUN`）。

## 配置

activation 使用 `CYRENE_CAPABILITY_BINDING_ID` 和
`CYRENE_CAPABILITY_CONFIGURATION_JSON`。默认通过 `WECOM_CORP_SECRET` 与
`WECOM_BOT_SECRET` 分别解析应用和 bot credentials；配置可以通过
`corp_secret_env_ref` 和 `bot_secret_env_ref` 指定其他大写环境变量名。为保持兼容，
仍接受旧的直接 secret 字段。human `@wecom/cli` identity 与 bot/app identity 分开：
必须显式启用 CLI，并指定专用 profile 目录。CLI 登录状态不会与应用或 bot 凭据共享。

`cli.config_dir` 通过子进程环境变量 `WECOM_CLI_CONFIG_DIR` 传入官方 CLI，
不使用 `--config` 参数。指定 profile 时会移除宿主继承的 `WECOM_CLI_ACCESS_TOKEN`
和 `WECOM_CLI_ADDITIONAL_HEADERS`（包括带后缀的请求头变量），防止其他身份的凭据
覆盖该 profile。代理等其他宿主环境配置保留。运行官方 CLI 登录流程时也应设置同一个
环境变量，授权该专用 profile。参见[官方 CLI 参考](https://github.com/WecomTeam/wecom-cli/blob/f9b2815/docs/cli-reference.md)。

CLI-only binding 使用 `"mode": "cli"`、`cli_enabled: true` 和
`cli_config_dir`，无需应用或 WebSocket bot secret。签名授权工具还要求 binding
配置由上游分配的不透明 `authorization_ref`、受信 workload、audience、issuer、
organization、workspace、operator subject
allowlist，以及通过 `caller_context_key_env_ref` 指定的 32 字节十六进制 HMAC key
（默认 `WECOM_AUTHORIZATION_CONTEXT_KEY`）。新工具只接受固定的签名
`{caller_context, arguments}` envelope，参数只能引用当前 binding 的不透明
`authorization_ref` 或 actor-owned 的 `session_ref`。若 start 响应丢失，status 也可在相同
actor 和 binding 范围内通过原始签名 start request ID 只读恢复。

健康结果只覆盖 `wecom.message.aibot.sessions.list`。本地 CLI 登录状态不会被推断为
授权有效期；网络错误或无法分类的厂商错误保持为 `UNKNOWN`。厂商没有返回可验证的
到期时间时，到期字段为空。续期 session 保存在所选 CLI profile 下、按 binding 隔离且
权限受限的目录中。对于已授权 profile，connector 不会重新运行 `auth init`；只有能力
探针失败时返回的匹配官方授权 URL 才能创建续期 session，并且成功前会复查相同 CLI
Bot ID 与读取授权。只有 profile 缺失时，经过签名验证的 operator 才能启动官方
`auth init --noninteractive --no-browser` onboarding。官方 CLI 文档说明该扫码 onboarding
可能创建新 bot，因此只用于缺失 profile，并与 connector 的 app/bot identity 分开。
厂商未提供受支持的续期 URL 时，会返回针对当前能力的错误。

Navigator URL 必须使用 `localhost`、`127.0.0.1` 或 `::1`，不能包含 URL credentials、
query 或 path prefix。token 从指定环境变量读取，不会由 health diagnostics 返回。
Navigator task intake 使用 workspace bearer，通过同宿主请求调用
`POST /api/v1/workspaces/{workspace}/work/connectors/{connector}/events`。其中 `connector`
固定为 activation 的 `binding_id`，不能由 Navigator 配置覆盖。只有入站消息精确匹配已配置的
trusted sender map 时才会附带并 admission `task`。map 以外的消息仍可
交付给已获授权的 canonical event subscriber，但不会创建 Navigator work。

Trusted task metadata 会包含同一 account、conversation、chat type 与 sender 的 notification
descriptor。Navigator task 进入终态并 enqueue 结果后，后台 worker 只 claim
`type=wecom.message`、配置的 connector ID 和来自 trust map 的稳定 recipient key。发送前先
持久化 `started`，然后通过规范 `send_message` 发送。Navigator 旧 finish contract 不接受
`accepted`，所以匹配厂商确认后仍记录 `uncertain`，同时在 result 中保存
`submission_status=accepted`、transport request ID 和可选 vendor message reference；这不能证明送达。
厂商明确拒绝后记录 failed；传输错误或缺失确认也记录 uncertain，且不会自动重发。启用 Navigator 后，
worker 随已订阅或显式启动的 bot runtime 启动，并在 Navigator 持有 intake session 期间运行，
在 runtime shutdown 或 close 时停止；关闭 Navigator 时不会创建 worker。

`create_navigator_wecom_bridge(config)` 还提供同宿主 bridge，支持 `health()`、
`list_tools()`、`call_tool(tool_id, arguments)`、`start()` 和 `close()`。工具目录与调用都经由规范
`tool.provider.v1` protobuf operation，并复用现有 write approval 检查。构造和 health check
不会启动 bot；由持有该 bridge 的 Work API 生命周期显式启动。

默认 `approved_write_operations` 为空。支持的批准标识为
`calendar.schedules.create`、`todo.create` 和 `todo.finish`。binding 必须预先批准对应
operation，并且每次调用需要提供完全匹配的 `write_approval`；否则写入会被拒绝。operation
catalog 会列出公开的读取操作和批准要求。connector 不会把调用方提供的命令字符串、service
名称或 subcommand 交给通用 CLI executor。

独立的 `tests/test_wecom_live_smoke.py` 使用宿主已安装的 CLI 验证授权状态、服务 schema
与规范 tool-provider 调用。可通过 `WECOM_LIVE_CLI_CONFIG_DIR` 选择已授权的 profile；
同时验证空 profile 不会继承宿主令牌。该测试不发送消息、不创建业务数据，也不验证机器人
登录与入站投递；这些操作需要单独经过授权的真实租户测试。

## 交付和生命周期

- bot 和 hybrid 模式使用 OpenWS 智能机器人身份。连接断开后会以有界 backoff 重建传输；
  runtime cancellation 和 shutdown 会关闭 listener、heartbeat 与 WebSocket tasks。
- application 和 hybrid 模式使用配置的 application agent 与缓存的 access token。遇到
  invalid-token 厂商响应时只刷新一次再重试。
- bot 与应用的回执语义各自独立。应用 `errcode=0` 返回厂商 `msgid`；bot
  `send_message` 只有在 request ID 收到明确 `errcode=0` 的厂商响应后才返回 accepted；这不证明
  已送达或已读。超时或连接故障时接受状态未知，因此返回 typed `UNKNOWN`。
- Navigator 终态回复走同一个规范 `send_message` 路径与 OpenWS 回执。outbox worker 发送前
  持久化 `started`；结果未知会记为 `uncertain`，不会自动重新发送。
- 应用出站 image/file 支持此 binding 拥有的 `vendor_media` reference 或 remote HTTP(S)
  URI。connector 按 WeCom 限制校验 image 不超过 2 MiB、file 不超过 20 MiB；每条应用消息
  最多发送一个 media part。
- `wecom_connection_health` 分别报告应用配置、bot 连接和重连、human CLI profile 的可用与
  授权状态，以及本地 Navigator 配置；不返回 secret 值。
- 入站订阅 filter 支持 event type、account ID、conversation ID、kind 和 sender ID。一个
  connector 进程生命周期内同一个厂商 event ID 只发出一次；Navigator durable dedup 使用
  配置的 connector 与 workspace scope。

```bash
python3 -m pytest plugins/connectors/wecom/tests \
  --ignore=plugins/connectors/wecom/tests/test_wecom_live_smoke.py
python3 plugins/connectors/wecom/tools/generate_message_connector_bindings.py
```

### Reproduce capability bindings / 复现能力绑定

Install the optional `bindings` dependency and run
`python tools/generate_message_connector_bindings.py` from this package.
The generator is pinned to `grpcio-tools==1.62.3`, matching the declared
Protobuf 4.x runtime, and reads both canonical capability schemas in this repository.
CI checks that regeneration leaves the checked-in bindings unchanged.

The Navigator receipt compatibility test uses a digest-pinned projection of
`NotificationFinish.outcome` from Cyrene-Navigator commit
`33a153a252e03c362eb8b8472e401676fa5c0297`. Public CI checks out that exact
owner commit and verifies the source OpenAPI file against the projection. A
Plugins-only test checkout still validates the checked-in projection digest;
the owner-contract check is never silently skipped.

安装可选的 `bindings` 依赖后，在本包运行上述命令。生成器固定为
`grpcio-tools==1.62.3`，与声明的 Protobuf 4.x 运行时兼容；消息和工具能力均从本仓库
的规范 schema 生成，CI 会检查生成结果与已提交文件完全一致。

Navigator 回执兼容性测试使用从 Cyrene-Navigator commit
`33a153a252e03c362eb8b8472e401676fa5c0297` 生成的固定摘要投影。Public CI 会检出该精确
owner commit，并对照投影校验 OpenAPI 源文件。仅有 Plugins 的测试工作区仍会校验已提交
投影的摘要，不会静默跳过 owner contract 检查。
