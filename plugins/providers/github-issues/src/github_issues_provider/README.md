# Provider Package

This directory implements the GitHub Issues and repository status `tool.provider.v1` binding. Read `settings.py` first for repository allowlists, `trust.py` for signed actor context, `account_center.py` for legacy and profile lease ports, then `github_api.py` and `plugin.py` for transport and dispatch.

The fixed catalog includes repo-scoped issue search with state, labels, update-window bounds, and bounded pagination; Actions job summaries and best-effort log excerpts with common credential patterns redacted; exact branch/file/PR readbacks; task-branch creation/deletion; compare-and-update file commits; draft-PR creation; and exact-attempt failed-CI reruns. Every new operation uses a one-use GitHub App profile lease. Locator targets match the corresponding write and readback operations, while the canonical argument digest binds query text, content, and preconditions.

| File | Responsibility |
| --- | --- |
| `__init__.py` | Public provider class export. |
| `account_center.py` | Typed Account Center legacy and generic credential lease ports. |
| `github_api.py` | Fixed-host repository, issue, branch, file, PR, Actions, and CI REST requests with bounded readback. |
| `plugin.py` | Closed tool schemas, profile lease calls, fixed dispatch, legacy issue calls, and webhook normalization. |
| `settings.py` | Binding and allowlist validation. |
| `trust.py` | Trusted caller signature verification and re-signing. |
| `vendor_cli.py` | Reject credential, custom-host, local-file, and out-of-binding CLI arguments. |
| `_generated/` | Canonical tool provider protobuf binding. |

## GitHub provider 源码

此目录实现 GitHub Issue 与仓库状态的 `tool.provider.v1` binding。先读 `settings.py`
了解仓库 allowlist，再读 `trust.py` 了解签名 actor 上下文、`account_center.py` 了解旧版与
profile 租约端口，最后看 `vendor_cli.py`、`github_api.py` 和 `plugin.py` 了解 CLI 策略、API 传输与分发。

固定工具目录包括按仓库隔离的 Issue 搜索（state、labels、更新时间水位与有界分页）、
Actions job 摘要与脱敏日志摘录、分支/文件/PR 精确回读、任务分支创建/删除、按 blob SHA
比对提交文件、创建 draft PR，以及按 attempt 重跑失败 CI。所有新增操作都使用一次性
GitHub App profile 租约。写入和对应回读共享 locator target；完整规范化参数摘要绑定查询文本、
文件内容和 SHA 前置条件。

| 文件 | 职责 |
| --- | --- |
| `account_center.py` | Account Center 旧版和通用凭据租约端口。 |
| `github_api.py` | 固定主机的仓库、Issue、分支、文件、PR、Actions 与 CI REST 请求及有界回读。 |
| `plugin.py` | 封闭的工具 schema、profile 租约调用、固定分发、旧版 Issue 调用和 Webhook 规范化。 |
| `vendor_cli.py` | 拒绝凭据、定制主机、本地文件和越权仓库参数。 |

`github_vendor_cli` uses the host-pinned `gh` executable and the Account Center
installation token. It accepts an argv array, never a shell string. The policy
blocks credential/configuration commands, host overrides, local file input or
output, and repository flags outside this binding. Prefer typed API tools for
query results; the CLI tool returns a command receipt only and suppresses stdout
and stderr. A successful mutating command is reported as
`REMOTE_STATE_UNVERIFIED` until a separate read-back exists.

`github_vendor_cli` 使用宿主固定版本的 `gh` 和 Account Center 安装令牌。工具只接收 argv 数组，不接收 shell 命令字符串；会拒绝凭据/配置命令、主机覆盖、本地文件输入输出，以及超出当前绑定的仓库参数。查询结果优先使用结构化 API 工具；CLI 仅返回命令回执并抑制 stdout/stderr。写命令即使退出码为零，也会报告 `REMOTE_STATE_UNVERIFIED`，直至单独回读确认。
