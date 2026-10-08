# Dataset Validator / 数据集校验器

`cyrene.tools.dataset-validator` is the canonical implementation of
`tool.dataset.validator.v1`. It validates JSONL, JSON, CSV, and Parquet files
staged for the Plugin process, including Alpaca instruction/history,
prompt-completion, OpenAI messages, and ShareGPT conversation shapes. JSONL is
validated one row at a time with a 16 MiB row limit; malformed or oversized rows
are reported while later rows are still checked. Final target schemas reject
unexpected learned-row and message fields and unsupported roles. Products keep
their own dataset or training state and invoke this stateless capability through
`DirectPluginRuntime`.

本插件是 `tool.dataset.validator.v1` 的唯一实现。Product 保留 Dataset、训练任务
及错误状态权威，只通过标准 `connection_ref` 直连本无状态校验能力。

The typed method is:

- `validate` — `type.cyrene.io/tool.dataset.validator.v1.validate.request` →
  `type.cyrene.io/tool.dataset.validator.v1.validate.response`

Supported explicit schemas are `instruction`, `instruction_history`,
`prompt_completion`, and `messages`; legacy `conversation` and `sharegpt` remain
available. `instruction_history` requires `instruction`, `input`, `output`,
`system`, and `history` where history is an array of non-empty
`[user, assistant]` pairs. `messages` requires ordered `system` (optional at
the beginning), `user`, and `assistant` string messages and a complete final
assistant turn.
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# Dataset Validator

cyrene.tools.dataset-validator 是 tool.dataset.validator.v1 的规范实现。它校验为 Plugin 进程 staging 的 JSONL、JSON、CSV 和 Parquet 文件，包括 Alpaca instruction/history、prompt-completion、OpenAI messages 和 ShareGPT conversation 结构及有界质量指标。JSONL 逐行校验，单行限制为 16 MiB；损坏或超限行会报告，后续行仍会继续检查。最终输出 schema 会拒绝意外训练字段、消息字段和不支持的角色。Product 保留自己的 dataset 或 training 状态，并通过 DirectPluginRuntime 调用这项无状态 capability。

显式 schema 包括 `instruction`、`instruction_history`、`prompt_completion` 和 `messages`，并继续支持旧的 `conversation` 与 `sharegpt`。`instruction_history` 要求 `instruction`、`input`、`output`、`system`、`history`，其中 history 是由非空 `[user, assistant]` 对组成的数组。`messages` 要求按顺序排列的 system（可选且只能在开头）、user 和 assistant 文本消息，并以完整 assistant 回复结束。

类型化 method 如下：

- validate：请求 type URL 为 type.cyrene.io/tool.dataset.validator.v1.validate.request，响应 type URL 为 type.cyrene.io/tool.dataset.validator.v1.validate.response。
