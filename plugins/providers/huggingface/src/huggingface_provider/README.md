# Hugging Face provider source

This directory implements the fixed Hub provider runtime.

| File | Responsibility |
| --- | --- |
| `plugin.py` | Tool catalog, signed call validation, profile scope checks, and result mapping. |
| `settings.py` | Host-resolved Account Center connection, service keys, and repo allowlists. |
| `trust.py` | Exchange actor context verification and Account Center lease signing. |
| `account_center.py` | Private DirectPluginRuntime client for one-use profile credential leases. |
| `huggingface_api.py` | Fixed Hub API transport, metadata projections, and bounded file transfers. |
| `_generated/` | Canonical `tool.provider.v1` protobuf binding. |

Read `settings.py`, then `trust.py` and `account_center.py`, before changing
lease boundaries. Read `huggingface_api.py` before changing upstream paths.

## Hugging Face provider 源码

此目录实现固定 Hub provider runtime。

| 文件 | 职责 |
| --- | --- |
| `plugin.py` | 工具目录、签名校验、profile scope 校验和结果映射。 |
| `settings.py` | 宿主解析的 Account Center 连接、服务密钥与 repo allowlist。 |
| `trust.py` | Exchange actor 上下文验证及 Account Center 租约签名。 |
| `account_center.py` | 兑换一次性 profile 凭据租约的私有 DirectPluginRuntime 客户端。 |
| `huggingface_api.py` | 固定 Hub API 传输、metadata 投影和限额文件传输。 |
| `_generated/` | 规范的 `tool.provider.v1` protobuf 绑定。 |

修改租约边界前先读 `settings.py`、`trust.py` 和 `account_center.py`；修改上游路径前先读
`huggingface_api.py`。
