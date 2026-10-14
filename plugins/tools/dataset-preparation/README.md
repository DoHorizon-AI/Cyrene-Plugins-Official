# Dataset Preparation / 数据集整理

This stateless Plugin is the canonical implementation of
`dataset.preparation.v1`. It owns content-based import detection, JSON/JSONL/text,
CSV, and Parquet parsing, field mapping, Unicode normalization, quality errors, content
deduplication, deterministic group splitting, and verified JSONL, CSV, and
Parquet exports.

本无状态插件是 `dataset.preparation.v1` 的唯一实现。Catalyst 仍拥有 Dataset、
DatasetVersion、Preparation 状态机、人工确认、ArtifactRef 发布及数据谱系。

Products pass absolute paths inside a Platform-supervised shared staging scope.
The capability never resolves ArtifactRef identity and never owns Product state.
All calls use typed `DirectPluginRuntime` methods: `inspect`, `prepare`,
`transform`, `curate_training_records`, and `remap_training_record`.

`inspect` accepts an optional `format_hint` for staged files whose content does
not identify CSV unambiguously. `prepare` consumes the confirmed `source_format`;
both methods support `JSONL`, `JSON`, `TEXT`, `CSV`, and `PARQUET`.
`transform` accepts `JSONL`, `JSON`, `CSV`, and `PARQUET` sources and produces
a compressed Parquet artifact. CSV inputs are rejected when headers are blank
or duplicated, or when any row has missing or extra columns.

`curate_training_records` streams mixed Alpaca, prompt-completion, messages,
ShareGPT, and ChatML rows into the versioned `cyrene.training-record.v1`
envelope. Its bounded receipt includes per-source counts; raw rows and
unsupported content remain available for Catalyst review. JSONL is read one
line at a time and JSON arrays are incrementally decoded. A malformed JSONL
line becomes its own reviewable record; an unrecoverable JSON-array error marks
only that source FAILED with an item locator, preserves any parsed prefix, and
lets the remaining batch continue. A JSON checkpoint
and job-local SQLite index allow a retry to resume a committed prefix without
duplicating output. `remap_training_record` uses the same normalizer for human
field or role corrections and records the previous raw value and digest.
Alpaca `system` text and `history` user/assistant pairs remain separate ordered
messages. Unknown semantic fields and tool/function/observation roles stay
review blockers; reserved management fields cannot be mapped into learned text.

The curation action accepts at most 20 sources, each up to 64 MiB. It reports
malformed and blank lines, preserves tool calls, multimodal values, and
unmapped roles in raw data, and requires explicit source permission for
`model_training`. Exact duplicates are explicitly excluded; probable near
duplicates and conflicting answers are flagged for review. Catalyst owns the
review and approval state. Recoverable parser, format, and structure errors
remain in review even when normalization is unavailable; Catalyst must require
a successful remap or explicit exclusion before publication.
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# Dataset Preparation

此无状态 Plugin 是 dataset.preparation.v1 的规范实现。它负责基于内容的导入检测，JSON/JSONL/text、CSV 和 Parquet 解析，字段映射、Unicode 规范化、质量错误、内容去重、确定性分组拆分，以及经过验证的 JSONL、CSV 和 Parquet 导出。

Product 在 Platform 监管的共享 staging 范围内传入绝对路径。此 capability 不解析 ArtifactRef identity，也不拥有 Product 状态。所有调用均使用 DirectPluginRuntime 类型化 method：inspect、prepare、transform、curate_training_records 和 remap_training_record。

对于无法通过内容明确识别为 CSV 的 staging 文件，inspect 可接受可选 format_hint。prepare 使用已经确认的 source_format；两种 method 都支持 JSONL、JSON、TEXT、CSV 和 PARQUET。

transform 接受 JSONL、JSON、CSV 和 PARQUET 来源，并生成压缩后的 Parquet artifact。如果 CSV header 为空、存在重复项，或任意行包含缺失或多余列，输入就会被拒绝。

`curate_training_records` 将混合 Alpaca、prompt-completion、messages、ShareGPT 和 ChatML 记录流式写入版本化的 `cyrene.training-record.v1` envelope。它返回逐来源计数，原始行和不支持内容会保留供 Catalyst 审核；JSONL 按行读取，JSON array 增量解码。JSON checkpoint 与任务级 SQLite 索引支持从已提交前缀恢复，避免重试时重复写入。`remap_training_record` 复用相同规范化规则处理人工字段或角色修正，并记录修正前原始值和摘要。

JSONL 损坏行会作为独立审核记录保留；无法恢复的 JSON array 错误只会将对应来源标记为 FAILED，并带 item locator 和已解析前缀报告，后续批次来源仍会继续处理。

Alpaca 的 `system` 文本和 `history` 用户/助手轮次会以独立且有序的消息保留。未知语义字段以及 tool/function/observation 角色会阻止审核通过；禁止将保留的管理字段映射为学习文本。

整理动作最多接收 20 个来源，每个来源不超过 64 MiB。它报告损坏行和空行，在原始数据中保留工具调用、多模态值和未映射角色，并要求来源策略明确允许 `model_training`。完全重复项会标记并排除；疑似近似重复和答案冲突进入审核。审核和批准状态由 Catalyst 管理。

可修复的解析、格式和结构错误会留在审核队列中，即使当前无法生成规范化内容；发布前必须由 Catalyst 要求成功修正，或明确排除该记录。
