# Capability Contract Versioning / 能力契约版本规则

## Compatible change / 兼容变更

- Preserve protobuf package names, message names, field numbers, field types,
  enum numeric values, and published type URLs.
- Add fields with new numbers and optional or repeated presence semantics.
- Keep older readers functional; unknown fields must remain ignorable.
- Run the owner TCK and generation checks before publishing.

- 保留 protobuf package、消息名、字段号、字段类型、枚举数值与已发布 type URL。
- 新字段使用新编号，并采用 optional 或 repeated 的兼容存在语义。
- 旧 reader 必须继续工作，未知字段必须可忽略。
- 发布前运行 owner 对应 TCK 与生成检查。

## Breaking change / 破坏性变更

- Create a new protobuf package version and capability interface version.
- Reserve every removed field number and name; never reuse either.
- Publish an explicit compatibility and removal policy before migrating a
  consumer.

- 创建新的 protobuf package 版本和能力接口版本。
- 删除字段时同时 `reserved` 原字段号与字段名，禁止复用。
- 迁移消费者前先发布明确的兼容与移除策略。

## Ownership and release / 所有权与发布

`contracts/capabilities.yaml` names the owning Plugin area and TCK. Shared
contracts are released from this repository. Products consume a versioned
artifact or generated source from the same contract release; they do not read a
mutable Platform checkout. Normal Plugins builds also require no Platform
checkout.

`contracts/capabilities.yaml` 标明 owner 与 TCK。共享契约随本仓库发布；Product 使用同一契约发布的
版本化制品或生成源码，不读取可变的 Platform checkout，Plugins 的普通构建也不依赖 Platform checkout。
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# 能力契约版本规则

## 兼容变更

- 保留 protobuf package 名称、消息名称、字段编号、字段类型、枚举数值以及已发布的 type URL。
- 新增字段时使用新编号，并采用 optional 或 repeated 的存在语义。
- 必须保证旧 reader 继续工作；未知字段必须可以忽略。
- 发布前运行 owner 对应的 TCK 和生成检查。

## 破坏性变更

- 创建新的 protobuf package 版本和 capability interface 版本。
- 删除字段时，将字段编号和字段名称都标记为 reserved；两者都不能重新使用。
- 迁移消费者前，先发布明确的兼容和删除策略。

## 所有权与发布

contracts/capabilities.yaml 会列出 owner 所属的 Plugin 区域和 TCK。共享契约从本仓库发布。Product 使用同一契约版本发布的制品或生成源码；它们不读取可变的 Platform checkout。Plugins 的常规构建也不需要 Platform checkout。
