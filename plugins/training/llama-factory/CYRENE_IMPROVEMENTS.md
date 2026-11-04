# Cyrene LLaMA-Factory improvements / Cyrene LLaMA-Factory 改进说明

## Reference release / 参考版本

- Repository: `https://github.com/Icy-Lunar/LlamaFactory`
- Commit: `194cc85c5d87612693ac9d47d13c2aecd8a81e22`
- The full trainer source and deployment ZIP remain outside this repository. This Plugin stores only the capability adapter and immutable source reference.

- 仓库：`https://github.com/Icy-Lunar/LlamaFactory`
- 提交：`194cc85c5d87612693ac9d47d13c2aecd8a81e22`
- 完整训练器源码和部署 ZIP 不进入本仓库；本 Plugin 只保存能力适配器与不可变源码引用。

## Main changes / 主要改进

- Guided training WebUI with branch-based parameter disclosure, multilingual localized text, editable generated configuration, and early VRAM estimation.
- Local-directory-only model loading; the training workflow does not download models.
- LoRA and full-parameter tuning, multimodal controls, validation by dataset ratio, and evaluation aligned with checkpoint saves.
- Hugging Face-only quick inference for post-configuration smoke tests; vLLM and SGLang inference paths are removed.
- CUDA, Intel XPU, Linux ROCm, Windows ROCm, and WSL-aware bootstrap flows with one-command launchers and TensorBoard host selection.
- Windows Liger installation reuses a compatible `triton-windows` provider instead of resolving the Linux `triton` distribution. XPU keeps its matching Triton provider.
- Deployment packaging excludes Git metadata, virtual environments, logs, caches, tests, model weights, and generated outputs.

- 引导式训练 WebUI 根据用户选择逐步展示参数，提供多语言本地化、可编辑自动配置和提前显存估算。
- 模型仅从本地目录读取，训练流程不负责下载模型。
- 支持 LoRA、全参数训练、多模态处理、按比例划分验证集，并在保存 checkpoint 时触发验证。
- 仅保留 Hugging Face 快速推理用于配置后的初步测试，移除 vLLM 与 SGLang 推理路径。
- 提供 CUDA、Intel XPU、Linux ROCm、Windows ROCm 与 WSL 感知的一键部署，并支持选择 WebUI/TensorBoard 监听地址。
- Windows Liger 优先复用兼容的 `triton-windows`，不再解析 Linux `triton`；XPU 保留对应的 Triton 提供者。
- 部署包排除 Git 元数据、虚拟环境、日志、缓存、测试、模型权重与生成输出。

## Verification boundary / 验证边界

The reference trainer passed its focused WebUI, installer, packaging, Liger, and backend-selection tests before publication. Real GPU acceptance remains environment-specific and must be recorded separately for each CUDA, XPU, or ROCm host.

参考训练器在发布前已通过 WebUI、安装器、打包、Liger 与后端选择专项测试。真实 GPU 验收仍与具体环境有关，必须针对每台 CUDA、XPU 或 ROCm 主机单独记录。
