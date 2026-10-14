# Package source

| File | Responsibility |
| --- | --- |
| `contracts.py` | Public DTOs, strict codecs, schema validation, and signed caller contexts |
| `schema.json` | Packaged copy of the canonical integration support v1 JSON Schema |
| `__init__.py` | Supported package exports |
| `py.typed` | Marks the package as typed |

Read the schema and DTOs before implementing a transport adapter. Verify the
signed context against the raw inner `arguments` object before provider work.

该目录存放公开 Python SDK。实现传输适配器前先阅读 schema 和 DTO；调用供应商前，应使用原始
内层 `arguments` 对象验证签名上下文。
