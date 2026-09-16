# QQNT Direct Host Protocol / QQNT Direct Host 协议

This is a Cyrene-owned protocol between the Python connector worker and one
configured, authorized QQ Host child. It is not OneBot, and it is not a public
Product API. The generic Cyrene direct runtime remains the outer data plane.

这是 Python connector worker 与一个已配置、已授权 QQ Host 子进程之间的
Cyrene 自有协议。它不是 OneBot，也不是 Product 公共 API；外层仍使用 Cyrene
现有 direct runtime。

## Transport / 传输

The child is started with inherited binary stdin/stdout. Each frame is:

```text
uint32 big-endian payload length
UTF-8 JSON object
```

The maximum payload is 8 MiB. The connector creates no TCP listener, Unix
socket, WebSocket endpoint, or OneBot endpoint. The worker also probes the
Linux x86_64 Host process tree and fails closed if it observes an IPv4 or IPv6
TCP socket in `LISTEN`; outbound QQ connections remain allowed and outside
this connector IPC boundary.

子进程通过继承的二进制 stdin/stdout 启动。每帧由 4 字节大端长度和 UTF-8 JSON
对象组成，单帧上限为 8 MiB。Connector 不创建 TCP listener、Unix socket、
WebSocket 或 OneBot endpoint。Worker 还会检查 Linux x86_64 Host 进程树；发现 IPv4 或
IPv6 TCP socket 处于 `LISTEN` 时会 fail closed。QQ Host 自身的外联网络行为仍然允许，且
不属于本 Connector 的 IPC 边界。

## Installation selection / 安装选择

The first target is Linux x86_64.  The worker accepts an operator-provided
absolute Host path and binding data path, canonicalizes them before launch, and
rejects missing, non-regular, non-executable, symlinked-data, or non-Linux
selections.  An optional `installation_manifest` records one exact
`cyrene.qq.installation.v1` selection; zero or multiple manifests are rejected,
and its build, platform, architecture, Host path, and data path must agree with
the binding configuration.  The Host hello remains the authoritative observed
QQ build/ABI check.

首个目标固定为 Linux x86_64。Worker 接受操作员提供的绝对 Host 路径和 binding 数据路径，
启动前完成规范化，并拒绝不存在、非普通文件、不可执行、数据目录为符号链接或非 Linux 的
选择。可选的 `installation_manifest` 用于记录唯一的
`cyrene.qq.installation.v1` 安装选择；零个或多个 manifest 都会被拒绝，manifest 中的 build、
平台、架构、Host 路径和数据路径必须与 binding 配置一致。QQ build/ABI 的最终实测校验仍以
Host hello 为准。

## Supervision / 进程监督

An unexpected process or stdio exit is recoverable only through a bounded
binding-local restart budget with exponential backoff and a crash circuit.  A
recovery starts a new generation and reinitializes the session and subscriptions;
it never retries the operation that observed the failure.  Protocol, version,
account, login, and configuration failures remain fail-closed and are not
automatically retried.  Shutdown drains and reaps the binding-local process
group.

非预期进程或 stdio 退出只能通过 binding 独立的有界重启预算、指数退避和崩溃熔断恢复。
恢复会启动新 generation，并重新初始化 session 与订阅；不会重试观察到故障的原操作。
协议、版本、账号、登录和配置错误保持 fail-closed，不自动重试。关闭流程会排空并回收
binding 独立的进程组。

## Correlation / 关联

Every request carries `binding_id`, `generation`, and a request ID in the
format `<binding_id>:<generation>:<counter>`. The parent rejects responses and
events from another binding or generation. A timed-out or cancelled request is
removed before a `cancel` frame is sent; a late response is ignored.

每个请求携带 `binding_id`、`generation`，请求 ID 格式为
`<binding_id>:<generation>:<counter>`。父进程拒绝来自其他 binding 或 generation
的响应与事件。超时或取消请求会先从关联表移除，再发送 `cancel`；迟到响应直接丢弃。

## Message shapes / 消息形状

Handshake request:

```json
{
  "type": "request",
  "operation": "hello",
  "request_id": "qq-main:1:1",
  "binding_id": "qq-main",
  "generation": 1,
  "params": {
    "protocol": "cyrene.qq.host.v1",
    "protocol_version": "1",
    "platform": "linux-x86_64",
    "required_client_version": "<exact-approved-build>",
    "required_host_abi": "<exact-approved-host-abi>"
  }
}
```

The response must return the same protocol, version, binding, generation,
platform, exact `client_version`, and exact `abi` configured by the operator.
A mismatch or missing ABI fails closed.

握手响应必须回传相同的协议、版本、binding、generation、平台和准确
`client_version`，并回传配置的准确 `abi`；任一不一致或缺失都必须
fail closed。

Normal request/response uses `type=request|response`, `operation`, `params`,
`ok`, and either `result` or bounded `{code,message}` error data. Shutdown is a
`type=shutdown` control message. Cancellation is a `type=cancel` message with
the original request identity.

普通调用使用 `type=request|response`、`operation`、`params`、`ok`，以及 `result`
或有界的 `{code,message}` 错误数据。关闭使用 `type=shutdown`，取消使用带原始请求
身份的 `type=cancel`。

The worker validates every successful Host result before exposing it to the
connector or extension caller. Results must be bounded JSON objects, may not
contain credential-like fields such as tokens, secrets, tickets, or cookies,
and `qq.message.send` must return a native `message_id`. Media and file
results may expose only bounded HTTP(S) `remote_uri` values or binding-private
`qq://`/`staging://` local references. Any violation is returned as
`PROTOCOL_MISMATCH`; callback registration created for that request is removed
before the failure is surfaced.

Worker 会在成功结果进入 Connector 或扩展调用方之前校验每一个 Host result。结果必须是
有界 JSON 对象，不能包含 token、secret、ticket、cookie 等凭据类字段；
`qq.message.send` 必须返回 native `message_id`。媒体和文件结果只能暴露有界的 HTTP(S)
`remote_uri` 或 binding 私有的 `qq://`/`staging://` 本地引用。违反任一约束都会返回
`PROTOCOL_MISMATCH`；该请求若已登记回调，也会在向上报告失败前清理登记。

Events use `type=event`, a stable `event_id`, the binding and generation, an
event name, and a typed `payload`. `message.received` is normalized directly
to `message.connector.v1`; it is never serialized to an intermediate OneBot
JSON envelope.

事件使用 `type=event`、稳定 `event_id`、binding/generation、事件名和类型化
`payload`。`message.received` 直接映射到 `message.connector.v1`，不会先序列化为
OneBot JSON 再解析。

Completion callbacks use the originating request ID and are accepted only for
the two fixed callback records declared in the operation matrix:
`message.send_completion` and `media.download_complete`.

完成回调必须携带发起调用的 request ID，并且只接受矩阵中登记的两个固定回调：
`message.send_completion` 与 `media.download_complete`。

```json
{
  "type": "event",
  "event": "message.send_completion",
  "event_id": "qq-main:1:9-completion",
  "request_id": "qq-main:1:9",
  "binding_id": "qq-main",
  "generation": 1,
  "payload": {
    "message_id": "<native-message-id>",
    "sequence": 7,
    "random": 11,
    "peer_uid": "<native-peer-uid>",
    "status": "completed"
  }
}
```

`media.download_complete` uses the same correlation rule and may carry only
bounded media/file/element identity, progress, a local result reference, a
validated HTTP(S) URI, status, and structured error fields. Missing,
cross-generation, or mismatched request IDs are dropped and never exposed as
generic QQ events. The callback record is published as typed
`type.cyrene.io/qq.client.v1.Callback` data; it is not requestable.

`media.download_complete` 同样按 request ID 关联，只能携带有界的媒体/文件/元素身份、
进度、本地结果引用、经过校验的 HTTP(S) URI、状态和结构化错误。缺失、跨 generation 或
不匹配的 request ID 会被丢弃，不会降级成通用 QQ 事件。回调记录通过类型化的
`type.cyrene.io/qq.client.v1.Callback` 发布，不能作为请求调用。

Completion records are terminal: after the first valid callback consumes the
originating request identity, later callbacks for the same request are
dropped even when they use a different event ID. Callback identity fields
(`peer_uid`, `peer_uin`, `group_code`, `user_uid`, and `user_uin`) remain
independent values and are never inferred from one another.

完成回调是终态记录：首个合法回调消费原始请求身份后，同一请求的后续回调即使使用不同
event ID 也会丢弃。回调中的 `peer_uid`、`peer_uin`、`group_code`、`user_uid` 和
`user_uin` 保持为相互独立的值，不会互相推断。

The three `qq.session.*` operations are explicit lifecycle actions. Calling
one does not implicitly execute the other two; ordinary message or extension
operations perform the missing ordered bootstrap stages once per Host
generation.

三个 `qq.session.*` 操作是显式生命周期动作。调用其中一个不会隐式执行另外两个；普通消息
或扩展操作只会在每个 Host generation 内按顺序补齐尚未完成的启动阶段，并且每阶段只执行一次。

## Host responsibilities / Host 责任

The configured Host adapter owns the version-specific official QQ integration
and maps each fixed `qq.*` operation to the authorized typed Service method
listed in `QQNT_DIRECT_API_MATRIX.md`. It must not accept arbitrary
`service`/`method` or raw request passthrough. Credentials, tickets, session
files, and message bodies must not be written to connector diagnostics.

配置的 Host adapter 负责特定 QQ 版本的官方集成，并将固定的 `qq.*` 操作映射到矩阵中
登记的类型化 Service 方法。它不得接受任意 `service`/`method` 或原始请求透传；凭据、
票据、会话文件和消息正文不得写入 Connector diagnostics。

The checked-in fake Host is only an independently authored protocol fixture.
Its green tests prove framing, lifecycle, and mapping mechanics, not official
QQ compatibility. Real API rows remain `NOT_RUN` until the exact authorized
QQ build is exercised.

Only the named `message.received`, `request.received`, and fixed completion
events cross the application seam. Events from additional QQ services are
dropped until they receive an explicit contract and matrix entry; they are not
forwarded as an untyped generic event.

仓库中的 fake Host 只是独立编写的协议测试 fixture。其测试通过只证明分帧、生命周期和
映射机制，不证明官方 QQ 兼容性。精确授权 QQ build 未实测前，真实 API 行保持
`NOT_RUN`。

只有命名的 `message.received`、`request.received` 和固定完成回调可以跨越应用边界。其他
QQ Service 事件在拥有明确契约和矩阵条目之前会被丢弃，不会作为无类型通用事件转发。
