# Contributing to Cyrene Plugins / 为 Cyrene Plugins 贡献

1. Keep Product semantics and Platform lifecycle authority outside this repository.
2. Treat the versioned contracts under `contracts/` as the public compatibility boundary.
3. Every independently packaged Plugin must declare an SPDX license expression or include a local license file.
4. Preserve protected protocol research documents and their recorded digests unless a dedicated review approves a revision.
5. Never commit credentials, private endpoints, tenant data, model weights, datasets, or developer-specific absolute paths.
6. Do not commit generated build output, caches, virtual environments, or test reports.
7. Keep configured runtimes fail-closed when a binding, endpoint, authentication fact, or required dependency is unavailable.
8. Update tests and `source-manifest.json` through the clean export process; do not copy private Git history into this repository.

## Capability ownership and package releases

Platform owns Cyrene identity, policy, delegation, and generic Plugin lifecycle. Plugins own versioned capability contracts and fixed vendor transports; Account Center owns external account links and short-lived credential leases. Product services own durable business state, workflow, and REST surfaces. Fixed webhook and messaging paths must not call model or DSH APIs.

Official Python Package Runtime releases use the explicit package/component mappings in `tooling/release/official_plugin_package.py`. The descriptor's primary capability is a selection hint; the embedded `plugin.manifest.json` remains the authority for the package's complete capability set. Automatic pushes keep the existing five-package draft behavior. After the Integration v1 source is merged and its exact SHA is checked, select `integration-v1` in the workflow's `workflow_dispatch` input to build and publish only Account Center, GitHub Issues, and WeCom. The selector accepts only its fixed package-set and package-ID allowlist. It publishes under immutable `preview-<componentId>-<version>-<sourceSHA>` or `stable-<componentId>-<version>-<sourceSHA>` tags only after verifying source-bound assets, attestations, the immutable release readback, and the tag's exact source commit. It never advances the latest release. Do not select the mutable `plugins-latest` release for Package Runtime installation.

Platform 拥有 Cyrene 身份、策略、委托和通用 Plugin 生命周期。Plugins 拥有版本化 capability 契约和固定供应商传输；Account Center 拥有外部账号绑定与短期凭据租约。Product 服务拥有持久业务状态、工作流和 REST 界面。固定 webhook 和消息链路不得调用模型或 DSH API。

官方 Python Package Runtime 发布通过 `tooling/release/official_plugin_package.py` 中显式维护的 package/component 映射构建。descriptor 的 primary capability 仅用于选择；包中 `plugin.manifest.json` 仍是该 package 全部 capability 的权威来源。自动 push 保持现有五个 package 的 draft 行为。Integration v1 源码经正常合并并核对精确 SHA 后，可在工作流 `workflow_dispatch` 中选择 `integration-v1`，仅构建并发布 Account Center、GitHub Issues 和 WeCom。选择器只接受固定的 package-set 和 package-ID allowlist。工作流仅在核验源码绑定资产、attestations、不可变 release 回读以及 tag 指向的精确源码 commit 后，使用不可变 `preview-<componentId>-<version>-<sourceSHA>` 或 `stable-<componentId>-<version>-<sourceSHA>` tag 发布；不会推进 latest release。Package Runtime 安装不得选择可变的 `plugins-latest` release。

贡献必须遵守 Product、Platform 与 Plugin 的权威边界；缺少 binding、endpoint、认证事实
或必要依赖时必须 fail-closed。不得把私有 Git 历史、凭据、私有 endpoint、租户数据、
模型权重、数据集或生成构建物带入公开仓库。

The authoritative checks are defined in `.github/workflows/public-ci.yml`.
At minimum, run the focused checks for the languages and contracts changed,
plus:

```bash
python3 tools/ci/verify_source_manifest.py --root . --allow-git-metadata
python3 tools/ci/check_public_repository.py
```

Changes merge through normal review and must pass exact-SHA hosted CI. An
unrun, skipped, or simulated check is not a pass.

变更必须通过正常评审合并，并以 exact-SHA hosted CI 为准；未运行、跳过或模拟检查均不
算通过。
