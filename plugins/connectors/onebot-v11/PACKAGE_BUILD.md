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
