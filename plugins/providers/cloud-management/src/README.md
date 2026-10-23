# Source package layout

| Directory | Responsibility |
| --- | --- |
| `cloud_management_provider/` | Runtime binding, signed tool catalog, provider APIs, operation refs, and bounded CLI execution. |

Read `settings.py` and `tool_catalog.py` first, then `plugin.py`; provider requests and subprocess behavior live in `providers.py` and `process.py`.

<!-- 中文翻译 -->

# 源码目录

| 目录 | 职责 |
| --- | --- |
| `cloud_management_provider/` | 运行时绑定、签名工具目录、供应商 API、操作引用和受限 CLI 执行。 |

建议先阅读 `settings.py` 和 `tool_catalog.py`，再读 `plugin.py`；供应商请求和子进程行为位于 `providers.py` 与 `process.py`。
