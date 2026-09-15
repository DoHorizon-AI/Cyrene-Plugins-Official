# QQNT Direct Real Smoke / QQNT 直连真实烟测

This document describes the protected-environment smoke gate for the
`qqnt-direct` profile. The gate is a manual job in the existing `public-ci`
workflow. It is separate from the fake Host TCK and must never be reported as
passed from a fake Host or a local simulation.

本文档描述 `qqnt-direct` 的受保护环境真实烟测。它与 fake Host TCK 分离，不能用
fake Host 或本地模拟结果替代真实通过。

## Gate shape / 门禁形态

The `public-ci` workflow is manual-capable and the smoke job only runs from
`main`:

- GitHub Environment: `qq-real-smoke`, with required reviewers;
- runner labels: `self-hosted`, `linux`, `x64`, `qq-real`;
- one concurrency group per `main` ref, because the QQ session/data directory is
  binding-owned;
- the runner must already contain one authorized exact QQ Linux x86_64 build,
  one Host executable that implements `cyrene.qq.host.v1` over inherited stdio,
  and a dedicated test account/session;
- the workflow accepts the exact client build and Host ABI as dispatch inputs;
- the existing workflow already handles push and pull-request events; the smoke
  job explicitly rejects every non-`workflow_dispatch` event;
- the scenario JSON stays on the protected runner and is not committed to this
  public repository.

`public-ci` 已有 push/PR 触发，真实烟测 job 只允许从 `main` 手工触发，并要求受保护
Environment、专用 Linux x64 runner、精确 QQ build、实现 `cyrene.qq.host.v1` 继承 stdio
协议的 Host，以及专用测试账号。job 会拒绝非 `workflow_dispatch`，脚本仍强制要求两个
精确 build/ABI 值。
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

The dispatch inputs `client_version` and `host_abi` become
`QQNT_REQUIRED_CLIENT_VERSION` and `QQNT_REQUIRED_HOST_ABI`. The handshake must
return both exact values. Passwords are not accepted; a pre-authorized session
or an operator-run QR login must establish the account before this automated
gate.

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

`tools/ci/qqnt_direct_real_smoke.py` records only:

- exact platform, client build, Host ABI and binding identity;
- accepted fixed operation names with priority and registered Service/method;
- private/group send acceptance and presence of native message IDs;
- count of canonical inbound events;
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
