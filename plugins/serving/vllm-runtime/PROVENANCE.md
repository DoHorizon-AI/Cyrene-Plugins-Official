# Provenance / 来源记录

This package is Cyrene-owned code. vLLM is an external, operator-installed engine
launched as a process; no vLLM source is vendored here.

本软件包是 Cyrene 自有代码。vLLM 是由操作者安装、以进程方式启动的外部引擎，
本仓库不内嵌任何 vLLM 源码。

## Package / 软件包

| Field | Value |
| --- | --- |
| Package | `cyrene-vllm-runtime` |
| Capability | `execution.engine.v1` |
| License | Apache-2.0 (repository `LICENSE`) |
| Source | `Cyrene-Plugins-Official/plugins/serving/vllm-runtime` |

## Upstream engine / 上游引擎

| Field | Value |
| --- | --- |
| Project | [vllm-project/vllm](https://github.com/vllm-project/vllm) |
| Package | `vllm` |
| Locked version | `release-lock.json` → `engines."execution.engine.v1".acceptedVersion` |
| License | Apache-2.0 |
| Resolved dependencies | `torch==2.11.0`, `transformers>=5.5.3` (from the locked vLLM metadata) |
| Invocation | `--vllm-command "vllm serve"` (operator-installed launcher) |

## Model sources / 模型来源

`import_model` accepts a pinned Hugging Face revision or an absolute local
directory. It validates weights, `config.json`, tokenizer, chat template,
license, and a manifest digest before publishing a `model` artifact into the
Platform artifact plane. `trust_remote_code` stays disabled on this path.

`import_model` 接受固定的 Hugging Face revision 或绝对本地目录。发布 `model` 制品到
Platform 制品平面之前，会校验权重、`config.json`、tokenizer、chat template、许可证和
清单摘要；该路径始终禁用 `trust_remote_code`。

## Verification status / 验证状态

Local tests exercise model import against a real local artifact CAS and the
execution lifecycle against a stand-in accelerator process. A real vLLM launch,
CUDA inference, and GPU release remain unverified and are RC acceptance work.

本地测试用真实本地制品 CAS 覆盖模型导入，用替身加速器进程覆盖执行生命周期。真实 vLLM
启动、CUDA 推理和 GPU 回收仍未验证，属于 RC 验收工作。