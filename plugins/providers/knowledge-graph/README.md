# SQLite Knowledge Graph Provider

This package implements the stateful `knowledge.graph.v1` capability using a
private SQLite database. It supports tenant and namespace scoped node/edge
upserts, reads, search, bounded neighborhood expansion, and atomic batch
import/export. Pagination tokens are HMAC-signed with a key stored in the
private graph database and bind tenant, namespace, page size, and search
filters. Expansion has independent node, edge, and serialized response-byte
limits; a quota breach returns a typed error instead of a partial graph. The
host remains responsible for authorization and source policy. | 本插件提供租户与命名空间隔离的图存储；分页令牌由私有数据库密钥签名并绑定查询范围，图展开分别限制节点、边和响应字节，超限返回类型化错误。

## Configuration

`database_path` is an absolute path to the private database file. Optional
limits bound batch writes, pages, namespace size, and graph expansion. State is
retained when the plugin is uninstalled.

## Development

```bash
uv run --project . pytest
uv run --project . ruff check src tests
```

The tests exercise storage behavior directly and through the canonical
`cyrene-knowledge-graph-contracts` SDK once that SDK is available in the
workspace lock.

---

## 中文翻译

# SQLite 知识图谱提供方

本包使用独立 SQLite 数据库实现有状态的 `knowledge.graph.v1` 能力，支持按租户与命名空间隔离的节点／边写入、读取、搜索、限界邻域展开及原子批量导入导出。授权与来源策略仍由宿主负责。

## 配置

`database_path` 是数据库文件的绝对路径。可选限制项用于限制批量写入、分页、命名空间规模、图展开节点／边和响应大小。卸载插件时保留数据。

## 开发

```bash
uv run --project . pytest
uv run --project . ruff check src tests
```
