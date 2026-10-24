# Teams connector source / Teams 连接器源代码

| Path | Responsibility |
| --- | --- |
| `teams_connector/activity.py` | Normalize a JWT-authenticated Teams message to message.connector.v1 and its stable trust envelope. |
| `teams_connector/config.py` | Load tenant/team allowlists and host secret-file references. |
| `teams_connector/connector.py` | Send a canonical channel reply through the Microsoft Teams SDK. |
| `teams_connector/ingress.py` | Host the SDK callback, HMAC Navigator relay, and health endpoints. |
| `teams_connector/navigator.py` | Authenticate an internal event submission with a raw-body HMAC. |
| `teams_connector/plugin.py` | Expose the connector through DirectPluginRuntime. |

Read `activity.py` and `config.py` before `ingress.py`; then follow the runtime
entrypoint in `plugin.py` for the outbound capability path.

先读 `activity.py` 与 `config.py`，再读 `ingress.py`；随后从 `plugin.py` 查看出站
capability 入口。
