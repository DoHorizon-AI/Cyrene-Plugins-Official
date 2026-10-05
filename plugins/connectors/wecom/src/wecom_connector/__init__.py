"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 __init__.py                                                     │
│  Package: wecom_connector                                           │
│  Role: Unified WeCom application, bot, Navigator, and CLI connector. │
│                                                                     │
│  模块职责：统一的企业微信连接器与工具包。                                  │
│  · message.connector.v1：应用出站、bot 入站事件和本地 Navigator intake   │
│  · tool.provider.v1：官方 @wecom/cli 结构化工具调用集成              │
└─────────────────────────────────────────────────────────────────────┘
"""

from .bot_client import (
    InboundMessage,
    ScriptedWsTransport,
    WeComBotClient,
    WeComBotRequestError,
    WeComWsTransport,
)
from .bot_runtime import WeComBotRuntime
from .cli import (
    WeComCliClient,
    WeComCliError,
    WeComCliStatus,
)
from .connector import (
    CAPABILITY_ID,
    DELIVERY_RESULT_TYPE_URL,
    INBOUND_MESSAGE_EVENT_TYPE,
    INBOUND_MESSAGE_TYPE_URL,
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
from .navigator import (
    NavigatorIntegrationConfig,
    NavigatorTaskClient,
    TrustedConversation,
    notification_recipient_key,
)
from .navigator_workbridge import (
    NavigatorWeComBridge,
    create_navigator_wecom_bridge,
)
from .notification_worker import NavigatorNotificationWorker
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
    "INBOUND_MESSAGE_EVENT_TYPE",
    "INBOUND_MESSAGE_TYPE_URL",
    "LIST_TOOLS_METHOD",
    "NavigatorIntegrationConfig",
    "NavigatorNotificationWorker",
    "NavigatorTaskClient",
    "NavigatorWeComBridge",
    "SEND_MESSAGE_METHOD",
    "SEND_MESSAGE_REQUEST_TYPE_URL",
    "ScriptedWsTransport",
    "TOOL_PROVIDER_CAPABILITY_ID",
    "UrllibWeComTransport",
    "VENDOR",
    "WeComBotClient",
    "WeComBotRequestError",
    "WeComBotRuntime",
    "WeComCliClient",
    "WeComCliError",
    "WeComCliStatus",
    "WeComConnector",
    "WeComInstanceConfig",
    "WeComToolProvider",
    "WeComTransport",
    "WeComWsTransport",
    "TrustedConversation",
    "notification_recipient_key",
    "create_navigator_wecom_bridge",
]
