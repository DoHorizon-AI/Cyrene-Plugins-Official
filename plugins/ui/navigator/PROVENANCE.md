# Provenance / 来源记录

This package is Cyrene-owned code. It is not a fork of an upstream web console.

本软件包是 Cyrene 自有代码，不是任何上游 Web 控制台的 fork。

## Package / 软件包

| Field | Value |
| --- | --- |
| Package | `cyrene.ui.navigator` |
| License | Apache-2.0 (repository `LICENSE`) |
| Source | `Cyrene-Plugins-Official/plugins/ui/navigator` |

## Source record / 来源记录

The web console is a rebuild, not a restored implementation. The public
Navigator history contains only a standalone Vite preview shell that probed API
availability and did not render conversations or control Harness turns.

Web 控制台属于重建而非恢复实现。Navigator 公开历史中只有一个独立 Vite 预览
shell，它只探测 API 可用性，不渲染会话，也不控制 Harness turn。

| Record | Reference |
| --- | --- |
| Navigator clean public history | `Cyrene-Navigator` commit `a8091ca8e8a01ca8d103aa10ef706891c75c4fb3` |
| Preview shell source | `apps/desktop/ui/` at `a8091ca` (`main.ts`, `api.ts`, `index.html`, `style.css`, `vite.config.ts`) |
| Removal that declared the Plugins move | `Cyrene-Navigator` commit `4ddc9dea943b63f99f0b6fabf51d93d92c26042b` |
| Previous full client | WinUI/C# under `apps/windows/` (history only; not a web console) |
| Private archive | `Cyrene-Navigator-history-archive` is the only declared home of former private demo assets; it is not a build input |

The preview shell is deliberately not reused: it only probes `/openapi.json`
and cannot serve the RC acceptance journeys. The rebuild consumes the published
OpenAPI contracts instead.

预览 shell 被明确弃用：它只探测 `/openapi.json`，无法承担 RC 验收旅程；重建版本
改为消费已发布的 OpenAPI 契约。

## Third-party dependencies / 第三方依赖

The rebuild will pin React, TypeScript, Vite, and the OpenAPI generator in
`Cyrene-Workspace/release-lock.json` when it lands. No third-party source is
vendored in this package.

重建落地时，React、TypeScript、Vite 与 OpenAPI 生成器将固定记录在
`Cyrene-Workspace/release-lock.json`。本包不内嵌任何第三方源码。