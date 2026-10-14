# Workspace Connector Product credentials

The Connector adds a Product `Authorization: Bearer` header only after the
Authority-redeemed `ExecutionCredential` exactly matches a row in a protected
credential map. The map does not accept roles or user-supplied URLs, and new
operation IDs are added as data rather than Product-specific code.

## Protected map

The systemd unit loads the root-managed source file
`/etc/cyrene/workspace-product-credentials.json` as
`%d/product-credentials.json` and sets
`CYRENE_CONNECTOR_PRODUCT_CREDENTIAL_MAP` to that credential copy. The
persistent source should be owned by root:root with mode `0400` or `0600`. The
Connector validates the loaded copy against its effective UID and accepts GID
`root` (GID 0, as produced by the verified systemd `LoadCredential=` setup) or
its effective GID. It requires a regular file, one link, no final-component
symlink, and exact mode `0400` or `0600`; both modes leave the file readable
only by its owner. Do not point the service directly at the persistent
root-owned source.
The same bearer value must also be configured in Yield's authenticator by its
SHA-256 digest, bound to the exact organization and workspace; this change does
not weaken or replace Yield authentication.

Version 2 is a JSON object with `version: 2` and a nonempty `credentials`
array. Each row binds a token to the stable authority-signed Product scope,
target origin, and execution-device fence used by the adapter. Product resource
IDs and endpoint paths can be created at runtime, so a generic row selects
them through a resource constraint and origin:

```json
{
  "version": 2,
  "credentials": [
    {
      "organizationId": "org-example",
      "workspaceId": "workspace-example",
      "operationOwnerId": "yield",
      "operationId": "op_training_start",
      "scope": "workspace:workspace-example:yield:op_training_start",
      "resourceConstraint": {
        "kind": "authorityApprovedResource"
      },
      "targetComponent": "yield",
      "httpMethod": "POST",
      "endpointOrigin": "https://yield.internal.example",
      "executionDeviceId": "device-example",
      "executionDeviceGeneration": 1,
      "executionAuthorizationIdHex": "00000000000000000000000000000000",
      "executionDeviceCertificateSha256Hex": "0000000000000000000000000000000000000000000000000000000000000000",
      "contractActivationGeneration": 1,
      "bearerToken": "replace-with-a-random-token-of-at-least-32-bytes"
    }
  ]
}
```

The example values are placeholders. Use an independently generated random
bearer value accepted by the Product, at least 32 printable ASCII bytes, with
no whitespace or control characters. Hex fields must be lowercase and have
the exact byte lengths shown. Unknown fields, empty scopes, overlapping
scope/resource-constraint rows, duplicate bearer values, malformed tokens,
and unsupported versions are rejected. The file is limited to 64 KiB and
1,024 rows.

`resourceConstraint` is a closed data enum. Use
`{"kind":"exact","resourceId":"draft-123"}` when the token must match one
fixed resource ID. Use `{"kind":"authorityApprovedResource"}` for operations
whose resource IDs are created or selected at runtime. That option does not
authorize a caller-supplied wildcard: dispatch still requires the redeemed
Authority credential and resolved target to agree exactly on the invocation's
resource ID, full endpoint, HTTP method, operation, target component, and
execution-device fence. The map does not build route templates or accept a URL
from the caller. `endpointOrigin` must be the canonical origin only, without a
path or trailing slash.
Multiple exact rows may share a static scope when their resource IDs differ;
duplicate exact IDs and any overlap with an `authorityApprovedResource` row
are rejected.

The static map match includes organization, workspace, operation owner and ID,
scope, target component, HTTP method, endpoint origin, execution device ID and
generation, execution authorization ID, device certificate digest, and
contract activation generation. A different workspace, operation, origin,
target, or device binding is denied. Before selecting a token, the adapter
checks the invocation's operation and resource against the redeemed credential
and resolved target, then checks the credential's full endpoint, resource ID,
HTTP method, operation, target, and device fence against that exact target. The
map does not carry principal roles and does not authorize calls by itself; the
signed credential must first pass Authority redemption and Connector binding
checks.

## Rotation

The adapter reopens and validates the credential copy for every Product
dispatch. Replace map files atomically; an in-place write is rejected if the
file changes while being read. A newly installed copy is used on the next
dispatch. The systemd `LoadCredential=` copy is a per-service-start snapshot,
so changing the root-managed source requires restarting the service before
the Connector can read the new copy. Invalid or missing copies fail closed;
the Connector does not send an unauthenticated fallback request. Bearer
values are not placed in command-line arguments, environment variables,
outbox records, journals, diagnostics, or error strings.

## 中文说明

Connector 只会在 Authority 已兑换的签名凭据与受保护映射中的一行完全
匹配后，才向 Product 请求添加 `Authorization: Bearer`。映射不接受角色或
请求方提供的 URL；新增操作 ID 通过配置行接入，不增加 Product 专属分支。

systemd 单元把 root 管理的
`/etc/cyrene/workspace-product-credentials.json` 加载为
`%d/product-credentials.json`，并通过
`CYRENE_CONNECTOR_PRODUCT_CREDENTIAL_MAP` 传入副本路径。持久源文件应由
root:root 拥有，权限设为 `0400` 或 `0600`。Connector 要求加载后的副本
UID 与服务有效 UID 相同，GID 为 root（GID 0；已验证的 systemd
`LoadCredential=` 结果）或服务有效 GID；同时要求普通文件类型、单链接、
末级路径非符号链接，以及严格的 `0400` 或 `0600` 权限。这两种权限都只允许
文件所有者读取。不要让服务直接读取 root 持久源文件。
Yield 侧的 authenticator 也必须预先配置同一令牌的 SHA-256 摘要，并把它绑定
到精确的 organization/workspace；本接线不会放宽或替代 Yield 的认证。

JSON v2 映射包含 `version: 2` 和非空的 `credentials` 数组。每行绑定组织、
工作区、操作所有者和 ID、scope、resource constraint、目标组件、HTTP 方法、
endpoint origin、执行设备 ID 和 generation、执行授权 ID、设备证书摘要以及
契约激活 generation。resource constraint 是封闭的数据枚举：固定资源使用
`{"kind":"exact","resourceId":"draft-123"}`；动态资源使用
`{"kind":"authorityApprovedResource"}`。后者不是请求方可自行指定的通配授权；
dispatch 仍要求已兑换的 Authority 凭据与 invocation、Authority 解析目标在
resource ID、完整 endpoint、HTTP 方法、操作、目标组件和设备 fence 上完全一致。
映射不会构造 route template，也不接受请求方 URL。`endpointOrigin` 必须是无路径、
无末尾斜线的规范 origin。同一静态 scope 可以配置多个不同 Exact resource ID；
重复 ID 或与 `authorityApprovedResource` 重叠的行会被拒绝。十六进制字段必须为
小写并具有固定字节长度；Bearer
值至少 32 个可打印 ASCII 字节，不含空白或控制字符。未知字段、空 scope、
重叠的 scope/resource constraint、重复令牌、无效令牌、不支持的版本、超过
64 KiB 或超过 1,024 行的映射都会被拒绝。

适配器每次 Product dispatch 都会重新打开并校验凭据副本。映射文件必须以
原子替换方式更新；读取期间观察到文件元数据变化时会拒绝本次调用。
`LoadCredential=` 生成的是每次服务启动时的凭据副本，因此更新 root 管理的
源文件后，必须重启服务才能加载新副本。缺少或无效的副本会关闭 dispatch，
不会退化为无认证请求。Bearer 值不会写入命令行、环境变量、outbox、journal、
诊断输出或错误文本。
