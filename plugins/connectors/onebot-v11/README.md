# Official OneBot v11 Connector / 官方 OneBot v11 连接器

`cyrene.connectors.onebot-v11` owns only the generic `message.connector.v1`
capability. It provides OneBot v11 HTTP API, forward WebSocket, and reverse
WebSocket transport profiles through the .NET 10 Native AOT executable
`bin/cyrene-onebot-v11`.

`cyrene.connectors.im` is the separate owner of `qq.client.v1`, QQNT direct,
the QQ Host protocol, and the QQ/IM Python parity suite. QQ operations and
QQ installation documents are intentionally absent from this package.

`src/onebot_v11_connector` remains a Python behavior reference only. The
formal package is native-only; the reference package is used for deterministic
cross-language checks while the migration is being evaluated.

## Public boundary / 公共边界

- plugin ID: `cyrene.connectors.onebot-v11`
- version: `0.4.0`
- capability: `message.connector.v1@1`
- protocol: `cyrene.plugin.runtime.v1.DirectPluginRuntime`
- package RIDs: `linux-x64`, `linux-arm64`

The canonical protobuf and type URLs remain owned by the shared contracts.
This connector does not own Product session, reply, persona, command, or
persistence policy.
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# 官方 OneBot v11 Connector

cyrene.connectors.onebot-v11 只拥有通用 message.connector.v1 capability。它通过 .NET 10 Native AOT 可执行文件 bin/cyrene-onebot-v11，提供 OneBot v11 HTTP API、forward WebSocket 和 reverse WebSocket transport profile。

cyrene.connectors.im 独立负责 qq.client.v1、QQNT direct、QQ Host 协议和 QQ/IM Python 一致性测试套件。本 package 有意不包含 QQ 操作和 QQ 安装文档。

src/onebot_v11_connector 仅作为 Python 行为参考保留。正式 package 只包含原生实现；迁移评估期间，此参考 package 用于确定性的跨语言检查。

## 公共边界

- Plugin ID：cyrene.connectors.onebot-v11
- 版本：0.4.0
- capability：message.connector.v1@1
- protocol：cyrene.plugin.runtime.v1.DirectPluginRuntime
- package RID：linux-x64、linux-arm64

规范 protobuf 和 type URL 仍由共享 contracts 持有。此 connector 不负责 Product session、reply、persona、command 或 persistence 策略。
