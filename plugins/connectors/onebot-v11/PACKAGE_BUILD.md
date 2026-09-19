# OneBot v11 package and rollback artifact / OneBot v11 正式包与回滚制品

The formal package is the RID-specific C# Native AOT payload. The Python 0.2.0
payload remains available as a separately assembled rollback artifact. Neither
builder publishes, mutates the source tree, copies an official QQ installation,
or includes account data, session files, secrets, or credentials.

正式包是按 RID 区分的 C# Native AOT payload；Python 0.2.0 payload 作为独立回滚制品保留。
两个构建器都不会发布、修改源码树、复制官方 QQ 安装，也不会包含账号数据、会话文件、密钥或凭据。

## Native AOT package / Native AOT 正式包

From the Plugins repository root, after a successful Native AOT publish:

```bash
python3 plugins/connectors/onebot-v11/tools/assemble_native_package.py \
  --repository-root . \
  --binary runtime/dotnet-native-aot/Cyrene.OneBot.V11.Host/bin/Release/net10.0/linux-x64/publish/cyrene-onebot-v11 \
  --rid linux-x64 \
  --output /tmp/cyrene-onebot-v11-native-linux-x64.zip
```

The output is an installed package payload with `bin/cyrene-onebot-v11`, a
Native AOT manifest, and no Python source or dependency lock. The package
manifest remains `CANDIDATE` until immutable artifact, provenance, SBOM, and
protected real-QQ acceptance requirements are satisfied.

## Python rollback package / Python 回滚包

The old Python runtime is assembled explicitly from the tracked
`rollback/python-0.2.0` metadata snapshot:

```bash
python3 plugins/connectors/onebot-v11/tools/assemble_package.py \
  --repository-root . --output /tmp/cyrene-onebot-v11-python-0.2.0.zip
```

This artifact is retained for rollback only; it is not the formal runtime
projection and is not a dual-runtime compatibility mode.

## Artifact vertical gate / Artifact 垂直门禁

The package CI job extracts the Native AOT ZIP into an isolated directory and
verifies its executable, metadata, and absence of Python runtime files. The
rollback CI job separately extracts the Python ZIP and starts two direct
workers from that installed artifact. Those workers exercise direct `Health`,
QQ extension `Invoke`, message `InvokeStream`, binding-local fake Host dispatch,
and shutdown cleanup:

包 CI 还会把 ZIP 解压到隔离目录，并从该已安装 artifact 启动两个 direct worker。两个 worker
会实际执行 direct `Health`、QQ 扩展 `Invoke`、消息 `InvokeStream`、binding 隔离的 fake Host
调用和关闭清理，不增加 OneBot transport 中转：

```bash
PYTHONPATH=sdk/python/cyrene_plugin_runtime/src \
  python3 -m pytest -q \
  plugins/connectors/onebot-v11/tests/test_package.py \
  -k installed_artifact
```

The rollback gate is an artifact-level Plugin gate. Neither package gate is the
Workspace P2.5 Product/AstrBot/pgvector harness, and neither can promote an
official QQ runtime row to `IMPLEMENTED`; those remain separate acceptance
gates.

这是 Plugins 内的 artifact-level 门禁，不是 Workspace P2.5 Product/AstrBot/pgvector 三仓
harness，也不能把任何能力行提升为官方 QQ runtime 的 `IMPLEMENTED`；两者仍是独立验收门禁。

## Python reference artifact / Python 参考制品

The migration also assembles one immutable Python reference archive so a
future cleanup can remove Python from the formal plugin without losing a
reproducible cross-language comparison target. The archive records the exact
Git revision, content digests, formal runtime identity, and the current
`qqnt_real_smoke` state. It is a reference input for protected validation, not
a second formal runtime and not a release downgrade target.

迁移期间还会额外生成一个不可变的 Python reference archive，使正式插件清理 Python 后仍能
进行可复现的跨语言对照。制品记录精确 Git revision、内容摘要、正式运行时身份以及当前
`qqnt_real_smoke` 状态。它只用于受保护验证，不是第二运行时，也不是正式回滚目标：

```bash
SOURCE_DATE_EPOCH="$(git log -1 --format=%ct HEAD)" \
  python3 tools/ci/assemble_onebot_reference.py \
  --repository-root . \
  --output /tmp/cyrene-onebot-v11-python-reference-0.2.0.zip
```

The reference archive must be retained in the immutable release handoff with
its sidecar SHA-256 file before the formal Python runtime is removed from the
repository.

正式 Python runtime 从仓库移除前，必须先把 reference archive 及其 SHA-256 sidecar
保存在不可变 release handoff 中。

## Protected real OneBot smoke / 受保护真实 OneBot 烟测

The `onebot-real-smoke` repository-dispatch job runs only on `main`, with the
protected `onebot-real-smoke` environment and runner labels
`self-hosted, linux, x64, onebot-real`. It downloads the tested `linux-x64`
Native AOT package and covers `http_api`, `forward_websocket`, and
`reverse_websocket`. HTTP requires a successful real `send_message`; both
WebSocket profiles additionally require a matching inbound marker event.

`ONEBOT_REAL_SMOKE_APPROVED=YES`, the three endpoint variables, the dedicated
account and conversation IDs, and the fixed reverse-listener port must be
configured in the protected environment. `ONEBOT_REAL_ACCESS_TOKEN` is an
environment secret. The external reverse-WebSocket OneBot runtime must be
preconfigured to connect to that fixed listener. The script writes only
redacted health, delivery, event, and binary-digest evidence.

`onebot-real-smoke` passing is necessary for generic OneBot Python cleanup, but
does not authorize removal of the `qqnt-direct` Python reference. That profile
still requires the separate protected real QQNT smoke to pass.

`onebot-real-smoke` 只在 `main`、受保护的 `onebot-real-smoke` environment 以及
`self-hosted, linux, x64, onebot-real` runner 上执行。它下载已经通过 Native AOT
门禁的 `linux-x64` 正式包，并覆盖 `http_api`、`forward_websocket`、
`reverse_websocket` 三种 profile。HTTP 必须完成真实 `send_message`；两个 WebSocket
profile 还必须收到同一标记对应的入站事件。

受保护环境必须配置 `ONEBOT_REAL_SMOKE_APPROVED=YES`、三个 endpoint 变量、专用账号与
会话 ID、固定反向监听端口；`ONEBOT_REAL_ACCESS_TOKEN` 使用 environment secret。外部
反向 WebSocket OneBot runtime 必须预先连接该固定监听器。脚本只写入脱敏的 health、投递、
事件和二进制摘要证据。

`onebot-real-smoke` 通过只是清理通用 OneBot Python 的必要条件，不能授权删除
`qqnt-direct` Python reference；该 profile 仍需单独的真实 QQNT smoke 通过。
