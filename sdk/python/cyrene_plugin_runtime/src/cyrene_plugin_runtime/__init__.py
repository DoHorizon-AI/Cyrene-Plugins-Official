"""Plugin-owned direct endpoint runtime.

中文:Plugin 持有的直连端点运行时。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import (
        DirectPayload,
        DirectPluginClient,
        DirectPluginDeadlineExceeded,
        DirectPluginError,
        DirectPluginFailure,
        DirectPluginInvocationCancelled,
        DirectPluginProtocolError,
        DirectPluginTransportError,
    )
    from .server import DirectPluginService

__all__ = [
    "DirectPayload",
    "DirectPluginClient",
    "DirectPluginDeadlineExceeded",
    "DirectPluginError",
    "DirectPluginFailure",
    "DirectPluginInvocationCancelled",
    "DirectPluginProtocolError",
    "DirectPluginService",
    "DirectPluginTransportError",
    "emit_diagnostic_error",
    "format_cyrene_log",
    "map_plugin_error",
    "parse_w3c_traceparent",
    "sanitize_request_id",
    "serve",
]

_CLIENT_EXPORTS = frozenset(
    name for name in __all__
    if name not in {
        "DirectPluginService",
        "serve",
        "emit_diagnostic_error",
        "format_cyrene_log",
        "map_plugin_error",
        "parse_w3c_traceparent",
        "sanitize_request_id",
    }
)

_LOGGING_EXPORTS = frozenset(
    {
        "emit_diagnostic_error",
        "format_cyrene_log",
        "parse_w3c_traceparent",
        "sanitize_request_id",
    }
)


def __getattr__(name: str) -> Any:
    """Load public runtime helpers without importing the CLI module eagerly.

        中文:加载公开的运行时辅助项,但不提前导入 CLI 模块。"""

    if name in _CLIENT_EXPORTS:
        from . import client

        return getattr(client, name)
    if name in {"DirectPluginService", "serve"}:
        from . import server

        return getattr(server, name)
    if name in _LOGGING_EXPORTS:
        from . import logging

        return getattr(logging, name)
    if name == "map_plugin_error":
        from . import errors

        return getattr(errors, name)
    raise AttributeError(name)
