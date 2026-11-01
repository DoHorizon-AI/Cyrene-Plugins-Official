# Tool Provider SDK Source

| File | Responsibility |
|---|---|
| `contracts.py` | Stable capability, method, and payload type URL identifiers. |
| `_generated/tool_provider_pb2.py` | Protobuf messages generated from the canonical tool schema. |
| `py.typed` | Marks the package as typed for downstream checkers. |

Import the generated module from the package root as `tool_provider_pb2`.

| 文件 | 职责 |
|---|---|
| `contracts.py` | 稳定的 capability、方法及 payload type URL 标识符。 |
| `_generated/tool_provider_pb2.py` | 从规范 tool schema 生成的 protobuf 消息。 |
| `py.typed` | 标记 package 提供类型信息。 |

从 package 根目录导入 `tool_provider_pb2` 即可使用生成消息。
