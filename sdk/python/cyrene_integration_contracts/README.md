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

本 SDK 提供集成支持 v1 的类型化 DTO、严格 JSON 编解码、JSON Schema 校验和 HMAC
签名工具。它不读取凭据、不访问供应商、不解析授权所有权，也不持久化业务状态。签名密钥
必须由可信 binding 配置注入，且不会被序列化。

公开 schema 的唯一源文件位于
`contracts/json/integration-support/v1/schema.json`；SDK 为安装后的校验内置相同文件，
tool-provider TCK 会验证两个副本逐字节一致。
