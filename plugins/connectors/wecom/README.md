# Official WeCom Application Connector / 企业微信应用连接器

`message.connector.v1` implementation for WeCom (企业微信) **application
messages**. v1 ships the outbound path only and declares only `send_message` in
its manifest; a Product binding that requires other methods fails closed.

`message.connector.v1` 的企业微信**应用消息**实现。v1 只交付出站路径，manifest 只声明
`send_message`；需要其他方法的 Product 绑定会 fail closed。

| Method | Status |
| --- | --- |
| `send_message` | implemented (text / markdown / image / file, `touser` / `toparty`, mentions) |
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
- Image and file parts accept either a binding-owned `vendor_media` reference or
  an absolute HTTP(S) `remote_uri`. Remote bytes are downloaded with the binding
  timeout, bounded to WeCom's 2 MiB image / 20 MiB file limits, uploaded through
  `/cgi-bin/media/upload`, and then sent with the returned temporary `media_id`.
- WeCom application messages allow one media part per outbound message. Media
  references from another vendor or agent binding, mixed text/media payloads,
  undersized files, oversized files, and non-JPEG/PNG images fail closed.

```bash
python3 -m pytest plugins/connectors/wecom/tests
python3 plugins/connectors/wecom/tools/generate_message_connector_bindings.py
```
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# 企业微信应用连接器

这是 WeCom（企业微信）应用消息的 message.connector.v1 实现。v1 只发布出站路径，并且 manifest 只声明 send_message；要求其他 method 的 Product binding 会失败关闭。

| Method | 状态 |
| --- | --- |
| send_message | 已实现（text / markdown / image / file、touser / toparty、mentions） |
| inbound_message | v1 不支持：WeCom callback 需要由 owner 持有的 HTTP callback surface、URL 验证和 AES 解密；本实现不臆造该入口，此需求记录为后续工作。 |
| inbound_request / respond_request | v1 不支持：WeCom 审批流程需要单独的映射，以及厂商 request identity 处理。 |

## 配置

配置由 CYRENE_CAPABILITY_BINDING_ID 和 CYRENE_CAPABILITY_CONFIGURATION_JSON 提供：

```json
{
  "corp_id": "ww...",
  "corp_secret": "<secret>",
  "agent_id": 1000002,
  "base_url": "https://qyapi.weixin.qq.com"
}
```

binding_id 来自 activation environment；base_url 和 timeout 为可选项。conversation.vendor 必须是 wecom.app；kind: private 时目标字段为 touser，kind: group 时目标字段为 toparty（department ID）。其他 kind 会被作为拒绝的 delivery result 返回，不会猜测目标。

## 交付语义

- Access token 缓存在内存中，并预留 expiry skew；WeCom 返回 40001、40014 或 42001 时只刷新一次。
- 厂商 errcode 按确定性规则映射：0 -> accepted，45009/45047 -> rate_limited，其他错误 -> rejected，并在 reason 中附上 errcode/errmsg。
- 传输失败会抛出 UNAVAILABLE，而不会虚报交付结果。reply reference 会作为厂商事实 reply_reference_ignored 返回，因为 WeCom 应用消息不支持 thread。
- image 和 file part 可以使用由 binding 持有的 vendor_media reference，或绝对 HTTP(S) remote_uri。远程字节会在 binding timeout 内下载，大小限制为 WeCom image 2 MiB、file 20 MiB；随后上传至 /cgi-bin/media/upload，再使用返回的临时 media_id 发送。
- 每条出站应用消息最多包含一个 media part。来自其他厂商或 agent binding 的 media reference、混合 text/media payload、过小或过大的文件，以及非 JPEG/PNG image 都会失败关闭。

```bash
python3 -m pytest plugins/connectors/wecom/tests
python3 plugins/connectors/wecom/tools/generate_message_connector_bindings.py
```
