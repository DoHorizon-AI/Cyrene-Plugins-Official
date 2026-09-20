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
