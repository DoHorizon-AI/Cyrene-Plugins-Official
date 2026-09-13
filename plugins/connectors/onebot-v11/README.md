# Official OneBot v11 Connector / 官方 OneBot v11 Connector

This package implements the Plugins-owned `message.connector.v1` capability.
It maps canonical ordered message parts to OneBot v11 actions and normalizes
inbound events back to the same contract. It owns no Product session, reply,
persona, command, or persistence policy.

本包实现由 Plugins 所有的 `message.connector.v1` 能力，负责规范消息与 OneBot v11 action
之间的转换，不拥有 Product 的会话、回复、人格、命令或持久化策略。

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
- `on_invoke(...)` is a temporary generic-worker compatibility adapter and is
  not the target Product data path.

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
