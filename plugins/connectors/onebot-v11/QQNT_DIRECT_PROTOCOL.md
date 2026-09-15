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
socket, WebSocket endpoint, or OneBot endpoint. The QQ Host's own outbound
network activity remains outside this connector IPC boundary.

子进程通过继承的二进制 stdin/stdout 启动。每帧由 4 字节大端长度和 UTF-8 JSON
对象组成，单帧上限为 8 MiB。Connector 不创建 TCP listener、Unix socket、
WebSocket 或 OneBot endpoint。QQ Host 自身的外联网络行为不属于本 Connector 的
IPC 边界。

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
    "required_client_version": "<exact-approved-build>"
  }
}
```

The response must return the same protocol, version, binding, generation,
platform, and exact `client_version`. A mismatch fails closed.

握手响应必须回传相同的协议、版本、binding、generation、平台和准确
`client_version`；任一不一致都必须 fail closed。

Normal request/response uses `type=request|response`, `operation`, `params`,
`ok`, and either `result` or bounded `{code,message}` error data. Shutdown is a
`type=shutdown` control message. Cancellation is a `type=cancel` message with
the original request identity.

普通调用使用 `type=request|response`、`operation`、`params`、`ok`，以及 `result`
或有界的 `{code,message}` 错误数据。关闭使用 `type=shutdown`，取消使用带原始请求
身份的 `type=cancel`。

Events use `type=event`, a stable `event_id`, the binding and generation, an
event name, and a typed `payload`. `message.received` is normalized directly
to `message.connector.v1`; it is never serialized to an intermediate OneBot
JSON envelope.

事件使用 `type=event`、稳定 `event_id`、binding/generation、事件名和类型化
`payload`。`message.received` 直接映射到 `message.connector.v1`，不会先序列化为
OneBot JSON 再解析。

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

仓库中的 fake Host 只是独立编写的协议测试 fixture。其测试通过只证明分帧、生命周期和
映射机制，不证明官方 QQ 兼容性。精确授权 QQ build 未实测前，真实 API 行保持
`NOT_RUN`。
