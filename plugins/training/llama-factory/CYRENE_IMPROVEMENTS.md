# Cyrene LLaMA-Factory improvements / Cyrene LLaMA-Factory 改进说明

## Reference release / 参考版本

- Repository: `https://github.com/Icy-Lunar/LlamaFactory`
- Commit: `8c392ce83c6410a1679099e1799e02d7d150c3b7`
- The full trainer source and deployment ZIP remain outside this repository. This Plugin stores only the capability adapter and immutable source reference.

- 仓库：`https://github.com/Icy-Lunar/LlamaFactory`
- 提交：`8c392ce83c6410a1679099e1799e02d7d150c3b7`
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

### 2026-11-05 startup preview / 启动修复预览

CI follow-up removes the stale KT inference selector, restores Python license
headers and formatting, fixes pytest's source-script import path, and prevents
fork previews from publishing upstream PyPI/Docker packages. Legacy integration
tests now prepare local fixtures; the full trainer matrix is rerunning.
The passing focused tests below do not replace that gate. Source merge/review
and real hardware acceptance remain separate from Plugin package CI.

CI 后续修复删除失效 KT 推理字段检查、补齐许可证与格式、修复 pytest 的脚本导入路径，
并阻止 fork 预览向上游 PyPI/Docker 发布。遗留集成测试已改为先准备本地文件，完整
测试矩阵重新运行中；下列专项测试通过不能替代该门禁。源码合并/评审、实机验收与
Plugin 包 CI 分开记录。

- Start the training child before TensorBoard; optional dashboard failure cannot prevent training.
- Move Liger probes out of WebUI callbacks. Apply sequential preprocessing and conservative DataLoader settings only to ROCm/WSL; CUDA/XPU dependency selection is unchanged.
- Persist session/child/lifecycle logs with heartbeat, startup stack dumps, bounded early-stage timeout, localized status and safe stop/reconnect handling.
- Fix missing saved-config `NoneType.items` and JSON configuration reading used by this Plugin's `compile` output.
- Local focused trainer suite: 245 passed, 1 skipped. Real offline tiny-model CPU smoke passed for both YAML and JSON through weight loading, LoRA, training, validation and checkpoint save. Real AMD/WSL, CUDA and XPU acceptance for this revision: `NOT_RUN`; hosted CI is separate.
- The remote overnight hang has no proven single root cause yet; WSL multiprocessing changes are preventive hardening, not evidence of hardware acceptance.

- 先创建训练子进程，再启动 TensorBoard；可选看板失败不阻止训练。
- Liger 检测不再阻塞 WebUI。仅 ROCm/WSL 使用单进程预处理与保守 DataLoader 设置，CUDA/XPU 依赖选择不变。
- 补全会话、子进程、生命周期日志、心跳、启动堆栈、早期超时、本地化状态与停止/重连处理。
- 修复空配置 `NoneType.items`，以及本 Plugin `compile` 输出的 JSON 训练配置读取错误。
- 本地训练器专项测试 245 通过、1 跳过；YAML/JSON 真实小模型 CPU 权重加载、LoRA、训练、验证和保存均通过。本修订版的 AMD/WSL、CUDA、XPU 实机验收为 `NOT_RUN`，hosted CI 单独记录。
- 远程整夜等待尚未确定唯一根因；WSL 多进程设置是预防性加固，不是硬件验收证据。

The reference trainer passed its focused WebUI, installer, packaging, Liger, and backend-selection tests before publication. Real GPU acceptance remains environment-specific and must be recorded separately for each CUDA, XPU, or ROCm host.

参考训练器在发布前已通过 WebUI、安装器、打包、Liger 与后端选择专项测试。真实 GPU 验收仍与具体环境有关，必须针对每台 CUDA、XPU 或 ROCm 主机单独记录。
