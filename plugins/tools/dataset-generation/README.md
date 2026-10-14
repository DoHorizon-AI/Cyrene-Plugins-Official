# Dataset Generation / 数据集生成

`cyrene.tools.dataset-generation` implements `dataset.generation.v1` as a
stateless DirectPluginRuntime capability. `prepare_sft` reads an approved
ContentRevision block snapshot (`blocks_path`) or a manual sample document
(`samples_path`), parses only the frozen
instruction/Alpaca or conversation JSON row shapes, assigns whole source
families to train/validation/test, and writes a deterministic
`CYRENE_SFT_BUNDLE_V1` ZIP. `generate_qa` is a separate explicit method backed
only by an activation-resolved `model.provider.v1` endpoint. Missing provider
configuration leaves manual SFT preparation available and makes QA generation
return `UNAVAILABLE`.

The private method payload schemas are in `contracts/v1/schema.json`. Path keys
are executor-local absolute paths; Product routes must expose ArtifactRefs only.
`prepare_sft` accepts exactly one of `blocks_path` and `samples_path`. Approved
block documents use Catalyst's camelCase Product ContentRevision shape and
require `state: "APPROVED"` plus `policy.allowedUsePurposes` containing
`model_training` (or legacy `allowTraining: true`). SFT-ready block text is JSON
with one of these exact
shapes:

```json
{"instruction":"Question","output":"Answer"}
{"instruction":"Question","input":"Optional context","output":"Answer"}
{"conversations":[{"from":"human","value":"Question"},{"from":"gpt","value":"Answer"}]}
```

PDF prose and other non-record blocks are skipped with bounded warnings. QA
generation writes these same learned-row JSON objects to `drafts_path`; its
sidecar records stable sample IDs and `review_state: "PENDING"`. Catalyst must
create and approve a new ContentRevision before those drafts are included in a
published SFT bundle.

The ZIP members are `train.jsonl`, `validation.jsonl`, `test.jsonl`,
`provenance.jsonl`, and `manifest.json`. Training JSONL contains only the
selected learned row fields. Provenance JSONL is one canonical UTF-8 JSON object
per training row and carries sample ID, source family, origin, citations, split,
revision/run identity, training policy, and any generation recipe/provider/budget
receipt. Generated drafts also carry `generation_receipt` with the Product
`recipeId`, `recipeVersion`, `recipeDigest`, `bindingId`, `model`, `budget`,
`usage`, `generatedAt`, and `sourceBlockIds` fields. The manifest records the
profile, ranked-hash source-family split algorithm and ratios, row
counts, schema fields, and per-file SHA-256 digests. ZIP member order, timestamps,
and permissions are fixed so identical inputs and IDs produce identical bytes.

`generate_qa` splits source families before making calls, bounds examples/calls
and input/output budgets, stores provider-reported token usage only when present,
and checkpoints completed results after each successful call. It never retries
an ambiguous provider result. Provider-reported usage beyond a declared input
or output cap is returned as a warning; an input-cap overrun prevents later
calls. Package tests use a scripted provider over the real DirectPluginRuntime
connection.

`prepare_training_sft` adds the Catalyst v0.3 path-based export for an approved
`cyrene.training-record.v1` JSONL snapshot. It streams records into a temporary
SQLite lineage index and writes the same immutable bundle profile without
loading the full source or learned rows into memory; one JSONL row is capped at
16 MiB, and blank or malformed lines block export with a diagnostic report.
`sft` retains prior turns
as LLaMA-Factory `history` pairs and preserves the system field; `messages`
retains ordered system/user/assistant messages; `promptCompletion` accepts only
one user/assistant turn without a system message. Unsupported roles, tool or
multimodal fields, unresolved review rows, and lossy target conversions block
the export with `export-diagnostics.json`. Policy-denied and explicitly
excluded rows stay out of learned JSONL and are counted. Provenance records
source/revision/family/conversation/locator and digests, while raw records and
review fields remain in Catalyst's source snapshot. The manifest keeps the
export ProcessingRun recipe digest distinct from the curation recipe digests;
provenance preserves each record's curation recipe and processing history.

---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

`cyrene.tools.dataset-generation` 以无状态 DirectPluginRuntime capability 实现
`dataset.generation.v1`。`prepare_sft` 读取已批准的 ContentRevision blocks 或手工
样本文档，只解析冻结的 instruction/Alpaca 或 conversation JSON 行格式，按完整
source family 分配 train/validation/test，并生成确定性的 `CYRENE_SFT_BUNDLE_V1`
ZIP。`generate_qa` 是单独的显式方法，只通过 activation 已解析的
`model.provider.v1` endpoint 调用。未配置 Provider 时，手工 SFT 仍可用，而 QA
生成返回 `UNAVAILABLE`。

私有方法负载 schema 位于 `contracts/v1/schema.json`。路径字段是 executor 本地绝对
路径；Product 路由只能公开 ArtifactRef。`prepare_sft` 必须且只能提供一个
`blocks_path` 或 `samples_path`。已批准 block 文档使用 camelCase Product
ContentRevision 结构，并要求 `state: "APPROVED"` 和
`policy.allowedUsePurposes` 包含 `model_training`（或旧版 `allowTraining: true`）。
适用于 SFT 的 block text 必须是上述三种精确 JSON 行形状之一。

PDF prose 和其他非记录 block 会跳过并返回有界 warning。QA 生成将相同的 learned-row
JSON 对象写入 `drafts_path`；旁路文件记录稳定 sample ID 和
`review_state: "PENDING"`。Catalyst 必须先创建并批准新的 ContentRevision，再将这些
草稿纳入已发布的 SFT bundle。

ZIP 成员为 `train.jsonl`、`validation.jsonl`、`test.jsonl`、`provenance.jsonl` 和
`manifest.json`。训练 JSONL 只含选定的学习行字段。Provenance JSONL 每条训练行对应
一个规范 UTF-8 JSON 对象，记录 sample ID、source family、origin、citations、split、
revision/run 标识、训练策略，以及可用的 generation recipe/provider/budget 回执。
Manifest 记录 profile、split 算法和比例、行数、schema fields 和逐文件 SHA-256 摘要。
ZIP 成员顺序、时间戳和权限固定，因此相同输入和标识会产生相同字节。

`generate_qa` 在 Provider 调用前先拆分 source family，限制 examples/calls 和
input/output 预算；只有 Provider 实际上报时才记录 token usage，并在每次成功调用后
保存检查点。Provider 上报的 usage 超过输入或输出上限时会返回 warning；输入预算超限后
不会继续发起调用。Provider 结果不明时绝不重试。Package tests 使用真实
DirectPluginRuntime connection 上的 scripted Provider。

`prepare_training_sft` 为 Catalyst v0.3 增加基于文件路径的导出，输入是已批准的
`cyrene.training-record.v1` JSONL 快照。插件将记录流式写入临时 SQLite 血缘索引，并
使用现有不可变 bundle profile，不会将整个来源或训练行一次性加载到内存；每行限制为
16 MiB，空行或损坏行会阻止导出并生成诊断报告。`sft` 通过
LLaMA-Factory `history` 对保留先前轮次，并保留 system 字段；`messages` 保留有序的
system/user/assistant 消息；`promptCompletion` 只接受没有 system 消息的单轮
user/assistant 记录。未知角色、tool 或多模态字段、未解决的审核记录及有损目标转换会
阻止导出，并生成 `export-diagnostics.json`。策略禁止和显式排除的记录不会进入训练
JSONL，并会计数。Provenance 记录来源、revision、family、conversation、定位和摘要；原件
及审核字段继续保存在 Catalyst 来源快照中。Manifest 区分导出 ProcessingRun Recipe 摘要
与来源整理 Recipe 摘要；Provenance 保留每条记录的整理 Recipe 和处理历史。
