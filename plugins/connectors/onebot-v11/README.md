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
