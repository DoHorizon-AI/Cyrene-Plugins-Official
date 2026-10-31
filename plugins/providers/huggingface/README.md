# Hugging Face Hub Provider

This package implements profile-scoped Hugging Face Hub metadata and small
artifact operations through `tool.provider.v1`. Every call is bound to the
Exchange actor, provider binding, opaque profile reference, exact operation,
resource target, and canonical argument digest. The provider redeems the
one-use Account Center lease itself; Exchange never receives the HF token.

The fine-grained token is linked and rotated through an Account Center host
secret reference. The reference is present in the signed link request, while
the token value stays in the host secret store and Account Center ciphertext.
Private and gated authorization failures remain structured errors. File
transfers are limited to 1 MiB, and an upload is reported confirmed only after
the Hub tree readback contains the exact target path. An uncertain upload is
never retried automatically.

The `CYRENE_CAPABILITY_CONFIGURATION_JSON` activation root accepts an optional
`read_only` boolean (omitted means `false`). When enabled, every configured repository grant must be read-only;
the provider hides upload and native CLI tools from its catalog and rejects
direct signed calls before redeeming an Account Center lease. This binding-wide
server gate complements the per-repository permission checks.

| Tool | Operation | Scope |
| --- | --- | --- |
| `huggingface.models.list` | Search model metadata | Public results, bounded to 100 |
| `huggingface.datasets.list` | Search dataset metadata | Public results, bounded to 100 |
| `huggingface.spaces.list` | Search Space metadata | Public results, bounded to 100 |
| `huggingface.repo.get` | Read one repository's metadata | Configured repo ID/type and optional file count |
| `huggingface.file.download` | Download one file | Configured read scope, 1 MiB maximum |
| `huggingface.file.upload` | Upload one file | Configured write scope, 1 MiB maximum |

`huggingface.repo.get` returns `file_count` only when Hub metadata includes a
`siblings` array. Missing or malformed file inventory is reported as `null`,
not `0`; repository file names are not returned.

Read `settings.py` for activation and repository scope validation,
`account_center.py` and `trust.py` for identity/lease boundaries, and
`huggingface_api.py` and `plugin.py` for fixed HTTP and tool dispatch.

Run focused tests from this directory:

```bash
uv run --project . --extra test pytest -q tests
```

## Hugging Face Hub 提供方

此 package 通过 `tool.provider.v1` 提供按 profile 授权的 Hugging Face Hub
metadata 和小型文件操作。每次调用都绑定 Exchange actor、provider binding、opaque
profile reference、精确操作、资源目标和规范化参数摘要。provider 自行兑换
Account Center 一次性租约；Exchange 不会收到 HF token。

细粒度 token 通过 Account Center 宿主密钥引用完成绑定和轮换。签名请求中只出现密钥
引用；token 值保留在 host secret store，并由 Account Center 加密保存。私有或 gated
资源的授权失败以结构化错误返回。文件传输限制为 1 MiB；上传只有在 Hub tree 回读确认
目标路径存在后才报告成功。结果不确定的上传不会自动重试。

`huggingface.repo.get` 仅在 Hub metadata 包含 `siblings` 数组时返回 `file_count`。
文件清单缺失或格式无效时返回 `null`，不会误报为 `0`，也不会返回文件名列表。

`CYRENE_CAPABILITY_CONFIGURATION_JSON` activation 根对象可配置 `read_only` 布尔值（省略时为 `false`）。启用后
所有配置仓库权限都必须只读；provider
会从目录隐藏上传与原生 CLI 工具，并在兑换 Account Center 租约前拒绝直接签名调用。该 binding 级服务端门禁会与
每仓库权限校验同时生效。

建议阅读顺序：先看 `settings.py` 的配置与 repo scope 校验，再看 `account_center.py`
和 `trust.py` 的身份与租约边界，最后看 `huggingface_api.py` 和 `plugin.py` 的固定
HTTP 传输及工具分发。
