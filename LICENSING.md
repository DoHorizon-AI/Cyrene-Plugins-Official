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
- Python packages, Java packages, the OneBot connector, and protected QQ-side research documents currently receive no additional grant from the root policy.

根许可尚未确定前，Python、Java、OneBot Connector 与受保护的 QQ 侧研究文档不会因公开
可见而获得额外授权。任何仓库级开源授权都需要 operator 明确批准，并同步更新根
`LICENSE`、本映射与 package metadata。

## Contributions / 贡献

Contributors retain copyright in their contributions. A contribution accepted
inside an explicitly licensed component is distributed under that component's
existing outbound license. New independently packaged components must declare
an SPDX license expression or include a local license file.

贡献者保留其贡献的版权。进入已有明确许可组件的贡献按该组件既有 outbound license
分发；新的独立 package 必须声明 SPDX 许可表达式或包含本地 license 文件。

This document records repository policy and is not legal advice.

本文记录仓库策略，不构成法律意见。
