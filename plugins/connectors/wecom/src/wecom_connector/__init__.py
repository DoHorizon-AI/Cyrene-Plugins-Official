"""WeCom application-message connector for ``message.connector.v1``."""

from .connector import (
    CAPABILITY_ID,
    DELIVERY_RESULT_TYPE_URL,
    SEND_MESSAGE_METHOD,
    SEND_MESSAGE_REQUEST_TYPE_URL,
    VENDOR,
    ConnectorError,
    UrllibWeComTransport,
    WeComConnector,
    WeComInstanceConfig,
    WeComTransport,
)

__all__ = [
    "CAPABILITY_ID",
    "DELIVERY_RESULT_TYPE_URL",
    "SEND_MESSAGE_METHOD",
    "SEND_MESSAGE_REQUEST_TYPE_URL",
    "VENDOR",
    "ConnectorError",
    "UrllibWeComTransport",
    "WeComConnector",
    "WeComInstanceConfig",
    "WeComTransport",
]
