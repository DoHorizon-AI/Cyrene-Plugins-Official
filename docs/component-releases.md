# Workspace connection component releases

The [`component-release.yml`](../.github/workflows/component-release.yml)
workflow publishes four independently discoverable components:

| Component ID | Native package | OCI image |
| --- | --- | --- |
| `cy-workspace-relay` | Yes | Yes |
| `cy-workspace-connector` | Yes | Yes |
| `cy-workspace-sidecar` | Yes | No |
| `cy-workspace-frontend-bridge` | Yes | Yes |

## Selection and rebuild scope

Pushes select components from changed paths. A component's own Rust crate,
systemd unit, or Dockerfile selects that component. Changes under
`runtime/rust/cyrene-workspace-product-adapters/` select the Connector. The
shared client SDK, shared contracts, root Cargo manifests, and release tooling
select all four components. A manual dispatch can select one component or
`all`.

The selected set is intersected with the verified component catalog. Native
and OCI target matrices are derived from its supported targets; an unmapped
native target or unknown component fails closed.

## Release identity and assets

Each selected component gets its own immutable tag:

```text
preview-<componentId>-<40-character-source-SHA>
stable-<componentId>-<40-character-source-SHA>
```

Every component release contains its own `component-release-index-v1.json`,
the index attestation, only that component's target manifests, and the
corresponding native archives and attestations. OCI images use target-scoped
tags and are referenced from their component/target manifests by repository
and digest. No repository-wide release aggregates the four components.

The release workflow fetches catalog bytes from an immutable Workspace
catalog release and verifies the publisher workflow, source identity, channel,
schema, digest, and GitHub artifact attestation before selection or packaging.
The verifier source is checked out at a fixed commit independently of the
selected catalog release. An exact catalog release tag can be supplied through
the workflow input or channel repository variable; when absent, the verifier
discovers the latest immutable release for the matching channel. It does not
fall back to mutable branch content.

## Official Python Package Runtime releases

The [`data-tools-package-release.yml`](../.github/workflows/data-tools-package-release.yml)
workflow builds independently installable Plugin ZIPs from the explicit package
specifications in `tooling/release/official_plugin_package.py`. A push retains
the existing `legacy5` draft release. Manual dispatch and pull requests use the
following fixed selections:

| Selection | Packages | Result |
| --- | --- | --- |
| `legacy5` | Dataset Preparation, Document Parsing, Dataset Generation, Knowledge Preparation, Exact Match | Draft package release |
| `integration-v1` | Account Center, GitHub Issues, WeCom | Immutable package releases |
| `authorization-cloud-v1` | Cloud Resources, Email Notification, WeCom | Immutable package releases |
| `teams-cloud-v1` | Teams, Cloud Management, Hugging Face, Account Center, GitHub Issues | Immutable package releases |
| `office-pilot` | Microsoft Graph, Azure DevOps Observation | Immutable package releases |
| `conversation-assistant` | Model API Connector, Memory SQLite, Memory PGVector, Knowledge Graph, Knowledge Retrieval, SenseVoice Speech, Media | Immutable package releases |
| Single package | Public Web Tools or Bilibili Connector | Immutable package release |
| Pull request | All 24 unique package IDs | Build and test only; no publication |

Manual dispatch can also select one fixed package ID; legacy packages retain draft
mode, while the other package IDs publish immutable releases. The named sets are
closed allowlists. Their releases bind their ZIPs, wheelhouse,
SBOM, attestations, and immutable `preview-<componentId>-<version>-<sourceSHA>`
or `stable-<componentId>-<version>-<sourceSHA>` tags to the exact source commit.
No package selection advances a mutable latest release.

---

# Workspace 连接组件发布

[`component-release.yml`](../.github/workflows/component-release.yml) 将四个组件作为
彼此独立、可单独发现的发布项：

| Component ID | 原生包 | OCI 镜像 |
| --- | --- | --- |
| `cy-workspace-relay` | 是 | 是 |
| `cy-workspace-connector` | 是 | 是 |
| `cy-workspace-sidecar` | 是 | 否 |
| `cy-workspace-frontend-bridge` | 是 | 是 |

## 选择与重建范围

推送事件按变更路径选择组件。组件自己的 Rust crate、systemd unit 或 Dockerfile
只选择该组件。`runtime/rust/cyrene-workspace-product-adapters/` 下的变更只选择
Connector。共享 client SDK、共享 contracts、根 Cargo 清单及发布工具变更会选择全部
四个组件。手动 dispatch 可以选一个组件或 `all`。

选中集合还会与经过验证的 component catalog 交叉校验。原生和 OCI target 矩阵从其中声明
的受支持 target 生成；未知组件或未映射的原生 target 会 fail closed。

## 发布标识与资产

每个选中的组件各自发布一个不可变 tag：

```text
preview-<componentId>-<40 位源码 SHA>
stable-<componentId>-<40 位源码 SHA>
```

每个组件 release 都有自己的 `component-release-index-v1.json` 与 index attestation，
并且只包含该组件的 target manifest、对应原生归档和 attestation。OCI 镜像使用带 target
标识的 tag；组件/target manifest 通过仓库地址和 digest 引用镜像。不会把四个组件汇总到
单一仓库级 release。

发布流程从 Workspace 不可变 catalog release 获取数据，并在选择或打包前验证发布方
workflow、source identity、channel、schema、digest 和 GitHub artifact attestation。验证器
源码单独从固定 commit checkout，catalog 数据版本与工具源码版本相互独立。可通过 workflow
input 或 channel repository variable 指定精确 catalog release tag；未指定时，验证器只会
发现同 channel 的最新不可变 release，不会回退到可变分支内容。

## 官方 Python Package Runtime 发布

[`data-tools-package-release.yml`](../.github/workflows/data-tools-package-release.yml)
根据 `tooling/release/official_plugin_package.py` 中的显式 package spec 构建可独立安装的
Plugin ZIP。自动 push 保持现有 `legacy5` draft 发布。手动 dispatch 和 pull request 使用以下
固定选择：

| 选择 | Package | 结果 |
| --- | --- | --- |
| `legacy5` | Dataset Preparation、Document Parsing、Dataset Generation、Knowledge Preparation、Exact Match | Draft package release |
| `integration-v1` | Account Center、GitHub Issues、WeCom | 不可变 package release |
| `authorization-cloud-v1` | Cloud Resources、Email Notification、WeCom | 不可变 package release |
| `teams-cloud-v1` | Teams、Cloud Management、Hugging Face、Account Center、GitHub Issues | 不可变 package release |
| `office-pilot` | Microsoft Graph、Azure DevOps Observation | 不可变 package release |
| `conversation-assistant` | Model API Connector、Memory SQLite、Memory PGVector、Knowledge Graph、Knowledge Retrieval、SenseVoice Speech、Media | 不可变 package release |
| 单个 package | Public Web Tools 或 Bilibili Connector | 不可变 package release |
| Pull request | 全部 24 个唯一 package ID | 仅构建与测试，不发布 |

手动 dispatch 也可以选择一个固定 package ID；legacy package 保持 draft 模式，其他 package
会发布不可变 release。命名集合均为固定 allowlist。不可变发布会将 ZIP、wheelhouse、SBOM、attestations 以及
`preview-<componentId>-<version>-<sourceSHA>` 或 `stable-<componentId>-<version>-<sourceSHA>`
tag 绑定到精确源码 commit。任何 package 选择都不会推进可变的 latest release。
