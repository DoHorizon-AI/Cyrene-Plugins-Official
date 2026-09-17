# OneBot v11 Native AOT operations / OneBot v11 Native AOT 运维手册

This document is shipped inside the formal Native AOT package. It describes
the process boundary and operational evidence; it does not grant an external
OneBot runtime or an official QQ installation.

本文档随正式 Native AOT 包发布，说明进程边界与运维证据；它不包含外部 OneBot runtime，也
不代表已经提供官方 QQ 安装或真实账号环境。

## Package and startup / 包与启动

The package is RID-specific and has one executable entrypoint:

```text
bin/cyrene-onebot-v11
```

Supported package projections are `linux-x64` and `linux-arm64` for generic
OneBot. The `qqnt-direct` profile is only exposed on `linux-x64` because its
external QQ Host ABI is currently limited to Linux x86_64. The package does
not require Python, a .NET runtime, a source checkout, or a local SDK.

包按 RID 区分，只有一个原生入口：`bin/cyrene-onebot-v11`。通用 OneBot 支持
`linux-x64` 和 `linux-arm64`；`qqnt-direct` 因外部 QQ Host ABI 目前只支持 Linux x86_64，
仅在 `linux-x64` 投影开放。正式包不需要 Python、.NET runtime、源码 checkout 或本地 SDK。

The host listens on loopback h2c by default. The endpoint can be selected with
`--listen host:port` or `CYRENE_PLUGIN_LISTEN`; port `0` asks the OS for a free
port. The first bounded JSON line on stdout is the `direct_plugin_ready`
announcement and contains the `grpc://` `connection_ref`. Supervisors must use
that reference instead of guessing a port.

Host 默认在 loopback 上监听 h2c。可通过 `--listen host:port` 或
`CYRENE_PLUGIN_LISTEN` 指定 endpoint，端口为 `0` 时由操作系统分配空闲端口。stdout 首个受限
JSON 行是 `direct_plugin_ready` announcement，其中包含 `grpc://` `connection_ref`；监督者
必须使用该引用，不应猜测端口。

Configuration is supplied through the host-owned environment contract:

- `CYRENE_CAPABILITY_CONFIGURATION_JSON` — one validated binding configuration;
- `CYRENE_CAPABILITY_BINDING_ID` — the stable binding identity.

Do not put credentials in command-line arguments or logs. `access_token` is
only used for the configured OneBot peer and is redacted from diagnostics.

配置通过 Host 所有的环境变量合同传入：`CYRENE_CAPABILITY_CONFIGURATION_JSON` 和
`CYRENE_CAPABILITY_BINDING_ID`。不要把凭据放在命令行参数或日志中；`access_token` 只用于
配置的 OneBot peer，诊断输出会脱敏。

## Health and lifecycle / 健康与生命周期

The supervisor calls `Health` after reading `direct_plugin_ready` and after
each restart. `SERVING` means the Native AOT direct runtime is serving its
declared capability; it does not mean an external OneBot account is logged in.
For `qqnt-direct`, a started QQ Host process is only `ProcessReady`; account
readiness is established by the fixed session/login state machine.

监督者在读取 `direct_plugin_ready` 后以及每次重启后调用 `Health`。`SERVING` 只表示 Native AOT
direct runtime 正在提供声明的能力，不表示外部 OneBot 账号已经登录。`qqnt-direct` 的 QQ Host
进程启动只代表 `ProcessReady`，账号就绪必须由固定的 session/login 状态机确认。

The expected shutdown sequence is:

1. stop accepting new invokes and subscriptions;
2. cancel or complete in-flight streams;
3. close OneBot transports or the QQ Host bridge;
4. send process termination and wait for the bounded shutdown timeout;
5. use a forced kill only after the timeout, then record the exit code.

Side-effecting actions are never replayed automatically after reconnect. A
timeout or cancellation removes the pending request before attempting a
best-effort control frame. Responses from an old binding generation are
discarded.

标准关闭顺序是：停止接收新调用和订阅；结束或取消进行中的 stream；关闭 OneBot transport 或
QQ Host bridge；发送进程终止并等待有界 shutdown timeout；超时后才强制终止并记录退出码。副作用
action 在重连后不会自动 replay；timeout/cancellation 会先移除 pending，再尝试发送 best-effort
控制帧；旧 binding generation 的响应会被丢弃。

## Security boundary / 安全边界

- expose only loopback h2c and the configured reverse-WebSocket listener;
- keep each binding's data directory and generation isolated;
- reject unknown capabilities, methods, fields, raw service/method names,
  arbitrary passthrough payloads, oversized frames, and unsafe result shapes;
- do not copy an official QQ installation, account data, session files, or
  credentials into a package or an evidence artifact;
- treat fake OneBot peers and fake QQ Hosts as protocol evidence only.

- 只暴露 loopback h2c 和配置的 reverse-WebSocket listener；
- 隔离每个 binding 的数据目录与 generation；
- 拒绝未知 capability、method、field、raw service/method 名、任意透传载荷、超大 frame 和
  不安全 result 结构；
- 不得把官方 QQ 安装、账号数据、session 文件或凭据复制进包或证据 artifact；
- fake OneBot peer 与 fake QQ Host 只能作为协议证据。

## Rollback and evidence / 回滚与证据

The formal package is `cyrene.connectors.onebot-v11@0.3.0`. Python
`cyrene.connectors.onebot-v11@0.2.0` is a separately assembled rollback
artifact. Switching back requires an explicit supervisor/package decision; the
formal package does not contain a dual-runtime fallback.

正式包身份是 `cyrene.connectors.onebot-v11@0.3.0`。Python
`cyrene.connectors.onebot-v11@0.2.0` 是独立组装的回滚制品。回滚必须由监督者/包管理器显式决策，
正式包不包含双运行时 fallback。

Every release or smoke record should include the exact source revision, RID,
package and binary SHA-256, host ABI (for `qqnt-direct`), platform, operation
scope, UTC timestamp, and redaction status. `qq-real-smoke` is recorded as
`PASS` only with an authorized real QQ environment; without protected
credentials it remains `NOT_RUN`/`SKIPPED`. Fake Host, TCK, and package smoke
must never be promoted to real-environment evidence.

每条发布或 smoke 记录至少包含 exact source revision、RID、包和 binary SHA-256、`qqnt-direct`
的 Host ABI、平台、操作范围、UTC 时间和脱敏状态。`qq-real-smoke` 只有在授权的真实 QQ 环境
中才能记为 `PASS`；缺少受保护凭据时保持 `NOT_RUN`/`SKIPPED`。Fake Host、TCK 和 package
smoke 不能提升为真实环境证据。

## Performance sampling / 性能采样

The repository CI benchmark is intentionally descriptive. From a clean
installed package, run:

```bash
python3 tools/ci/benchmark_onebot_native_aot.py \
  --binary /path/to/unpacked/bin/cyrene-onebot-v11 \
  --package /path/to/cyrene-onebot-v11-native-linux-x64.zip \
  --rid linux-x64 \
  --evidence /tmp/onebot-native-performance.json
```

It records cold-start readiness, gRPC channel readiness, Health latency,
repeated local HTTP-backed `send_message` latency, sampled peak RSS, binary
size, and source revision. Compare this output with an independently installed
Python `0.2.0` rollback artifact on the same host and configuration. These
numbers inform migration benefits only; they do not weaken contract, security,
or real-QQ acceptance gates.

该基准是描述性采样。从 clean installed package 运行上面的命令即可记录 Native AOT 数据；应在
同一主机、同一配置下另行运行 Python `0.2.0` 回滚制品进行对照。数据只用于评估迁移收益，不会
降低契约、安全或真实 QQ 验收门禁。
