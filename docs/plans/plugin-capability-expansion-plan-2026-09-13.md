# Cyrene Plugins 能力扩展计划 / Cyrene Plugins Capability Expansion Plan

> **版本 / Version**: v1.1.0（v1.0 基线见文末修订记录）
>
> **创建 / Created**: 2026-09-13 (America/New_York)
>
> **基线 / Baseline**: `Cyrene-Plugins-Official@develop`；v1.0 阶段落于 `1b8255a` 之后
>
> **状态 / Status**: `ACTIVE` — Wave 0 / 1 / 2 已落地到本地 `develop`（证据见各 Wave 的 Evidence 块）；Wave 3 前置决策已固定；W2-3 晋升与 W1-4 解禁均为 owner 决策。
>
> **范围 / Scope**: 仅本仓库。Platform 与 Product 仓库不变更；不新增架构层。

## 1. 目标与边界 / Goal and Boundary

本轮不铺新架构，只做两类事：

1. **索引与可见性**：把"本仓库实际有哪些 capability、谁实现、验证到什么程度、TCK 状态"变成机器可读、CI 可防漂移的索引。
2. **在已稳定 capability 上补实现**：优先补契约已就绪但实现未跟上的能力；新能力先定契约再实现。

明确不做 / Non-goals:

- 不复活 `TrainingBackend`、`Quantization`、`Notification`、`Storage`、`GatewayFilter`
  （依据 `contracts/legacy-spi-disposition.yaml` 的 `removed_no_capability_consumer`）。
- 不为没有指名消费者或指名契约的能力先造抽象。
- 不改 Platform 的安装、解析、生命周期、endpoint 边界。
- 不重跑无关历史审计；不修改 Platform / Product 仓库。

## 2. 已验证现状 / Verified Baseline (2026-09-13 观察)

| Capability | Contract | Implementation | Plugin manifest | Consumer evidence（informational） | TCK |
| --- | --- | --- | --- | --- | --- |
| `message.connector.v1` | proto + json | `plugins/connectors/onebot-v11` | yes | 无公开消费者（observed_at=2026-09-13） | `contracts/tck/message-connector-v1` + Rust TCK |
| `model.provider.v1` | proto (v1 + v2) | `runtime/dotnet-native-aot` (OpenAI, Anthropic) | none | Navigator 文档：not connected（observed_at=2026-09-13） | `contracts/tck/model-provider-v1` + Rust TCK |
| `agent.runtime.v1` | proto | `runtime/rust/cyrene-plugin-server` | none | 未见已接通消费者（observed_at=2026-09-13） | Rust TCK |
| `memory.provider.v1` | proto | `runtime/rust/cyrene-plugin-server` | none | Navigator 文档：not connected（observed_at=2026-09-13） | Rust TCK |
| `computer.runtime.v1` | proto（含 `list_dir`） | `plugins/tools/computer-runtime` | yes（maturity: migrating，待 owner） | 未发现（observed_at=2026-09-13） | `contracts/tck/computer-runtime-v1` + Rust TCK + packaged acceptance |
| `tool.provider.v1` | proto（v1） | `runtime/rust/cyrene-mcp-provider`（DISPATCH_VERIFIED / SIMULATED；未接外部 MCP server 与消费者） | none | 无（observed_at=2026-09-13） | `contracts/tck/tool-provider-v1` + Rust TCK + provider/server 测试 |
| `model.analyzer.v1` | owner-scoped | `plugins/models/hf-model-analyzer` | yes | Yield（observed_at=2026-09-13） | 插件 tests |
| `compatibility.evaluator.v1` | owner-scoped | `plugins/policy/compat-rules` | yes | Yield（observed_at=2026-09-13） | 插件 tests |
| `evaluation.runner.v1` | owner-scoped | `plugins/evaluation/exact-match` | yes | Echo（`Cyrene-Services/Cyrene-Echo@603511c`）已通过 `EvaluationExecutionPort` 直连消费（observed_at=2026-09-13） | 插件 tests |
| `dataset.preparation.v1` | owner-scoped | `plugins/tools/dataset-preparation` | yes | Catalyst（observed_at=2026-09-13） | 插件 tests |
| `tool.dataset.validator.v1` | owner-scoped | `plugins/tools/dataset-validator` | yes | Yield（observed_at=2026-09-13） | 插件 tests |

> **Consumer evidence 规则**：上表 Consumer 列 `evidence_scope: informational`、`observed_at=2026-09-13`，
> 仅用于本仓库规划参考。它不是 Plugins 单仓的 canonical truth，catalog 与 CI **不得**读取、固化或
> 将其解释为跨仓当前状态；跨仓状态以对应 Product 仓库为准。

已知缺口 / Known gaps（本计划的工作来源）:

1. `contracts/capabilities.yaml` 已生成，但尚未携带验证层级（见 §3，Wave 3 的 W3-0a 补齐）。
2. `legacy-spi-disposition.yaml` 中 `environment.builder.v1` / `execution.engine.v1` 的 owner 不在本公开导出中（标记 `not_present_in_this_export`，保持）。
3. `agent.runtime.v1` / `memory.provider.v1` / `model.provider.v1` 有实现但无发布清单，无法从清单发现（已由 runtime registry 记录）。
4. manifest schema 目前没有校验消费者（Wave 3 的 W3-0b 补齐）。
5. 语言投影 TCK 对 `model.provider.v1` v2 七字段的覆盖不齐（Wave 3 的 W3-0c 补齐）。
6. 命名平面不一致（contract `list_dir` vs dispatch `ListDir`；C# `model.v1` vs 契约 id）——Wave 6 处理。

## 3. 验证层级与执行模式 / Verification Levels and Execution Mode

能力是否"可用"由两个正交轴表达，二者与 `maturity` 分离：

**Verification level（证据等级，必须由 authored 证据支撑）**

| Level | 含义 |
| --- | --- |
| `DECLARED` | 仅被 manifest / runtime registry 声明存在，无验证证据。 |
| `CONTRACT_VERIFIED` | 契约本身通过 schema / lint / 契约 round-trip 检查。 |
| `IMPLEMENTATION_VERIFIED` | 实现级测试（unit / component）通过；不包含真实 host 分派。 |
| `DISPATCH_VERIFIED` | 真实 host 进程成功分派该 capability 方法。 |
| `INTEGRATION_VERIFIED` | 真实 host + 真实消费者路径（或 managed runtime 全链路）在受控环境跑通。 |
| `LIVE_VERIFIED` | 生产形态环境 + 真实外部依赖被观测。 |

**Execution mode（证据如何产生）**：`REAL` / `SIMULATED` / `MOCK`。

规则 / Rules:

- 每一级都必须有 authored 证据（日期、环境、命令、结果）；**不得**由测试文件或代码存在自动推断。
- `INTEGRATION_VERIFIED` / `LIVE_VERIFIED` 要求 `execution_mode = REAL`。
- 任何 capability **不得**因 unit test PASS 自动推断为 `supported` / `recommended`。
- `maturity`（声明的生命周期状态，来自 manifest）与 `verification_level`（证据等级）是两个轴；
  catalog 同时展示，但互不推断，也不做"最高等级"汇总宣传。
- 证据写入 `contracts/capability-verification.json`（只存证据，不复制 manifest 数据，不成为 capability authority）；
  catalog 生成器校验其引用的 capability / implementation 必须存在，并强制上述不变量。

## 4. 执行与勾选规则 / Execution Rules

- 所有条目初始为 `[ ]`。
- `[x]` 仅当：实现落盘 **且** 本仓库对应 gate 通过（source manifest verify + 相关语言测试）**且** Evidence 行写好。
- 每完成一项：用 `tools/ci/update_source_manifest.py` 刷新 `source-manifest.json`，
  运行 `python3 tools/ci/verify_source_manifest.py --root . --allow-git-metadata`，再勾选。
- **Protected surface 规则**：`source-manifest.json` 的 protected files 机制保留；接受 protected
  digest 变更必须显式使用 `--accept-protected-changes`，且工具必须打印 old → new digest 差异，
  使 protected surface 的变化在 CI / review 中可见，不与普通源码变化等价。
- **Write-scope guard（禁止跨仓写入）**：每轮开始前在 `Cyrene-Workspace` 运行
  `python3 scripts/write_scope_guard.py --snapshot`，提交后用
  `--verify --allow Cyrene-Plugins-Official` 复核。Platform 或任一 Service 仓库出现新的
  worktree 变化或 HEAD 移动即视为越界，必须先还原再继续（负测试已覆盖）。
- 本地提交在 `develop` 分支，逐项原子提交；进度一律在本文档维护。

## 5. Wave 0 — 索引与可见性 / Index and Visibility（已完成）

- [x] W0-1 `tools/ci/update_source_manifest.py`：新增/修改文件后刷新 manifest 条目；protected files 变更需显式 flag。
- [x] W0-2 `contracts/runtime-implementations.json`：登记无发布清单的运行时实现（Rust native host、.NET providers），含 declared-without-contract 列表。
- [x] W0-3 `tools/ci/capability_catalog.py`（生成 + `--check`）与 `contracts/capabilities.yaml`（提交产物）。
- [x] W0-4 CI 接入：`public-ci.yml` source-hygiene 增加 catalog drift check；ruff 文件列表加入新脚本。
- [x] W0-5 `legacy-spi-disposition.yaml` 标注 `not_present_in_this_export`；`contracts/README.md` layout 表对齐。
- [x] W0-6 README `Published surface` 增补 catalog 与 plans 目录。

Evidence / 证据:

- `python3 tools/ci/capability_catalog.py --root . --check` → `CAPABILITY_CATALOG: PASS capabilities=10 implementations=11`。
- drift 注入后 `--check` → `FAIL`（exit 2），恢复后 PASS；`update_source_manifest.py` 对 protected file 的防改测试同为 exit 2。
- `verify_source_manifest.py` → `SOURCE_MANIFEST: PASS`；`check_public_repository.py` → `PUBLIC_HYGIENE: PASS`。
- `uvx ruff check --no-cache` 对新脚本 → `All checks passed`。

## 6. Wave 1 — Model Provider 完成度 / Provider Completeness

- [x] W1-1 Anthropic 真 SSE streaming：wire `stream: true`，逐事件 yield，携带 usage / finish_reason；加测试。
- [x] W1-2 OpenAI `chat_completion_v2`：tools / tool_choice / parallel_tool_calls / tool_call delta / usage；Native AOT 序列化上下文更新；fixtures 测试。
- [x] W1-3 Anthropic v2：tools 声明与 `tool_result` 消息块（含 system 提升为顶层字段、`input_json_delta` 流式分片）。
- [ ] W1-4 Provider host 按 interface version 分发 v1 / v2（**保持 DEFER**）。

**W1-4 stop gate（v1.1 固定）**：在真实 Provider host / binding / DirectPluginRuntime dispatch 接通前，
OpenAI / Anthropic `chat_completion_v2` 的 `verification_level` 最多为 `IMPLEMENTATION_VERIFIED`
（`execution_mode = SIMULATED`），**不得**标记 `INTEGRATION_VERIFIED` / `LIVE_VERIFIED` / production-supported。
不为了补齐表格制造无消费者的 proto↔C# abstraction。

Evidence / 证据:

- W1-1：`dotnet test runtime/dotnet-native-aot/Cyrene.Provider.Tests --configuration Release` → `Passed: 15, Failed: 0`（新增 Anthropic 流式 2 例）。
- W1-1：`python3 tools/check_dotnet_aot_rules.py` → `SUCCESS`；6 个 Native AOT 工程全部 build succeeded。
- W1-2/W1-3：`dotnet test ... --configuration Release` → `Passed: 19, Failed: 0`；新增 4 个 v2 测试（OpenAI round-trip、OpenAI streaming tool deltas、Anthropic round-trip、Anthropic streaming tool_use 分片）。
- W1-2/W1-3：能力模型扩展 mirrored proto：`ChatMessage.ToolCallId/ToolCalls`、`ChatCompletionParameters.Tools/ToolChoice/ParallelToolCalls/IncludeUsage`、`ChatCompletionChunk.ToolCalls`、`ChatCompletionResult.ToolCalls/TotalTokens`。

## 7. Wave 2 — Computer Runtime 正式化 / Formalization

- [x] W2-1 computer runtime `list_dir` 全链路补齐：plugin server 增加 `ListDir` 分派（此前仅 crate 与 proto 已声明）→ manifest 声明 → 集成测试。
- [x] W2-2 建立 `contracts/tck/computer-runtime-v1/`：dotnet 投影 TCK + Rust 契约 TCK + host 集成测试构成覆盖（无 Java/Python 投影，因此不建对应 runner）。
- [x] W2-3 packaged-runtime acceptance（**晋升 supported 的前置**，v1.1 扩写）：
  - install / start：以打包二进制按 manifest launch 启动并完成 health 握手（真实安装/放置属 Platform launcher 职责，本仓库不伪造）
  - direct dispatch（经 DirectPluginRuntime gRPC，而非直接函数调用）
  - `ExecuteCommand` / `ListDir` / path traversal denial / environment filtering / artifact roundtrip
  - cancellation（客户端断开 stream → server 取消并回收进程组）
  - timeout（`CommandTimeout` + 进程组回收）
  - descendant process cleanup（后台孙进程在超时与取消后均被回收）
  - **Linux 全项通过**；Windows 待验收环境，记为缺口（测试以 `#![cfg(unix)]` 显式限定）
  - **`migrating → supported` 仍由 owner 决定**；acceptance 通过不自动改变 maturity。
- [x] W2-4 Browser 决策记录：不扩展 `computer.runtime.v1`，未来按独立 `browser.runtime.v1` 处理（见决策记录）。

Evidence / 证据:

- W2-1：`cargo test --manifest-path runtime/rust/cyrene-plugin-server/Cargo.toml` → `7 passed`（含 `test_w2_list_dir_roundtrip_and_traversal_denied`）；`cargo clippy --all-targets -- -D warnings` 与 `cargo fmt --check` 通过。
- W2-1：`plugins/tools/computer-runtime/plugin.manifest.json` 声明 `ListDir`；catalog 重新生成后 implementation 方法与契约方法对齐（大小写差异保留可见，Wave 6 处理）。
- W2-3：`cargo test --manifest-path runtime/rust/cyrene-plugin-server/Cargo.toml` → 14 passed（10 集成 + 4 packaged acceptance）；acceptance 直接启动打包二进制、经 gRPC 驱动 10 项清单，超时与取消均验证后台孙进程被回收（`/proc` 轮询）。
- W2-3 附带修复：stream 客户端断开（`tx.closed()` / send 失败）现在会置取消标志并回收受管命令的进程组，此前会留下孤儿进程；断连检测依赖客户端连接驱动正常运行（单线程测试 runtime 中阻塞等待曾掩盖该检测，测试已改为全异步等待）。
- W2-2：`dotnet run --project contracts/tck/computer-runtime-v1/dotnet/ComputerRuntimeContractTck.csproj` → `computer.runtime.v1 generated C# payload TCK: PASS`。
- W2-2：catalog TCK 来源扩展为 `contracts/tck/*` + `contracts/rust/.../tests/*_tck.rs`。

## 8. Wave 3 — Gate Hardening + Tool Provider / MCP

### 8.1 前置决策（v1.1 固定，不再讨论）

**A. Tool identity**：canonical identity = `(binding_id, provider_tool_id)`；`display_name` 不作为全局 identity。
如果模型协议需要平面名称，由 Agent Runtime 做 deterministic projection 并保留 inverse map。

**B. Dynamic tool list**：采用 per-AgentRun snapshot semantics。运行开始时取得 `ToolCatalogSnapshot`，
当前 run 中工具集合不自动变化；未来如需 refresh 必须显式增加，不是 v1 默认行为。

**C. stdio MCP process supervision**：MCP provider 不得演变为 generic process supervisor。
生产路径复用现有 managed execution / Computer Runtime / Platform lifecycle primitive；
如必须提供 local-dev launcher，明确标记 `LOCAL_DEV` / non-production。

**D. MCP v1 scope**：只实现 `tools/list` 与 `tools/call`；`resources` 与 `prompts` 全部 defer，
避免提前进入 memory / context / prompt ownership 问题。

### 8.2 条目

- [x] W3-0a Gate hardening：capability catalog 增加 `verification_level` + `execution_mode`（§3），
  新增 `contracts/capability-verification.json`（authored 证据），生成器执行不变量校验。
- [x] W3-0b Gate hardening：manifest schema validation 前移（原 W5-4）。使用 pinned CI/dev validator
  （不引入生产 runtime 依赖）；**catalog 的 manifest 输入必须先通过 schema validation，再参与 catalog generation**。
- [x] W3-0c1 Gate hardening：contract authority invariant（§12）落地。Rust 契约 TCK 已覆盖 v2 七字段；
  C# 与 Python 投影 TCK 补齐同一组 round-trip 断言并执行通过。
- [ ] W3-0c2 Gate hardening：JVM/Kotlin 投影 v2 七字段检查（本环境无 gradle，未执行；不写无法运行的测试）。
- [x] W3-0d Gate hardening：protected surface 可见性（`--accept-protected-changes` 时打印 old → new digest；CI/review 可见）。
- [x] W3-1 `tool.provider.v1` 契约：proto 载荷、method id、interface version、TCK 骨架。
- [x] W3-2 MCP stdio provider：`tools/list` / `tools/call` / schema 转换 / 超时 / 取消 / 类型化错误（按 §8.1 A-D）。
- [ ] W3-3 MCP HTTP/SSE transport（**owner 同意 defer 至 Wave 6**：尚无远程 MCP server 需求，先不扩大传输与验证面；出现首个远程场景时启动，见 W6-5）。
- [x] W3-4 agent runtime 接入真实 `ToolProvider` 主机路径 + TCK。

Evidence / 证据:

- W3-0a：新增 `contracts/capability-verification.json`（11 条记录：8 条带证据、3 条 DECLARED）；`capability_catalog.py --check` → `PASS capabilities=10 implementations=11`；catalog 每个 implementation 现携带 `verification_level` / `execution_mode` / `verified_at`。
- W3-0a 负测试：`LIVE_VERIFIED + SIMULATED` → `FAIL (exit 2)`；删除一条证据记录 → `FAIL: missing records`（exit 2）；恢复后 PASS。
- W3-0b：`python3 tools/ci/validate_manifests.py --root .` → `MANIFEST_SCHEMA: PASS manifests=7`；`jsonschema==4.23.0` 为 pinned CI/dev 依赖，catalog 生成前强制校验（生产 runtime 无新依赖）。
- W3-0c1：Rust 既有 `structured_chat_v2_round_trip_preserves_tools_history_and_usage` 覆盖七字段；C# TCK `dotnet run --project contracts/tck/model-provider-v1/dotnet/ModelProviderContractTck.csproj` → `PASS`；Python TCK `bash contracts/tck/model-provider-v1/generate-bindings.sh` → `model.provider.v1 generated Python payload TCK: PASS` 且四语言生成 PASS。
- W3-0d：实测 updater `--accept-protected-changes` 输出 `SOURCE_MANIFEST_PROTECTED: plugins/connectors/onebot-v11/README.md c0c1691bcd72... -> a3c0f3345c41...`；verifier 现在每次 PASS 打印 pinned protected surface（path + sha256）。
- W3-2 修正（v1.1）：MCP provider 改为 per-binding 稳定 logical session + 注入式 `McpSessionAdapter`（默认 `StdioProcessAdapter` 仅供测试/本地）；证据：`cargo test --manifest-path runtime/rust/cyrene-mcp-provider/Cargo.toml` → 6 passed（含同一会话服务 list+多次 call、传输损坏自动重开、超时后会话存活、shutdown 释放子进程）；server 14 passed 不变。
- W3-4：agent runtime 新增 `adapter/tool_catalog.rs`：per-run `ToolCatalogSnapshot`、deterministic 平面名投影（`[A-Za-z0-9_-]`，冲突按 binding 前缀 + 序号去重）与 inverse map、`ToolCatalogSource` trait、`SnapshotToolProvider`（快照外工具 fail closed，不触达 provider）；turn loop 现在把 run 的 tool declarations 广告给模型。证据：`cargo test --manifest-path runtime/rust/cyrene-agent-runtime/Cargo.toml` → 14 passed（含新 TCK vector 6 在 native loop 与 rig adapter 上运行、投影/截断/广告断言）；`cargo test --manifest-path runtime/rust/cyrene-plugin-server/Cargo.toml` → 10 passed（含真实 agent loop × MCP provider 端到端）；clippy/fmt 干净。
- W3-2：新增 crate `runtime/rust/cyrene-mcp-provider`（每操作一次短生命周期 MCP session；`kill_on_drop`；快照强制；`catalog_version` 为 sha256 摘要）；plugin server 增加 `with_mcp_servers` 与 `tool.provider.v1` 分派（未配置 bindings 时 fail closed）。证据：`cargo test --manifest-path runtime/rust/cyrene-mcp-provider/Cargo.toml` → `6 passed`（含超时回收与取消杀子进程）；`cargo test --manifest-path runtime/rust/cyrene-plugin-server/Cargo.toml` → `9 passed`（含 dispatch 与 fail-closed）；fmt/clippy 干净；catalog → `capabilities=11 implementations=12`，tool.provider.v1 记 `DISPATCH_VERIFIED / SIMULATED`。
- W3-1：新增 `contracts/proto/cyrene/tool/provider/v1/tool_provider.proto` + Rust 投影/标识 + `contracts/tck/tool-provider-v1/`（含 `tool_provider_contract_tck.rs`）；buf 1.45.0（与 CI 同版本）`lint` / `format --diff --exit-code` / `build` 全通过；`cargo test --manifest-path contracts/rust/cyrene-plugin-contracts/Cargo.toml` → 3 个 tool_provider 契约测试通过，fmt/clippy 干净；catalog → `capabilities=11`，`tool.provider.v1` 以 contract-only 形式出现。

## 9. Wave 4 — Evaluation Pack

优先实现顺序（固定）：

- [ ] W4-1 regex / contains
- [ ] W4-2 JSON structural equality
- [ ] W4-3 JSON Schema validation
- [ ] W4-4 numeric tolerance
- [ ] W4-5 pairwise comparison
- [ ] W4-6 LLM judge（使用 `model.provider.v1`）

约束：evaluator 契约/TCK 不得因其他方向（如 WeCom）推迟；Echo 已是 `evaluation.runner.v1` 的
真实消费方（见 §2），新 evaluator 的 `verification_level` 按 §3 逐条记录。

Evidence / 证据:

- （待填）

## 10. Wave 5 — Connector Ecosystem

- [ ] W5-1 WeCom connector：`message.connector.v1` 实现，复用 `contracts/tck/message-connector-v1` 模板。
  如 WeCom 优先级更高，可与 Wave 4 并行，但不得推迟 evaluator contract/TCK。
- [ ] W5-2 候选：Discord / Telegram connector。

Evidence / 证据:

- （待填）

## 11. Wave 6 — Provider / Retrieval Backlog（有消费者或决策后启动）

- [ ] W6-1 `rerank.v1` 契约 + 首个实现（Cohere / Jina / Voyage / 本地 reranker）。
- [ ] W6-2 Gemini native provider（`model.provider.v1` 的又一实现，无新契约）。
- [ ] W6-3 capability 命名统一，含 `speech.provider.v1` / `rerank.provider.v1` 无契约声明的处理，以及 contract/dispatch 大小写差异。
- [ ] W6-4 memory hybrid search（运行时特性，非新插件）。
- [ ] W6-5 MCP HTTP/SSE transport（原 W3-3；首个远程 MCP server 场景出现时启动）。

Evidence / 证据:

- （待填）

## 12. 保持不变的决策 / Standing Decisions

- Browser 不进入 `computer.runtime.v1`；未来独立 `browser.runtime.v1`。
- `TrainingBackend` / `Quantization` / `Notification` / `Storage` / `GatewayFilter` 不复活。
- `environment.builder.v1` / `execution.engine.v1` 在当前 public export 无实现时，继续明确标记
  `not_present_in_this_export` / `not_published`，不创建假插件。
- capability catalog 必须由真实 manifest / runtime registry / TCK / verification evidence 投影生成，
  不得手工成为新的 authority。
- `runtime-implementations.json` 仅服务于"没有 package manifest 的运行时实现"，不复制 manifest 已拥有
  的数据，不演化为第三套 capability authority。
- `contracts/proto/**` 是唯一 canonical external wire contract；各语言模型是机械投影（W3-0c 检查）。

## 13. 决策记录 / Decision Log

| 日期 | 决策 | 依据 |
| --- | --- | --- |
| 2026-09-13 | catalog 输出使用 README 已承诺的 `contracts/capabilities.yaml`，不引入第三个文件名 | `contracts/README.md` layout |
| 2026-09-13 | 运行时实现注册表用 JSON（仓库无 PyYAML 依赖，工具保持 stdlib-only） | 仓库现有依赖面 |
| 2026-09-13 | W0 不包含 manifest schema 全量校验（需引入依赖，移入 Wave 3 的 W3-0b） | 保持 Wave 0 零新依赖 |
| 2026-09-13 | catalog 的 implementation 侧记录实际 dispatch 名，contract 侧保留 proto 标识符原文；差异可见 | 不掩盖命名平面差异，Wave 6 处理 |
| 2026-09-13 | W1-4 暂缓：provider host 为 fail-closed stub，先写 proto↔C# 转换层是无消费者抽象 | 与删除 TrainingBackend / Quantization 同一判据 |
| 2026-09-13 | Browser 不进入 `computer.runtime.v1`：信任模型与生命周期不同；未来独立 capability | `computer_runtime.proto` 不主张抗恶意代码隔离 |
| 2026-09-13 (v1.1) | 验证层级与 execution_mode 作为独立轴引入；禁止由测试存在推断等级；INTEGRATION/LIVE 要求 REAL | 避免"代码存在 = 能力可用" |
| 2026-09-13 (v1.1) | W2-3 增加 packaged-runtime acceptance 作为晋升前置；晋升由 owner 决定 | 构建/测试通过不等于生产就绪 |
| 2026-09-13 (v1.1) | MCP v1 只做 `tools/list` + `tools/call`；identity = `(binding_id, provider_tool_id)`；per-run snapshot；不承担进程监管 | 避免提前进入 memory/context/prompt ownership |
| 2026-09-13 (v1.1) | manifest schema validation 前移为 Wave 3 gate；使用 pinned dev/CI validator | catalog 输入必须先通过 schema 校验 |
| 2026-09-13 (v1.1) | 后续优先级：Wave 3 MCP → Wave 4 Evaluation Pack → Wave 5 Connector → Wave 6 Provider/Retrieval Backlog | owner 排序 |
| 2026-09-13 (v1.1) | 新 capability 的 dispatch method 使用契约标识符原文（`tool.provider.v1` 为 `list_tools` / `call_tool`）；legacy PascalCase 分支保留到 Wave 6 命名统一 | 避免制造新的不一致 |
| 2026-09-13 (v1.1) | W3-3 移入 W6-5：stdio 已覆盖本地/agent 路径，无远程 MCP 需求，暂不扩大传输面 | owner 同意 |
| 2026-09-13 (v1.1) | 平面工具名字母表取 `[A-Za-z0-9_-]`（各厂商 tool-name 语法的交集）；点号/斜杠/空格折叠为 `_`，冲突按 binding 前缀与序号确定性去重 | 模型侧名称约束 + 决策 A |
| 2026-09-13 (v1.1) | MCP stdio 使用 **per-binding 稳定 logical session**：同一 binding 的 `tools/list` 与后续 `tools/call` 共用同一 MCP 会话（此前每次操作新开）；session 生命周期由注入的 `McpSessionAdapter` 管理，provider 不承担进程监管；传输损坏时丢弃会话并在下次操作重开；超时不再杀会话 | owner 前置要求（Wave 4 之前） |

## 14. v1.0 → v1.1 修订记录 / Revision Notes

1. §3 新增验证层级与 execution_mode 定义；catalog 输出与 `capability-verification.json` 纳入 Wave 3 的 W3-0a。
2. W1-4 保持 DEFER，新增 stop gate 措辞。
3. W2-3 扩写为 packaged-runtime acceptance 清单；`migrating → supported` 明确为 owner 决策。
4. Wave 3 前置决策 A-D 固定为文内 §8.1，不再作为开放问题。
5. manifest schema validation 由原 W5-4 前移到 W3-0b。
6. 新增 W3-0c 契约权威不变量与 `model.provider.v1` v2 七字段投影检查。
7. §2 consumer evidence 列标注 `observed_at` 与 `evidence_scope: informational`；更新 Echo 行（已建仓并直连消费 `evaluation.runner.v1`）；明确 catalog/CI 不消费该列。
8. Wave 重排并同步 ID：原 Wave 4（Connector）→ Wave 5；新增 Wave 4 = Evaluation Pack（W4-1..W4-6）；原 Wave 5（Backlog）→ Wave 6，其中原 W5-2（evaluator pack）迁入 Wave 4、原 W5-4（manifest schema）迁入 W3-0b，其余映射为 W6-1..W6-4。原条目均未执行、无证据，重编号不影响已发布检查。
9. §12 固化既有决策清单。
10. §4 增加 protected surface 可见性规则（W3-0d 落实）。
