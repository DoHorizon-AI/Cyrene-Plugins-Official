# OneBot v11 Native AOT operations / OneBot v11 Native AOT 运维

The formal executable is `bin/cyrene-onebot-v11`, using
`cyrene.plugin.runtime.v1.DirectPluginRuntime`. It declares only
`message.connector.v1` and starts in a fail-closed state until one generic
OneBot binding is supplied by the host.

正式入口是 `bin/cyrene-onebot-v11`，使用 `DirectPluginRuntime`，只声明
`message.connector.v1`。Host 未提供通用 OneBot binding 时，进程保持 fail-closed。

Supported profiles are `http_api`, `forward_websocket`, and
`reverse_websocket`. Binding identity comes from host configuration; process
IDs, ports, and runtime generations are lifecycle observations and must not be
used as capability identity. The process does not start a QQ Host child.

支持 `http_api`、`forward_websocket` 和 `reverse_websocket`。binding 身份来自
Host 配置；进程 ID、端口和 runtime generation 只是生命周期观察，不能替代能力身份。
本进程不会启动 QQ Host 子进程。

Use the generic OneBot Native AOT tests and the source-hygiene/package gates as
implementation evidence. Real endpoint smoke remains deployment-owner evidence
and is not inferred from offline transport tests.
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# OneBot v11 Native AOT 运维

正式可执行文件为 bin/cyrene-onebot-v11，使用 cyrene.plugin.runtime.v1.DirectPluginRuntime。它只声明 message.connector.v1；在 host 提供一个通用 OneBot binding 之前，进程保持失败关闭。

支持的 profile 为 http_api、forward_websocket 和 reverse_websocket。binding 身份来自 host 配置；进程 ID、端口和 runtime generation 属于生命周期观测，不能用作 capability identity。本进程不会启动 QQ Host 子进程。

使用通用 OneBot Native AOT 测试和 source-hygiene/package 门禁作为实现证据。真实 endpoint smoke 属于部署 owner 的证据，不能从离线传输测试推断。
