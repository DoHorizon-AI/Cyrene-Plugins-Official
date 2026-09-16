# Official OneBot v11 and QQNT Direct Connector / 官方 OneBot v11 与 QQNT Direct Connector

This package implements the Plugins-owned `message.connector.v1` capability.
The original OneBot v11 transport remains available, and the `qqnt-direct`
profile adds a direct QQ Host adapter without changing the generic OneBot path.
It owns no Product session, reply, persona, command, or persistence policy.

本包实现由 Plugins 所有的 `message.connector.v1` 能力。原有 OneBot v11 传输保持可用，
`qqnt-direct` profile 新增直接连接 QQ Host 的适配器，不改变通用 OneBot 路径。本包不拥有
Product 的会话、回复、人格、命令或持久化策略。

## QQNT direct profile / QQNT direct profile

`qqnt-direct` starts one explicitly configured QQ Host executable as a child
and exchanges Cyrene-owned length-delimited JSON frames over inherited stdio.
It rejects OneBot endpoint/token settings, requires an exact allow-listed QQ
client version and Linux x86_64 target, and keeps one data directory and
generation per binding. It creates no connector-owned TCP listener and never
round-trips native events through OneBot JSON.

`qqnt-direct` 将一个明确配置的 QQ Host 可执行文件作为子进程启动，通过继承 stdio 交换
Cyrene 自有长度分帧 JSON。它拒绝 OneBot endpoint/token 配置，要求精确 allow-list QQ
版本和 Linux x86_64 目标，并为每个 binding 保持独立数据目录与 generation。它不创建
Connector 自有 TCP listener，也不会把原生事件绕行 OneBot JSON。

The fixed QQ extension operations are documented in
[`QQNT_DIRECT_API_MATRIX.md`](QQNT_DIRECT_API_MATRIX.md), and the frame
contract is documented in [`QQNT_DIRECT_PROTOCOL.md`](QQNT_DIRECT_PROTOCOL.md).
The matrix intentionally keeps real API rows at `NOT_RUN` until an authorized
exact QQ build is exercised; the committed fake Host only proves mechanics.

固定 QQ 扩展操作见 [`QQNT_DIRECT_API_MATRIX.md`](QQNT_DIRECT_API_MATRIX.md)，分帧契约见
[`QQNT_DIRECT_PROTOCOL.md`](QQNT_DIRECT_PROTOCOL.md)。在获授权的精确 QQ build 实测前，
矩阵中的真实 API 行有意保持 `NOT_RUN`；仓库 fake Host 只证明机制。

## Direct data plane / 直连数据平面

A Product obtains or supplies one configured binding, then calls this Plugin
endpoint directly. Platform may install, resolve, authorize, supervise, and
return the binding endpoint; it does not receive connector request or response
payloads.

Product 获取或提供一个配置好的 binding 后直接调用本插件端点。Platform 可以安装、发现、授权、
监督并返回 binding endpoint，但不接收 connector 的请求与响应载荷。

The package builder vendors the Plugins-owned `cyrene_plugin_runtime` into the
archive. An installed service package therefore starts without a source
checkout or a Platform-owned copy of the data-plane runtime.

`binding_id` is stable configuration identity. A process, PID, connection, or
runtime generation is a lifecycle observation and cannot replace it. The same
binding owns outbound `send_message`, inbound `inbound_message`, and normalized
`inbound_request` events.

The package exposes direct host APIs:

- `send_message(request, cancellation=...)` performs the capability action;
- `respond_request(request, cancellation=...)` maps friend or group-invite
  approval to the corresponding OneBot request action;
- `on_subscribe(..., emitter)` binds an inbound event stream;
- `publish_inbound_event(event)` emits one binding-local inbound event;
- `on_configure(settings)` applies one host-provided binding;
- `on_invoke(...)` is the formal `DirectPluginRuntime.Invoke` data-plane entry
  for `message.connector.v1` and `qq.client.v1`; it routes to the selected
  profile without adding a OneBot JSON or transport hop.

`on_invoke(...)` 是 `DirectPluginRuntime.Invoke` 的正式数据平面入口，负责
`message.connector.v1` 和 `qq.client.v1`；它只路由到选定 profile，不增加
OneBot JSON 或额外传输中转。

## Contract authority / 契约权威

The canonical schema is
[`contracts/proto/cyrene/message/connector/v1/message_connector.proto`](../../../contracts/proto/cyrene/message/connector/v1/message_connector.proto).
The checked-in Python module is a generated projection, not a second schema.
Regenerate it without a Platform checkout:

```bash
uv run --frozen python plugins/connectors/onebot-v11/tools/generate_message_connector_bindings.py
git diff --exit-code -- plugins/connectors/onebot-v11/src/onebot_v11_connector/_generated/message_connector_pb2.py
```

The generator, Rust/C# projection tests, and cross-language generation checks
live under
[`contracts/tck/message-connector-v1`](../../../contracts/tck/message-connector-v1/README.md).

`inbound_request` uses the JSON schema
[`contracts/json/message-connector-v1-inbound-request.schema.json`](../../../contracts/json/message-connector-v1-inbound-request.schema.json).
`respond_request` uses the JSON schema
[`contracts/json/message-connector-v1-request-response.schema.json`](../../../contracts/json/message-connector-v1-request-response.schema.json)
and request type URL
`type.cyrene.io/message.connector.v1.respond_request.request`. `friend` calls
`set_friend_add_request`; `group_invite` calls `set_group_add_request` with
`sub_type=invite`. Other connector families declare inbound messages but do
not claim this OneBot-specific request action until their vendor mapping is
implemented.

## Transport profiles / 传输配置

`forward_websocket` owns the OneBot TCP/WebSocket session, token handshake,
frame parsing, ping/pong, JSON actions, `echo` correlation, reconnect backoff,
deadline/cancellation, and shutdown. `reverse_websocket` owns a binding-local
listener. `http_api` remains available for hosts exposing the OneBot HTTP API.
Side-effecting sends are never retried implicitly after disconnect.

Compatible OneBot v11 runtimes use the same connector identity. Offline
protocol tests do not claim a live endpoint; runtime E2E remains
deployment-owner evidence.
