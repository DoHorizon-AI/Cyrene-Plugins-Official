# Teams connector package / Teams 连接器包

| File | Responsibility |
| --- | --- |
| `activity.py` | Validate scope facts and normalize Teams message content. |
| `config.py` | Parse private sidecar settings and read secret files into memory. |
| `connector.py` | Implement `message.connector.v1/send_message` using the official SDK. |
| `ingress.py` | Start the Teams callback ASGI app and wait for Navigator admission. |
| `navigator.py` | HMAC-sign the exact normalized event body. |
| `plugin.py` | Adapt DirectPluginRuntime invocation to the connector. |
| `_generated/` | Python projection generated from the canonical message connector proto. |

The inbound callback does not filter on @mentions. Microsoft Teams RSC sends
the rest of an installed team's channel messages; Navigator decides whether a
message starts a thread or continues an active root thread.

入站回调不会要求每条消息都 @Cyrene。Teams RSC 会发送已安装团队的频道消息；Navigator
根据根线程判断消息是开启新任务还是继续已有对话。
