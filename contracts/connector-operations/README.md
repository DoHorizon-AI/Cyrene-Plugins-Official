# Connector Operations / 连接器操作清单

`v1/connector-operations.json` lists every operation Platform can grant to a connector, with what Exchange and Navigator say about it. It is generated from those three repositories at the exact commits in `sources`.

**Nothing reads this file yet.** It changes no behavior. Later work is meant to load it in place of the constants it was generated from.

| Field | Comes from |
| --- | --- |
| `operationId`, `providerIds`, `roles` | Platform `connector_grant.rs` and its operator policy |
| `exchangeToolIds` | Exchange `_TOOL_IDS_BY_PROVIDER`; empty when Exchange has no mapping |
| `mutating` | Exchange `_MUTATING_OPERATIONS` and Navigator's operation table, which must agree; `null` when neither states it |
| `navigatorToolId`, `resourceProjection` | Navigator's operation table; `null` when the operation is not in it |
| `scopeLockBranchField`, `profileIsOuterClaim` | Navigator `tool-providers.ts` |

`exchangeOnlyOperations` lists operations Exchange maps but Platform cannot grant, so a connector grant cannot reach them. They are recorded, not endorsed.

Regenerate from clean checkouts. The command refuses to write if the sources disagree:

```bash
python3 tooling/connector_operations/sources.py generate --platform PATH --exchange PATH --navigator PATH
```

CI runs `check` against the pinned commits, so it proves the file matches those commits, not the current heads of the other repositories.

---
<!-- Chinese Translation / 中文翻译 -->

# 连接器操作清单

`v1/connector-operations.json` 列出 Platform 能授权给连接器的每个操作，以及 Exchange 和 Navigator 对它的记录。它从这三个仓库 `sources` 中记录的精确提交生成。

**目前还没有代码读取这个文件**，它不改变任何行为。后续工作会用它代替生成它的那些常量。

| 字段 | 来源 |
| --- | --- |
| `operationId`、`providerIds`、`roles` | Platform 的 `connector_grant.rs` 及其操作员策略 |
| `exchangeToolIds` | Exchange 的 `_TOOL_IDS_BY_PROVIDER`；Exchange 没有映射时为空 |
| `mutating` | Exchange 的 `_MUTATING_OPERATIONS` 和 Navigator 的操作表，两者必须一致；都没有说明时为 `null` |
| `navigatorToolId`、`resourceProjection` | Navigator 的操作表；不在表中时为 `null` |
| `scopeLockBranchField`、`profileIsOuterClaim` | Navigator 的 `tool-providers.ts` |

`exchangeOnlyOperations` 列出 Exchange 有映射、但 Platform 不能授权的操作，连接器授权到不了它们。这里只是记录，不代表认可。

从干净的检出重新生成。来源之间有分歧时，命令会拒绝写入：

```bash
python3 tooling/connector_operations/sources.py generate --platform PATH --exchange PATH --navigator PATH
```

CI 针对固定的提交运行 `check`，所以它证明的是文件和这些提交一致，不是和其他仓库的最新代码一致。
