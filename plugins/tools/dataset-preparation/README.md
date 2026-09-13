# Dataset Preparation / 数据集整理

This stateless Plugin is the canonical implementation of
`dataset.preparation.v1`. It owns content-based import detection, JSON/JSONL/text
parsing, field mapping, Unicode normalization, quality errors, content
deduplication, deterministic group splitting, standard JSONL exports, and
DuckDB JSONL-to-Parquet conversion.

本无状态插件是 `dataset.preparation.v1` 的唯一实现。Catalyst 仍拥有 Dataset、
DatasetVersion、Preparation 状态机、人工确认、ArtifactRef 发布及数据谱系。

Products pass absolute paths inside a Platform-supervised shared staging scope.
The capability never resolves ArtifactRef identity and never owns Product state.
All calls use typed `DirectPluginRuntime` methods: `inspect`, `prepare`, and
`transform`.
