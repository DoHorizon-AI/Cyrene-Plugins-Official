# Integration support Python contracts

`cyrene_integration_contracts` provides typed DTOs, strict JSON parsers and
serializers, JSON Schema validation, and HMAC helpers for the Plugins-owned
integration support v1 contract. It does not load credentials, contact vendors,
resolve authorization ownership, or persist business state. Signing keys must
be supplied from trusted binding configuration and are never serialized.

The canonical public schema is
`contracts/json/integration-support/v1/schema.json`; the package bundles the
same file for installed-schema validation. The tool-provider TCK checks the two
copies remain byte-identical.

Write tools use `write_replay_identity(context, payload)` after trusted-context
verification. Renewal start is scoped by the signed request ID; email send is
scoped by its stable request reference. The helper returns the scoped replay key
and payload hash separately so providers can replay a durable result and reject
changed-body reuse. Providers persist intent before side effects. A failure known
to precede every side effect may be marked retryable; ambiguous post-call
failures remain `UNKNOWN` for manual reconciliation and are never automatically
resent. Email adapters also reserve the scoped signed request ID so it cannot be
reused with a different request reference.

Cloud adapters define positive allowlists of safe scalar metadata for each
provider. They omit raw provider response and error objects, credentials, secrets,
private endpoints, and tenant identifiers from `CloudObservation.attributes`.

本 SDK 提供集成支持 v1 的类型化 DTO、严格 JSON 编解码、JSON Schema 校验和 HMAC
签名工具。它不读取凭据、不访问供应商、不解析授权所有权，也不持久化业务状态。签名密钥
必须由可信 binding 配置注入，且不会被序列化。

公开 schema 的唯一源文件位于
`contracts/json/integration-support/v1/schema.json`；SDK 为安装后的校验内置相同文件，
tool-provider TCK 会验证两个副本逐字节一致。

写操作必须先验证可信上下文，再调用 `write_replay_identity(context, payload)`。renewal start
使用签名 request ID；邮件发送使用稳定的 request reference。helper 分别返回作用域内的 replay key
和 payload hash，供应商由此返回精确重放结果并拒绝相同 key 搭配不同正文。副作用发生前必须先
持久化 intent；调用后结果不明确时保留为 `UNKNOWN` 并人工对账，禁止自动重发。
能够确认副作用尚未发生的失败可以标记为可重试；副作用可能已经发生时，必须人工对账并禁止自动重发。
邮件适配器还应保留作用域内的签名 request ID，防止同一 request ID 改绑到另一个 request reference。

云适配器为各供应商定义安全标量元数据的正向 allowlist。不得将原始供应商响应或错误对象、凭据、
密钥、私有 endpoint 或租户标识写入 `CloudObservation.attributes`。
