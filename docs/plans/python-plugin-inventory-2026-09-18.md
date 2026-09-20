# Python 插件盘点与后续迁移计划

## 盘点口径

盘点基于当前 `Cyrene-Plugins-Official` 工作树，统计条件是：

- 存在 `plugin.manifest.json`；
- `runtime.language` 为 `python`；
- 排除当前正在迁移的 `cyrene.connectors.onebot-v11`；
- `plugins/tools/computer-runtime` 的实现语言为 Rust，因此不计入 Python 插件。

盘点日期按迁移计划记录为 2026-09-18；代码量为 Git 跟踪的实现 Python 文件物理行数，不含测试、生成绑定和工具脚本。测试数量来自当前测试集合的实际运行结果。

## 总览

| 指标 | 结果 |
| --- | ---: |
| 其他 Python 插件 | 8 |
| 不同 capability | 6 |
| manifest 方法总数 | 12 |
| 实现代码 | 3,767 LOC |
| 已有测试文件 | 6 / 8 |
| 当前可收集测试用例 | 36 |
| 实际通过测试 | 36 |
| 有独立 live 生产烟测 | 0 |
| 有独立不可变发布制品/发布 job | 0 |
| 每插件锁定依赖文件 | 0 |

### 共同状态

8 个插件均是 Python `prepared-runtime` 形态，版本要求为 Python `>=3.11`，通过 `cyrene.plugin.runtime.v1.DirectPluginRuntime` 暴露能力。公共 CI 会对它们执行 editable 安装、Ruff 和 manifest 校验；测试 job 当前只显式收集 WeCom、Evaluator Pack、LLM Judge、Dataset Preparation、Dataset Validator 五组测试。

Exact Match、HuggingFace Analyzer、Compatibility Rules 当前没有测试目录，因此只接受 lint、manifest 和静态源码门禁，不能把它们描述为已完成行为验收。

## 插件明细

| # | ID / 名称 | 分类 / 版本 | Capability / 方法 | 入口 | 实现 / 测试 | 依赖与边界 | 当前验证、打包与优先级 |
| ---: | --- | --- | --- | --- | ---: | --- | --- |
| 1 | `cyrene.connectors.wecom`<br>Official WeCom Application Connector | connector<br>0.1.0 | `message.connector.v1`<br>`send_message` | `wecom_connector:WeComConnector` | 727 / 2 文件 / 13 用例 | `grpcio`、`protobuf`；代码使用共享 `cyrene_plugin_runtime` 的环境读取，但 `pyproject.toml` 未将其声明为直接依赖。拥有 HTTPS WeCom token 获取、缓存、刷新和应用消息发送；不提供入站回调。 | 测试含 Scripted transport 和本地 fake HTTP，不是 WeCom live；CI 有测试，setuptools data-files 带 manifest/schema；**P4**，外部 API、secret、回调边界最复杂。 |
| 2 | `cyrene.evaluation.evaluator-pack`<br>Deterministic Evaluator Pack | evaluation<br>0.1.0 | `evaluation.runner.v1`<br>`evaluate` | `evaluator_pack:EvaluatorPackPlugin` | 356 / 1 文件 / 9 用例 | 共享 runtime、`jsonschema`；contains、regex、JSON structural、JSON Schema、numeric tolerance，纯进程内确定性计算。 | 本地单元/DirectPluginRuntime 测试，CI 有测试；setuptools data-files 带 manifest 和 contract schema；**P1**，适合最早建立跨语言 golden fixtures。 |
| 3 | `cyrene.evaluation.exact-match`<br>Exact Match Evaluation Runner | evaluation<br>0.1.0 | `evaluation.runner.v1`<br>`evaluate` | `exact_match_evaluator:ExactMatchEvaluationRunner` | 199 / 1 文件 / 0 用例 | 仅共享 runtime；严格字段相等和 usage 投影，纯进程内确定性计算。 | 没有测试文件，CI 只有 lint/manifest；setuptools data-files 只带 manifest；**P1（补测后）**，实现最简单但当前行为证据不足。 |
| 4 | `cyrene.evaluation.llm-judge`<br>Model-Backed LLM Judge | evaluation<br>0.1.0 | `evaluation.runner.v1`<br>`evaluate` | `llm_judge:LLMJudgePlugin` | 538 / 1 文件 / 8 用例 | 共享 runtime、`cyrene-model-provider-contracts`；通过 `model.provider.v1` 的 loopback/Unix `model_endpoint` 调用模型，支持 pointwise 与 pairwise。 | 测试使用模型 provider double，没有真实模型 smoke；CI 有测试，setuptools data-files 带 manifest、request/response/config schema；**P3**，需要先冻结跨能力 type URL 和错误语义。 |
| 5 | `cyrene.models.hf-analyzer`<br>HuggingFace Model & VRAM Analyzer | model utility<br>0.3.0 | `model.analyzer.v1`<br>`analyze`、`validate_shards`、`compute_sha256` | `hf_model_analyzer:HfModelAnalyzer` | 401 / 1 文件 / 0 用例 | 仅共享 runtime；读取模型目录、解析模型标识、估算 VRAM、校验 shard、计算 SHA-256。manifest 声明可选 `HF_TOKEN`，当前实现未发现 live HuggingFace 网络调用。 | 没有测试文件，CI 只有 lint/manifest；setuptools data-files 只带 manifest；**P1（补测后）**，本地纯计算但文件边界必须先做 fixtures。 |
| 6 | `cyrene.policy.compat-rules`<br>Hardware, CUDA & Model Compatibility Evaluator | policy / evaluator<br>0.2.0 | `compatibility.evaluator.v1`<br>`evaluate` | `compat_rules:CompatibilityRuleEvaluator` | 531 / 1 文件 / 0 用例 | 仅共享 runtime；硬件、驱动、CUDA、serving/training、精度和内存规则均为本地 JSON 规则计算。 | 没有测试文件，CI 只有 lint/manifest；setuptools data-files 只带 manifest；**P1（补测后）**，规则迁移前必须建立 issue/verdict golden cases。 |
| 7 | `cyrene.tools.dataset-preparation`<br>Dataset Preparation | data tool<br>0.1.0 | `dataset.preparation.v1`<br>`inspect`、`prepare`、`transform` | `dataset_preparation:DatasetPreparationPlugin` | 585 / 1 文件 / 3 用例 | 共享 runtime、`duckdb`；读取和写入受监督 staging 路径，解析 JSON/JSONL/text，规范化、去重、分组切分、JSONL/Parquet 转换。 | 测试使用临时文件与本地 DuckDB，不是外部数据 live；CI 有测试，setuptools data-files 带 manifest/schema；**P2**，DuckDB 与路径安全是迁移重点。 |
| 8 | `cyrene.tools.dataset-validator`<br>Training Dataset Deep Validator | data tool<br>0.2.0 | `tool.dataset.validator.v1`<br>`validate` | `dataset_validator:DatasetValidatorPlugin` | 430 / 1 文件 / 3 用例 | 共享 runtime、`duckdb`；本地校验 JSON/JSONL/CSV/Parquet、instruction/conversation 结构和质量指标。 | 测试使用临时文件与本地 DirectPluginRuntime，不是外部数据 live；CI 有测试，setuptools data-files 带 manifest/schema；**P2**，与 preparation 共享文件格式和 DuckDB 风险。 |

## 现有 CI、包与发布状态

证据位置为 [.github/workflows/public-ci.yml](../../.github/workflows/public-ci.yml)：

- `source-hygiene` 安装 8 个 Python 包并执行 Ruff；
- 测试 job 覆盖 5 个插件、共 36 项当前测试；
- Exact Match、HF Analyzer、Compatibility Rules 没有 pytest 路径；
- 8 个插件没有单独的 Native AOT 包构建、不可变 ZIP、artifact handoff 或 live smoke job；
- release handoff 当前面向 OneBot Native AOT、OneBot Python reference/rollback 及其他运行时制品，不包含这 8 个插件的独立发布包；
- 仓库只有 OneBot 的 `requirements.lock`，其他 Python 插件仅在 `pyproject.toml` 使用版本范围，没有逐插件 lockfile。

因此，当前“Python 插件可安装”不等于“已形成可发布、可回放、可跨语言迁移的制品”。

## 建议迁移顺序

这里的优先级表示迁移实施顺序，不表示产品价值排序：

1. **P1：Exact Match、Evaluator Pack、HF Analyzer、Compatibility Rules**。先为无测试插件补齐 contract、边界错误、取消和 DirectPluginRuntime golden tests，再做 C# 纯函数/JSON 映射实现。
2. **P2：Dataset Validator、Dataset Preparation**。先冻结路径权限、文件格式、DuckDB/Parquet、digest 和错误契约，再评估 C# DuckDB 绑定或独立数据转换边界。
3. **P3：LLM Judge**。保留 `model.provider.v1`、type URL、模型 endpoint、超时、JSON 解析和逐样本失败语义，先做跨语言 fixture，再做真实 provider smoke。
4. **P4：WeCom**。补充 `cyrene-plugin-runtime` 直接依赖声明、独立锁定依赖、token/secret 边界、真实企业微信受保护 smoke，并明确是否需要 HTTP callback 入站能力。

每个插件迁移前都应先建立：源码 SHA、依赖 SBOM、已安装 artifact 测试、跨语言 golden fixtures、CI exact-SHA 记录和回滚制品。没有这些证据时，只能标记为“源码可用”或“本地测试通过”，不能标记为迁移完成。

## 统计结论

当前仓库除 OneBot 外有 **8 个 Python 插件**：6 个属于纯本地或受控本地依赖能力，2 个明确存在外部服务边界（WeCom、LLM provider），其中 Dataset 两个插件依赖 DuckDB 文件处理。真正的主要风险不是 Python 文件数量，而是测试缺口、独立发布制品缺失、WeCom 运行时依赖未显式声明，以及 LLM/WeCom 的外部边界尚未有 live 验收。
