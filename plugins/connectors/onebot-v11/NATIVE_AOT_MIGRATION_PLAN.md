# OneBot v11 Native AOT migration plan / OneBot v11 Native AOT 迁移计划

本计划把 Python 原型迁移、跨语言等价、Native AOT 发布和真实环境验收拆成
可独立审计的工作流。它是 `0.3.0` 正式候选包的实施计划，不改变现有公共
protobuf、type URL 或 Product/Platform/Plugins ownership。

## 1. Scope and decision / 范围与决策

### In scope / 本次范围

- 将 `cyrene.connectors.onebot-v11` 的正式运行入口切换为 C# Native AOT。
- 保持 `message.connector.v1`、`qq.client.v1`、`DirectPluginRuntime` 的
  `Invoke`、`InvokeStream`、`Health`，以及所有既有 canonical schema/type URL。
- 覆盖通用 OneBot v11 的 HTTP API、Forward WebSocket、Reverse WebSocket、
  action correlation、消息/请求事件规范化、订阅、取消、超时、关闭和重连。
- 覆盖 `qqnt-direct` 的固定 `cyrene.qq.host.v1` stdio 协议、session bootstrap、
  generation、取消、重启/熔断、事件和固定 QQ operation registry。
- 生成 `linux-x64` 与 `linux-arm64` 的通用 OneBot 包；只生成 `linux-x64` 的
  `qqnt-direct` 包投影。
- 保留 Python `0.2.0` 作为行为参考和独立回滚制品，不在正式包提供双运行时 fallback。

### Out of scope / 明确不做

- 不重写 QQ 原生客户端或引入第三方 QQ 客户端源码。
- 不把 OneBot action 扩展成 Product session、回复策略、人格、持久化或命令系统。
- 不修改公共 protobuf、type URL、canonical message schema 来迁就实现。
- 不把 fake Host 或离线 OneBot peer 结果记为真实 QQ smoke；真实凭据测试单独登记。
- 不以 JDK 25 作为本次主实现。若未来选择 JVM 原生化，另行评估 GraalVM Native Image。

## 2. Stable architecture / 稳定架构

| 层 | 归属 | 职责 | 禁止事项 |
| --- | --- | --- | --- |
| Product | Product | workflow、reply/session policy、持久化 | 不拥有 QQ Host/session |
| Platform | Platform | install/resolve/authorize/supervise、binding endpoint | 不中转 connector payload |
| Plugin Host | Plugins | DirectPluginRuntime、profile routing、生命周期 | 不动态加载程序集 |
| OneBot Core/Transport | Plugins | mapping、HTTP/WS、bounded queues、errors | 不猜测跨 binding 目标 |
| QQ Host bridge | Plugins | inherited stdio、fixed operations、generation | 不接受 raw service/method |
| External QQ Host | 授权运行时 | QQ 原生能力与真实账号状态 | 不由本次迁移重写 |

代码布局固定为：

- `runtime/dotnet-native-aot/Cyrene.OneBot.V11.Core/`：配置、映射、传输、QQ Host
  边界、订阅和错误模型；
- `runtime/dotnet-native-aot/Cyrene.OneBot.V11.Host/`：Native AOT 入口和 gRPC 服务；
- `runtime/dotnet-native-aot/Cyrene.OneBot.V11.Tests/`：C# 行为、协议、进程和 golden 测试；
- `plugins/connectors/onebot-v11/`：清单、schema、包构建、回滚制品和行为证据。

所有 capability/method/operation 均使用显式 registry；JSON 使用 source-generated
metadata；正式 Host 不使用 runtime reflection、dynamic assembly loading 或 dynamic
code generation。

## 3. Compatibility contract / 兼容性合同

### Public identity / 公共身份

| 项目 | 固定值 |
| --- | --- |
| plugin ID | `cyrene.connectors.onebot-v11` |
| formal version | `0.3.0` |
| generic capability | `message.connector.v1` |
| direct capability | `qq.client.v1` |
| runtime protocol | `cyrene.plugin.runtime.v1.DirectPluginRuntime` |
| package runtime | `native-executable` |
| rollback | Python `0.2.0`, 独立制品 |

### Platform matrix / 平台矩阵

| RID | generic OneBot | `qqnt-direct` | 说明 |
| --- | --- | --- | --- |
| `linux-x64` | PASS target | PASS target | fake Host + authorized real smoke |
| `linux-arm64` | PASS target | NOT_SUPPORTED | QQ Host ABI 仍限定 Linux x86_64 |
| `win-x64` | NOT_DELIVERED | NOT_SUPPORTED | 保留现有 AOT 基础工程，不交付本插件包 |

### Lifecycle and failure model / 生命周期与失败模型

通用传输必须能区分 `Created`、`Starting`、`ProcessReady`、`AccountReady`、
`Degraded`、`Recovering`、`Closed`；QQ 进程 `ProcessReady` 不代表账号已登录。

跨边界强制执行以下规则：

1. timeout/cancellation 先从 pending 表移除，再发送 best-effort cancel；
2. side-effecting action 失败后不得在重连/恢复时自动 replay；
3. binding 与 generation 不匹配的响应/事件直接丢弃或 fail closed；
4. 每个 binding/订阅拥有有界队列；队列满时关闭订阅，不无限等待；
5. unknown operation、unknown field、reserved field、敏感结果、超大 frame/JSON、
   arbitrary passthrough 全部拒绝；
6. 日志只保留经过限制和脱敏的诊断，不输出 token/password/secret/ticket/cookie。

## 4. Workstreams and gates / 工作流与门禁

### W0 — contract and reference baseline / 契约与参考基线

交付：Python 测试分类清单、canonical message/action/event golden fixtures、
QQ Host frame fixture、配置兼容表、错误/取消/重连表和 parity matrix。

入口条件：公共 proto 已冻结；Python 测试可在锁定依赖下运行。

退出门禁：每个行为项都有 C# 测试、package smoke、或明确的外部环境证据；没有
证据的项必须标记 `NOT_PROVEN`，不得用“CI 通过”替代。

### W1 — AOT host spike / AOT 最小闭环

验证 `Health`、一个 unary invoke、一个 stream invoke、JSON source generation、
fake OneBot peer 和 fake QQ Host stdio framing。

退出门禁：每个目标 RID `dotnet publish` 成功；AOT analyzer 0 warning；从源码目录
外的临时目录启动；ready announcement、h2c gRPC 和 shutdown 均可重复。

### W2 — generic OneBot / 通用 OneBot

按以下顺序实现和验收：配置互斥与 schema → HTTP → Forward WS → Reverse WS →
echo correlation → inbound normalization → heartbeat/reconnect/close → send/
reply/attachment/request response → stream/subscription/cancel/timeout。

退出门禁：Python generic 行为每项有等价 C# 证据；三种 transport 均有真实协议 peer
测试；多 binding 不串事件、不串 action、不串 account identity。

### W3 — QQ Host bridge / QQ Host 桥接

按以下顺序实现和验收：安装发现与 data directory → process group → hello/version/ABI
→ length-delimited frames → fixed registry/typed validation → session bootstrap/login
readiness → generation/cancel/late response → bounded recovery/circuit → canonical
message/request/event/callback → sensitive/reference/result boundary。

退出门禁：fake Host TCK 全部通过；每个 operation 只能来自显式 registry；不存在 TCP
listener、raw payload 或任意 service/method；真实 QQ 行另行记录。

### W4 — cross-language equivalence / 跨语言等价

Python 只做 reference：同一输入 fixture 比较 OneBot action、参数、DeliveryResult、
protobuf payload、事件顺序、错误 domain code 和 cancellation/deadline 结果。

当前逐项清单固定在
`plugins/connectors/onebot-v11/behavior_matrix.json`，由
`tools/verify_behavior_matrix.py` 校验 Python 测试集合、C# 方法引用和 CI 证据引用；golden
fixture 由 `tools/generate_equivalence_fixtures.py` 从 Python reference 重新生成，C#
`NativeEquivalenceTests` 在不加载 Python 的情况下消费同一份 JSON。

差异处理顺序：先确认 fixture/contract → 修 C# → 若 Python 确实错误才更新基线，并在
变更记录写明原因。等价测试不能仅通过文件 hash 或同名测试证明。

### W5 — package and CI / 包与 CI

CI 独立运行：C# OneBot tests、DirectPluginRuntime TCK、RID-specific Native AOT publish、
clean package install、manifest/descriptor/schema、无 Python 依赖、binary smoke、
package hash/source-manifest。Provider AOT job 继续保留，但不能代替 OneBot job。

退出门禁：正式 ZIP 只有原生入口、协议资料和校验信息；无 `.py`、Python lock、源码
checkout 或 SDK 本地路径；安装目录可独立启动并执行 Health/Invoke/InvokeStream。

### W6 — operations, performance, real environment / 运维、性能与真实环境

在相同硬件和配置记录 Python reference 与 C# candidate 的冷启动、Health、action/event
latency、RSS、吞吐、包体积、重连恢复时间；性能数据只用于收益判断，不降低功能/安全门禁。

真实 QQ smoke 使用授权 exact build，单独记录 build、Host ABI、platform、账号环境、
操作范围、时间、日志脱敏结果和 `PASS`/`NOT_RUN`；凭据缺失时保持 `NOT_RUN`。

## 5. Test pyramid / 测试金字塔

| 层级 | 内容 | 证据 |
| --- | --- | --- |
| Contract | buf、generated projection、type URL、schema | CI contract job |
| Unit | mapper、validator、profile、error classification | C# tests + Python reference |
| Protocol | HTTP/WS peer、QQ frame/TCK、late/duplicate/cancel | 独立 fake peer/Host |
| Process | AOT binary、stdio、process group、listener guard | 临时安装目录 evidence |
| Package | clean install、RID、hash、无 Python | immutable package job |
| Real | authorized exact QQ smoke | 单独的受保护环境记录 |

当前历史附件写的是 `112 passed`，但现有 Python 工作树的参数化收集数可能随测试
演进变化；因此实施时以当前命令的收集结果和逐项 matrix 为准，不硬编码旧计数。

## 6. Delivery sequence / 交付顺序

全程使用一个 feature branch，切片之间采用 Conventional Commit；每次推送后创建/更新普通 PR，
合并回 `Develop` 后重新 checkout feature branch 再继续。切片建议如下：

1. `generic-parity-hardening`：通用 profile、HTTP/WS 错误分类、bounded subscription、
   C# parity tests；
2. `qq-host-parity`：QQ Host registry/validator/process/generation 等价覆盖；
3. `equivalence-and-matrix`：golden 扩展、parity matrix gate、差分证据；
4. `package-operations`：RID package、clean install、性能/运行手册和 rollback；
5. canonical `Develop` CI 全绿后，普通 PR 合并到 `main`，再做远端 SHA/ancestor/clean
   worktree read-back。

任一切片的 CI 启动失败、凭据缺失或 hosted log 不可见都必须标记为阻塞证据，不得记为
代码 PASS；真实 QQ smoke 未运行不影响 fake 协议测试，但不能被其替代。

## 7. Final acceptance / 最终验收

- [ ] Python reference 当前行为项逐项有 C# 等价证据，历史 112 项不能以数量代替语义覆盖。
- [ ] `linux-x64`、`linux-arm64` generic Native AOT 包可在 clean directory 安装、启动、Health 和 Invoke。
- [ ] `qqnt-direct` x64 fake Host 完成启动、登录 readiness、固定 operation、事件、取消、恢复和关闭全流程。
- [ ] DirectPluginRuntime unary/stream、订阅、取消、超时、关闭、错误和 bounded queue 语义通过。
- [ ] 正式包完全不依赖 Python；Python `0.2.0` 仅作为独立可回滚制品存在。
- [ ] manifest/descriptor、schema、source-manifest、hash/provenance 和 CI 均通过。
- [ ] 完成普通 PR、正常合并、canonical `Develop`/`main` read-back；目标 SHA 是远端实际 SHA。
- [ ] 真实 QQ smoke 单独记录 `PASS` 或 `NOT_RUN`，不以模拟测试冒充真实运行时证明。
