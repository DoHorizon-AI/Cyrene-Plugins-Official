# Knowledge Graph SDK Source

| File | Responsibility |
|---|---|
| `codec.py` | Method routing, type URLs, and protobuf codecs. |
| `_generated/knowledge_graph_pb2.py` | Generated messages from the canonical graph proto. |
| `py.typed` | Marks the package as typed. |

Use `codec.encode_request`/`decode_response` with the method-specific generated
message classes.

| 文件 | 职责 |
|---|---|
| `codec.py` | 方法路由、type URL 和 protobuf 编解码。 |
| `_generated/knowledge_graph_pb2.py` | 从规范图谱 proto 生成的消息。 |
| `py.typed` | 标记 package 提供类型信息。 |

使用方法专属生成消息与 `codec.encode_request`/`decode_response`。
