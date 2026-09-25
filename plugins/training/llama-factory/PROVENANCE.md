# Provenance / 来源记录

This package is Cyrene-owned code, not a fork or vendored copy of LLaMA-Factory.

本软件包是 Cyrene 自有代码，不是 LLaMA-Factory 的 fork 或内嵌副本。

## Package / 软件包

| Field | Value |
| --- | --- |
| Package | `cyrene-llama-factory` |
| Capability | `training.llama-factory.v1` |
| License | Apache-2.0 (repository `LICENSE`) |
| Source | `Cyrene-Plugins-Official/plugins/training/llama-factory` |

## Upstream engine / 上游引擎

LLaMA-Factory is installed and executed by the operator as an external trainer.
No upstream source is copied into this package.

LLaMA-Factory 由操作者作为外部训练器安装并执行，本软件包不复制任何上游源码。

| Field | Value |
| --- | --- |
| Project | [hiyouga/LLaMA-Factory](https://github.com/hiyouga/LLaMA-Factory) |
| Package | `llamafactory` |
| Locked version | `release-lock.json` → `engines."training.llama-factory.v1".acceptedVersion` |
| License | Apache-2.0 |
| Invocation | `CYRENE_LLAMA_FACTORY_ENTRYPOINT` or `python -m llamafactory.cli` |
| Version probe | `CYRENE_LLAMA_FACTORY_VERSION` reported by `inspect` |

## Verification status / 验证状态

Local tests dispatch `inspect`, `compile`, and `parse_event` over the real
`DirectPluginRuntime` against a materialized run directory. A real CUDA SFT +
LoRA run, checkpoint production, and adapter export are not verified by those
tests and remain Wave 3 acceptance work.

本地测试通过真实 `DirectPluginRuntime` 在已生成的运行目录上覆盖 `inspect`、`compile`、
`parse_event`。真实 CUDA SFT + LoRA、checkpoint 与 adapter 导出不在这些测试范围内，
仍属于 Wave 3 验收工作。
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# 来源记录

本 package 是 Cyrene 自有代码，不是 LLaMA-Factory 的 fork 或 vendor 副本。

## Package 信息

| 字段 | 值 |
| --- | --- |
| Package | cyrene-llama-factory |
| Capability | training.llama-factory.v1 |
| License | Apache-2.0（仓库 LICENSE） |
| 来源 | Cyrene-Plugins-Official/plugins/training/llama-factory |

## 上游 Engine

LLaMA-Factory 由 operator 作为外部 trainer 安装和执行。本 package 不复制任何上游源码。

| 字段 | 值 |
| --- | --- |
| 项目 | hiyouga/LLaMA-Factory |
| Package | llamafactory |
| 固定版本 | release-lock.json 中 engines."training.llama-factory.v1".acceptedVersion |
| License | Apache-2.0 |
| 调用方式 | CYRENE_LLAMA_FACTORY_ENTRYPOINT 或 python -m llamafactory.cli |
| 版本探测 | inspect 报告的 CYRENE_LLAMA_FACTORY_VERSION |

## 验证状态

本地测试通过真实 DirectPluginRuntime，在物化的 run 目录上分派 inspect、compile 和 parse_event。真实 CUDA SFT + LoRA run、checkpoint 生成和 adapter 导出不在这些测试的验证范围内，仍属于 Wave 3 验收工作。
