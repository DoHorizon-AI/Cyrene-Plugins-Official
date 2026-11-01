# Memory Provider SDK Source

| File | Responsibility |
|---|---|
| `memory.py` | Immutable request/response values and the seven method codecs. |
| `_wire.py` | Dependency-free protobuf wire helpers used by the SDK. |
| `py.typed` | Marks the package as typed for downstream checkers. |

Read `memory.py` first; codec helpers follow the canonical field numbers in
`contracts/proto/cyrene/memory/provider/v1/memory_provider.proto`.

| 文件 | 职责 |
|---|---|
| `memory.py` | 不可变请求/响应值和七个方法的编解码器。 |
| `_wire.py` | SDK 使用的零依赖 protobuf wire 辅助函数。 |
| `py.typed` | 标记 package 提供类型信息。 |

建议先读 `memory.py`；字段编号以规范 proto 为准。
