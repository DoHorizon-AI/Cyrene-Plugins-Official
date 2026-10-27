# Teams connector package / Teams 连接器包

| File | Responsibility |
| --- | --- |
| `activity.py` | Validate scope facts and normalize channel/private messages plus personal install lifecycle events. |
| `config.py` | Parse private sidecar settings and read secret files into memory. |
| `connector.py` | Implement channel replies and host-resolved private sends with `message.connector.v1/send_message`. |
| `delivery_spool.py` | Persist canonical event bodies, coordinate leases/pause CAS, and retry Navigator delivery. |
| `ingress.py` | Start the Teams callback ASGI app and choose direct or durable admission. |
| `navigator.py` | HMAC-sign the exact canonical event body with a fresh timestamp. |
| `plugin.py` | Adapt DirectPluginRuntime invocation to the connector. |
| `_generated/` | Python projection generated from the canonical message connector proto. |

Read `activity.py`, `navigator.py`, and `delivery_spool.py` before `ingress.py`.
The spool stores only the normalized Navigator body and delivery metadata. It
never stores Bot Framework or Navigator authorization headers.

先阅读 `activity.py`、`navigator.py` 与 `delivery_spool.py`，再看 `ingress.py`。spool
只保存规范 Navigator body 和投递元数据，不保存 Bot Framework 或 Navigator authorization header。

The inbound callback does not filter on @mentions. Microsoft Teams RSC sends
the rest of an installed team's channel messages; Navigator decides whether a
message starts a thread or continues an active root thread.

入站回调不会要求每条消息都 @Cyrene。Teams RSC 会发送已安装团队的频道消息；Navigator
根据根线程判断消息是开启新任务还是继续已有对话。

Personal bot messages and installation add/remove events require the explicit
`personal_chat_enabled` activation flag and `TEAMS_PERSONAL_CHAT_ENABLED=true`.
Both default to off. The source forwards signed personal installation facts in
the private trust envelope; Work owns actor authorization, route persistence,
and revocation. Private sends use the existing canonical PRIVATE conversation
kind and require a host-resolved `owner_private` target.

个人 bot 消息与安装/移除事件要求显式设置 `personal_chat_enabled` 与
`TEAMS_PERSONAL_CHAT_ENABLED=true`，两者默认关闭。源码只在私有 trust envelope 中转发经签名的
个人安装事实；Work 负责 actor 授权、路由持久化与撤销。个人消息发送复用现有 canonical PRIVATE
conversation kind，并要求宿主解析的 `owner_private` 目标。
