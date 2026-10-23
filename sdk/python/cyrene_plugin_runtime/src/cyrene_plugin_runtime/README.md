# Runtime package modules / 运行时包模块

| File | Responsibility | 中文职责 |
|---|---|---|
| `server.py` | Host one configured Plugin on the direct gRPC protocol. | 通过直连 gRPC 协议托管一个已配置 Plugin。 |
| `client.py` | Open an opaque local connection reference and invoke the Plugin contract. | 打开不透明本地连接引用并调用 Plugin 契约。 |
| `bootstrap.py` | Start the vendored server from an unpacked source package. | 从已解包源码包启动随包分发的服务。 |
| `dependency_preparer.py` | Build a lock-addressed Python runtime through the generic Platform adapter protocol. | 通过 Platform 通用适配协议构建按依赖锁寻址的 Python 运行时。 |

The `server.py` entrypoint accepts `--tls-cert-file`, `--tls-key-file`, and
`--tls-client-ca-file` together. This enables gRPC mutual TLS and requires a
client certificate issued by the configured CA. The key and certificates are
read from absolute mounted file paths; the server never prints their contents.
When omitted, plaintext is restricted to loopback or Unix-domain listeners for
local compatibility.

`server.py` entrypoint 可同时接收 `--tls-cert-file`、`--tls-key-file` 和
`--tls-client-ca-file`，启用 gRPC 双向 TLS，并要求客户端证书由配置的 CA 签发。服务端从绝对路径读取只读挂载的
文件，不会输出证书或私钥内容。省略这些参数时，明文兼容模式仅允许 loopback 或 Unix-domain listener。

Read the generated wire contract first, then `server.py` and `client.py`.
Package lifecycle integrations should read `dependency_preparer.py` and
`bootstrap.py` together.

建议先阅读生成的 wire contract，再阅读 `server.py` 和 `client.py`。包生命周期集成应将
`dependency_preparer.py` 与 `bootstrap.py` 配套阅读。
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# Runtime package 模块

| 文件 | 职责 |
| --- | --- |
| server.py | 使用直连 gRPC 协议托管一个已配置 Plugin。 |
| client.py | 打开不透明的本地 connection reference 并调用 Plugin contract。 |
| bootstrap.py | 从已解包的源码 package 启动随包提供的 server。 |
| dependency_preparer.py | 通过 Platform 通用 adapter 协议构建由 lock 标识的 Python runtime。 |

先阅读生成的 wire contract，然后阅读 server.py 和 client.py。package 生命周期集成应一并阅读 dependency_preparer.py 与 bootstrap.py。
