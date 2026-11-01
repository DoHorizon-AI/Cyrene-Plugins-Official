# Knowledge graph provider package

This package adapts the canonical `knowledge.graph.v1` protobuf SDK to a
tenant-scoped SQLite graph store. Authorization decisions remain with the host.
| 此包把规范图谱 SDK 接到租户隔离的 SQLite 图存储；授权仍由宿主决策。

| File | Responsibility |
| --- | --- |
| `plugin.py` | Validates and dispatches direct graph invocations. |
| `settings.py` | Loads the closed activation configuration. |
| `sqlite_store.py` | Persists and queries graph nodes and edges. |

Suggested order: `settings.py` → `sqlite_store.py` → `plugin.py`.
