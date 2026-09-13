# Model-Backed LLM Judge / 模型评审插件

`evaluation.runner.v1` implementation that judges samples with a model reached
through `model.provider.v1`. Pointwise scoring (`llm_judge.v1`) and pairwise
comparison (`llm_pairwise.v1`) live here together; both keep their own
owner-scoped request and response definitions.

`evaluation.runner.v1` 实现，通过 `model.provider.v1` 调用模型评审样本。pointwise 打分
（`llm_judge.v1`）与 pairwise 比较（`llm_pairwise.v1`）同在本插件，二者各有自己的
owner-scoped 请求/响应定义。

| Evaluator id | Semantics / 语义 | Params |
| --- | --- | --- |
| `llm_judge.v1` | Judge scores the actual answer against the expected answer (`0..1`); `passed` uses `pass_threshold` | `rubric`, `max_chars`, `pass_threshold` |
| `llm_pairwise.v1` | Judge picks a winner between candidate A and B; per-sample `winner` is `a` / `b` / `tie` | `rubric`, `max_chars`, `allow_tie` |

**Activation configuration** (`CYRENE_CAPABILITY_CONFIGURATION_JSON`):
`model_endpoint` (required, loopback/Unix connection ref resolved by the host),
`model` (required), and optional `temperature` (default `0`), `max_tokens`,
`timeout_seconds`, `pass_threshold`.

**激活配置**（`CYRENE_CAPABILITY_CONFIGURATION_JSON`）：`model_endpoint`（必填，宿主解析出的
loopback/Unix 连接引用）、`model`（必填），可选 `temperature`（默认 `0`）、`max_tokens`、
`timeout_seconds`、`pass_threshold`。

Behaviour:

- The judge asks for one JSON object and parses it strictly (fenced or embedded
  JSON is accepted); an unparseable reply fails only that sample with a bounded
  detail, while a fixtureless tie under `allow_tie: false` is recorded as
  `judged: false`.
- Model provider transport failures fail the whole request closed
  (`UNAVAILABLE`); request-level contract violations return `INVALID_REQUEST`.
- Responses carry a `judge` identity block (`model`, `plugin_binding_id`) for
  reproducibility. Response shapes: `contracts/v1/schema.json#/$defs/JudgeResponse`
  and `#/$defs/PairwiseResponse`.

```bash
python3 -m pytest plugins/evaluation/llm-judge/tests
```
