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
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# 基于模型的 LLM Judge

这是 evaluation.runner.v1 的一个实现，通过 model.provider.v1 调用模型来评审样本。pointwise 打分（llm_judge.v1）和 pairwise 比较（llm_pairwise.v1）放在同一 package 中；二者分别保有自己的 owner 范围请求和响应定义。

| Evaluator ID | 语义 | 参数 |
| --- | --- | --- |
| llm_judge.v1 | Judge 根据预期答案给实际答案打分（0..1）；passed 由 pass_threshold 决定 | rubric、max_chars、pass_threshold |
| llm_pairwise.v1 | Judge 在候选答案 A 和 B 之间选出胜者；每个样本的 winner 为 a、b 或 tie | rubric、max_chars、allow_tie |

**激活配置**（CYRENE_CAPABILITY_CONFIGURATION_JSON）：model_endpoint（必填，由 host 解析出的 loopback/Unix connection reference）、model（必填），以及可选的 temperature（默认 0）、max_tokens、timeout_seconds 和 pass_threshold。

## 行为

- Judge 会请求一个 JSON object 并严格解析（允许使用代码围栏或嵌入式 JSON）。无法解析的回复只会让该样本失败，并返回有界 detail；当 allow_tie 为 false 且出现无 fixture 的平局时，记录 judged: false。
- Model provider 传输失败会使整个请求失败关闭（UNAVAILABLE）；请求级契约违规会返回 INVALID_REQUEST。
- 响应携带 judge identity block（model、plugin_binding_id），以支持结果复现。响应结构分别见 contracts/v1/schema.json#/$defs/JudgeResponse 和 #/$defs/PairwiseResponse。

```bash
python3 -m pytest plugins/evaluation/llm-judge/tests
```
