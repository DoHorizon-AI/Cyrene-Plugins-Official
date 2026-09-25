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
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# 来源记录

本 package 是 Cyrene 自有代码。vLLM 是由 operator 安装并以独立进程运行的外部 engine；本仓库没有 vendor 任何 vLLM 源码。

## Package 信息

| 字段 | 值 |
| --- | --- |
| Package | cyrene-vllm-runtime |
| Capability | execution.engine.v1 |
| License | Apache-2.0（仓库 LICENSE） |
| 来源 | Cyrene-Plugins-Official/plugins/serving/vllm-runtime |

## 上游 Engine

| 字段 | 值 |
| --- | --- |
| 项目 | vllm-project/vllm |
| Package | vllm |
| 固定版本 | release-lock.json 中 engines."execution.engine.v1".acceptedVersion |
| License | Apache-2.0 |
| 已解析依赖 | torch==2.11.0、transformers>=5.5.3（来自固定版本的 vLLM metadata） |
| 调用方式 | --vllm-command "vllm serve"（由 operator 安装的 launcher） |

## 模型来源

import_model 接受固定的 Hugging Face revision 或绝对本地目录。在将 model artifact 发布到 Platform artifact plane 前，它会验证权重、config.json、tokenizer、chat template、许可证和 manifest digest。此路径始终禁用 trust_remote_code。

## 验证状态

本地测试使用真实的本地 artifact CAS 测试模型导入，并使用加速器替身进程测试 execution 生命周期。真实 vLLM 启动、CUDA 推理和 GPU 释放仍未经验证，属于 RC 验收工作。
