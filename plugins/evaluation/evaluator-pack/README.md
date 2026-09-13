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
