# `cloud_management_provider`

| File | Responsibility |
| --- | --- |
| `settings.py` | Parse strict profile scopes, caller pins, host credential references, and durable state path. |
| `tool_catalog.py` | Define the fixed tool IDs, operation IDs, and input schemas. |
| `plugin.py` | Verify signed `tool.provider.v1` calls and map typed provider failures. |
| `providers.py` | Call fixed Azure, Google Compute Engine, and Runpod REST routes; poll operation refs. |
| `process.py` | Execute fixed vendor CLIs through a bounded argv-only subprocess runner. |
| `ledger.py` | Persist write request IDs and operation references to prevent blind replay. |
| `operations.py` | Sign opaque operation references using the host-bound Exchange key. |

<!-- 中文翻译 -->

# `cloud_management_provider`

| 文件 | 职责 |
| --- | --- |
| `settings.py` | 解析严格的 profile 范围、调用方绑定、主机凭据引用和持久状态目录。 |
| `tool_catalog.py` | 定义固定工具 ID、operation ID 和参数 schema。 |
| `plugin.py` | 校验签名的 `tool.provider.v1` 调用并映射类型化供应商错误。 |
| `providers.py` | 调用固定的 Azure、Google Compute Engine 和 Runpod REST 路由；轮询 operation ref。 |
| `process.py` | 通过仅接受 argv 的有界子进程执行器运行固定供应商 CLI。 |
| `ledger.py` | 持久化写请求 ID 和 operation ref，避免不确定结果后盲目重放。 |
| `operations.py` | 使用主机绑定的 Exchange key 签署不透明 operation ref。 |
