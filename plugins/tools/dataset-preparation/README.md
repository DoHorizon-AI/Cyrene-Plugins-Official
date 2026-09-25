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
All calls use typed `DirectPluginRuntime` methods: `inspect`, `prepare`, and
`transform`.

`inspect` accepts an optional `format_hint` for staged files whose content does
not identify CSV unambiguously. `prepare` consumes the confirmed `source_format`;
both methods support `JSONL`, `JSON`, `TEXT`, `CSV`, and `PARQUET`.
`transform` accepts `JSONL`, `JSON`, `CSV`, and `PARQUET` sources and produces
a compressed Parquet artifact. CSV inputs are rejected when headers are blank
or duplicated, or when any row has missing or extra columns.
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# Dataset Preparation

此无状态 Plugin 是 dataset.preparation.v1 的规范实现。它负责基于内容的导入检测，JSON/JSONL/text、CSV 和 Parquet 解析，字段映射、Unicode 规范化、质量错误、内容去重、确定性分组拆分，以及经过验证的 JSONL、CSV 和 Parquet 导出。

Product 在 Platform 监管的共享 staging 范围内传入绝对路径。此 capability 不解析 ArtifactRef identity，也不拥有 Product 状态。所有调用均使用 DirectPluginRuntime 类型化 method：inspect、prepare 和 transform。

对于无法通过内容明确识别为 CSV 的 staging 文件，inspect 可接受可选 format_hint。prepare 使用已经确认的 source_format；两种 method 都支持 JSONL、JSON、TEXT、CSV 和 PARQUET。

transform 接受 JSONL、JSON、CSV 和 PARQUET 来源，并生成压缩后的 Parquet artifact。如果 CSV header 为空、存在重复项，或任意行包含缺失或多余列，输入就会被拒绝。
