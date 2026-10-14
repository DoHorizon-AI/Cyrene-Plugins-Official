# `cloud_resources_provider`

The package validates host activation configuration, verifies signed Exchange
tool calls, resolves one opaque target through fixed provider adapters, and
serializes shared integration contract DTOs.

| File | Responsibility |
| --- | --- |
| `settings.py` | Parse the binding's provider, target, caller, and secret-reference allowlists. |
| `cloud_api.py` | Acquire provider credentials and issue bounded HTTPS GET requests. |
| `plugin.py` | Verify caller context and expose the two fixed tool-provider methods. |

<!-- 中文翻译 -->

## `cloud_resources_provider`

此包验证主机激活配置、校验带签名的 Exchange 工具调用、通过固定供应商适配器解析一个不透明目标，
并序列化共享集成契约 DTO。

| 文件 | 职责 |
| --- | --- |
| `settings.py` | 解析绑定中的供应商、目标、调用方和密钥引用白名单。 |
| `cloud_api.py` | 获取供应商凭据并发送有界 HTTPS GET 请求。 |
| `plugin.py` | 校验调用方上下文并暴露两个固定的工具提供方方法。 |
