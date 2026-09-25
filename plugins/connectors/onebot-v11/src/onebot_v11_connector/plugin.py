"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 plugin.py                                                       │
│  Module: onebot_v11_connector.plugin                                │
│  Role: Python reference entrypoint for the generic OneBot connector.  │
│                                                                     │
│  模块职责：独立 OneBot v11 Python 参考运行时入口                       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cyrene_plugin_runtime.configuration import read_environment_settings

from .connector import (
    ApplicationEventEmitter,
    CancellationToken,
    OneBotV11Connector,
)


class ConnectorPlugin:
    """Expose one configured generic OneBot v11 profile.

        中文:公开一个已配置的通用 OneBot v11 配置。"""

    plugin_id = "cyrene.connectors.onebot-v11"
    version = "0.4.0"
    capabilities = ("message.connector.v1",)

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        onebot_transport: Any | None = None,
    ) -> None:
        selected = dict(config) if config is not None else _environment_config()
        self._delegate: Any = OneBotV11Connector(selected, transport=onebot_transport)

    @property
    def configured_binding_id(self) -> str | None:
        """Return the selected profile's stable binding identity.

            中文:返回所选配置的稳定绑定标识。"""

        return self._delegate.configured_binding_id

    @property
    def runtime_profile(self) -> str | None:
        """Return the selected profile label.

            中文:返回所选配置的标签。"""

        return self._delegate.runtime_profile

    def on_configure(self, settings: Mapping[str, str]) -> str | None:
        """Configure the generic OneBot profile through the worker seam.

            中文:通过工作进程接口配置通用 OneBot 配置。"""

        return self._delegate.on_configure(settings)

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: CancellationToken | None = None,
        request_type_url: str | None = None,
        request_id: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, Any]:
        """Route the formal DirectPluginRuntime invocation to the selected profile.

        The runtime invokes this method for the package data plane. Payload
        semantics stay in the selected profile, so this router adds no OneBot
        JSON hop or alternate transport.

        DirectPluginRuntime 的 Invoke 是当前插件的正式调用入口；路由层只选择
        profile，不改变 payload 语义，也不新增另一条传输路径。
        """

        return self._delegate.on_invoke(
            capability,
            action,
            payload,
            cancellation=cancellation,
            request_type_url=request_type_url,
            request_id=request_id,
            stream_results=stream_results,
        )

    def on_subscribe(
        self,
        subscription_id: str,
        capability: str,
        filter_payload: bytes,
        emitter: ApplicationEventEmitter,
    ) -> str | None:
        """Forward one binding-local event subscription to the selected profile.

            中文:将一个绑定范围内的事件订阅转发给所选配置。"""

        return self._delegate.on_subscribe(
            subscription_id, capability, filter_payload, emitter
        )

    def on_unsubscribe(self, subscription_id: str, reason: str) -> None:
        """Forward subscription cleanup to the selected profile.

            中文:将订阅清理请求转发给所选配置。"""

        self._delegate.on_unsubscribe(subscription_id, reason)

    def on_cancel(self, request_id: str, reason: str) -> None:
        """Forward direct runtime cancellation to the selected profile.

            中文:将直连运行时的取消请求转发给所选配置。"""

        cancel = getattr(self._delegate, "on_cancel", None)
        if callable(cancel):
            cancel(request_id, reason)

    def on_shutdown(self, grace_period_ms: int) -> None:
        """Stop the selected profile and its binding-local resources.

            中文:停止所选配置及其绑定范围内的资源。"""

        self._delegate.on_shutdown(grace_period_ms)

    def close(self) -> None:
        """Close the selected profile for tests and embedding hosts.

            中文:为测试和嵌入式宿主关闭所选配置。"""

        close = getattr(self._delegate, "close", None)
        if callable(close):
            close()


def _environment_config() -> dict[str, Any] | None:
    """Decode the standard worker configuration without reading other sources.

        中文:解码标准工作进程配置,不读取其他来源。"""

    settings = read_environment_settings()
    if settings is None:
        return None
    encoded = settings.get("config", "{}")
    decoded = json.loads(encoded)
    if not isinstance(decoded, Mapping):
        raise TypeError("worker configuration must be an object")
    result = dict(decoded)
    result["binding_id"] = settings["binding_id"]
    return result
