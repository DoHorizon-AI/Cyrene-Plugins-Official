# execution.engine.v1 TCK / 服务引擎契约测试

This TCK validates the `execution.engine.v1` binding payloads against the
owner-scoped schema published by the vLLM serving runtime. It runs in-process
against the Plugins implementation and can additionally probe an operator
binding through environment variables.

本 TCK 按 vLLM 服务运行时发布的 owner-scoped schema 校验 `execution.engine.v1`
绑定载荷；默认对本仓库实现做进程内验证，也可以通过环境变量检查操作者部署的绑定。

```bash
python3 -m pytest -q contracts/tck/execution-engine-v1
```

Optional external binding / 可选外部绑定:

```bash
CYRENE_ENGINE_TCK_BASE_URL=http://127.0.0.1:19400 \
CYRENE_ENGINE_TCK_TOKEN=<mode-0600-file> \
python3 -m pytest -q contracts/tck/execution-engine-v1
```

Coverage map / 覆盖说明:

- Schema conformance: `POST /imports` and execution lifecycle responses validate
  against `plugins/serving/vllm-runtime/contracts/v1/schema.json`.
- Import invariants: a validated `model` artifact uses the content-addressed
  Platform URI and the manifest digest is stable across repeated imports.
- Release evidence: `POST /executions/{id}/stop` reports `released: true` and
  `ready: false`.
- Fail-closed imports: `trustRemoteCode` stays refused and an incomplete model
  directory is rejected with a typed error.