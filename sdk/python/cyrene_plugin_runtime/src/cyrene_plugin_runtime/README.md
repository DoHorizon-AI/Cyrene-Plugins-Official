# Runtime package modules / 运行时包模块

| File | Responsibility | 中文职责 |
|---|---|---|
| `server.py` | Host one configured Plugin on the direct gRPC protocol. | 通过直连 gRPC 协议托管一个已配置 Plugin。 |
| `client.py` | Open an opaque local connection reference and invoke the Plugin contract. | 打开不透明本地连接引用并调用 Plugin 契约。 |
| `bootstrap.py` | Start the vendored server from an unpacked source package. | 从已解包源码包启动随包分发的服务。 |
| `dependency_preparer.py` | Build a lock-addressed Python runtime through the generic Platform adapter protocol. | 通过 Platform 通用适配协议构建按依赖锁寻址的 Python 运行时。 |

Read the generated wire contract first, then `server.py` and `client.py`.
Package lifecycle integrations should read `dependency_preparer.py` and
`bootstrap.py` together.

建议先阅读生成的 wire contract，再阅读 `server.py` 和 `client.py`。包生命周期集成应将
`dependency_preparer.py` 与 `bootstrap.py` 配套阅读。
