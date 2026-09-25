# QQNT Direct Real Smoke / QQNT 直连真实烟测

This document describes the protected-environment smoke gate for the
`qqnt-direct` profile. The gate is an operator-dispatched job in the existing
`public-ci` workflow. It is separate from the fake Host TCK and must never be reported as
passed from a fake Host or a local simulation.

本文档描述 `qqnt-direct` 的受保护环境真实烟测。它与 fake Host TCK 分离，不能用
fake Host 或本地模拟结果替代真实通过。

## Gate shape / 门禁形态

The `public-ci` workflow exposes a protected `repository_dispatch` event and
the smoke job only runs from `main`:

- GitHub Environment: `qq-real-smoke`, with required reviewers;
- runner labels: `self-hosted`, `linux`, `x64`, `qq-real`;
- one concurrency group per `main` ref, because the QQ session/data directory is
  binding-owned;
- the runner must already contain one authorized exact QQ Linux x86_64 build,
  one Host executable that implements `cyrene.qq.host.v1` over inherited stdio,
  and a dedicated test account/session;
- the exact client build and Host ABI are protected Environment variables;
  the runner script hard-requires both exact values and returns `NOT_RUN` when
  either is absent;
- the existing workflow already handles push and pull-request events; the smoke
  job explicitly rejects every non-`repository_dispatch` event;
- the scenario JSON stays on the protected runner and is not committed to this
  public repository.

`public-ci` 已有 push/PR 触发，真实烟测 job 只允许通过受权限控制的
`repository_dispatch` 从 `main` 触发，并要求受保护
Environment、专用 Linux x64 runner、精确 QQ build、实现 `cyrene.qq.host.v1` 继承 stdio
协议的 Host，以及专用测试账号。job 会拒绝非 `repository_dispatch`，脚本仍强制要求受保护
Environment 中的两个精确 build/ABI 值；缺失时脚本返回 `NOT_RUN`。
场景 JSON 只放在受保护 runner，不提交到公共仓库。

Environment variables / Environment 变量：

| Variable | Meaning |
| --- | --- |
| `QQNT_HOST_EXECUTABLE` | Absolute executable path to the authorized QQ Host. |
| `QQNT_HOST_ARGS_JSON` | JSON array of non-secret Host arguments; default is `[]`. |
| `QQNT_DATA_DIR` | Existing binding-private QQ session/data directory. |
| `QQNT_ACCOUNT_ID` | Dedicated smoke account identity. |
| `QQNT_SMOKE_SCENARIO_PATH` | Absolute path to the protected scenario JSON. |
| `QQNT_REAL_SMOKE_APPROVED` | Must be exactly `yes`, set only in the protected Environment. |
| `QQNT_REQUIRED_CLIENT_VERSION` | Exact authorized QQ client build expected by the Host handshake. |
| `QQNT_REQUIRED_HOST_ABI` | Exact authorized Host ABI expected by the handshake. |

The handshake must return the exact protected `QQNT_REQUIRED_CLIENT_VERSION` and
`QQNT_REQUIRED_HOST_ABI` values. Passwords are not accepted; a pre-authorized
session or an operator-run QR login must establish the account before this
automated gate.

An authorized operator can dispatch the gate from the repository's default
`main` branch with the GitHub CLI:

```bash
gh api repos/DoHorizon-AI/Cyrene-Plugins-Official/dispatches \
  -f event_type=qq-real-smoke
```

The `qq-real-smoke` Environment reviewers remain the final approval boundary.

授权运维可以使用 GitHub CLI 从仓库默认 `main` 触发：

```bash
gh api repos/DoHorizon-AI/Cyrene-Plugins-Official/dispatches \
  -f event_type=qq-real-smoke
```

最终审批边界仍是 `qq-real-smoke` Environment 的 reviewer。

## Protected preflight / 受保护环境预检

Before dispatching, the operator must verify that the runner uses a dedicated
QQ account and data directory, that the Host executable is the authorized
Linux x86_64 build, and that the two protected version/ABI variables describe
that same installation. The account must already be authorized or be ready
for an operator-controlled QR login; credentials must never be placed in the
repository, scenario JSON, event payload, or evidence artifact.

触发前，运维必须确认 runner 使用专用 QQ 账号和数据目录，Host 可执行文件是授权的
Linux x86_64 构建，并且两个受保护版本/ABI 变量与同一安装完全一致。账号必须已经授权，
或准备好由运维控制二维码登录；凭据不得放入仓库、场景 JSON、事件 payload 或证据产物。

## Scenario contract / 场景契约

The scenario must contain two dedicated targets, two inbound assertions, a
bounded marker prefix, and explicit fixed-operation parameters:

```json
{
  "marker_prefix": "cyrene-qq-smoke",
  "private_conversation": {
    "conversation_id": "<private-peer-id>",
    "peer_uid": "<private-peer-uid>"
  },
  "group_conversation": {
    "conversation_id": "<group-code>",
    "peer_uid": "<group-peer-uid>"
  },
  "expected_inbound": [
    {
      "conversation_kind": "private",
      "conversation_id": "<private-peer-id>",
      "text_contains": "-private"
    },
    {
      "conversation_kind": "group",
      "conversation_id": "<group-code>",
      "text_contains": "-group"
    }
  ],
  "operations": {
    "qq.message.history_include_self": {
      "account_id": "$ACCOUNT_ID",
      "peer": {"kind": "private", "peer_uid": "$PRIVATE_PEER_UID"},
      "offset": 0,
      "count": 20
    },
    "qq.message.by_id": {
      "account_id": "$ACCOUNT_ID",
      "message_id": "$PRIVATE_MESSAGE_ID"
    },
    "qq.group.list": {"account_id": "$ACCOUNT_ID"},
    "qq.friend.list": {"account_id": "$ACCOUNT_ID"},
    "qq.media.download": {
      "account_id": "$ACCOUNT_ID",
      "media_id": "<dedicated-media-id>"
    },
    "qq.file.list": {
      "account_id": "$ACCOUNT_ID",
      "group_id": "<dedicated-group-code>"
    },
    "qq.group.modify_remark": {
      "account_id": "$ACCOUNT_ID",
      "group_id": "<dedicated-group-code>",
      "remark": "cyrene-qq-smoke"
    },
    "qq.search.contact": {
      "account_id": "$ACCOUNT_ID",
      "query": "<dedicated-contact-query>"
    },
    "qq.profile.long_nick": {
      "account_id": "$ACCOUNT_ID",
      "long_nick": "cyrene-qq-smoke"
    },
    "qq.online.devices": {"account_id": "$ACCOUNT_ID"}
  }
}
```

Every operation must be present in the fixed `qq.client.v1` allow-list. The
runner script rejects unknown operations, callback-only operations, raw
`service`/`method` passthrough, passwords, tokens, and unresolved placeholders.
The scenario must use a dedicated account and reversible P2 values.

每个 operation 都必须来自固定 `qq.client.v1` 白名单。脚本拒绝未知操作、仅回调操作、
原始 `service`/`method` passthrough、密码、token 和未解析占位符；P2 修改必须使用专用
账号并且可恢复。

## Evidence / 证据

`tools/ci/im_real_smoke.py` records only:

- exact platform, client build, Host ABI and binding identity;
- accepted fixed operation names with priority and registered Service/method;
- private/group send acceptance and presence of native message IDs;
- count of canonical inbound events;
- clean `qq.login.offline` acceptance and transition to `LOGIN_REQUIRED`;
- clean shutdown and restart binding/account identity checks.

It never writes raw QQ responses, message contents, session files, passwords,
tokens, or scenario parameters to the evidence artifact. A successful run
produces `QQ_REAL_SMOKE: PASS`; missing protected configuration is
`NOT_RUN`/exit 2 and must remain `NOT_RUN` in the API matrix.

脚本只输出脱敏的版本、ABI、固定操作、收发结果、事件数量和重启身份证据，不写入原始
响应、消息正文、会话文件、密码、token 或场景参数。配置缺失时是 `NOT_RUN`，不能写成
真实通过。

After a successful run, update the corresponding rows in
`QQNT_DIRECT_API_MATRIX.md` with the exact `main` SHA, workflow run, authorized
QQ build/ABI, and the observed result. Do not change unrelated or out-of-plan
rows from `QQ_SIDE_INTERFACES.md`.

真实运行成功后，才可以在矩阵中按 exact `main` SHA、workflow run、授权 QQ build/ABI 和
实际结果更新对应行；`QQ_SIDE_INTERFACES.md` 中未进入计划的额外接口仍保持后续范围。
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# QQNT Direct 真实环境烟测

本文描述 qqnt-direct profile 在受保护环境中的 smoke 门禁。该门禁是通过现有 public-ci workflow 由 operator 触发的 job。它与 fake Host TCK 分离，绝不能将 fake Host 或本地模拟报告为通过。

## 门禁形态

public-ci workflow 暴露受保护的 repository_dispatch event；smoke job 只会从 main 运行：

- GitHub Environment：qq-real-smoke，并配置 required reviewer。
- Runner label：self-hosted、linux、x64、qq-real。
- 每个 main ref 使用独立 concurrency group，因为 QQ session/data directory 归 binding 所有。
- runner 必须预先安装一个获授权的准确 QQ Linux x86_64 build、一个通过继承 stdio 实现 cyrene.qq.host.v1 的 Host 可执行文件，以及一个专用测试账号/session。
- 准确的客户端 build 和 Host ABI 放在受保护 Environment variables 中；runner script 强制要求二者完全匹配，缺失任一项时返回 NOT_RUN。
- 现有 workflow 已处理 push 和 pull-request event；smoke job 会明确拒绝所有非 repository_dispatch event。
- 场景 JSON 留在受保护 runner 上，不提交到公共仓库。

Environment variables：

| 变量 | 含义 |
| --- | --- |
| QQNT_HOST_EXECUTABLE | 获授权 QQ Host 的绝对可执行文件路径。 |
| QQNT_HOST_ARGS_JSON | 不含 secret 的 Host 参数 JSON array，默认值为 []。 |
| QQNT_DATA_DIR | 已存在的、由 binding 私有持有的 QQ session/data directory。 |
| QQNT_ACCOUNT_ID | 专用 smoke 账号身份。 |
| QQNT_SMOKE_SCENARIO_PATH | 受保护场景 JSON 的绝对路径。 |
| QQNT_REAL_SMOKE_APPROVED | 必须严格为 yes，且只能在受保护 Environment 中设置。 |
| QQNT_REQUIRED_CLIENT_VERSION | Host 握手应匹配的、获授权 QQ 客户端准确 build。 |
| QQNT_REQUIRED_HOST_ABI | 握手应匹配的、获授权 Host 准确 ABI。 |

握手必须返回与受保护 QQNT_REQUIRED_CLIENT_VERSION 和 QQNT_REQUIRED_HOST_ABI 完全相同的值。不接受密码；自动门禁开始前，账号必须已有授权 session，或由 operator 手动完成二维码登录。

授权 operator 可以通过 GitHub CLI 从仓库默认 main branch 触发门禁：

```bash
gh api repos/DoHorizon-AI/Cyrene-Plugins-Official/dispatches -f event_type=qq-real-smoke
```

qq-real-smoke Environment 的 reviewer 仍是最终审批边界。

## 受保护环境预检

触发前，operator 必须确认 runner 使用专用 QQ 账号和 data directory，Host 可执行文件是获授权的 Linux x86_64 build，并且两个受保护版本/ABI 变量描述同一安装。账号必须已获授权，或准备好由 operator 控制二维码登录；不能把凭据写入仓库、场景 JSON、event payload 或证据制品。

## 场景契约

场景必须包含两个专用目标、两条入站断言、有界 marker prefix 和明确的固定操作参数：

```json
{
  "marker_prefix": "cyrene-qq-smoke",
  "private_conversation": {
    "conversation_id": "<private-peer-id>",
    "peer_uid": "<private-peer-uid>"
  },
  "group_conversation": {
    "conversation_id": "<group-code>",
    "peer_uid": "<group-peer-uid>"
  },
  "expected_inbound": [
    {
      "conversation_kind": "private",
      "conversation_id": "<private-peer-id>",
      "text_contains": "-private"
    },
    {
      "conversation_kind": "group",
      "conversation_id": "<group-code>",
      "text_contains": "-group"
    }
  ],
  "operations": {
    "qq.message.history_include_self": {
      "account_id": "$ACCOUNT_ID",
      "peer": {"kind": "private", "peer_uid": "$PRIVATE_PEER_UID"},
      "offset": 0,
      "count": 20
    },
    "qq.message.by_id": {
      "account_id": "$ACCOUNT_ID",
      "message_id": "$PRIVATE_MESSAGE_ID"
    },
    "qq.group.list": {"account_id": "$ACCOUNT_ID"},
    "qq.friend.list": {"account_id": "$ACCOUNT_ID"},
    "qq.media.download": {
      "account_id": "$ACCOUNT_ID",
      "media_id": "<dedicated-media-id>"
    },
    "qq.file.list": {
      "account_id": "$ACCOUNT_ID",
      "group_id": "<dedicated-group-code>"
    },
    "qq.group.modify_remark": {
      "account_id": "$ACCOUNT_ID",
      "group_id": "<dedicated-group-code>",
      "remark": "cyrene-qq-smoke"
    },
    "qq.search.contact": {
      "account_id": "$ACCOUNT_ID",
      "query": "<dedicated-contact-query>"
    },
    "qq.profile.long_nick": {
      "account_id": "$ACCOUNT_ID",
      "long_nick": "cyrene-qq-smoke"
    },
    "qq.online.devices": {"account_id": "$ACCOUNT_ID"}
  }
}
```

每项操作都必须存在于固定的 qq.client.v1 allow-list 中。runner script 会拒绝未知操作、仅用于 callback 的操作、原始 service/method 透传、密码、token 和未解析的 placeholder。场景必须使用专用账号；P2 修改操作必须可恢复。

## 证据

tools/ci/im_real_smoke.py 只记录：

- 精确平台、客户端 build、Host ABI 和 binding identity。
- 已接受的固定操作名称、优先级和已登记的 Service/method。
- 私聊/群聊发送是否被接受，以及是否存在原生 message ID。
- 规范化入站 event 数量。
- 正常接受 qq.login.offline 且状态转换到 LOGIN_REQUIRED 的证据。
- 正常关闭，以及重启后的 binding/账号身份检查结果。

它绝不会将原始 QQ 响应、消息正文、session 文件、密码、token 或场景参数写入证据制品。运行成功时输出 QQ_REAL_SMOKE: PASS；缺少受保护配置时输出 NOT_RUN 并以 exit 2 退出，API matrix 中也必须继续标记为 NOT_RUN。

真实运行成功后，应在 QQNT_DIRECT_API_MATRIX.md 对应条目中记录精确的 main SHA、workflow run、获授权 QQ build/ABI 和观测结果。不要修改 QQ_SIDE_INTERFACES.md 中与本计划无关的行。
