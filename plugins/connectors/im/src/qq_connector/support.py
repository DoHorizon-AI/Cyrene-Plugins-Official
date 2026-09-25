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
    """Structured failure returned at the connector boundary.

        中文:在连接器边界返回的结构化失败结果。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CancellationToken(Protocol):
    """Minimal cancellation seam provided by the worker.

        中文:由工作进程提供的最小取消接口。"""

    def is_cancelled(self) -> bool:
        """Return whether the current direct invocation has been cancelled.

            中文:返回当前直连调用是否已取消。"""

        ...


class ApplicationEventEmitter(Protocol):
    """Worker-owned bounded emitter for application events.

        中文:由工作进程持有、用于应用事件的有界发射器。"""

    def emit(self, event_type: str, payload: bytes, type_url: str = "") -> bool:
        """Deliver one typed event and report whether it was accepted.

            中文:投递一个有类型事件并报告其是否被接受。"""

        ...


def _raise_if_cancelled(cancellation: CancellationToken | None) -> None:
    """Raise the stable cancellation error before a side effect starts.

        中文:在副作用开始前引发稳定的取消错误。"""

    if cancellation is not None and cancellation.is_cancelled():
        raise ConnectorError("CANCELLED", "QQ operation was cancelled")
