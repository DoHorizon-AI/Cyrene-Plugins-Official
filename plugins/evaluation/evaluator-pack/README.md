# Deterministic Evaluator Pack / 确定性评估器包

`evaluation.runner.v1` implementation carrying five deterministic evaluators.
Each evaluator keeps its own owner-scoped request definition and a stable
evaluator id; the payload carries both, so a Product binding selects an
evaluator without this plugin abstracting a shared evaluator contract.

`evaluation.runner.v1` 实现，承载五个确定性 evaluator。每个 evaluator 保有自己的
owner-scoped 请求定义与稳定 evaluator id；identity 由载荷携带，Product 绑定即可选择
evaluator，本插件不抽象跨 evaluator 的共享契约。

| Evaluator id | Semantics / 语义 | Params |
| --- | --- | --- |
| `contains.v1` | Expected substring appears in the actual string | `case_sensitive` (default `true`) |
| `regex.v1` | Python `re` match over the actual string | `pattern` (required), `mode` (`search`\|`fullmatch`), `flags` (`ignorecase`\|`multiline`\|`dotall`) |
| `json_structural.v1` | Deep equality of JSON structures; object key order is ignored, array order matters | none |
| `json_schema.v1` | The actual value validates against a caller-supplied JSON Schema | `schema` (required) |
| `numeric_tolerance.v1` | `abs(expected - actual) <= abs_tolerance + rel_tolerance * abs(expected)` | `abs_tolerance`, `rel_tolerance` (both default `0`) |

Contract: `contracts/v1/schema.json` (owner-scoped). Invocation uses the shared
evaluation runner convention: capability `evaluation.runner.v1`, method
`evaluate`, type URL `type.cyrene.io/evaluation.runner.v1.evaluate.request`.

契约位于 `contracts/v1/schema.json`（owner-scoped）。调用沿用评估器统一约定：capability
`evaluation.runner.v1`、method `evaluate`、type URL `type.cyrene.io/evaluation.runner.v1.evaluate.request`。

Request-level violations (unsupported evaluator, missing fields, invalid regex
or schema, empty records) fail the whole request closed; sample-level
mismatches are reported per sample with a bounded detail string.

```bash
python3 -m pytest plugins/evaluation/evaluator-pack/tests
```
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# 确定性评估器包

这是承载五个确定性 evaluator 的 evaluation.runner.v1 实现。每个 evaluator 保留自己的 owner 范围请求定义和稳定 evaluator ID；payload 会同时携带两者，因此 Product binding 可以选择 evaluator，而此 plugin 不会抽象出共享 evaluator contract。

| Evaluator ID | 语义 | 参数 |
| --- | --- | --- |
| contains.v1 | 预期子串出现在实际字符串中 | case_sensitive（默认 true） |
| regex.v1 | 使用 Python re 对实际字符串进行匹配 | pattern（必填）、mode（search 或 fullmatch）、flags（ignorecase、multiline、dotall） |
| json_structural.v1 | JSON 结构的深度相等比较；忽略 object key 顺序，保留 array 顺序 | 无 |
| json_schema.v1 | 实际值根据调用方提供的 JSON Schema 验证 | schema（必填） |
| numeric_tolerance.v1 | abs(expected - actual) <= abs_tolerance + rel_tolerance * abs(expected) | abs_tolerance 和 rel_tolerance（均默认 0） |

契约位于 contracts/v1/schema.json（owner 范围）。调用沿用统一 evaluation runner 约定：capability 为 evaluation.runner.v1，method 为 evaluate，type URL 为 type.cyrene.io/evaluation.runner.v1.evaluate.request。

请求级违规（不支持的 evaluator、缺少字段、无效 regex 或 schema、记录为空）会使整个请求失败关闭；样本级不匹配则按样本报告，并将 detail 字符串限制在有界长度内。

```bash
python3 -m pytest plugins/evaluation/evaluator-pack/tests
```
