# Dataset Validator / 数据集校验器

`cyrene.tools.dataset-validator` is the canonical implementation of
`tool.dataset.validator.v1`. It validates JSONL, JSON, CSV, and Parquet files
staged for the Plugin process, including instruction/conversation shapes and
bounded quality metrics. Products keep their own dataset or training state and
invoke this stateless capability through `DirectPluginRuntime`.

本插件是 `tool.dataset.validator.v1` 的唯一实现。Product 保留 Dataset、训练任务
及错误状态权威，只通过标准 `connection_ref` 直连本无状态校验能力。

The typed method is:

- `validate` — `type.cyrene.io/tool.dataset.validator.v1.validate.request` →
  `type.cyrene.io/tool.dataset.validator.v1.validate.response`
