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
  --listen 127.0.0.1:51051
```

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
