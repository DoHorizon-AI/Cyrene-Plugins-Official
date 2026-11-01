# Direct Plugin Runtime / 插件直连运行时

This Plugins-owned package exposes one configured Plugin instance through one
gRPC endpoint. A Product invokes that endpoint directly. Platform may supervise
the process and publish an opaque connection reference, but no Platform process
receives or forwards the capability payload.

本包由 Plugins 仓库维护，一个进程只暴露一个已配置插件实例。Product 直接调用该
gRPC endpoint。Platform 可以监管进程并发布不透明连接引用，但任何 Platform 进程
都不接收或转发 capability payload。

```bash
cyrene-plugin-runtime \
  --entrypoint model_api_connector:ModelApiConnector \
  --capability model.provider.v1 \
  --interface-version 1 \
  --interface-version 2 \
  --listen plugin-runtime.internal:7443 \
  --tls-cert-file /run/secrets/plugin-server-cert.pem \
  --tls-key-file /run/secrets/plugin-server-key.pem \
  --tls-client-ca-file /run/secrets/plugin-client-ca.pem
```

Remote DirectPluginRuntime endpoints use mutual TLS and publish a `grpcs://`
connection reference. The listen hostname is included in that reference and
must resolve to the endpoint from the Product or Exchange network. The server
certificate must include the DNS name used by the Product or Exchange target.
The client CA file controls which client
certificates are accepted. Mount all three PEM files read-only from the host
secret provider; pass file paths only, never PEM contents. The three TLS flags
are all-or-none, and TLS mode always requires a client certificate. Without
TLS flags, the compatibility mode is restricted to loopback or Unix sockets.
Local tests can continue to use the default `127.0.0.1:0` listener.

远程 DirectPluginRuntime endpoint 必须使用双向 TLS，并发布 `grpcs://` connection reference。listen hostname 会写入
connection reference，并且必须能从 Product 或 Exchange 网络解析到该 endpoint。服务端证书必须包含 Product 或
Exchange 实际连接使用的 DNS 名称；客户端 CA 文件决定哪些客户端证书可通过验证。三个 PEM
文件都应从主机 secret provider 只读挂载；命令行只传文件路径，不能传 PEM 内容。三个 TLS flag 必须全部提供或全部省略，
TLS 模式始终要求客户端证书。省略 TLS 参数时，兼容模式仅限 loopback 或 Unix socket。本地测试仍可使用默认的
`127.0.0.1:0` listener。

Products use the SDK facade after receiving a local connection reference from
their composition layer:

```python
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient

with DirectPluginClient.for_local_connection_ref(connection_ref) as plugin:
    response = plugin.invoke(
        capability="model.provider.v1",
        interface_version="1",
        method="chat_completion",
        request=DirectPayload(type_url=request_type, value=request_bytes),
        deadline_seconds=30,
    )
```

Product 在组合层拿到本地连接引用后，通过 SDK facade 直连插件。调用中不包含
Platform binding id，业务负载也不会发送到 Platform 进程。

Use `plugin.subscribe(capability=..., interface_version="1", request=filter_payload,
cancel_event=stop_event)` for provider events. It selects subscription mode and
preserves each payload's `event_type`. Close the iterator when stopping early;
the native RPC is cancelled and the provider receives unsubscribe. Reconnection
and authorization remain the host's responsibility.

订阅使用 `plugin.subscribe`，保留事件类型。提前停止时须关闭迭代器，以取消原生 RPC
并释放 provider 订阅；重连和授权仍由宿主负责。

Plugin configuration remains environment or secret-provider input to the
plugin process. The runtime never accepts a secret on its command line and does
not log configuration values.

Installations use the Plugins-owned `cyrene-plugin-python-preparer` adapter.
Platform invokes it through `cyrene.package-dependency-preparer.v1`; the adapter
verifies `requirements.lock`, creates the isolated `uv` environment, and returns
only its relative runtime executable and immutable evidence. Python tooling can
therefore change without adding language-specific code to Platform.

安装阶段使用 Plugins 自有的 `cyrene-plugin-python-preparer` 适配器。Platform 仅通过
统一协议传入包目录、运行时目录和锁摘要；适配器负责校验 `requirements.lock`、创建隔离
的 `uv` 环境并返回不可变证据，因此 Python 工具链变化无需修改 Platform。

Transport failures use the stable `DirectInvocationError.Code` enum. Plugin
domain failures also retain their machine-readable symbolic value in
`domain_code`, so Products can preserve distinctions such as `INVALID_INPUT`
and `UNSUPPORTED_INPUT` without parsing an error message.

插件配置继续通过环境或 secret provider 注入插件进程。运行时不通过命令行接收
secret，也不记录配置值。

传输失败使用稳定的 `DirectInvocationError.Code` 枚举；插件领域失败还会在
`domain_code` 中保留机器可读的符号值，因此 Product 无需解析错误文本，也能区分
`INVALID_INPUT`、`UNSUPPORTED_INPUT` 等错误。
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# Direct Plugin Runtime

这是由 Plugins 持有的 package，通过一个 gRPC endpoint 暴露一个已配置的 Plugin instance。Product 会直接调用该 endpoint。Platform 可以监管进程并发布不透明 connection reference，但任何 Platform process 都不会接收或转发 capability payload。

```bash
cyrene-plugin-runtime \
  --entrypoint model_api_connector:ModelApiConnector \
  --capability model.provider.v1 \
  --interface-version 1 \
  --interface-version 2 \
  --listen 127.0.0.1:51051
```

Product 的组合层收到本地 connection reference 后，可通过 SDK facade 使用 Plugin：

```python
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient

with DirectPluginClient.for_local_connection_ref(connection_ref) as plugin:
    response = plugin.invoke(
        capability="model.provider.v1",
        interface_version="1",
        method="chat_completion",
        request=DirectPayload(type_url=request_type, value=request_bytes),
        deadline_seconds=30,
    )
```

Product 在组合层取得本地 connection reference 后，通过 SDK facade 直接调用 Plugin。调用不会携带 Platform binding ID，业务 payload 也不会发送到 Platform process。

Plugin configuration 继续通过 environment 或 secret provider 注入 Plugin process。runtime 不会通过 command line 接收 secret，也不会记录配置值。

安装使用 Plugins 所有的 cyrene-plugin-python-preparer adapter。Platform 通过 cyrene.package-dependency-preparer.v1 调用它；adapter 验证 requirements.lock，创建隔离的 uv environment，并只返回相对 runtime executable 路径和不可变证据。因此 Python tooling 可以变化，而无需向 Platform 增加语言专用代码。

传输失败使用稳定的 DirectInvocationError.Code enum。Plugin domain failure 也会在 domain_code 中保留机器可读符号值，因此 Product 不必解析错误消息，就能区分 INVALID_INPUT 和 UNSUPPORTED_INPUT 等结果。

## Runtime package 模块

| 文件 | 职责 |
| --- | --- |
| server.py | 使用直连 gRPC 协议托管一个已配置 Plugin。 |
| client.py | 打开不透明的本地 connection reference 并调用 Plugin contract。 |
| bootstrap.py | 从已解包的源码 package 启动随包提供的 server。 |
| dependency_preparer.py | 通过 Platform 通用 adapter 协议构建由 lock 标识的 Python runtime。 |

先阅读生成的 wire contract，然后阅读 server.py 和 client.py。package 生命周期集成应一并阅读 dependency_preparer.py 与 bootstrap.py。
