"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 plugin.py                                                       │
│  Module: onebot_v11_connector.plugin                                │
│  Role: Profile router for the generic and direct connector adapters. │
│                                                                     │
│  模块职责：按 binding 配置选择 onebot-v11 或 qqnt-direct 实现。          │
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
from .qqnt_direct import QQ_CAPABILITY_ID, QQNTDirectConfig, QQNTDirectConnector


class ConnectorPlugin:
    """Dispatch one package activation to exactly one selected profile."""

    plugin_id = "cyrene.connectors.onebot-v11"
    version = "0.2.0"
    capabilities = ("message.connector.v1", QQ_CAPABILITY_ID)

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        onebot_transport: Any | None = None,
        qq_host: Any | None = None,
    ) -> None:
        selected = dict(config) if config is not None else _environment_config()
        if selected is not None and selected.get("runtime_profile") == "qqnt-direct":
            self._delegate: Any = QQNTDirectConnector(selected, host=qq_host)
        else:
            self._delegate = OneBotV11Connector(selected, transport=onebot_transport)

    @property
    def configured_binding_id(self) -> str | None:
        """Return the selected profile's stable binding identity."""

        return self._delegate.configured_binding_id

    @property
    def runtime_profile(self) -> str | None:
        """Return the selected profile label."""

        return self._delegate.runtime_profile

    def on_configure(self, settings: Mapping[str, str]) -> str | None:
        """Configure the selected profile through the generic worker seam."""

        if _settings_profile(settings) == "qqnt-direct":
            try:
                parsed = QQNTDirectConfig.from_settings(settings)
                replacement = QQNTDirectConnector(parsed)
            except Exception as exc:  # noqa: BLE001 - worker seam returns typed text.
                return str(exc)
            previous = self._delegate
            self._delegate = replacement
            close = getattr(previous, "close", None)
            if callable(close):
                close()
            return None
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
        """Forward one binding-local event subscription to the selected profile."""

        return self._delegate.on_subscribe(
            subscription_id, capability, filter_payload, emitter
        )

    def on_unsubscribe(self, subscription_id: str, reason: str) -> None:
        """Forward subscription cleanup to the selected profile."""

        self._delegate.on_unsubscribe(subscription_id, reason)

    def on_cancel(self, request_id: str, reason: str) -> None:
        """Forward direct runtime cancellation to the selected profile."""

        cancel = getattr(self._delegate, "on_cancel", None)
        if callable(cancel):
            cancel(request_id, reason)

    def on_shutdown(self, grace_period_ms: int) -> None:
        """Stop the selected profile and its binding-local resources."""

        self._delegate.on_shutdown(grace_period_ms)

    def close(self) -> None:
        """Close the selected profile for tests and embedding hosts."""

        close = getattr(self._delegate, "close", None)
        if callable(close):
            close()


def _environment_config() -> dict[str, Any] | None:
    """Decode the standard worker configuration without reading other sources."""

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


def _settings_profile(settings: Mapping[str, str]) -> str | None:
    """Read only the profile discriminator from a worker settings mapping."""

    encoded = settings.get("config", "{}")
    try:
        decoded = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, Mapping):
        return None
    profile = decoded.get("runtime_profile")
    return profile if isinstance(profile, str) else None
