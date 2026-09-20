# OneBot v11 Native AOT migration record / OneBot v11 Native AOT 迁移记录

The OneBot formal runtime is now the C#/.NET 10 Native AOT implementation at
version `0.4.0`. The migration keeps the canonical message contract,
`DirectPluginRuntime`, method names, and type URLs unchanged.

本插件正式运行时已切换到 C#/.NET 10 Native AOT，版本为 `0.4.0`。迁移保持
canonical message contract、`DirectPluginRuntime`、方法名与 type URL 不变。

The QQ/IM implementation was split into the independent
`cyrene.connectors.im@0.1.0` package. That package owns `qq.client.v1`, the
`cyrene.qq.host.v1` boundary, QQ schemas/docs, fake-Host TCK, and the Python
reference suite. OneBot no longer routes or declares QQ operations.

QQ/IM 实现已拆分到独立的 `cyrene.connectors.im@0.1.0` 包。该包负责
`qq.client.v1`、`cyrene.qq.host.v1` 边界、QQ schema/文档、fake-Host TCK 和
Python reference 测试套件；OneBot 不再路由或声明 QQ 操作。

## Current evidence / 当前证据

- generic OneBot C# tests and Native AOT host build pass locally;
- IM C# QQ Host/parity tests, Python reference tests, package tests, and the
  IM behavior matrix are maintained under `plugins/connectors/im/`;
- the real QQ smoke remains `NOT_RUN` until the exact authorized QQ build, Host
  ABI, account, and protected environment are provided;
- Python cleanup is intentionally not authorized by this record alone: it
  requires final cross-language parity, packaged IM TCK, manifest/catalog/CI
  acceptance, and an explicit cleanup decision.

真实 QQ smoke 在精确授权 QQ build、Host ABI、账号和受保护环境到位前保持
`NOT_RUN`。仅凭本记录不会清理 Python；仍需完成跨语言等价、打包 IM TCK、manifest/catalog/CI
验收，并得到明确清理决定。
