# Media Runtime Modules / 媒体运行模块

| File | Responsibility |
| --- | --- |
| library.py | SQLite catalog, content-addressed storage, integrity, and PNG rendering. |
| artifact_resolver.py | Fixed actor-scoped Work attachment HTTP peer. |
| settings.py | Binding, caller, state, and secret configuration validation. |
| tool_catalog.py | Fixed signed media operation catalog. |
| plugin.py | DirectPluginRuntime adapter and caller-context verification. |

Suggested reading order: tool_catalog.py → settings.py → artifact_resolver.py →
library.py → plugin.py. 推荐阅读顺序：工具目录、配置、附件访问、媒体库、运行时适配器。
