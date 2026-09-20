# cyrene.ui.navigator / Navigator Web Console

This package is the Plugins-owned home of the Cyrene Navigator web console.
The first RC rebuilds it as a React + TypeScript + Vite application served by
the Navigator same-origin Web Host; it does not extend the retired
API-probing preview shell that Navigator once carried.

本软件包是 Cyrene Navigator Web 控制台的 Plugins 归属地。首个 RC 将以
React + TypeScript + Vite 重建，并由 Navigator 同源 Web Host 托管；不会沿用
Navigator 历史中仅探测 API 的预览页。

## Status / 状态

| Field | Value |
| --- | --- |
| Package | `cyrene.ui.navigator` |
| Wave 0 | Directory restored with provenance; no implementation yet |
| Wave 1..2 | React + TypeScript + Vite console, OpenAPI-generated client, runtime-validated types |
| Owner | Plugins (UI surfaces), Navigator (Conversation/AgentRun authority) |

## Planned surface / 计划界面

| Page | Responsibility |
| --- | --- |
| Home & diagnostics | GPU, disk, services, plugins, credentials, blockers |
| Models | Model import, validation state, artifacts |
| Datasets | Dataset import, preview, mapping, quality, split |
| Training | TrainingSpec, preflight, events, checkpoints, cancel, resume |
| Deployments | Deployment drafts, vLLM lifecycle, ready probes |
| Gateway | Endpoints, routes, API keys, base URL, client snippets |
| Settings | Workspace, credentials metadata, session management |

UI rules fixed by the RC plan / RC 计划固定的 UI 规则:

- The UI does not copy Product state and never treats browser cache as
  authority; every view re-reads its owning Product API.
- The client is generated from the published OpenAPI contracts and validates
  responses at runtime.
- No page asks the user to edit the database, environment variables, JSON files,
  or copy artifact paths by hand.
- Every cross-Product step is an explicit Import, Send to, Open in, or Publish.

## Provenance / 来源

See [`PROVENANCE.md`](PROVENANCE.md). The public Navigator history only contains
a vanilla-TypeScript preview shell; the full previous client was WinUI/C#, not a
web console. This package therefore rebuilds the web console rather than
copying a web implementation that never existed publicly.

实现来源见 [`PROVENANCE.md`](PROVENANCE.md)。Navigator 公开历史中只有
vanilla-TypeScript 预览 shell，完整旧客户端是 WinUI/C#，并非 Web 控制台；因此本包
是重新实现 Web 控制台，而不是复制从未公开存在过的 Web 实现。