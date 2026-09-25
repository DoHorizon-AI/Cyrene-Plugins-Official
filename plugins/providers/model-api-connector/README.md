# Official Model API Connector

`model.provider.v1` implementation that adapts the typed Cyrene chat contract
onto one operator-configured OpenAI-compatible endpoint.

## What it owns

Only the typed transport. The connector decodes a `ChatCompletionRequest`,
calls `POST <base_url>/v1/chat/completions`, and re-encodes the answer as the
contract's chunks:

- `chat_completion` (interface 1) — the text-compatible method. The v1 codec
  cannot carry a role, tool calls or a reported total, so those are never
  claimed on this version.
- `chat_completion_v2` (interface 2) — adds the assistant role preamble,
  indexed streamed tool-call fragments and the provider-reported total.

Routing, credentials, quota, retries and resource lifecycle stay with the
Product that resolved the binding. | 路由、凭据、配额与生命周期不属于插件。

## What it deliberately does not do

- No vendor compatibility layer, no AstrBot/legacy adapter tree, no local model
  loading.
- No invented usage: token counts are only reported when the provider reports
  them.
- No silent renumbering: a streamed tool fragment without an index is an error,
  because the gateway could not merge it correctly.

## Cancellation

The connector owns the upstream socket. `on_cancel` shuts that socket down, so
a client that stops waiting does not leave the provider generating. A cancelled
stream reports no chunks and no usage.

## Configuration

| Variable | Meaning |
| --- | --- |
| `CYRENE_CHAT_BASE_URL` | Upstream base URL; may carry a path prefix. Required. |
| `CYRENE_CHAT_API_KEY` | Bearer credential sent to the upstream, when set. |
| `CYRENE_CHAT_TIMEOUT` | Per-request timeout in seconds (default 30). |
| `CYRENE_EMBEDDINGS_SUPPORTED` | Reserved: embeddings are not implemented, so the method fails closed with `METHOD_NOT_SUPPORTED`. |

## Running it

The runtime materialises `src/cyrene_plugin_runtime/bootstrap.py` beside the
plugin and launches the manifest entrypoint:

```
python -m cyrene_plugin_runtime.server \
  --entrypoint model_api_connector:ModelApiConnector \
  --capability model.provider.v1 --interface-version 1 --interface-version 2 \
  --listen 127.0.0.1:0
```

`tests/` exercises the connector against a local OpenAI-compatible peer,
including streamed tool fragments and cancellation; the cross-repository
acceptance lane lives in `Cyrene-Exchange/tests/test_platform_integration.py`.
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# 官方 Model API Connector

这是 model.provider.v1 的实现，会将类型化 Cyrene chat contract 适配到一个由 operator 配置、兼容 OpenAI API 的 endpoint。

## 所属职责

本 connector 只负责类型化传输。它解码 ChatCompletionRequest，调用 POST <base_url>/v1/chat/completions，并将响应重新编码为契约定义的 chunk：

- chat_completion（interface 1）：兼容文本的方法。v1 codec 无法承载 role、tool call 或上报的总数，因此此版本不会声称提供这些信息。
- chat_completion_v2（interface 2）：增加 assistant role 前导内容、带索引的流式 tool-call 片段，以及 provider 上报的总数。

路由、凭据、配额、重试和资源生命周期仍由解析 binding 的 Product 负责。

## 明确不负责的事项

- 不提供厂商兼容层，不包含 AstrBot/legacy adapter 树，也不加载本地模型。
- 不编造 usage：只有 provider 上报 token count 时才会返回。
- 不会静默重新编号：缺少 index 的流式 tool fragment 会被视为错误，因为 gateway 无法正确合并。

## 取消

connector 拥有上游 socket。on_cancel 会关闭该 socket，因此停止等待的客户端不会让 provider 继续生成。流被取消时不返回 chunk 和 usage。

## 配置

| 变量 | 含义 |
| --- | --- |
| CYRENE_CHAT_BASE_URL | 上游 base URL，可包含路径前缀，必填。 |
| CYRENE_CHAT_API_KEY | 设置后，会作为 Bearer credential 发送给上游。 |
| CYRENE_CHAT_TIMEOUT | 单请求超时时间（秒），默认 30。 |
| CYRENE_EMBEDDINGS_SUPPORTED | 预留配置：尚未实现 embeddings，因此对应 method 会以 METHOD_NOT_SUPPORTED 失败关闭。 |

## 运行

runtime 会在 Plugin 旁物化 src/cyrene_plugin_runtime/bootstrap.py，并启动 manifest 指定的入口：

```bash
python -m cyrene_plugin_runtime.server --entrypoint model_api_connector:ModelApiConnector --capability model.provider.v1 --interface-version 1 --interface-version 2 --listen 127.0.0.1:0
```

tests/ 会使用本地 OpenAI-compatible peer 测试 connector，包括流式 tool fragment 和取消。跨仓验收路径位于 Cyrene-Exchange/tests/test_platform_integration.py。
