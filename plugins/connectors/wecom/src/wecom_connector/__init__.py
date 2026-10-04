"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 __init__.py                                                     │
│  Package: wecom_connector                                           │
│  Role: Unified WeCom application, bot WebSocket, and CLI connector. │
│                                                                     │
│  模块职责：统一的企业微信连接器与工具包。                                  │
│  · message.connector.v1：出站应用消息与智能机器人 WebSocket 长连接通道    │
│  · tool.provider.v1：官方 @wecom/cli 结构化工具调用集成              │
└─────────────────────────────────────────────────────────────────────┘
"""

from .bot_client import (
    InboundMessage,
    ScriptedWsTransport,
    WeComBotClient,
    WeComWsTransport,
)
from .cli import (
    WeComCliClient,
    WeComCliError,
    WeComCliStatus,
)
from .connector import (
    CAPABILITY_ID,
    DELIVERY_RESULT_TYPE_URL,
    SEND_MESSAGE_METHOD,
    SEND_MESSAGE_REQUEST_TYPE_URL,
    TOOL_PROVIDER_CAPABILITY_ID,
    VENDOR,
    ConnectorError,
    UrllibWeComTransport,
    WeComConnector,
    WeComInstanceConfig,
    WeComTransport,
)
from .tool_provider import (
    CALL_TOOL_METHOD,
    LIST_TOOLS_METHOD,
    WeComToolProvider,
)

__all__ = [
    "CALL_TOOL_METHOD",
    "CAPABILITY_ID",
    "ConnectorError",
    "DELIVERY_RESULT_TYPE_URL",
    "InboundMessage",
    "LIST_TOOLS_METHOD",
    "SEND_MESSAGE_METHOD",
    "SEND_MESSAGE_REQUEST_TYPE_URL",
    "ScriptedWsTransport",
    "TOOL_PROVIDER_CAPABILITY_ID",
    "UrllibWeComTransport",
    "VENDOR",
    "WeComBotClient",
    "WeComCliClient",
    "WeComCliError",
    "WeComCliStatus",
    "WeComConnector",
    "WeComInstanceConfig",
    "WeComToolProvider",
    "WeComTransport",
    "WeComWsTransport",
]
