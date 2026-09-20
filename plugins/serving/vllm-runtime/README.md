# vLLM Serving Runtime / vLLM 服务运行时

Plugins-owned execution host for Reactor serving bindings. Reactor keeps deployment
intent; this runtime owns only the accelerator process.

Reactor 拥有部署意图；本运行时只拥有加速器进程。

| Endpoint | Responsibility | 职责 |
| --- | --- | --- |
| `POST /imports` | Validate an external model source and publish a `model` artifact | 校验外部模型来源并发布 `model` 制品 |
| `POST /executions/{id}` | Materialize the artifact, launch vLLM, confirm readiness | 物化制品、拉起 vLLM、确认就绪 |
| `GET /executions/{id}` | Report the execution and its model readback | 返回执行与模型回读 |
| `POST /executions/{id}/stop` | Terminate the process and confirm release | 终止进程并确认回收 |
| `POST /serving/{id}/v1/chat/completions` | Proxy inference to the launched process | 代理推理到已启动进程 |
| `GET /healthz`, `GET /readyz` | Liveness and readiness | 探活 |

Every route except the probes requires the binding credential as `Authorization: Bearer`.
`BASE_PLUS_LORA` executions resolve the base artifact from the supplied `modelVersion`
and launch vLLM with `--enable-lora`; `FULL_MODEL` executions serve the artifact directly.

除探活外，所有路由都要求绑定凭据；`BASE_PLUS_LORA` 会从 `modelVersion` 解析基础模型并以
LoRA 方式启动，`FULL_MODEL` 直接服务该制品。

`POST /imports` refuses `trustRemoteCode: true`, validates weights, `config.json`,
tokenizer, chat template, license, and a manifest digest, then publishes a portable
`model` artifact into the Platform artifact CAS. Private Hugging Face revisions resolve
through a `CredentialRef`: the ref's SHA-256 names a mode-0600 token file under
`<runtime-home>/credentials`, and the token is never echoed in a response.

`POST /imports` 拒绝 `trustRemoteCode: true`，校验权重、`config.json`、tokenizer、
chat template、许可证与清单摘要，然后把可移植 `model` 制品发布到 Platform 制品 CAS。
私有 Hugging Face revision 通过 `CredentialRef` 解析：ref 的 SHA-256 指向
`<runtime-home>/credentials` 下的 0600 令牌文件，令牌永不出现在响应中。

```bash
cyrene-vllm-runtime serve \
  --runtime-home "$CYRENE_RUNTIME_HOME/reactor-serving" \
  --artifact-root "$CYRENE_ARTIFACT_ROOT" \
  --credential-file "$CYRENE_RUNTIME_HOME/reactor/private/serving.token" \
  --control-url http://127.0.0.1:19400 \
  --vllm-command "vllm serve"
```

Verification: `cd plugins/serving/vllm-runtime && python -m pytest tests` exercises the whole
lifecycle against a stand-in process; a real vLLM launch and GPU inference are not verified.
