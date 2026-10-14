# Knowledge Preparation / 知识库构建

This stateless Plugin implements `dataset.knowledge.v1` for Catalyst. It consumes
approved ContentBlock JSONL plus SourceRevision metadata and writes one
`CYRENE_KNOWLEDGE_BUNDLE_V1` ZIP. Catalyst remains the authority for source and
content revisions, review, access identity, processing runs, and published
DatasetVersions.

The ZIP contains `manifest.json`, `chunks.jsonl`, `sources.jsonl`,
`hierarchy.json`, and `checksums.json`. Each chunk retains its source and content
revision IDs, original locator, block-local text range, ContentPolicy and
asset references. Chunk IDs use logical source/revision/block identity; byte
digests never merge different sources. Blocks enter the bundle only when
`allowKnowledge=true` and `knowledge_retrieval` appears in `allowedUsePurposes`.
Training-only blocks remain valid input, are excluded from this package, and
are reported. `model_training` can coexist with `knowledge_retrieval` in a
dual-purpose policy. Missing or malformed ContentPolicy rejects the build.

All capability calls use typed `DirectPluginRuntime` method `build`. Absolute
paths are private executor staging paths and never enter the public package
request. The response includes the ZIP digest and size, manifest digest, result
receipt digest and conversion counts.

## Independent reference consumer

`knowledge_reference.py` uses only the Python standard library. It does not
import Cyrene modules or connect to a database. It verifies the caller-supplied
published package digest, then every digest in `checksums.json`, then the
manifest's file digests. Only after verification does it filter chunks by
`allowKnowledge`, `allowedUsePurposes` and the non-empty intersection between
the stored `allowedPrincipalRefs` and caller-supplied authenticated principal
refs. It ranks authorized text with CPU BM25 and returns source revision,
locator, hierarchy path and asset references.

```bash
python -m knowledge_reference verify \
  --bundle knowledge.zip \
  --package-digest sha256:<published-artifact-digest>

python -m knowledge_reference search \
  --bundle knowledge.zip \
  --package-digest sha256:<published-artifact-digest> \
  --principal-ref workspace://principals/reader-1 \
  --use-purpose knowledge_retrieval \
  --query "account recovery policy" \
  --limit 5
```

The same consumer API is available to other Python applications:

```python
from knowledge_reference import search_bundle

result = search_bundle(
    "knowledge.zip",
    "account recovery policy",
    principal_refs=["workspace://principals/reader-1"],
    use_purpose="knowledge_retrieval",
    package_digest="sha256:<published-artifact-digest>",
    limit=5,
)
```

## Package checks

Run the package-local tests from this directory:

```bash
python -m pytest
```

---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# Knowledge Preparation

此无状态 Plugin 为 Catalyst 实现 `dataset.knowledge.v1`。它读取已审核的
ContentBlock JSONL 和 SourceRevision 元数据，并写出单个
`CYRENE_KNOWLEDGE_BUNDLE_V1` ZIP。Catalyst 仍是来源和内容版本、审核、访问身份、处理
运行及已发布 DatasetVersion 的权威。

ZIP 包含 `manifest.json`、`chunks.jsonl`、`sources.jsonl`、`hierarchy.json` 和
`checksums.json`。每个 chunk 保留来源版本和内容版本 ID、原始 locator、block 内文本
范围、ContentPolicy 及 asset refs。Chunk ID 基于逻辑来源/版本/block identity；不同
来源不会因字节摘要相同而合并。`allowKnowledge=false` 的 block 会被排除并计入报告。
只有 `allowKnowledge=true` 且 `allowedUsePurposes` 明确包含
`knowledge_retrieval` 的 block 才会进入知识包。仅允许 `model_training` 的 block 是
合法输入，但会从本知识包排除并计入报告；双用途策略可以同时列出两种 purpose。
策略缺失或格式错误时构建失败。

所有 capability 调用均通过类型化 `DirectPluginRuntime` method `build`。绝对路径只在
执行器私有 staging 中使用，不进入公开请求或包。响应包含 ZIP 摘要和大小、manifest
摘要、result receipt 摘要及转换计数。

## 独立 Reference Consumer

`knowledge_reference.py` 只使用 Python 标准库，不导入 Cyrene 模块，也不连接数据库。
它先校验调用者提供的已发布 package digest，再校验 `checksums.json` 和 manifest 中的
文件摘要。校验完成后，才按 `allowKnowledge`、`allowedUsePurposes`，以及存储的
`allowedPrincipalRefs` 与调用者已认证 principal refs 的非空交集筛选 chunk。它使用 CPU
BM25 对获准文本排序，返回 source revision、locator、层级路径和 asset refs。

```bash
python -m knowledge_reference verify \
  --bundle knowledge.zip \
  --package-digest sha256:<published-artifact-digest>

python -m knowledge_reference search \
  --bundle knowledge.zip \
  --package-digest sha256:<published-artifact-digest> \
  --principal-ref workspace://principals/reader-1 \
  --use-purpose knowledge_retrieval \
  --query "account recovery policy" \
  --limit 5
```

其他 Python 应用也可直接调用 `knowledge_reference.search_bundle`。包内的 ACL 不会认证
调用者；调用方必须传入已认证 principal refs 和从已发布 ArtifactRef 读取的摘要。
