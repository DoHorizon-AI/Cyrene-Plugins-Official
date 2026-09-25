# Directory Guide: `plugins/connectors/onebot-v11/src/onebot_v11_connector`

This directory contains the source files for the module boundary shown in its path. Read the direct entry points first, then follow child packages and tests.

该目录存放 `plugins/connectors/onebot-v11/src/onebot_v11_connector` 对应模块边界的源码。建议先读直接入口，再按子包和测试继续展开。

## Contents / 内容

| File | Responsibility | 职责 |
| --- | --- | --- |
| `__init__.py` | Package initialization and public exports | Package initialization and public exports |
| `plugin.py` | Generic OneBot v11 plugin entry point | 通用 OneBot v11 插件入口 |
| `connector.py` | OneBot HTTP/WebSocket connector implementation | OneBot HTTP/WebSocket 连接器实现 |
| `transport.py` | OneBot transport lifecycle and request handling | OneBot 传输生命周期与请求处理 |
| `_generated/` | Generated OneBot API models | 生成的 OneBot API 模型 |

### Child directories / 子目录

| Directory | Responsibility | 职责 |
| --- | --- | --- |
| `_generated/` | Child package or layer | 子包或分层目录 |

## Suggested reading order / 推荐阅读顺序

Read the direct implementation or package entry point, then child directories in the table above, and finally the nearest tests or conformance notes.

先读直接实现或包入口，再按上表进入子目录，最后阅读最近的测试或一致性说明。
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# 目录指南：plugins/connectors/onebot-v11/src/onebot_v11_connector

本目录包含路径所示模块边界的源文件。先阅读直接入口，再继续查看子 package 和测试。

## 内容

| 文件 | 职责 |
| --- | --- |
| __init__.py | package 初始化和公共导出 |
| plugin.py | 通用 OneBot v11 plugin 入口 |
| connector.py | OneBot HTTP/WebSocket connector 实现 |
| transport.py | OneBot transport 生命周期和请求处理 |
| _generated/ | 生成的 OneBot API model |

## 子目录

| 目录 | 职责 |
| --- | --- |
| _generated/ | 子 package 或分层目录 |

## 建议阅读顺序

先阅读直接实现或 package 入口，再查看上表中的子目录，最后阅读最近的测试或一致性说明。
