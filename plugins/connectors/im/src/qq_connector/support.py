"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 support.py                                                       │
│  Module: qq_connector                                                │
│  Role: Small runtime seams shared by the Python QQ reference.         │
│                                                                     │
│  模块职责：为 Python QQ 参考实现提供最小运行时错误与事件边界             │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from typing import Protocol

MESSAGE_CONNECTOR_TYPE_PREFIX = "type.cyrene.io/cyrene.message.connector.v1."
INBOUND_MESSAGE_EVENT_TYPE = "inbound_message"
INBOUND_REQUEST_EVENT_TYPE = "inbound_request"
INBOUND_MESSAGE_TYPE_URL = f"{MESSAGE_CONNECTOR_TYPE_PREFIX}InboundMessagePayload"
DELIVERY_RESULT_TYPE_URL = f"{MESSAGE_CONNECTOR_TYPE_PREFIX}DeliveryResult"
RESPOND_RESULT_TYPE_URL = "type.cyrene.io/message.connector.v1.respond_request.response"


class ConnectorError(RuntimeError):
    """Structured failure returned at the connector boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CancellationToken(Protocol):
    """Minimal cancellation seam provided by the worker."""

    def is_cancelled(self) -> bool:
        """Return whether the current direct invocation has been cancelled."""

        ...


class ApplicationEventEmitter(Protocol):
    """Worker-owned bounded emitter for application events."""

    def emit(self, event_type: str, payload: bytes, type_url: str = "") -> bool:
        """Deliver one typed event and report whether it was accepted."""

        ...


def _raise_if_cancelled(cancellation: CancellationToken | None) -> None:
    """Raise the stable cancellation error before a side effect starts."""

    if cancellation is not None and cancellation.is_cancelled():
        raise ConnectorError("CANCELLED", "QQ operation was cancelled")
