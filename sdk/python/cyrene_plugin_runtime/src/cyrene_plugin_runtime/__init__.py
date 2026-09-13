"""Plugin-owned direct endpoint runtime."""

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
    "serve",
]

_CLIENT_EXPORTS = frozenset(name for name in __all__ if name not in {"DirectPluginService", "serve"})


def __getattr__(name: str) -> Any:
    """Load public runtime helpers without importing the CLI module eagerly."""

    if name in _CLIENT_EXPORTS:
        from . import client

        return getattr(client, name)
    if name in {"DirectPluginService", "serve"}:
        from . import server

        return getattr(server, name)
    raise AttributeError(name)
