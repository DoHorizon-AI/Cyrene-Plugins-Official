###############################################################################
# 📄 File: plugins/connectors/onebot-v11/src/onebot_v11_connector/__init__.py
# Module: Cyrene Plugins Official
# Role: Protocol or message connector implementation.
#
# This file documents an active plugin boundary; runtime behavior is unchanged.
#
# 模块：Cyrene Plugins Official
# 职责：协议或消息连接器实现。
# 本文件属于活动插件边界；运行时行为保持不变。
###############################################################################
"""Official generic OneBot v11 connector package."""

from .connector import (
    CAPABILITY_ID,
    DELIVERY_RESULT_TYPE_URL,
    INBOUND_MESSAGE_EVENT_TYPE,
    INBOUND_MESSAGE_TYPE_URL,
    INBOUND_REQUEST_EVENT_TYPE,
    INBOUND_REQUEST_TYPE_URL,
    INTERFACE_VERSION,
    ONEBOT_VENDOR,
    RESPOND_REQUEST_METHOD,
    RESPOND_REQUEST_TYPE_URL,
    RESPOND_RESULT_TYPE_URL,
    SEND_MESSAGE_METHOD,
    SEND_MESSAGE_REQUEST_TYPE_URL,
    ApplicationEventEmitter,
    CancellationToken,
    ConnectorError,
    OneBotInstanceConfig,
    OneBotTransport,
    OneBotV11Connector,
    UrllibOneBotTransport,
    build_onebot_segments,
    normalize_inbound_event,
    normalize_request_event,
)
from .plugin import ConnectorPlugin
from .transport import OneBotReverseWebSocketServer, OneBotWebSocketTransport

__all__ = [
    "CAPABILITY_ID",
    "DELIVERY_RESULT_TYPE_URL",
    "INBOUND_MESSAGE_EVENT_TYPE",
    "INBOUND_MESSAGE_TYPE_URL",
    "INBOUND_REQUEST_EVENT_TYPE",
    "INBOUND_REQUEST_TYPE_URL",
    "INTERFACE_VERSION",
    "ONEBOT_VENDOR",
    "RESPOND_REQUEST_METHOD",
    "RESPOND_REQUEST_TYPE_URL",
    "RESPOND_RESULT_TYPE_URL",
    "SEND_MESSAGE_REQUEST_TYPE_URL",
    "SEND_MESSAGE_METHOD",
    "ApplicationEventEmitter",
    "CancellationToken",
    "ConnectorError",
    "OneBotInstanceConfig",
    "OneBotTransport",
    "OneBotV11Connector",
    "UrllibOneBotTransport",
    "OneBotWebSocketTransport",
    "OneBotReverseWebSocketServer",
    "ConnectorPlugin",
    "build_onebot_segments",
    "normalize_inbound_event",
    "normalize_request_event",
]
