# Cyrene Message Connector Contracts

This Plugins-owned consumer SDK provides generated Python bindings and strict
payload codecs for `message.connector.v1`, interface version `1`. It is generated
from the frozen `contracts/proto/cyrene/message/connector/v1/message_connector.proto`
at Plugins commit `8e271a0942ade7d5d7a197c79e71e500da7f6bf7` (the proto's git blob is
`d334637bede6d5553aeeb8a4c928e49a0e5e6a44`). The source descriptor name remains
`message_connector.proto` so this SDK can coexist in one Python process with the
connector-private generated modules in IM, Teams, WeCom, and OneBot.

The outbound `send_message` invocation and `inbound_message` event use protobuf;
`respond_request` invocation and `inbound_request` event use bounded JSON. Their
canonical type URLs are exposed by `REQUEST_TYPE_URLS`, `RESPONSE_TYPE_URLS`, and
`EVENT_TYPE_URLS`. The SDK contains no connector transport, QQ implementation,
Product policy, or DSH execution behavior.

Run `python generate_bindings.py` with `grpcio-tools==1.62.3` available to
regenerate the checked-in binding. The command uses the proto's containing
directory as its include root; this is required to keep the descriptor filename
compatible with the established connector bindings.

---

## 中文

此 Plugins 所有的消费者 SDK 为 `message.connector.v1` interface version `1`
提供生成的 Python bindings 和严格 payload codec。它由 Plugins commit
`8e271a0942ade7d5d7a197c79e71e500da7f6bf7` 的冻结协议
`contracts/proto/cyrene/message/connector/v1/message_connector.proto` 生成，proto
blob 为 `d334637bede6d5553aeeb8a4c928e49a0e5e6a44`。descriptor 文件名保持为
`message_connector.proto`，以便在同一个 Python 进程中与 IM、Teams、WeCom 和
OneBot connector 内部生成的绑定共存。

出站 `send_message` invocation 与 `inbound_message` event 使用 protobuf；
`respond_request` invocation 与 `inbound_request` event 使用有界 JSON。
`REQUEST_TYPE_URLS`、`RESPONSE_TYPE_URLS`、`EVENT_TYPE_URLS` 提供规范 type URL。
此 SDK 不包含 connector 传输、QQ 实现、Product 策略或 DSH 执行行为。

安装 `grpcio-tools==1.62.3` 后运行 `python generate_bindings.py` 可重新生成绑定。
命令把 proto 所在目录作为 include root；这样才能保持 descriptor 文件名与现有
connector bindings 兼容。
