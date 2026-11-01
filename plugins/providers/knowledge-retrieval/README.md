# Knowledge Bundle Retrieval Provider

This `tool.provider.v1` plugin accepts only `librarySelector`, `query`,
`limit`, and an optional provider-issued `chunkCursor` as model arguments. It
rejects caller-supplied principal references, artifact paths, package digests,
and purpose values. The host wraps those model arguments in an
`INTEGRATION_SUPPORT_SDK` signed caller context. The provider verifies the
signature, binding, issuer, subject, organization, workspace, method, operation,
expiry, and exact argument digest before resolving the subject through its
server-side principal mapping.

Each selector resolves through a fixed server configuration to one `ArtifactRef`
and one relative file under `artifact_root`. The provider verifies the exact
package digest, optional manifest digest, all archive members and checksums,
source/chunk lineage, and stored policy before ranking. BM25 scores only chunks
whose `allowKnowledge`, `allowedUsePurposes`, and `allowedPrincipalRefs` match
the mapped principals and fixed `knowledge_retrieval` purpose.

The static mapping is an identity-to-principal mapping; it does not provide
immediate revocation by itself. Navigator must recheck current library/source
authorization before signing each search call and recheck returned hit sources
before injecting results into model context or sending a reply. Calls without a
valid signature, mapping, registry entry, or current host authorization fail
closed. | 插件不以静态映射声称即时撤权；Navigator 必须在每次调用和结果注入／回复发送前实时核验来源权限。

## Tool input

The fixed tool ID, signed context method, and operation ID are all
`knowledge.search`. Model input contains `librarySelector`, `query`, and
`limit`; `chunkCursor` is accepted only when returned by this provider and is
bound to the caller identity, library, query, principals, and page size.

## Development

```bash
uv run --project . pytest
uv run --project . ruff check src tests
```

---

## 中文翻译

# 知识包检索提供方

此 `tool.provider.v1` 插件的模型参数只有 `librarySelector`、`query`、`limit`，以及可选的插件签发 `chunkCursor`。它拒绝调用方传入主体引用、制品路径、包摘要和用途。宿主将模型参数放入 `INTEGRATION_SUPPORT_SDK` 签名的 caller context。插件会在解析服务端主体映射前验证签名、binding、issuer、subject、组织、工作区、method、operation、有效期和参数摘要。

每个 selector 都由服务端固定配置映射到一个 `ArtifactRef` 和 `artifact_root` 下的相对文件。插件会校验包摘要、可选清单摘要、全部压缩包成员与校验和、来源／分块 lineage 及包内策略；只有 `allowKnowledge`、`allowedUsePurposes`、`allowedPrincipalRefs` 同主体映射及固定的 `knowledge_retrieval` 用途匹配后，才计算 BM25。

静态映射只负责把已验证身份映射为主体引用，本身不保证即时撤权。Navigator 必须在每次签名搜索调用前核验当前 library/source 授权，并在把命中加入模型上下文或发送回复前重新核验命中来源。签名、主体映射、注册表或当前宿主授权缺失时均失败关闭。

## 工具输入

固定工具 ID、签名 context method 和 operation ID 均为 `knowledge.search`。模型输入包含 `librarySelector`、`query` 和 `limit`；`chunkCursor` 只接受插件返回的值，并绑定调用者身份、library、查询、主体集合和页大小。

## 开发

```bash
uv run --project . pytest
uv run --project . ruff check src tests
```
