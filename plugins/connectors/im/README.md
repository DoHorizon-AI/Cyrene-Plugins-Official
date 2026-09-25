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

## QQ 边界

- QQ_API_PLAN.md：capability 和操作计划。
- QQ_SIDE_INTERFACES.md：QQ 侧服务清单。
- QQNT_DIRECT_API_MATRIX.md：固定映射矩阵。
- QQNT_DIRECT_PROTOCOL.md：Host 协议。
- QQNT_DIRECT_REAL_SMOKE.md：受保护的 smoke 门禁。

本 package 不包含复制而来的 QQ 客户端 runtime、账号数据、凭据或动态操作分派。
