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
