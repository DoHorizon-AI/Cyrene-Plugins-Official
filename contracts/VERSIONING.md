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
