# Knowledge retrieval provider package

This package verifies signed host tool calls, resolves one fixed ArtifactRef,
and ranks only chunks allowed for server-mapped principals. It uses the public
tool-provider and integration-support SDKs. | 此包验签宿主调用、解析固定 ArtifactRef，并只对服务端映射主体授权的分块排序；使用公开工具及集成支持 SDK。

| File | Responsibility |
| --- | --- |
| `settings.py` | Validates caller trust, principal mapping, bundle registry, and bounds. |
| `bundle.py` | Verifies bundle identity, checksums, records, ACL, and BM25 pages. |
| `plugin.py` | Verifies the signed envelope and implements `knowledge.search`. |

Suggested order: `settings.py` → `bundle.py` → `plugin.py`.
