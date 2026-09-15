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
from .qqnt_direct import (
    QQ_CAPABILITY_ID,
    QQ_REQUEST_TYPE_URL,
    QQ_RESPONSE_TYPE_URL,
    QQNTDirectConfig,
    QQNTDirectConnector,
)
from .qqnt_direct_discovery import (
    INSTALLATION_MANIFEST_SCHEMA,
    SUPPORTED_PLATFORM,
    QQInstallation,
    QQInstallationError,
    discover_explicit,
    discover_manifest,
    discover_manifests,
)
from .qqnt_direct_host import (
    QQ_HOST_PROTOCOL,
    QQ_HOST_PROTOCOL_VERSION,
    QQHostClient,
    QQHostError,
    QQHostLaunchConfig,
)
from .qqnt_direct_operations import (
    CALLBACK_ONLY_OPERATION_NAMES,
    QQ_OPERATION_NAMES,
    QQ_OPERATIONS,
    QQOperation,
)
from .qqnt_direct_protocol import (
    QQHostProtocolError,
    encode_frame,
    read_frame,
    write_frame,
)
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
    "QQ_CAPABILITY_ID",
    "QQNTDirectConfig",
    "QQNTDirectConnector",
    "QQ_REQUEST_TYPE_URL",
    "QQ_RESPONSE_TYPE_URL",
    "SUPPORTED_PLATFORM",
    "INSTALLATION_MANIFEST_SCHEMA",
    "QQInstallation",
    "QQInstallationError",
    "discover_explicit",
    "discover_manifest",
    "discover_manifests",
    "QQ_HOST_PROTOCOL",
    "QQ_HOST_PROTOCOL_VERSION",
    "QQHostClient",
    "QQHostError",
    "QQHostLaunchConfig",
    "QQOperation",
    "CALLBACK_ONLY_OPERATION_NAMES",
    "QQ_OPERATION_NAMES",
    "QQ_OPERATIONS",
    "encode_frame",
    "read_frame",
    "write_frame",
    "QQHostProtocolError",
    "build_onebot_segments",
    "normalize_inbound_event",
    "normalize_request_event",
]
