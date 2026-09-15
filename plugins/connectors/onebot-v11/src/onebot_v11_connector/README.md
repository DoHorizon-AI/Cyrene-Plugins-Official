# Directory Guide: `plugins/connectors/onebot-v11/src/onebot_v11_connector`

This directory contains the source files for the module boundary shown in its path. Read the direct entry points first, then follow child packages and tests.

该目录存放 `plugins/connectors/onebot-v11/src/onebot_v11_connector` 对应模块边界的源码。建议先读直接入口，再按子包和测试继续展开。

## Contents / 内容

| File | Responsibility | 职责 |
| --- | --- | --- |
| `__init__.py` | Package initialization and public exports | Package initialization and public exports |
| `connector.py` | Python implementation module | Python implementation module |
| `plugin.py` | Profile router for OneBot v11 and QQNT direct | OneBot v11 与 QQNT direct profile 路由 |
| `qqnt_direct.py` | Direct QQ adapter and canonical message mapping | QQ 直连适配与规范消息映射 |
| `qqnt_direct_host.py` | One-binding QQ Host subprocess lifecycle | 单 binding QQ Host 子进程生命周期 |
| `qqnt_direct_operations.py` | Fixed QQ API operation allow-list | 固定 QQ API 操作白名单 |
| `qqnt_direct_protocol.py` | Length-delimited stdio frame codec | 长度分帧 stdio 编解码 |
| `transport.py` | Python implementation module | Python implementation module |

### Child directories / 子目录

| Directory | Responsibility | 职责 |
| --- | --- | --- |
| `_generated/` | Child package or layer | 子包或分层目录 |

## Suggested reading order / 推荐阅读顺序

Read the direct implementation or package entry point, then child directories in the table above, and finally the nearest tests or conformance notes.

先读直接实现或包入口，再按上表进入子目录，最后阅读最近的测试或一致性说明。
