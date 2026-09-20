"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 plugin.py                                                        │
│  Module: qq_connector                                                │
│  Role: Python reference entrypoint for the independent QQ profile.    │
│                                                                     │
│  模块职责：独立 QQ/IM Python 参考运行时入口                            │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cyrene_plugin_runtime.configuration import read_environment_settings

from .qqnt_direct import QQNTDirectConfig, QQNTDirectConnector
from .support import ApplicationEventEmitter, CancellationToken


class ConnectorPlugin:
    """Expose exactly one configured QQNT direct profile."""

    plugin_id = "cyrene.connectors.im"
    version = "0.1.0"
    capabilities = ("message.connector.v1", "qq.client.v1")

    def __init__(
        self, config: Mapping[str, Any] | None = None, *, host: Any = None
    ) -> None:
        selected = dict(config) if config is not None else _environment_config()
        self._delegate = (
            QQNTDirectConnector(selected, host=host)
            if selected
            else QQNTDirectConnector()
        )

    @property
    def configured_binding_id(self) -> str | None:
        """Return the configured binding identity."""

        return self._delegate.configured_binding_id

    @property
    def runtime_profile(self) -> str | None:
        """Return the selected runtime profile."""

        return self._delegate.runtime_profile

    def on_configure(self, settings: Mapping[str, str]) -> str | None:
        """Configure the QQ profile through the worker activation seam."""

        try:
            replacement = QQNTDirectConnector(QQNTDirectConfig.from_settings(settings))
        except Exception as exc:  # noqa: BLE001 - worker seam returns typed text.
            return str(exc)
        previous = self._delegate
        self._delegate = replacement
        previous.close()
        return None

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
        """Forward one typed direct-runtime invocation to the QQ adapter."""

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
        """Create one binding-local QQ event subscription."""

        return self._delegate.on_subscribe(
            subscription_id, capability, filter_payload, emitter
        )

    def on_unsubscribe(self, subscription_id: str, reason: str) -> None:
        """Release one binding-local event subscription."""

        self._delegate.on_unsubscribe(subscription_id, reason)

    def on_cancel(self, request_id: str, reason: str) -> None:
        """Forward direct-runtime cancellation to the QQ adapter."""

        self._delegate.on_cancel(request_id, reason)

    def on_shutdown(self, grace_period_ms: int) -> None:
        """Stop the QQ Host process within the requested grace period."""

        self._delegate.on_shutdown(grace_period_ms)

    def close(self) -> None:
        """Close the QQ profile for tests and embedding hosts."""

        self._delegate.close()


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
