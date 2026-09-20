# IM Native AOT Operations / IM Native AOT 运维说明

The formal package is `cyrene.connectors.im@0.1.0`, launched as
`bin/cyrene-im` with the `DirectPluginRuntime` protocol. The executable
provides `message.connector.v1` and `qq.client.v1`.

正式包是 `cyrene.connectors.im@0.1.0`，入口为 `bin/cyrene-im`，使用
`DirectPluginRuntime` 协议，并提供 `message.connector.v1` 与 `qq.client.v1`。

## Runtime boundary / 运行边界

The IM runtime starts an authorized QQ Host child using inherited stdio and
the `cyrene.qq.host.v1` framing contract. It does not open a public listener,
load Python, load dynamic assemblies, or accept arbitrary QQ service/method
names. Host installation, client version, ABI, account, and restart policy are
binding-scoped configuration.

IM runtime 通过继承的 stdio 和 `cyrene.qq.host.v1` framing 合约启动授权 QQ
Host 子进程。不开放公共监听端口，不加载 Python 或动态程序集，也不接受任意
QQ Service/Method。Host 安装、客户端版本、ABI、账号和重启策略都属于 binding
级配置。

## Evidence policy / 证据策略

- C# unit/parity tests and the fake Host TCK establish implementation behavior.
- A packaged Native AOT exercise establishes that the released layout starts and
  serves the runtime contract outside the source tree.
- The protected real smoke is `NOT_RUN` unless the exact authorized QQ build and
  account environment are explicitly available.

- C# 单元/等价测试与 fake Host TCK 证明实现行为。
- 打包后的 Native AOT exercise 证明脱离源码目录仍能启动并提供运行时合约。
- 未获得精确授权 QQ build 与账号环境前，受保护真实烟测保持 `NOT_RUN`。
