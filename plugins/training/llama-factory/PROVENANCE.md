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
| Reference source commit | [`8c392ce83c6410a1679099e1799e02d7d150c3b7`](https://github.com/Icy-Lunar/LlamaFactory/commit/8c392ce83c6410a1679099e1799e02d7d150c3b7) |
| Upstream base | `hiyouga/LLaMA-Factory` (Apache-2.0) |
| License | Apache-2.0 |
| Invocation | `CYRENE_LLAMA_FACTORY_ENTRYPOINT` or `python -m llamafactory.cli` |
| Version probe | `CYRENE_LLAMA_FACTORY_VERSION` reported by `inspect` |

Deployment preview: [2026-11-05 ROCm/WSL + JSON compatibility](https://github.com/Icy-Lunar/LlamaFactory/releases/tag/preview-rocm-wsl-startup-20261105-8c392ce8).
ZIP SHA-256: `003160fd5397a16c63c715e62c85ceae0662ae9a10cac6c23a27399dd9b38983`.
The source PR is [Icy-Lunar/LlamaFactory#1](https://github.com/Icy-Lunar/LlamaFactory/pull/1).
This preview is not a merged stable release or a real-GPU acceptance claim.
The CI repair removes a stale inference selector and guards upstream publishers.
Whole-tree lint/license/build pass. Legacy remote-model fixtures now explicitly
prepare local test files without changing production loader guards. The full trainer
matrix is rerunning; integration still requires source CI/review. This reference
is a test candidate, not an approved runtime.
The Plugin package is released separately by its source-bound hosted workflow after normal review/merge.

部署预览见上述精确 release，ZIP SHA-256 同上。源码 PR 为 Icy-Lunar/LlamaFactory#1；
此预览不表示稳定版本已合并或真实 GPU 已验收。Plugin package 仍由本仓库的 hosted
工作流在正常评审合并后单独发布，不把训练器 ZIP 混作 capability package。
此次修复删除失效推理字段检查并限制上游发布任务，完整代码、许可证与构建检查通过。
遗留测试已改为先准备本地测试文件，正式加载器限制不变；全套测试矩阵重新运行中。
此引用仅为测试候选，不是已批准运行时；集成仍须等待源码 CI 与评审通过。

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
| 参考源码提交 | 8c392ce83c6410a1679099e1799e02d7d150c3b7 |
| 上游基础 | hiyouga/LLaMA-Factory（Apache-2.0） |
| License | Apache-2.0 |
| 调用方式 | CYRENE_LLAMA_FACTORY_ENTRYPOINT 或 python -m llamafactory.cli |
| 版本探测 | inspect 报告的 CYRENE_LLAMA_FACTORY_VERSION |

## 验证状态

本地测试通过真实 DirectPluginRuntime，在物化的 run 目录上分派 inspect、compile 和 parse_event。真实 CUDA SFT + LoRA run、checkpoint 生成和 adapter 导出不在这些测试的验证范围内，仍属于 Wave 3 验收工作。
