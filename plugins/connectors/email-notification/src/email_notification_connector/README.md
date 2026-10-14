# Package source / 软件包源码

| File | Responsibility |
| --- | --- |
| `settings.py` | Validate the binding and resolve only named environment secrets. |
| `smtp_transport.py` | Open a configured SMTP session and classify send outcomes around `DATA`. |
| `plugin.py` | Implement the fixed `tool.provider.v1` entrypoint and signed caller gate. |
| `__init__.py` | Export the runtime entrypoint. |
| `_generated/tool_provider_pb2.py` | Generated bindings for the canonical Tool Provider protobuf contract. |

Read `settings.py`, then `smtp_transport.py`, then `plugin.py`.

先阅读 `settings.py`，再阅读 `smtp_transport.py`，最后阅读 `plugin.py`。
