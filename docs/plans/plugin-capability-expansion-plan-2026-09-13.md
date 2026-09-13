# Cyrene Plugins 能力扩展计划 / Cyrene Plugins Capability Expansion Plan

> **版本 / Version**: v1.0.0
>
> **创建 / Created**: 2026-09-13 (America/New_York)
>
> **基线 / Baseline**: `Cyrene-Plugins-Official@develop 1b8255a`
>
> **状态 / Status**: `ACTIVE`
>
> **范围 / Scope**: 仅本仓库。Platform 与 Product 仓库不变更；不新增架构层。

## 1. 目标与边界 / Goal and Boundary

本轮不铺新架构，只做两类事：

1. **索引与可见性**：把"本仓库实际有哪些 capability、谁实现、成熟度、TCK 状态"变成机器可读、CI 可防漂移的索引。
2. **在已稳定 capability 上补实现**：优先补契约已就绪但实现未跟上的能力；新能力先定契约再实现。

明确不做 / Non-goals:

- 不复活 `TrainingBackend`、`Quantization`、`Notification`、`Storage`、`GatewayFilter`
  （依据 `contracts/legacy-spi-disposition.yaml` 的 `removed_no_capability_consumer`）。
- 不为没有指名消费者或指名契约的能力先造抽象。
- 不改 Platform 的安装、解析、生命周期、endpoint 边界。

## 2. 已验证现状 / Verified Baseline (2026-09-13, develop@1b8255a)

| Capability | Contract | Implementation | Plugin manifest | Consumer evidence | TCK |
| --- | --- | --- | --- | --- | --- |
| `message.connector.v1` | proto + json | `plugins/connectors/onebot-v11` | yes | 无公开消费者 | `contracts/tck/message-connector-v1` |
| `model.provider.v1` | proto (v1 + v2) | `runtime/dotnet-native-aot` (OpenAI, Anthropic) | none | Navigator 文档：not connected | `contracts/tck/model-provider-v1` |
| `agent.runtime.v1` | proto | `runtime/rust/cyrene-plugin-server` | none | Platform/Navigator 未接通 | crate TCK（Mock） |
| `memory.provider.v1` | proto | `runtime/rust/cyrene-plugin-server` | none | Navigator 文档：not connected | crate tests |
| `computer.runtime.v1` | proto（含 `list_dir`） | `plugins/tools/computer-runtime` | yes（maturity: migrating；缺 `ListDir`） | 未发现 | 无 |
| `model.analyzer.v1` | owner-scoped | `plugins/models/hf-model-analyzer` | yes | Yield | 插件 tests |
| `compatibility.evaluator.v1` | owner-scoped | `plugins/policy/compat-rules` | yes | Yield | 插件 tests |
| `evaluation.runner.v1` | owner-scoped | `plugins/evaluation/exact-match` | yes | Echo 未建仓 | 插件 tests |
| `dataset.preparation.v1` | owner-scoped | `plugins/tools/dataset-preparation` | yes | Catalyst | 插件 tests |
| `tool.dataset.validator.v1` | owner-scoped | `plugins/tools/dataset-validator` | yes | Yield | 插件 tests |

已知缺口 / Known gaps（本计划的工作来源）:

1. `contracts/capabilities.yaml` 缺失，而 `contracts/README.md` 已承诺该路径。
2. `legacy-spi-disposition.yaml` 中 `environment.builder.v1` / `execution.engine.v1` 的 owner 不在本公开导出中。
3. `agent.runtime.v1` / `memory.provider.v1` / `model.provider.v1` 有实现但无发布清单，无法从清单发现。
4. TCK 只覆盖 2 个 capability。
5. Anthropic `StreamChatAsync` 为假流式（完整请求后一次性 yield）；OpenAI 未实现 `chat_completion_v2` tools。
6. 命名平面不一致（C# `model.v1` / `speech.provider.v1` / `rerank.provider.v1` vs 契约 id）。
7. `manifests/plugin.manifest.schema.json` 当前没有任何校验消费者。

## 3. 执行与勾选规则 / Execution Rules

- 所有条目初始为 `[ ]`。
- `[x]` 仅当：实现落盘 **且** 本仓库对应 gate 通过（source manifest verify + 相关语言测试）**且** Evidence 行写好。
- 每完成一项：用 `tools/ci/update_source_manifest.py` 刷新 `source-manifest.json`，
  运行 `python3 tools/ci/verify_source_manifest.py --root . --allow-git-metadata`，再勾选。
- 本地提交在 `develop` 分支，逐项原子提交。
- 进度一律在本文档维护，不另建 ledger。

## 4. Wave 0 — 索引与可见性 / Index and Visibility

- [x] W0-1 `tools/ci/update_source_manifest.py`：新增/修改文件后刷新 manifest 条目；protected files 变更需显式 flag。
- [x] W0-2 `contracts/runtime-implementations.json`：登记无发布清单的运行时实现（Rust native host、.NET providers），含 declared-without-contract 列表。
- [x] W0-3 `tools/ci/capability_catalog.py`（生成 + `--check`）与 `contracts/capabilities.yaml`（提交产物）。
- [x] W0-4 CI 接入：`public-ci.yml` source-hygiene 增加 catalog drift check；ruff 文件列表加入新脚本。
- [x] W0-5 `legacy-spi-disposition.yaml` 标注 `not_present_in_this_export`；`contracts/README.md` layout 表对齐。
- [x] W0-6 README `Published surface` 增补 catalog 与 plans 目录。

Evidence / 证据:

- `python3 tools/ci/capability_catalog.py --root . --check` → `CAPABILITY_CATALOG: PASS capabilities=10 implementations=11`。
- 在 catalog 中注入一行后 `--check` → `FAIL`（exit 2），恢复后 PASS；`tools/ci/update_source_manifest.py` 对 protected file 的防改测试同样为 exit 2。
- `python3 tools/ci/verify_source_manifest.py --root . --allow-git-metadata` → `SOURCE_MANIFEST: PASS`；`python3 tools/ci/check_public_repository.py` → `PUBLIC_HYGIENE: PASS`。
- `uvx ruff check --no-cache` 对新增 `tools/ci/capability_catalog.py`、`tools/ci/update_source_manifest.py` → `All checks passed`。

## 5. Wave 1 — Model Provider 完成度 / Provider Completeness

- [x] W1-1 Anthropic 真 SSE streaming：wire `stream: true`，逐事件 yield，携带 usage / finish_reason；加测试。
- [x] W1-2 OpenAI `chat_completion_v2`：tools / tool_choice / parallel_tool_calls / tool_call delta / usage；Native AOT 序列化上下文更新；fixtures 测试。
- [x] W1-3 Anthropic v2：tools 声明与 `tool_result` 消息块（含 system 提升为顶层字段、`input_json_delta` 流式分片）。
- [ ] W1-4 Provider host 按 interface version 分发 v1 / v2（**暂缓**：本导出中的 .NET provider host 为未配置 stub，直接写 proto↔模型转换层会产生无消费者抽象；等 host/binding 注入落地后启动，见决策记录）。

Evidence / 证据:

- W1-1：`dotnet test runtime/dotnet-native-aot/Cyrene.Provider.Tests --configuration Release` → `Passed: 15, Failed: 0`（含新增 `Test_W1_AnthropicStreaming_YieldsOrderedDeltasAndUsage`、`Test_W1_AnthropicStreaming_ErrorEventFailsClosed`）。
- W1-1：`python3 tools/check_dotnet_aot_rules.py` → `SUCCESS`；6 个 Native AOT 工程全部 build succeeded。
- W1-2/W1-3：`dotnet test ... --configuration Release` → `Passed: 19, Failed: 0`；新增 4 个 v2 测试（OpenAI round-trip、OpenAI streaming tool deltas、Anthropic round-trip、Anthropic streaming tool_use 分片）。
- W1-2/W1-3：能力模型扩展 mirrored proto：`ChatMessage.ToolCallId/ToolCalls`、`ChatCompletionParameters.Tools/ToolChoice/ParallelToolCalls/IncludeUsage`、`ChatCompletionChunk.ToolCalls`、`ChatCompletionResult.ToolCalls/TotalTokens`。

## 6. Wave 2 — Computer Runtime 正式化 / Formalization

- [x] W2-1 computer runtime `list_dir` 全链路补齐：plugin server 增加 `ListDir` 分派（此前仅 crate 与 proto 已声明）→ manifest 声明 → 集成测试。
- [x] W2-2 建立 `contracts/tck/computer-runtime-v1/`：dotnet 投影 TCK + Rust 契约 TCK + host 集成测试构成覆盖（无 Java/Python 投影，因此不建对应 runner）。
- [ ] W2-3 `maturity: migrating` → `supported`（证据已备；是否宣称生产就绪属 **owner 决策**，不由实现方单方面提升）。
- [x] W2-4 Browser 决策记录：不扩展 `computer.runtime.v1`，未来按独立 `browser.runtime.v1` 处理（见决策记录）。

Evidence / 证据:

- W2-1：`cargo test --manifest-path runtime/rust/cyrene-plugin-server/Cargo.toml` → `7 passed`（含新增 `test_w2_list_dir_roundtrip_and_traversal_denied`，验证列表与路径逃逸拒绝）；`cargo clippy --all-targets -- -D warnings` 与 `cargo fmt --check` 通过。
- W2-1：`plugins/tools/computer-runtime/plugin.manifest.json` 声明 `ListDir`；catalog 重新生成后 implementation 方法与契约方法一致（`list_dir` ↔ `ListDir` 大小写差异继续保留可见，W5-5 处理）。
- W2-2：`dotnet run --project contracts/tck/computer-runtime-v1/dotnet/ComputerRuntimeContractTck.csproj` → `computer.runtime.v1 generated C# payload TCK: PASS`。
- W2-2：catalog TCK 来源扩展为 `contracts/tck/*` + `contracts/rust/.../tests/*_tck.rs`；agent/computer/memory/message/model 的既有 Rust 契约 TCK 现在都可见（此前只识别 2 个跨语言套件）。

## 7. Wave 3 — Tool Provider / MCP

- [ ] W3-1 `tool.provider.v1` 契约：proto 载荷、method id、interface version、TCK 骨架。
- [ ] W3-2 MCP stdio provider：list_tools / call_tool / schema 转换 / 超时 / 取消 / 类型化错误 / 工具命名空间策略。
- [ ] W3-3 MCP HTTP/SSE transport。
- [ ] W3-4 agent runtime 接入真实 `ToolProvider` 主机路径 + TCK。

前置决策（实现前需记录）: 多 server 工具命名冲突策略、工具列表动态变化的 snapshot 语义、stdio 子进程监管归属、MCP resources/prompts 是否进入范围。

Evidence / 证据:

- （待填）

## 8. Wave 4 — Connector 生态 / Connector Ecosystem

- [ ] W4-1 WeCom connector：`message.connector.v1` 实现，复用 `contracts/tck/message-connector-v1` 模板。
- [ ] W4-2 候选：Discord / Telegram connector。

Evidence / 证据:

- （待填）

## 9. Wave 5 — Backlog（有消费者或决策后启动）

- [ ] W5-1 `rerank.v1` 契约 + 首个实现（Cohere / Jina / Voyage / 本地 reranker）。
- [ ] W5-2 evaluator pack：regex / JSON structural / JSON Schema / numeric tolerance / pairwise → LLM judge（前置：Echo 消费者）。
- [ ] W5-3 Gemini native provider（`model.provider.v1` 的又一实现，无新契约）。
- [ ] W5-4 manifest schema 校验 gate（需先决定 jsonschema 依赖）。
- [ ] W5-5 capability 命名统一，含 `speech.provider.v1` / `rerank.provider.v1` 无契约声明的处理。
- [ ] W5-6 memory hybrid search（运行时特性，非新插件）。

Evidence / 证据:

- （待填）

## 10. 决策记录 / Decision Log

| 日期 | 决策 | 依据 |
| --- | --- | --- |
| 2026-09-13 | catalog 输出使用 README 已承诺的 `contracts/capabilities.yaml`，不引入第三个文件名 | `contracts/README.md` layout |
| 2026-09-13 | 运行时实现注册表用 JSON（仓库无 PyYAML 依赖，工具保持 stdlib-only） | 仓库现有依赖面 |
| 2026-09-13 | W0 不包含 manifest schema 全量校验（需引入依赖，移入 W5-4） | 保持 Wave 0 零新依赖 |
| 2026-09-13 | catalog 的 implementation 侧记录实际 dispatch 名（Rust host 为 PascalCase），contract 侧保留 proto 标识符原文；二者差异在索引中直接可见 | 不掩盖命名平面差异，交由 W5-5 处理 |
| 2026-09-13 | Browser 不进入 `computer.runtime.v1`：浏览器引入网络与渲染攻击面、真实后端未在公开 CI 固定，与有界主机执行的信任模型不同；未来按独立 `browser.runtime.v1` capability 设计（独立 binding / 权限 / 超时），当前保留内部 `BrowserService` 与可用性探测 | `computer_runtime.proto` 明确不主张抗恶意代码隔离 |
| 2026-09-13 | W1-4 暂缓：`Cyrene.Provider.*` 的 host 入口在当前导出中是 fail-closed stub（`DIRECT_RUNTIME_HOST_NOT_CONFIGURED`），先写 proto↔capability 转换层会造成无消费者的抽象；等 host/binding 注入路径明确后按真实需求实现 | 与删除 TrainingBackend / Quantization 的同一判据 |
