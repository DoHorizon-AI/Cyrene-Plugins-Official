# Hugging Face provider source

This directory implements the fixed Hub provider runtime.

| File | Responsibility |
| --- | --- |
| `plugin.py` | Tool catalog, signed call validation, profile scope checks, and result mapping. |
| `settings.py` | Host-resolved Account Center connection, service keys, and repo allowlists. |
| `trust.py` | Exchange actor context verification and Account Center lease signing. |
| `account_center.py` | Private DirectPluginRuntime client for one-use profile credential leases. |
| `huggingface_api.py` | Fixed Hub API transport, metadata projections, and bounded file transfers. |
| `vendor_cli.py` | Reject credential, endpoint, proxy, and local-file CLI operations. |
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
| `vendor_cli.py` | 拒绝凭据、定制端点、代理和本地文件 CLI 操作。 |
| `_generated/` | 规范的 `tool.provider.v1` protobuf 绑定。 |

修改租约边界前先读 `settings.py`、`trust.py` 和 `account_center.py`；修改上游路径前先读
`huggingface_api.py`。

`hf_vendor_cli` uses the host-pinned `hf` executable and the linked fine-grained
Hub token. It accepts argv only, runs with a fresh home, and suppresses all
stdout/stderr. Prefer the API tools for query results; the CLI returns a bounded
command receipt. Before launch, it matches the Account Center profile-scope
digest and resolves every repository target and read/write permission against
the host allowlist. Unscoped search, Jobs, inference, bucket and administrative
commands are rejected, along with credential/configuration commands, custom
endpoints, proxies, and local file operations. Mutations remain
`REMOTE_STATE_UNVERIFIED` until a separate read-back exists.

`hf_vendor_cli` 使用宿主固定版本的 `hf` 和绑定的细粒度 Hub 令牌。它只接收 argv，在全新 HOME 中运行，并抑制全部 stdout/stderr。查询结果优先使用 API 工具；CLI 只返回有界命令回执。启动前会核对 Account Center profile scope digest，并按宿主 allowlist 校验每个仓库目标和读写权限。未指定仓库的搜索、Jobs、推理、bucket 和管理命令都会拒绝；凭据/配置命令、定制端点、代理和本地文件操作也会拒绝。写操作在单独回读前始终为 `REMOTE_STATE_UNVERIFIED`。
