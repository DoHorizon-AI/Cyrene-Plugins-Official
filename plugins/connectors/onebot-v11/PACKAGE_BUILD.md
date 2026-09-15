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

输出目录必须为空。构建器不会发布产物、修改源码树、复制官方 QQ 安装，也不会包含原生 QQ
Host、账号数据、会话文件、密钥或凭据。得到的候选包在具备不可变 artifact、lock digest、
provenance 和受保护真实 QQ 验收前，仍保持 `NOT_PUBLISHED`。
