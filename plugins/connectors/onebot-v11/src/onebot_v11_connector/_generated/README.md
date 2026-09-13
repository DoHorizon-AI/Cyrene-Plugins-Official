# Directory Guide: `plugins/connectors/onebot-v11/src/onebot_v11_connector/_generated`

This directory contains generated Python projections of the Plugins-owned
`message.connector.v1` payload contract. The editable schema lives at
`contracts/proto/cyrene/message/connector/v1/message_connector.proto`.

该目录存放 `plugins/connectors/onebot-v11/src/onebot_v11_connector/_generated` 对应模块边界的源码。建议先读直接入口，再按子包和测试继续展开。

## Contents / 内容

| File | Responsibility | 职责 |
| --- | --- | --- |
| `__init__.py` | Package initialization and public exports | Package initialization and public exports |
| `message_connector_pb2.py` | Generated Python payload types | 生成的 Python 载荷类型 |

## Suggested reading order / 推荐阅读顺序

Do not edit generated files. Run
`uv run --frozen python plugins/connectors/onebot-v11/tools/generate_message_connector_bindings.py`
from the repository root and verify the resulting diff.

禁止直接编辑生成文件；从仓库根目录运行上述生成命令并检查差异。
