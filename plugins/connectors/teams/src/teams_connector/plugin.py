"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 plugin.py                                                        │
│  Module: teams_connector.plugin                                     │
│  Role: Expose the Teams transport through DirectPluginRuntime.       │
│                                                                      │
│  模块职责：通过 DirectPluginRuntime 公开 Teams 传输                    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cyrene_plugin_runtime.configuration import read_environment_settings

from .connector import TeamsConnector, TeamsConnectorError, _direct_error


class TeamsConnectorPlugin:
    """Expose one configured Teams bot binding to Exchange.

    中文：向 Exchange 公开一个已配置的 Teams bot binding。
    """

    plugin_id = "cyrene.connectors.teams"
    version = "0.1.0"
    capabilities = ("message.connector.v1",)

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        sdk_app_factory: Any | None = None,
    ) -> None:
        selected = dict(config) if config is not None else _environment_config()
        self._sdk_app_factory = sdk_app_factory
        self._delegate: TeamsConnector | None = None
        if selected is not None:
            self._configure(selected)

    @property
    def configured_binding_id(self) -> str | None:
        """Return the activation binding selected by the host.

        中文：返回宿主选择的 activation binding。
        """

        if self._delegate is None:
            return None
        return self._delegate.config.binding_id

    @property
    def runtime_profile(self) -> str:
        """Return the fixed Teams channel profile.

        中文：返回固定的 Teams channel profile。
        """

        return "msteams-channel"

    def on_configure(self, settings: Mapping[str, str]) -> str | None:
        """Configure the binding from DirectPluginRuntime activation settings.

        中文：根据 DirectPluginRuntime activation settings 配置 binding。
        """

        try:
            config = _configuration_from_settings(settings)
            self._configure(config)
        except (
            TeamsConnectorError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            return _direct_error(
                exc
                if isinstance(exc, TeamsConnectorError)
                else TeamsConnectorError(
                    "INVALID_REQUEST", "Teams activation configuration is invalid"
                )
            )
        return None

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: Any | None = None,
        request_type_url: str | None = None,
        request_id: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, Any]:
        """Dispatch one typed message capability invocation.

        中文：分发一次带类型的消息 capability 调用。
        """

        if self._delegate is None:
            return False, _direct_error(
                TeamsConnectorError("NOT_CONFIGURED", "Teams binding is not configured")
            )
        return self._delegate.on_invoke(
            capability,
            action,
            payload,
            cancellation=cancellation,
            request_type_url=request_type_url,
            request_id=request_id,
            stream_results=stream_results,
        )

    def on_shutdown(self, grace_period_ms: int) -> None:
        """Release resources owned by this binding.

        中文：释放此 binding 拥有的资源。
        """

        if self._delegate is not None:
            self._delegate.on_shutdown(grace_period_ms)

    def close(self) -> None:
        """Close resources when embedded by a host or tests.

        中文：在嵌入式宿主或测试环境中关闭资源。
        """

        self.on_shutdown(0)

    def _configure(self, config: Mapping[str, Any]) -> None:
        """Replace the binding-local transport after validating its config.

        中文：校验配置后替换 binding 范围内的传输对象。
        """

        if self._delegate is not None:
            self._delegate.on_shutdown(0)
        self._delegate = TeamsConnector(
            config,
            sdk_app_factory=self._sdk_app_factory,
        )


def _environment_config() -> dict[str, Any] | None:
    """Decode the generic worker configuration and binding ID.

    中文：解码通用 worker 配置与 binding ID。
    """

    settings = read_environment_settings()
    if settings is None:
        return None
    return _configuration_from_settings(settings)


def _configuration_from_settings(settings: Mapping[str, str]) -> dict[str, Any]:
    """Merge the JSON activation document with the runtime binding ID.

    中文：将 JSON activation document 与 runtime binding ID 合并。
    """

    encoded = settings.get("config", "{}")
    decoded = json.loads(encoded)
    if not isinstance(decoded, Mapping):
        raise TypeError("worker configuration must be an object")
    config = dict(decoded)
    binding_id = settings.get("binding_id")
    if binding_id:
        configured_id = config.get("binding_id")
        if configured_id is not None and configured_id != binding_id:
            raise TeamsConnectorError(
                "INVALID_REQUEST", "binding_id does not match the host activation"
            )
        config["binding_id"] = binding_id
    return config
