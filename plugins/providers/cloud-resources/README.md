# Cloud Resource Observation Provider

This independently packaged Apache-2.0 Plugin exposes two signed, bounded,
read-only tools: `cloud_resources_query` and `cloud_authorization_health`. Each
call selects one opaque `target_ref` from its activation binding. The binding
fixes the provider, target identity, cloud authentication mode, and host-injected
credential environment references. Tool arguments cannot select a URL, shell,
profile, cloud account, or credential.

The executable adapters cover Azure VM instance view through Azure managed
identity, Google Compute Engine instances through explicit metadata credentials
for GCE/GKE Workload Identity or a configured service-account JSON environment
reference, Hugging Face models, datasets, and Spaces metadata, Tailscale device
metadata through a scoped OAuth client, and one Runpod Pod through the REST
`GET /v1/pods/{podId}` endpoint.
Tailscale requests ask for `devices:core:read` and use only the fixed device
read endpoint. Runpod activation requires a host-injected Read Only API key and
uses the documented Bearer header, never the GraphQL query-string key form.
Runpod documents `desiredStatus` as an expected status; the observation keeps
`status=UNKNOWN` and preserves only that bounded source fact. Hugging Face reads
use only the configured read token and never access jobs, Spaces runtimes,
repository files, or model weights. Grant Azure managed identity only the
configured VM instance-view read permission, and use a Google service account
with `compute.readonly` scope plus read-only Compute IAM permissions.

Azure VM status comes from the Compute `instanceView` response. Google instance
status comes from the Compute Engine Instance resource. Hugging Face results
describe repository metadata availability only. None of these observations
creates a Cyrene Platform execution resource, placement, capacity, registry
entry, lease, or ready-compute claim. A failed provider read is returned as a
typed tool error so an owning durable observation store can retain its prior
observation and freshness evidence.

## Minimum permissions and packaged runtime

Use an Azure managed identity that can read the configured VM's instance view
only. For Google, use the `compute.readonly` scope and grant the attached or
service-account identity only the configured instance-read permission. A
Hugging Face read token is optional for public, non-gated metadata and should be
restricted to read access for private or gated repositories. Tailscale OAuth
requires only `devices:core:read`; configure Runpod with a Read Only API key.
The plugin never needs cloud write, workload-launch, repository-file, or model
weight permissions.

The official Package Runtime release vendors the Plugin runtime and integration
contracts source, including the contracts' `schema.json` package data, into the
prepared `src/` tree. Its wheelhouse requirements contain external runtime
distributions, `grpcio`, and `protobuf==4.25.9`; they do not require registry
wheels for the internal Cyrene SDK distributions. The `uv` path sources in
`pyproject.toml` are for local repository development.

Runtime dependencies are pinned in `pyproject.toml`; local repository checks use
`uv run --project . --extra test pytest -q tests`. Tests use vendor response
fixtures and local DirectPluginRuntime endpoints; they never contact a real
tenant or require provider tokens.

Official references:

- [Azure Compute Virtual Machines - Instance View REST API](https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/instance-view?view=rest-compute-2024-07-01)
- [Azure Identity Python library](https://pypi.org/project/azure-identity/)
- [Google Compute Engine Instance REST resource](https://cloud.google.com/compute/docs/reference/rest/v1/instances)
- [Google Auth Python library](https://pypi.org/project/google-auth/)
- [Hugging Face Hub API](https://huggingface.co/docs/hub/en/api)
- [Tailscale scoped trust credentials](https://tailscale.com/docs/reference/trust-credentials)
- [Tailscale OAuth clients](https://tailscale.com/docs/features/oauth-clients)
- [Runpod Find a Pod by ID REST API](https://docs.runpod.io/api-reference/pods/GET/pods/podId)
- [Runpod API key permissions](https://docs.runpod.io/get-started/credentials)

<!-- 中文翻译 -->

## 云资源只读观察提供方

本独立 Apache-2.0 Plugin 提供两个带签名、有限范围、只读工具：
`cloud_resources_query` 和 `cloud_authorization_health`。每次调用只能选择激活绑定
中配置的一个不透明 `target_ref`。绑定固定供应商、目标身份、云认证模式和由主机注入的
凭据环境变量引用。工具参数不能选择 URL、Shell、用户配置文件、云账号或凭据。

当前可执行适配器覆盖 Azure 托管身份读取 VM Instance View、Google Compute Engine
GCE/GKE Workload Identity 元数据凭据或绑定配置的服务账号 JSON 环境引用、Hugging Face 模型/数据集/Spaces 元数据，
以及使用限权 OAuth 客户端读取 Tailscale 设备元数据，并通过 REST
`GET /v1/pods/{podId}` 读取一个 Runpod Pod。Tailscale 请求只申请
`devices:core:read`，且只调用固定设备读取 Endpoint。Runpod 激活要求主机注入 Read Only
API key，并使用文档中的 Bearer header，不使用 GraphQL 查询字符串凭据方式。Runpod 文档将
`desiredStatus` 定义为预期状态；本观察保留 `status=UNKNOWN` 和这一项有界源事实。
Hugging Face 仅使用配置的只读令牌，不访问 Jobs、Spaces 运行时、仓库文件或模型权重。
Azure 托管身份只需获得目标 VM Instance View 的读取权限；Google 服务账号使用
`compute.readonly` scope 和只读 Compute IAM 权限。

## 最小权限与打包运行时

Azure 托管身份只应读取绑定中配置的 VM Instance View。Google 使用
`compute.readonly` scope，并只授予附加身份或服务账号读取该目标实例的权限。公开且未受限的
Hugging Face 元数据可不使用令牌；读取私有或 gated 仓库时，应使用仅具读取权限的受限令牌。
Tailscale OAuth 仅需 `devices:core:read`；Runpod 使用 Read Only API key。插件不需要云写入、
启动工作负载、读取仓库文件或下载模型权重的权限。

官方 Package Runtime 发布会将 Plugin runtime 和 integration contracts 源码（包括 contracts 的
`schema.json` 包数据）放入准备好的 `src/` 目录。wheelhouse 依赖包含外部运行时发行包、
`grpcio` 和 `protobuf==4.25.9`；内部 Cyrene SDK 不需要从 registry 安装 wheel。
`pyproject.toml` 中的 `uv` 路径源仅供仓库本地开发。

Azure VM 状态来自 Compute `instanceView`；Google 实例状态来自 Compute Engine Instance
资源；Hugging Face 结果只描述仓库元数据是否可用。这些观察不会创建 Cyrene Platform
执行资源、放置、容量、注册表记录、租约或就绪计算能力声明。供应商读取失败会返回类型化
工具错误，使拥有持久状态的上层可以保留之前的观察结果和新鲜度证据。

运行依赖在 `pyproject.toml` 中固定；本地检查使用 `uv run --project . --extra test pytest -q tests`。
测试使用供应商响应夹具和本地 DirectPluginRuntime Endpoint，不访问真实租户，也不需要供应商令牌。
