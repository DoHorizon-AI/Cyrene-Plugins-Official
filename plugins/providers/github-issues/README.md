# GitHub Issues and Repository Provider

This binding exposes fixed `tool.provider.v1` operations over GitHub Cloud at
`api.github.com`: repository inventory and metadata, branches, pull requests,
Actions runs, commit/CI status, and the existing issue read/write tools. Every
repository target must match both a stable numeric repository ID and the
configured owner/name pair. The provider accepts no arbitrary URL, command, or
caller-supplied identity.

New calls use a GitHub App installation profile and a one-use Account Center
lease bound to the verified actor, profile, operation, exact target, and
canonical argument digest. Account Center signs a repository-scoped installation
token only for the provider caller; the token stays in provider memory. Writes
retain readback or explicit uncertain outcomes, and the provider never retries
an ambiguous write. Legacy GitHub OAuth issue leases remain supported for
existing bindings during migration.

The host supplies the resolved Account Center connection reference and binding
in activation configuration. Local connections must be loopback; remote
connections require explicit `grpcs://` plus operator-provided mTLS material.
`hostSecretRefs` maps stable references to host-injected environment names and
never contains secret values. Exchange verification and provider signing use
different 32-byte HMAC keys.

`normalize_webhook` authenticates the original raw bytes before decoding JSON,
then checks the installation, event, action, and repository allowlists. It
emits normalized event facts and never invokes a model.

Run focused tests from this directory with
`uv run --project . --with pytest --with jsonschema pytest -q tests`.

## GitHub Issues 与仓库状态提供方

此 binding 通过 GitHub Cloud `api.github.com` 暴露固定的 `tool.provider.v1`
操作：仓库清单与 metadata、分支、PR、Actions runs、commit/CI 状态，以及现有
Issue 读写工具。每个仓库目标必须同时匹配稳定数字 repository ID 与配置的
owner/name。provider 不接受任意 URL、命令或调用方自选身份。

新调用使用 GitHub App installation profile 和 Account Center 一次性租约，租约绑定
可信 actor、profile、操作、精确目标和规范化参数摘要。Account Center 只向 provider
caller 签发限定仓库的 installation token，token 只保留在 provider 内存。写操作保留
回读结果或明确的未知状态；结果不确定时不会自动重试。迁移期间仍支持旧 binding 的
GitHub OAuth Issue 租约。

宿主在激活配置中提供已解析的 Account Center 连接引用和 binding。本地连接必须指向
loopback；远程连接必须显式使用 `grpcs://` 和 operator 提供的 mTLS 资料。
`hostSecretRefs` 只将稳定引用映射到宿主注入的环境变量名，不包含密钥值。Exchange
验证和 provider 签名使用彼此独立的 32 字节 HMAC key。

`normalize_webhook` 会先验证原始字节，再解码 JSON，然后检查 installation、事件、操作
和仓库 allowlist。它只输出规范化事件事实，不会调用模型。
