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
| Project | [Icy-Lunar/LlamaFactory](https://github.com/Icy-Lunar/LlamaFactory) |
| Package | `llamafactory` |
| Reference source commit | [`1cf21a11e7375cf64e56a7092f9ff3132efc0ad6`](https://github.com/Icy-Lunar/LlamaFactory/commit/1cf21a11e7375cf64e56a7092f9ff3132efc0ad6) |
| Upstream base | `hiyouga/LLaMA-Factory` (Apache-2.0) |
| License | Apache-2.0 |
| Invocation | `CYRENE_LLAMA_FACTORY_ENTRYPOINT` or `python -m llamafactory.cli` |
| Version probe | `CYRENE_LLAMA_FACTORY_VERSION` reported by `inspect` |

Deployment preview: [2026-11-23 VRAM calibration + multi-backend hardening](https://github.com/Icy-Lunar/LlamaFactory/releases/tag/preview-memory-calibration-20261123-1cf21a11).
ZIP SHA-256: `48994ea5e4af5fdbade459c16a644513b6d4ebfabff012daaa717e035035210b`.
The source PR is [Icy-Lunar/LlamaFactory#1](https://github.com/Icy-Lunar/LlamaFactory/pull/1).
This preview is not a merged stable release or a real-GPU acceptance claim.
The CI repair removes a stale inference selector and guards upstream publishers.
Focused WebUI, installer, Liger, backend, diagnostics, and memory tests report
158 passed with 2 environment-affected CLI smoke variants explicitly excluded.
Real CUDA, XPU, native ROCm, and ROCm/WSL acceptance is NOT_RUN. Integration
still requires source CI and review. This reference is a test candidate, not an
approved runtime.
The Plugin package is released separately by its source-bound hosted workflow after normal review/merge.

部署预览见上述精确 release，ZIP SHA-256 同上。源码 PR 为 Icy-Lunar/LlamaFactory#1；
此预览不表示稳定版本已合并或真实 GPU 已验收。Plugin package 仍由本仓库的 hosted
工作流在正常评审合并后单独发布，不把训练器 ZIP 混作 capability package。
WebUI、安装器、Liger、后端选择、诊断与显存估算聚焦测试共 158 项通过；2 项受本机环境
影响的 CLI smoke 明确排除且不标记为通过。真实 CUDA、XPU、原生 ROCm 与 ROCm/WSL
验收均为 NOT_RUN。此引用仅为测试候选，不是已批准运行时；集成仍须等待源码 CI 与评审。

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
| 项目 | Icy-Lunar/LlamaFactory |
| Package | llamafactory |
| 参考源码提交 | 1cf21a11e7375cf64e56a7092f9ff3132efc0ad6 |
| 上游基础 | hiyouga/LLaMA-Factory（Apache-2.0） |
| License | Apache-2.0 |
| 调用方式 | CYRENE_LLAMA_FACTORY_ENTRYPOINT 或 python -m llamafactory.cli |
| 版本探测 | inspect 报告的 CYRENE_LLAMA_FACTORY_VERSION |

## 验证状态

本地测试通过真实 DirectPluginRuntime，在物化的 run 目录上分派 inspect、compile 和 parse_event。真实 CUDA SFT + LoRA run、checkpoint 生成和 adapter 导出不在这些测试的验证范围内，仍属于 Wave 3 验收工作。
