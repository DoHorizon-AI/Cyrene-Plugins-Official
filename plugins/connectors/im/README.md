# Cyrene IM Connector / Cyrene IM 连接器

`cyrene.connectors.im` owns the QQ/IM side of the connector boundary. It
implements `message.connector.v1` and `qq.client.v1`, and delegates the
version-specific QQ integration to an authorized `cyrene.qq.host.v1` child
through inherited stdio.

`cyrene-im` is the formal .NET 10 Native AOT runtime. The Python package under
`src/qq_connector` is retained only as a behavioral reference until the
cross-language parity and packaged Host TCK gates are complete; it is not the
formal release entrypoint.

`qqnt-direct` is currently Linux x86_64 only. Real QQ API smoke evidence stays
`NOT_RUN` until the exact authorized client build, Host ABI, account, and
protected environment are supplied. Offline fake-Host TCK and deterministic
parity evidence are implementation evidence, not proof of vendor compatibility.

`cyrene.connectors.onebot-v11` is the separate generic OneBot v11 connector and
does not own QQ operations.

The QQ Connector UI name does not change the package ID (`cyrene.connectors.im`),
the `qq.client.v1` or `message.connector.v1` protocol IDs, or the separate
OneBot compatibility entrance. QR login is available only for a configured
binding whose operator has explicitly confirmed a dedicated account. Health
reports `HEALTHY` only after the active Host generation passes
`qq.login.self_status` for that account. The QR payload is opaque text intended
for local QR rendering; it expires within ten minutes and must not be logged.

`qq-connector-package` can stage an original Tencent installer only when a
trusted `cyrene.qq.package-lock.v1` lock supplies the exact Tencent HTTPS URL,
version, filename, and SHA-256. The example lock is deliberately empty. Staging
verifies and caches the original bytes, then atomically changes the active cache
pointer. Rollback changes that pointer only. The command does not redistribute,
extract, install, or execute the package, and staging never marks a QQ Host
ready. Package staging targets are Linux x64, Linux ARM64, and Windows x64;
the configured QQ Host runtime target remains Linux x64, and all real QQ Host
readiness remains `NOT_CONFIGURED` until an authorized artifact is configured.
The exact real API source/build gate is documented in
[`QQNT_DIRECT_REAL_SMOKE.md`](QQNT_DIRECT_REAL_SMOKE.md).

## QQ boundary / QQ 边界

- [`QQ_API_PLAN.md`](QQ_API_PLAN.md) — capability and operation plan
- [`QQ_SIDE_INTERFACES.md`](QQ_SIDE_INTERFACES.md) — QQ-side service inventory
- [`QQNT_DIRECT_API_MATRIX.md`](QQNT_DIRECT_API_MATRIX.md) — fixed mapping matrix
- [`QQNT_DIRECT_PROTOCOL.md`](QQNT_DIRECT_PROTOCOL.md) — Host protocol
- [`QQNT_DIRECT_REAL_SMOKE.md`](QQNT_DIRECT_REAL_SMOKE.md) — protected smoke gate

The package contains no copied QQ client runtime, account data, credentials, or
dynamic operation dispatch.
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# Cyrene IM Connector

cyrene.connectors.im 拥有 QQ/IM 连接器边界这一侧的实现。它实现 message.connector.v1 和 qq.client.v1，并通过继承的 stdio 将特定版本的 QQ 集成委派给经过授权的 cyrene.qq.host.v1 子进程。

cyrene-im 是正式的 .NET 10 Native AOT runtime。src/qq_connector 下的 Python package 仅作为行为参考保留，直到跨语言一致性和打包 Host TCK 门禁完成；它不是正式发布入口。

qqnt-direct 当前仅支持 Linux x86_64。只有在提供了精确授权的客户端构建、Host ABI、账号和受保护环境后，真实 QQ API smoke 证据才会从 NOT_RUN 更新。离线 fake-Host TCK 和确定性一致性证据属于实现证据，不能证明与厂商实现兼容。

cyrene.connectors.onebot-v11 是独立的通用 OneBot v11 connector，不负责 QQ 操作。

QQ Connector 的 UI 名称不会改变 package ID（cyrene.connectors.im）、qq.client.v1 或 message.connector.v1 协议 ID，也不会改变独立的 OneBot 兼容入口。只有操作人员明确确认专用账号的已配置 binding 才能发起 QR 登录。只有活动 Host 代次对该账号通过 qq.login.self_status 后，健康状态才会报告为 HEALTHY。QR payload 是供本地渲染二维码使用的不透明文本，十分钟内过期，并且不得写入日志。

只有当可信的 cyrene.qq.package-lock.v1 lock 提供确切的腾讯 HTTPS URL、版本、文件名和 SHA-256 时，qq-connector-package 才会暂存腾讯原始安装包。示例 lock 有意保持为空。暂存会校验并缓存原始字节，再原子切换活动缓存指针；回滚也只切换该指针。命令不会再分发、解包、安装或执行原包，暂存也不会将 QQ Host 标记为就绪。安装包暂存目标为 Linux x64、Linux ARM64 和 Windows x64；已配置 QQ Host runtime 仍只针对 Linux x64，且在配置授权制品前，所有真实 QQ Host 就绪状态均为 NOT_CONFIGURED。真实 QQ API 精确来源与 build 门禁见 QQNT_DIRECT_REAL_SMOKE.md。

## QQ 边界

- QQ_API_PLAN.md：capability 和操作计划。
- QQ_SIDE_INTERFACES.md：QQ 侧服务清单。
- QQNT_DIRECT_API_MATRIX.md：固定映射矩阵。
- QQNT_DIRECT_PROTOCOL.md：Host 协议。
- QQNT_DIRECT_REAL_SMOKE.md：受保护的 smoke 门禁。

本 package 不包含复制而来的 QQ 客户端 runtime、账号数据、凭据或动态操作分派。
