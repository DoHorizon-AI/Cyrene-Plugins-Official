# Provider Package

This directory implements the GitHub Issues and repository status `tool.provider.v1` binding. Read `settings.py` first for repository allowlists, `trust.py` for signed actor context, `account_center.py` for legacy and profile lease ports, then `github_api.py` and `plugin.py` for transport and dispatch.

| File | Responsibility |
| --- | --- |
| `__init__.py` | Public provider class export. |
| `account_center.py` | Typed Account Center legacy and generic credential lease ports. |
| `github_api.py` | Fixed-host repository, issue, PR, Actions, and CI REST requests. |
| `plugin.py` | Tool catalog, profile lease calls, legacy issue calls, and webhook normalization. |
| `settings.py` | Binding and allowlist validation. |
| `trust.py` | Trusted caller signature verification and re-signing. |
| `_generated/` | Canonical tool provider protobuf binding. |

## GitHub provider 源码

此目录实现 GitHub Issue 与仓库状态的 `tool.provider.v1` binding。先读 `settings.py`
了解仓库 allowlist，再读 `trust.py` 了解签名 actor 上下文、`account_center.py` 了解旧版与
profile 租约端口，最后看 `github_api.py` 和 `plugin.py` 了解传输与分发。

| 文件 | 职责 |
| --- | --- |
| `account_center.py` | Account Center 旧版和通用凭据租约端口。 |
| `github_api.py` | 固定主机的仓库、Issue、PR、Actions 与 CI REST 请求。 |
| `plugin.py` | 工具目录、profile 租约调用、旧版 Issue 调用和 Webhook 规范化。 |
