# Exact Match Evaluation Runner / 精确匹配评估运行器

This stateless Plugin is the canonical implementation of
`evaluation.runner.v1` for `exact_match.v1`. A Product reads its own immutable
artifact, sends JSON records through `DirectPluginRuntime`, and remains the
authority for run state, gates, annotations, feedback, and report storage.

本无状态插件是 `evaluation.runner.v1` 中 `exact_match.v1` 的唯一实现。Product
负责读取自己的不可变制品并通过 `DirectPluginRuntime` 发送 JSON 记录；运行状态、
门禁、标注、反馈及报告存储仍由 Product 负责。

## Boundary / 边界

- Plugin-owned: strict expected/actual equality, sample measurements, aggregate score.
- Product-owned: artifact access, evaluation lifecycle, threshold policy, persistence.
- Platform-owned: package lifecycle and opaque endpoint resolution only.

The endpoint exposes one typed method:

- `evaluate` — `type.cyrene.io/evaluation.runner.v1.evaluate.request` →
  `type.cyrene.io/evaluation.runner.v1.evaluate.response`

Real gRPC endpoint coverage is in
`conformance/tests/test_refactored_evaluators.py`.
