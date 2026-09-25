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
| Wave 0 | Directory restored with provenance |
| Wave 1 | React + TypeScript + Vite console with same-origin Web Host session handling |
| Wave 2 | Product adapters remain on Navigator proxy prefixes; owner contracts are read live |
| Owner | Plugins (UI surfaces), Navigator (Conversation/AgentRun authority) |

## Planned surface / 计划界面

| Page | Responsibility |
| --- | --- |
| Overview | Host readiness, Product reachability, resource counts, blockers |
| Models | Model import, validation state, serving bindings, artifacts |
| Datasets | Dataset containers and preparation handoff boundary |
| Training | Training drafts, owner preflight, explicit launch |
| Runs | Run lookup, current state, attempt diagnostics, cancel request |
| Deployments | Deployment lifecycle, serving bindings, endpoint readiness |
| Settings | Workspace session, proxy paths, write-only credentials |

UI rules fixed by the RC plan / RC 计划固定的 UI 规则:

- The UI does not copy Product state and never treats browser cache as
  authority; every view re-reads its owning Product API.
- The client follows the published OpenAPI contracts and validates response
  envelopes at runtime.
- No page asks the user to edit the database, environment variables, JSON files,
  or copy artifact paths by hand.
- Every cross-Product step is an explicit Import, Send to, Open in, or Publish.

## Local verification / 本地验证

```bash
npm install
npm run check
```

Set `NAVIGATOR_WEB_HOST_URL` when the local Vite server should proxy `/api/*` to
a Navigator Web Host on another address. Browser code itself only uses relative
same-origin paths such as `/api/v1/reactor/model-imports` and
`/api/v1/auth/session/refresh`.

设置 `NAVIGATOR_WEB_HOST_URL` 可让本地 Vite 服务将 `/api/*` 代理到其他地址的
Navigator Web Host。浏览器代码只使用 `/api/v1/reactor/model-imports`、
`/api/v1/auth/session/refresh` 等同源相对路径。

## Provenance / 来源

See [`PROVENANCE.md`](PROVENANCE.md). The public Navigator history only contains
a vanilla-TypeScript preview shell; the full previous client was WinUI/C#, not a
web console. This package therefore rebuilds the web console rather than
copying a web implementation that never existed publicly.

实现来源见 [`PROVENANCE.md`](PROVENANCE.md)。Navigator 公开历史中只有
vanilla-TypeScript 预览 shell，完整旧客户端是 WinUI/C#，并非 Web 控制台；因此本包
是重新实现 Web 控制台，而不是复制从未公开存在过的 Web 实现。
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# cyrene.ui.navigator / Navigator Web Console

本 package 是 Cyrene Navigator Web 控制台由 Plugins 持有的归属地。首个 RC 将使用 React、TypeScript 和 Vite 重建应用，并由 Navigator 同源 Web Host 提供服务；不会在已退役的 API 探测预览 shell 上继续扩展。

## 状态

| 字段 | 值 |
| --- | --- |
| Package | cyrene.ui.navigator |
| Wave 0 | 已根据来源记录恢复目录。 |
| Wave 1 | React + TypeScript + Vite 控制台，使用同源 Web Host session 处理。 |
| Wave 2 | Product adapter 仍通过 Navigator proxy 前缀访问；实时读取 owner contract。 |
| Owner | Plugins 负责 UI surface，Navigator 负责 Conversation/AgentRun authority。 |

## 计划界面

| 页面 | 职责 |
| --- | --- |
| Overview | Host 就绪状态、Product 可达性、资源数量和阻塞项。 |
| Models | 模型导入、验证状态、serving binding 和 artifact。 |
| Datasets | Dataset container 和 preparation handoff 边界。 |
| Training | Training 草稿、owner preflight 和显式启动。 |
| Runs | Run 查询、当前状态、attempt 诊断和取消请求。 |
| Deployments | Deployment 生命周期、serving binding 和 endpoint 就绪状态。 |
| Settings | Workspace session、proxy 路径和只写凭据。 |

### RC 计划固定的 UI 规则

- UI 不复制 Product 状态，也不将浏览器缓存视为 authority；每个视图都会重新读取其所属 Product API。
- 客户端遵循已发布的 OpenAPI contract，并在 runtime 校验响应 envelope。
- 页面不会要求用户手动编辑数据库、环境变量、JSON 文件或复制 artifact 路径。
- 每个跨 Product 步骤都必须是明确的 Import、Send to、Open in 或 Publish 操作。

## 本地验证

```bash
npm install
npm run check
```

设置 NAVIGATOR_WEB_HOST_URL 后，本地 Vite server 会将 /api/* 代理到其他地址上的 Navigator Web Host。浏览器代码只使用同源相对路径，例如 /api/v1/reactor/model-imports 和 /api/v1/auth/session/refresh。

## 来源

详见 PROVENANCE.md。Navigator 公开历史中只有 vanilla TypeScript 预览 shell；之前完整的客户端是 WinUI/C#，不是 Web 控制台。因此本 package 重建了 Web 控制台，并未复制公开历史中从未存在的 Web 实现。
