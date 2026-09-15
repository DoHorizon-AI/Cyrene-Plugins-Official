# OneBot v11 package candidate / OneBot v11 候选包

This package is assembled as an unpacked Platform payload, then optionally
compressed as ZIP. The builder copies the Plugins-owned SDK runtime into the
temporary payload at `src/cyrene_plugin_runtime`; it does not maintain a second
tracked runtime source tree.

本包按 Platform payload 方式装配，可保留为解包目录或压缩为 ZIP。构建器会把 Plugins 自有的
SDK runtime 复制到临时 payload 的 `src/cyrene_plugin_runtime`，不会在仓库中维护第二份源码。

## Build / 构建

From the Plugins repository root:

```bash
python3 plugins/connectors/onebot-v11/tools/assemble_package.py \
  --repository-root . --output /tmp/cyrene-onebot-v11.zip
```

To inspect an unpacked candidate:

```bash
python3 plugins/connectors/onebot-v11/tools/assemble_package.py \
  --repository-root . --output /tmp/cyrene-onebot-v11 --directory
```

The output directory must be empty. The builder never publishes, mutates the
source tree, copies an official QQ installation, or includes a native QQ Host,
account data, session files, secrets, or credentials. The resulting candidate
remains `NOT_PUBLISHED` until an immutable artifact, lock digest, provenance,
and the protected real-QQ acceptance are available.

## Artifact vertical gate / Artifact 垂直门禁

The package CI job also extracts the ZIP into an isolated directory and starts
two direct workers from that installed artifact. The workers exercise direct
`Health`, QQ extension `Invoke`, message `InvokeStream`, binding-local fake Host
dispatch, and shutdown cleanup without adding a OneBot transport hop:

包 CI 还会把 ZIP 解压到隔离目录，并从该已安装 artifact 启动两个 direct worker。两个 worker
会实际执行 direct `Health`、QQ 扩展 `Invoke`、消息 `InvokeStream`、binding 隔离的 fake Host
调用和关闭清理，不增加 OneBot transport 中转：

```bash
PYTHONPATH=sdk/python/cyrene_plugin_runtime/src \
  python3 -m pytest -q \
  plugins/connectors/onebot-v11/tests/test_package.py \
  -k installed_artifact
```

This is an artifact-level Plugin gate. It is not the Workspace P2.5
Product/AstrBot/pgvector harness and it cannot promote any row to official QQ
runtime `IMPLEMENTED`; those remain separate acceptance gates.

这是 Plugins 内的 artifact-level 门禁，不是 Workspace P2.5 Product/AstrBot/pgvector 三仓
harness，也不能把任何能力行提升为官方 QQ runtime 的 `IMPLEMENTED`；两者仍是独立验收门禁。

输出目录必须为空。构建器不会发布产物、修改源码树、复制官方 QQ 安装，也不会包含原生 QQ
Host、账号数据、会话文件、密钥或凭据。得到的候选包在具备不可变 artifact、lock digest、
provenance 和受保护真实 QQ 验收前，仍保持 `NOT_PUBLISHED`。
