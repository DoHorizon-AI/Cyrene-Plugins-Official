# Azure DevOps Observation Provider

This package exposes fixed, read-only Azure DevOps REST v7.1 operations through
`tool.provider.v1`. It lists pipelines, reads pipeline runs and classic Release
deployment/environment status, and emits bounded event pages for the workflow
runtime. Status projections include a stable `semanticFingerprint` and
`state` (`healthy`, `incident`, or `neutral`) so the runtime can suppress
repeated snapshots while retaining status changes. The runtime removes those
adapter fields before posting Work API events. The provider never queues a
pipeline, changes a release, invokes a CLI, or accepts a caller-selected URL,
organization, or project.

Each call requires Exchange-signed actor, organization, workspace, binding,
operation, and canonical argument context. The provider validates the exact
activation-configured source, then redeems a one-use Account Center lease bound
to that source and argument digest. OAuth access tokens remain in provider
memory. A missing Account Center lease fails closed; HTTP 401 and 403 responses
are returned as explicit authentication and authorization errors, never as an
empty successful page.

| Tool | Fixed operation | Scope |
| --- | --- | --- |
| `azure_devops.observations.poll` | Read a workflow event page | One configured pipeline or release-deployment source |
| `azure_devops.pipelines.list` | Pipelines List | One configured project |
| `azure_devops.pipeline_runs.list` | Runs List | One configured project and pipeline |
| `azure_devops.pipeline_run.get` | Runs Get | One run in the configured pipeline |
| `azure_devops.deployments.list` | Deployments List | One configured project, optionally narrowed to release definition/environment |
| `azure_devops.release_environment.get` | Get Release Environment | One release environment in the configured project |

The v7.1 Runs List endpoint returns at most the latest 10,000 runs and has no
continuation parameter. The provider pages that bounded result locally and marks
the terminal page `truncated` when the requested window reaches beyond the
available history. Deployment listing uses Azure DevOps continuation tokens
and fixed modified-time windows. Duplicate deployment IDs within or across
adjacent pages are removed. The workflow runtime must persist a cursor only
after a complete page sequence.

Activation configuration pins one Microsoft Entra tenant and Azure DevOps
organization, plus source IDs, project UUIDs, optional pipeline or Release
filters, and one Account Center profile reference per source. Host-injected
secret refs contain only 32-byte HMAC keys and
optional Account Center mTLS material. Azure DevOps profiles use Microsoft Entra
delegated OAuth for the Azure DevOps resource
(`499b84ac-1321-427f-aa17-267ca6975798/.default`) and need read permission on the
configured project. Each lease binds the tenant, organization, project, source,
and optional pipeline or Release filters to that profile. Legacy Azure DevOps
OAuth and direct token configuration are unsupported. See Microsoft's [Entra OAuth guidance](https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/entra-oauth),
[Pipelines List](https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/pipelines/list?view=azure-devops-rest-7.1),
[Runs List](https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/runs/list?view=azure-devops-rest-7.1),
[Deployments List](https://learn.microsoft.com/en-us/rest/api/azure/devops/release/deployments/list?view=azure-devops-rest-7.1),
and [Get Release Environment](https://learn.microsoft.com/en-us/rest/api/azure/devops/release/releases/get-release-environment?view=azure-devops-rest-7.1)
references.

Local tests use synthetic scopes and mocked HTTP responses. They do not prove
live Azure DevOps authorization or project access. Live acceptance remains
separate and requires an explicitly authorized project/profile.

Run focused checks from this directory:

```bash
uv run --project . --extra test pytest -q tests
uv run --project . --extra test ruff check src tests
```

## 中文说明

此 package 通过 `tool.provider.v1` 提供固定的 Azure DevOps REST v7.1 只读操作：列出
pipeline、读取 pipeline run 与经典 Release deployment/environment 状态，并向 workflow
runtime 提供有界事件页。它不会排队运行 pipeline、修改 release、执行 CLI，也不接受调用方
传入 URL、organization 或 project。

每次调用都必须携带 Exchange 签名的 actor、organization、workspace、binding、操作和规范化
参数上下文。provider 会先校验 activation 中精确配置的来源，再兑换绑定来源与参数摘要的一次性
Account Center lease。OAuth token 只在 provider 内存中使用。缺少 lease 时 fail closed；HTTP
401/403 会分别作为认证失败和授权拒绝返回，不会伪装成空结果。

v7.1 Runs List 最多返回最近 10,000 条 run，且没有 continuation 参数。provider 会在此有界
结果中进行本地分页；若请求时间窗超出可证明的历史范围，末页会标记 `truncated`。Deployment
List 使用 Azure DevOps continuation token 和固定的修改时间窗；会剔除同页或相邻页重复的
deployment ID。workflow runtime 只有在整个分页序列完整后才能持久化 cursor。

Activation 配置固定一个 organization，并通过来源 ID、project UUID、可选 pipeline/Release
过滤器和 Account Center profile ref 限定授权范围。宿主注入的 secret refs 仅包含 32 字节 HMAC
key 与可选 Account Center mTLS 材料。Azure DevOps profile 使用 Microsoft Entra delegated OAuth，
请求 Azure DevOps resource `499b84ac-1321-427f-aa17-267ca6975798/.default`，并需要对应 project
读取权限。不支持已弃用的 Azure DevOps OAuth 或直接配置 token。单元测试使用合成 scope 与 mock HTTP，不构成
真实 Azure DevOps 账号或项目访问验收。
