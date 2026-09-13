# Plugin Capability Contracts / 插件能力契约

This directory is the canonical source for capability payload contracts shared
by official Cyrene Plugins and their Product consumers. It owns message shapes,
stable capability and method identifiers, generated language projections, and
owner-scoped conformance tests.

本目录是 Cyrene 官方插件及其 Product 消费者共享能力载荷契约的唯一权威来源。这里负责消息结构、
稳定的能力与方法标识、各语言投影和按 owner 划分的一致性测试。

## Boundary / 边界

- Products exchange capability payloads directly with the selected Plugin
  endpoint. Platform does not proxy, parse, transform, or persist these bytes.
- Platform owns generic installation, resolution, admission, lifecycle,
  health, permission, lease/fence, and endpoint descriptor contracts.
- Product lifecycle and state contracts stay in their Product repositories.
- A contract is added here only when it has an identified owner and consumer.
  A private, single-implementation protocol stays with that implementation.

- Product 通过选定的插件端点直接交换能力载荷；Platform 不代理、解析、转换或持久化这些字节。
- Platform 只拥有通用安装、发现、准入、生命周期、健康、权限、Lease/Fence 和端点描述协议。
- Product 生命周期与状态契约保留在对应 Product 仓库。
- 只有明确 owner 和消费者的共享契约进入本目录；单实现私有协议留在实现目录。

## Layout / 目录

| Path | Responsibility / 职责 |
| --- | --- |
| `capabilities.yaml` | Machine-readable contract owner and version index / 契约 owner 与版本索引 |
| `legacy-spi-disposition.yaml` | One-time mapping or removal evidence for the retired Platform named SPI / 已退役 Platform 具名 SPI 的迁移或删除证据 |
| `proto/` | Canonical protobuf payload schemas / 规范 protobuf 载荷 schema |
| `json/` | Canonical JSON payload schemas for capability methods that carry bounded opaque vendor facts / 承载有界不透明厂商事实的规范 JSON 载荷 schema |
| `rust/` | Generated Rust projections and stable identifiers / Rust 投影与稳定标识 |
| `tck/` | Cross-language contract conformance / 跨语言契约一致性测试 |
| `VERSIONING.md` | Compatibility and release rules / 兼容性与发布规则 |

`cyrene.plugin.runtime.v1.DirectPluginRuntime` is the standard direct transport
for one configured Plugin process. It is owned here with the Plugin SDK. It is
not a Platform proxy: the Product opens the returned endpoint and exchanges
opaque typed bytes with that Plugin process directly.

`cyrene.plugin.runtime.v1.DirectPluginRuntime` 是单个已配置插件进程的标准直连传输，
由本仓库和 Plugin SDK 共同维护。它不是 Platform 代理；Product 打开控制面返回的
endpoint，与对应插件进程直接交换不透明的类型化字节。

Owner-scoped JSON Schema contracts stay beside the implementation and are
indexed here. `legacy-spi-disposition.yaml` records why each former Platform
named SPI was either replaced by a real owner contract or removed without
being copied.

Owner 范围 JSON Schema 与实现同目录维护，并在本目录建立索引。
`legacy-spi-disposition.yaml` 记录每个原 Platform 具名 SPI 被真实 owner 契约替代或
不复制直接删除的原因。

The protobuf package and type URL are stable across an authority move. Platform
compatibility copies are read-only migration artifacts and must be removed once
their consumers use this source.

JSON capability methods use the same deterministic type URL shape:
`type.cyrene.io/{capability_id}.{method}.{request|response}`. The message
connector `respond_request` and `inbound_request` schemas are the first shared
JSON payloads; their vendor request identity is intentionally opaque to the
Product policy layer.

协议迁移不得改变 protobuf package 或 type URL。Platform 中的兼容副本只读，并在消费者切换后删除。
