# Official WeCom Application Connector / 企业微信应用连接器

`message.connector.v1` implementation for WeCom (企业微信) **application
messages**. v1 ships the outbound path only and declares only `send_message` in
its manifest; a Product binding that requires other methods fails closed.

`message.connector.v1` 的企业微信**应用消息**实现。v1 只交付出站路径，manifest 只声明
`send_message`；需要其他方法的 Product 绑定会 fail closed。

| Method | Status |
| --- | --- |
| `send_message` | implemented (text / markdown, `touser` / `toparty`, mentions) |
| `inbound_message` | **not in v1**: WeCom callbacks require an owned HTTP callback surface plus URL verification and AES decryption; that surface is not invented here and is tracked as a follow-up |
| `inbound_request` / `respond_request` | not in v1: WeCom approval flows need their own mapping and vendor-request identity work |

## Configuration / 配置

`CYRENE_CAPABILITY_BINDING_ID` plus `CYRENE_CAPABILITY_CONFIGURATION_JSON`:

```json
{
  "corp_id": "ww...",
  "corp_secret": "<secret>",
  "agent_id": 1000002,
  "base_url": "https://qyapi.weixin.qq.com"
}
```

`binding_id` comes from the activation environment; `base_url` and timeouts are
optional. `conversation.vendor` must be `wecom.app`; `kind: private` targets
`touser`, `kind: group` targets `toparty` (department ids), and other kinds are
returned as rejected delivery results instead of guessing a target.

## Delivery semantics / 交付语义

- Access tokens are cached in memory with an expiry skew, and refreshed once
  when WeCom answers `40001`/`40014`/`42001`.
- Vendor `errcode` maps deterministically: `0` → accepted, `45009`/`45047` →
  rate_limited, anything else → rejected with `errcode`/`errmsg` in the reason.
- Transport failures raise `UNAVAILABLE` instead of claiming a delivery
  outcome; a `reply` reference is reported back as the vendor fact
  `reply_reference_ignored` because WeCom application messages do not thread.
- Media parts (`image`/`file`) fail closed with `INVALID_REQUEST` in v1: media
  upload is a follow-up.

```bash
python3 -m pytest plugins/connectors/wecom/tests
python3 plugins/connectors/wecom/tools/generate_message_connector_bindings.py
```
