# Cyrene Plugins licensing / Cyrene Plugins 授权说明

The repository currently has no single repository-wide open-source grant.
Public visibility permits inspection and CI participation; it does not grant a
license to source that lacks an explicit component-level designation.

本仓库当前没有统一的仓库级开源授权。公开可见允许审阅与参与 CI，但不会自动授权没有
明确组件级许可声明的源码。

## Determining the applicable license / 如何确定适用许可

Use these sources in order:

1. An SPDX identifier in the source file.
2. The nearest component package manifest's SPDX license expression.
3. A component-local `LICENSE`, `COPYING`, `NOTICE`, or provenance document.

按以下顺序判断：源码文件中的 SPDX 标识、最近组件 package manifest 的 SPDX 许可表达式、
以及组件本地的 `LICENSE`、`COPYING`、`NOTICE` 或 provenance 文档。

If none applies, no open-source license is granted for that material. Contact
the maintainers before copying, redistributing, or publishing it.

如果以上均不适用，则该材料未获得开源许可；复制、再分发或发布前必须联系维护者。

## Current component declarations / 当前组件声明

- The canonical Protobuf contracts carrying `SPDX-License-Identifier: Apache-2.0` are Apache-2.0 licensed.
- Rust packages whose `Cargo.toml` declares `license = "Apache-2.0"` are Apache-2.0 licensed.
- The optional Linux Computer Runtime backend links Microsoft's `mxc-sdk` 1.0.1 under MIT. Its full attribution and license text are in the root `THIRD_PARTY_NOTICES.md`. Bubblewrap is a separate optional runtime executable under LGPL-2.1-or-later; it is not vendored here.
- Cyrene Plugins-owned code in `plugins/providers/cloud-resources/` is Apache-2.0 under its component manifest. The provider's Azure, Google, Hugging Face, Tailscale, and Runpod client libraries remain third-party dependencies under their own upstream licenses.
- Cyrene Plugins-owned code in `plugins/connectors/email-notification/` is Apache-2.0 under its component manifest, project metadata, and local `LICENSE`.
- Cyrene-owned code in `plugins/connectors/wecom/` is Apache-2.0 under its component manifest and local `LICENSE`. The upstream `wecom-aibot-python-sdk` remains MIT-licensed at pinned commit `6bcb59a9a636c566f4c6ea5268b228e3def1611a`; its separate notice is retained under `plugins/connectors/wecom/third_party/licenses/`.
- The Cyrene Plugins-owned shared SDK in `sdk/python/cyrene_integration_contracts/` is Apache-2.0 under its package metadata and local `LICENSE`. This grant covers the SDK's DTOs, signing helpers, and packaged schema; Cloud Resources and Email Notification vendor that same SDK source without changing its ownership or license.
- Other Python packages, Java packages, the OneBot connector, and protected QQ-side research documents currently receive no additional grant from the root policy.

根许可尚未确定前，除下述明确授权的 SDK 外，其他 Python、Java、OneBot Connector 与受保护的
QQ 侧研究文档不会因公开可见而获得额外授权。任何仓库级开源授权都需要 operator 明确批准，
并同步更新根 `LICENSE`、本映射与 package metadata。

可选 Linux Computer Runtime backend 使用 Microsoft `mxc-sdk` 1.0.1（MIT）；完整署名和许可文本见根目录
`THIRD_PARTY_NOTICES.md`。Bubblewrap 是独立可选的运行时可执行文件，依 LGPL-2.1-or-later 许可，源码仓库不包含该二进制。

`sdk/python/cyrene_integration_contracts/` 依其 package metadata 与本地 `LICENSE` 采用
Apache-2.0。该 Cyrene Plugins-owned SDK 包含 DTO、签名辅助函数及打包 schema；Cloud Resources
和 Email Notification 会 vendoring 相同 SDK 源码，不改变其所有权或许可。

`plugins/providers/cloud-resources/` 中由 Cyrene Plugins 维护的代码依组件 manifest 采用
Apache-2.0。Azure、Google、Hugging Face、Tailscale 和 Runpod 客户端库仍分别遵循其上游许可。
`plugins/connectors/email-notification/` 中由 Cyrene Plugins 维护的代码依组件 manifest、项目
metadata 与本地 `LICENSE` 采用 Apache-2.0。

`plugins/connectors/wecom/` 中由 Cyrene 编写的代码依该组件 manifest 与本地 `LICENSE`
采用 Apache-2.0。上游 `wecom-aibot-python-sdk` 仍依固定 commit
`6bcb59a9a636c566f4c6ea5268b228e3def1611a` 采用 MIT；其独立许可文本保留在
`plugins/connectors/wecom/third_party/licenses/`。

## Contributions / 贡献

Contributors retain copyright in their contributions. A contribution accepted
inside an explicitly licensed component is distributed under that component's
existing outbound license. New independently packaged components must declare
an SPDX license expression or include a local license file.

贡献者保留其贡献的版权。进入已有明确许可组件的贡献按该组件既有 outbound license
分发；新的独立 package 必须声明 SPDX 许可表达式或包含本地 license 文件。

This document records repository policy and is not legal advice.

本文记录仓库策略，不构成法律意见。
