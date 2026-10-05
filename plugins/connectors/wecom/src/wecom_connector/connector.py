"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 connector.py                                                    │
│  Package: wecom_connector                                           │
│  Role: Unified WeCom application, bot WebSocket, and CLI connector. │
│                                                                     │
│  模块职责：企业微信应用消息、智能机器人长连接与官方 CLI 工具连接器。     │
│  · message.connector.v1：                                           │
│    - app 模式：厂商 wire translation、token 缓存与确定性状态映射。     │
│    - bot 模式：OpenWS WebSocket 智能机器人长连接收发与流式响应。       │
│    - hybrid 模式：根据 account_id 或会话类型自适应路由。             │
│  · tool.provider.v1：                                               │
│    - 暴露官方 @wecom/cli 结构化工具目录与安全执行调用。             │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

from google.protobuf.message import DecodeError

from ._generated import message_connector_pb2 as message_contract
from .bot_client import (
    DEFAULT_WS_URL,
    InboundMessage,
    WeComBotClient,
    WeComBotRequestError,
    WeComWsTransport,
)
from .bot_runtime import WeComBotRuntime
from .cli import WeComCliClient
from .errors import ConnectorError
from .navigator import NavigatorIntegrationConfig, NavigatorTaskClient
from .notification_worker import NavigatorNotificationWorker
from .tool_provider import (
    CALL_TOOL_METHOD,
    CALL_TOOL_REQUEST_TYPE_URL,
    CALL_TOOL_RESPONSE_TYPE_URL,
    LIST_TOOLS_METHOD,
    LIST_TOOLS_REQUEST_TYPE_URL,
    LIST_TOOLS_RESPONSE_TYPE_URL,
    TOOL_PROVIDER_CAPABILITY_ID,
    WeComToolProvider,
)

logger = logging.getLogger(__name__)

CAPABILITY_ID = "message.connector.v1"
SEND_MESSAGE_METHOD = "send_message"
VENDOR = "wecom.app"
BOT_VENDOR = "wecom.bot"
INBOUND_MESSAGE_EVENT_TYPE = "inbound_message"
INBOUND_MESSAGE_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.InboundMessagePayload"
)

SEND_MESSAGE_REQUEST_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest"
)
DELIVERY_RESULT_TYPE_URL = "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult"

DEFAULT_BASE_URL = "https://qyapi.weixin.qq.com"
SUPPORTED_TEXT_MSG_TYPES = ("text", "markdown")
INVALID_TOKEN_CODES = (40001, 40014, 42001)
RATE_LIMIT_CODES = (45009, 45047)
MIN_MEDIA_BYTES = 6
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
IMAGE_MIME_TYPES = {"image/jpeg", "image/jpg", "image/png"}
WECOM_WRITE_OPERATION_IDS = frozenset(
    {"calendar.schedules.create", "todo.create", "todo.finish"}
)

_KIND_NAMES = {
    message_contract.CONVERSATION_KIND_UNSPECIFIED: "unspecified",
    message_contract.CONVERSATION_KIND_PRIVATE: "private",
    message_contract.CONVERSATION_KIND_GROUP: "group",
    message_contract.CONVERSATION_KIND_CHANNEL: "channel",
}
_TARGET_KEYS = {"private": "touser", "group": "toparty"}


class CancellationToken(Protocol):
    """Minimal cooperative cancellation surface.

    中文:精简的协作式取消接口。
    """

    def is_cancelled(self) -> bool: ...


class WeComTransport(Protocol):
    """Injectable transport for token, upload, and message REST calls.

    中文:可注入的 REST 传输,用于 token、文件上传和消息发送请求。
    """

    def get_token(
        self,
        config: WeComInstanceConfig,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Mapping[str, Any]: ...

    def send_agent_message(
        self,
        token: str,
        payload: Mapping[str, Any],
        config: WeComInstanceConfig,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Mapping[str, Any]: ...

    def upload_media(
        self,
        token: str,
        media_type: str,
        remote_uri: str,
        file_name: str,
        mime_type: str,
        config: WeComInstanceConfig,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class WeComInstanceConfig:
    """One configured WeCom binding supporting App, Bot, or Hybrid modes.

    中文:一个已配置的 WeCom binding,支持应用模式、机器人长连接模式或混合模式。
    """

    binding_id: str
    corp_id: str | None = None
    corp_secret: str | None = None
    corp_secret_env_ref: str = "WECOM_CORP_SECRET"
    agent_id: int | None = None
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 10.0
    token_refresh_skew_seconds: float = 60.0
    mode: str = "app"  # "app" | "bot" | "hybrid"
    bot_id: str | None = None
    bot_secret: str | None = None
    bot_secret_env_ref: str = "WECOM_BOT_SECRET"
    websocket_url: str = DEFAULT_WS_URL
    home_channel: str | None = None
    allowed_users: tuple[str, ...] = ()
    cli_enabled: bool = False
    cli_path: str | None = None
    cli_config_dir: str | None = None
    approved_write_operations: tuple[str, ...] = ()
    navigator: NavigatorIntegrationConfig | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> WeComInstanceConfig:
        """Validate one configuration mapping.

        中文:校验一份配置映射。
        """

        binding_id = _required_text(value.get("binding_id"), "binding_id")

        # Mode resolution: explicit mode or inferred from present keys
        explicit_mode = value.get("mode")
        if explicit_mode:
            if explicit_mode not in {"app", "bot", "hybrid"}:
                raise ConnectorError(
                    "INVALID_REQUEST",
                    f"mode must be 'app', 'bot', or 'hybrid'; got {explicit_mode!r}",
                )
            mode = explicit_mode
        else:
            has_bot = bool(value.get("bot_id"))
            has_app = bool(value.get("corp_id"))
            if has_bot and has_app:
                mode = "hybrid"
            elif has_bot:
                mode = "bot"
            else:
                mode = "app"

        corp_id: str | None = None
        corp_secret: str | None = None
        agent_id: int | None = None
        if mode in {"app", "hybrid"}:
            corp_id = _required_text(value.get("corp_id"), "corp_id")
            corp_secret_env_ref = _secret_env_ref(
                value.get("corp_secret_env_ref", "WECOM_CORP_SECRET"),
                "corp_secret_env_ref",
            )
            corp_secret = _configured_secret(
                value.get("corp_secret"), corp_secret_env_ref, "corp_secret"
            )
            raw_agent_id = value.get("agent_id")
            if (
                isinstance(raw_agent_id, bool)
                or not isinstance(raw_agent_id, int)
                or raw_agent_id <= 0
            ):
                raise ConnectorError(
                    "INVALID_REQUEST", "agent_id must be a positive integer"
                )
            agent_id = raw_agent_id

        bot_id: str | None = None
        bot_secret: str | None = None
        bot_secret_env_ref = _secret_env_ref(
            value.get("bot_secret_env_ref", "WECOM_BOT_SECRET"),
            "bot_secret_env_ref",
        )
        websocket_url = str(value.get("websocket_url", DEFAULT_WS_URL))
        if mode in {"bot", "hybrid"}:
            bot_id = _required_text(value.get("bot_id"), "bot_id")
            bot_secret = _configured_secret(
                value.get("bot_secret"), bot_secret_env_ref, "bot_secret"
            )

        base_url = value.get("base_url", DEFAULT_BASE_URL)
        if not isinstance(base_url, str):
            raise ConnectorError("INVALID_REQUEST", "base_url must be a string")
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme == "https"
            or parsed.scheme == "http"
            and parsed.hostname
            in (
                "127.0.0.1",
                "::1",
                "localhost",
            )
        ):
            pass
        else:
            raise ConnectorError(
                "INVALID_REQUEST",
                "base_url must be an absolute https URL "
                "(http is allowed for loopback only)",
            )
        if not parsed.hostname:
            raise ConnectorError("INVALID_REQUEST", "base_url must contain a host")

        timeout = value.get("timeout_seconds", 10.0)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise ConnectorError(
                "INVALID_REQUEST", "timeout_seconds must be a positive number"
            )

        skew = value.get("token_refresh_skew_seconds", 60.0)
        if isinstance(skew, bool) or not isinstance(skew, (int, float)) or skew < 0:
            raise ConnectorError(
                "INVALID_REQUEST",
                "token_refresh_skew_seconds must be a non-negative number",
            )

        home_channel = value.get("home_channel")
        raw_allowed = value.get("allowed_users", ())
        if isinstance(raw_allowed, str):
            allowed_users = tuple(
                u.strip() for u in raw_allowed.split(",") if u.strip()
            )
        elif isinstance(raw_allowed, (list, tuple, set)):
            allowed_users = tuple(str(u).strip() for u in raw_allowed if str(u).strip())
        else:
            allowed_users = ()

        cli_obj = value.get("cli", {})
        cli_enabled = bool(
            value.get("cli_enabled")
            or (isinstance(cli_obj, Mapping) and cli_obj.get("enabled"))
        )
        cli_path = value.get("cli_path")
        if cli_path is None and isinstance(cli_obj, Mapping):
            cli_path = cli_obj.get("executable_path")
        cli_config_dir = value.get("cli_config_dir")
        if cli_config_dir is None and isinstance(cli_obj, Mapping):
            cli_config_dir = cli_obj.get("config_dir")
        if cli_enabled and not cli_config_dir:
            raise ConnectorError(
                "INVALID_REQUEST",
                "cli.config_dir is required to isolate the human CLI identity",
            )
        raw_write_approvals = value.get("approved_write_operations", ())
        if not isinstance(raw_write_approvals, (list, tuple, set)):
            raise ConnectorError(
                "INVALID_REQUEST", "approved_write_operations must be a list"
            )
        approved_write_operations = tuple(
            _required_text(operation, "approved_write_operations[]")
            for operation in raw_write_approvals
        )
        unsupported_write_approvals = sorted(
            set(approved_write_operations) - WECOM_WRITE_OPERATION_IDS
        )
        if unsupported_write_approvals:
            raise ConnectorError(
                "INVALID_REQUEST",
                "approved_write_operations contains unsupported operation(s): "
                f"{unsupported_write_approvals}",
            )

        navigator_value = value.get("navigator")
        navigator: NavigatorIntegrationConfig | None = None
        if navigator_value is not None:
            if not isinstance(navigator_value, Mapping):
                raise ConnectorError("INVALID_REQUEST", "navigator must be an object")
            if navigator_value.get("enabled", True):
                if mode not in {"bot", "hybrid"}:
                    raise ConnectorError(
                        "INVALID_REQUEST",
                        "navigator inbound integration requires bot or hybrid mode",
                    )
                navigator = NavigatorIntegrationConfig.from_mapping(
                    navigator_value, binding_id=binding_id
                )
                expected_bot_account = f"bot:{bot_id}"
                if any(
                    route.account_id != expected_bot_account
                    for route in navigator.trusted_conversations
                ):
                    raise ConnectorError(
                        "INVALID_REQUEST",
                        "navigator trusted account_id must match this bot_id",
                    )

        return cls(
            binding_id=binding_id,
            corp_id=corp_id,
            corp_secret=corp_secret,
            corp_secret_env_ref=corp_secret_env_ref
            if mode in {"app", "hybrid"}
            else "WECOM_CORP_SECRET",
            agent_id=agent_id,
            base_url=base_url.rstrip("/"),
            timeout_seconds=float(timeout),
            token_refresh_skew_seconds=float(skew),
            mode=mode,
            bot_id=bot_id,
            bot_secret=bot_secret,
            bot_secret_env_ref=bot_secret_env_ref,
            websocket_url=websocket_url,
            home_channel=str(home_channel) if home_channel else None,
            allowed_users=allowed_users,
            cli_enabled=cli_enabled,
            cli_path=str(cli_path) if cli_path else None,
            cli_config_dir=str(cli_config_dir) if cli_config_dir else None,
            approved_write_operations=approved_write_operations,
            navigator=navigator,
        )

    @classmethod
    def from_settings(cls, settings: Mapping[str, str]) -> WeComInstanceConfig:
        """Build configuration from the standard plugin activation environment.

        中文:根据标准 Plugin activation 环境构造配置。
        """

        encoded = settings.get("config") or "{}"
        try:
            decoded = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ConnectorError(
                "INVALID_REQUEST", "config must contain valid JSON"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise ConnectorError("INVALID_REQUEST", "config must be a JSON object")
        merged: dict[str, Any] = dict(decoded)
        binding_id = settings.get("binding_id")
        if binding_id:
            merged["binding_id"] = binding_id
        return cls.from_mapping(merged)


class UrllibWeComTransport:
    """Small urllib transport; the standard REST network surface for WeCom App API.

    中文:精简的 urllib 传输;这是此 connector 针对应用消息 API 的标准 REST 接口。
    """

    def get_token(
        self,
        config: WeComInstanceConfig,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Mapping[str, Any]:
        query = urllib.parse.urlencode(
            {"corpid": config.corp_id, "corpsecret": config.corp_secret}
        )
        url = f"{config.base_url}/cgi-bin/gettoken?{query}"
        return self._json_request(url, None, config, cancellation)

    def send_agent_message(
        self,
        token: str,
        payload: Mapping[str, Any],
        config: WeComInstanceConfig,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Mapping[str, Any]:
        url = (
            f"{config.base_url}/cgi-bin/message/send?"
            f"{urllib.parse.urlencode({'access_token': token})}"
        )
        body = json.dumps(payload).encode("utf-8")
        return self._json_request(
            url, body, config, cancellation, content_type="application/json"
        )

    def upload_media(
        self,
        token: str,
        media_type: str,
        remote_uri: str,
        file_name: str,
        mime_type: str,
        config: WeComInstanceConfig,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Mapping[str, Any]:
        raw_bytes, resolved_name, resolved_mime = self._fetch_remote_bytes(
            remote_uri, file_name, mime_type, media_type, config, cancellation
        )
        boundary = f"cyrene-{secrets.token_hex(16)}"
        disposition = (
            f'form-data; name="media"; filename="{_multipart_filename(resolved_name)}"'
        )
        content_header = (
            f"Content-Disposition: {disposition}\r\n"
            f"Content-Type: {resolved_mime}\r\n\r\n"
        )
        parts = [
            f"--{boundary}\r\n".encode(),
            content_header.encode(),
            raw_bytes,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        body = b"".join(parts)
        url = (
            f"{config.base_url}/cgi-bin/media/upload?"
            f"{urllib.parse.urlencode({'access_token': token, 'type': media_type})}"
        )
        content_type = f"multipart/form-data; boundary={boundary}"
        return self._json_request(
            url, body, config, cancellation, content_type=content_type
        )

    def _fetch_remote_bytes(
        self,
        remote_uri: str,
        file_name: str,
        mime_type: str,
        media_type: str,
        config: WeComInstanceConfig,
        cancellation: CancellationToken | None,
    ) -> tuple[bytes, str, str]:
        _raise_if_cancelled(cancellation)
        limit = MAX_IMAGE_BYTES if media_type == "image" else MAX_FILE_BYTES
        try:
            req = urllib.request.Request(
                remote_uri, headers={"User-Agent": "Cyrene/1.0"}
            )
            with urllib.request.urlopen(req, timeout=config.timeout_seconds) as resp:
                content = resp.read(limit + 1)
                response_mime = resp.headers.get_content_type()
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise ConnectorError(
                "UNAVAILABLE", f"failed to fetch remote media {remote_uri!r}: {exc}"
            ) from exc

        if len(content) < MIN_MEDIA_BYTES:
            raise ConnectorError(
                "INVALID_REQUEST",
                f"{media_type} must contain at least {MIN_MEDIA_BYTES} bytes",
            )
        if len(content) > limit:
            raise ConnectorError(
                "INVALID_REQUEST", f"{media_type} exceeds the {limit}-byte upload limit"
            )

        resolved_name = file_name or _remote_file_name(remote_uri, media_type)
        resolved_mime = mime_type or response_mime
        if resolved_mime == "application/octet-stream":
            guessed, _ = mimetypes.guess_type(resolved_name)
            resolved_mime = guessed or resolved_mime
        if media_type == "image" and resolved_mime.lower() not in IMAGE_MIME_TYPES:
            raise ConnectorError(
                "INVALID_REQUEST", "WeCom images must use JPEG or PNG media"
            )
        if "\r" in resolved_mime or "\n" in resolved_mime:
            raise ConnectorError("INVALID_REQUEST", "media MIME type is invalid")
        return content, resolved_name, resolved_mime

    def _json_request(
        self,
        url: str,
        body: bytes | None,
        config: WeComInstanceConfig,
        cancellation: CancellationToken | None,
        *,
        content_type: str | None = None,
    ) -> Mapping[str, Any]:
        _raise_if_cancelled(cancellation)
        request = urllib.request.Request(
            url, data=body, method="POST" if body else "GET"
        )
        if content_type is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(
                request, timeout=config.timeout_seconds
            ) as response:
                raw = response.read()
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise ConnectorError(
                "UNAVAILABLE", f"wecom request failed: {type(exc).__name__}: {exc}"
            ) from exc
        _raise_if_cancelled(cancellation)
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorError(
                "UNAVAILABLE", "wecom returned a non-JSON response"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise ConnectorError("UNAVAILABLE", "wecom returned a non-object response")
        return decoded


class WeComConnector:
    """Unified WeCom connector for application messages, WebSocket bot, and CLI tools.

    中文:统一的企业微信连接器,集成应用消息、WebSocket 智能机器人和 CLI 工具能力。
    """

    plugin_id = "cyrene.connectors.wecom"
    version = "0.3.0"
    capabilities = (CAPABILITY_ID, TOOL_PROVIDER_CAPABILITY_ID)

    def __init__(
        self,
        config: WeComInstanceConfig | None = None,
        transport: WeComTransport | None = None,
        *,
        ws_transport: WeComWsTransport | None = None,
        ws_transport_factory: Callable[[], WeComWsTransport] | None = None,
        cli_client: WeComCliClient | None = None,
    ) -> None:
        self._config: WeComInstanceConfig | None = None
        self._transport: WeComTransport | None = None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

        self._ws_transport = ws_transport
        self._ws_transport_factory = ws_transport_factory
        self._bot_client: WeComBotClient | None = None
        self._bot_runtime: WeComBotRuntime | None = None
        self._navigator_client: NavigatorTaskClient | None = None
        self._notification_worker: NavigatorNotificationWorker | None = None
        self._subscriptions: dict[str, _EventSubscription] = {}
        self._event_lock = threading.Lock()
        self._seen_events: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._cli_client: WeComCliClient = cli_client or WeComCliClient()
        self._tool_provider: WeComToolProvider = WeComToolProvider(
            cli_client=self._cli_client, cli_enabled=False
        )

        if config is not None:
            self.configure(
                config,
                transport=transport,
                ws_transport=ws_transport,
                ws_transport_factory=ws_transport_factory,
                cli_client=cli_client,
            )
        else:
            settings = _environment_settings()
            if settings is not None:
                self.configure(WeComInstanceConfig.from_settings(settings))

    @property
    def configured_binding_id(self) -> str | None:
        """Return the configured binding identity."""
        return self._config.binding_id if self._config is not None else None

    @property
    def bot_client(self) -> WeComBotClient | None:
        """Return the initialized WeComBotClient if configured."""
        return self._bot_client

    @property
    def tool_provider(self) -> WeComToolProvider:
        """Return the tool.provider.v1 provider."""
        return self._tool_provider

    @property
    def notification_worker(self) -> NavigatorNotificationWorker | None:
        """Return the configured Navigator notification outbox worker."""
        return self._notification_worker

    @property
    def cli_client(self) -> WeComCliClient:
        """Return the underlying CLI client."""
        return self._cli_client

    def configure(
        self,
        config: WeComInstanceConfig,
        *,
        transport: WeComTransport | None = None,
        ws_transport: WeComWsTransport | None = None,
        ws_transport_factory: Callable[[], WeComWsTransport] | None = None,
        cli_client: WeComCliClient | None = None,
    ) -> None:
        """Bind one configuration."""
        if config.cli_enabled and not config.cli_config_dir:
            raise ConnectorError(
                "INVALID_REQUEST",
                "cli_config_dir is required to isolate the human CLI identity",
            )
        if set(config.approved_write_operations) - WECOM_WRITE_OPERATION_IDS:
            raise ConnectorError(
                "INVALID_REQUEST",
                "approved_write_operations contains an unsupported operation",
            )
        previous_worker = self._notification_worker
        if previous_worker is not None:
            previous_worker.stop(timeout_seconds=2.0)
        self._notification_worker = None
        previous_runtime = self._bot_runtime
        if previous_runtime is not None:
            previous_runtime.stop(timeout_seconds=2.0)
        self._bot_runtime = None
        self._config = config
        self._transport = transport
        self._token = None
        self._token_expires_at = 0.0

        if ws_transport is not None:
            self._ws_transport = ws_transport
        if ws_transport_factory is not None:
            self._ws_transport_factory = ws_transport_factory
        if cli_client is not None:
            self._cli_client = cli_client
        elif config.cli_path or config.cli_config_dir:
            self._cli_client = WeComCliClient(
                executable_path=config.cli_path,
                config_dir=config.cli_config_dir,
            )

        if config.mode in {"bot", "hybrid"} and config.bot_id and config.bot_secret:
            self._bot_client = WeComBotClient(
                bot_id=config.bot_id,
                bot_secret=config.bot_secret,
                websocket_url=config.websocket_url,
                transport=self._ws_transport,
                transport_factory=self._ws_transport_factory,
            )
            self._bot_runtime = WeComBotRuntime(
                self._bot_client,
                self._handle_bot_inbound,
                startup_timeout_seconds=config.timeout_seconds
                + 2.0,
            )
        else:
            self._bot_client = None
        self._navigator_client = (
            NavigatorTaskClient(config.navigator) if config.navigator else None
        )
        self._notification_worker = (
            NavigatorNotificationWorker(
                config.navigator,
                self._send_navigator_notification,
            )
            if config.navigator is not None
            else None
        )
        self._tool_provider = WeComToolProvider(
            binding_id=config.binding_id,
            cli_client=self._cli_client,
            cli_enabled=config.cli_enabled,
            approved_write_operations=config.approved_write_operations,
            health_provider=self.connection_health,
        )

    def on_configure(self, settings: Mapping[str, str]) -> str | None:
        """Configure this activation through the standard Plugin worker seam.

        中文:通过标准 Plugin worker 接口配置此 activation。
        """
        try:
            config = WeComInstanceConfig.from_settings(settings)
        except (ConnectorError, ValueError, TypeError) as exc:
            code = exc.code if isinstance(exc, ConnectorError) else "INVALID_REQUEST"
            return f"{code}: {exc}"
        self.configure(config)
        return None

    def on_subscribe(
        self,
        subscription_id: str,
        capability: str,
        filter_payload: bytes,
        emitter: Any,
    ) -> str | None:
        """Start inbound delivery and attach one binding-local event stream.

        中文:启动入站接收并附加一个属于当前 binding 的事件流。
        """
        try:
            config = self._require_configured()
            if capability != CAPABILITY_ID:
                return (
                    "INVALID_REQUEST: unsupported subscription capability "
                    f"{capability!r}"
                )
            if not subscription_id.strip():
                return "INVALID_REQUEST: subscription_id must not be empty"
            if config.mode not in {"bot", "hybrid"} or self._bot_runtime is None:
                return (
                    "CAPABILITY_UNAVAILABLE: WeCom application mode has no inbound "
                    "transport"
                )
            if not callable(getattr(emitter, "emit", None)):
                return "CAPABILITY_UNAVAILABLE: event emitter is required"
            event_filter = _decode_event_filter(filter_payload)
            self._subscriptions[subscription_id] = _EventSubscription(
                emitter=emitter, filter=event_filter
            )
            self._bot_runtime.start()
            if self._notification_worker is not None:
                self._notification_worker.start()
            return None
        except ConnectorError as exc:
            self._subscriptions.pop(subscription_id, None)
            return f"{exc.code}: {exc.message}"
        except Exception as exc:
            self._subscriptions.pop(subscription_id, None)
            return f"CAPABILITY_UNAVAILABLE: WeCom bot start failed: {exc}"

    def on_unsubscribe(self, subscription_id: str, reason: str) -> None:
        """Detach one event consumer and stop idle inbound resources.

        中文:分离一个事件消费者,并在没有消费者时停止入站资源。
        """
        del reason
        self._subscriptions.pop(subscription_id, None)
        if not self._subscriptions and self._navigator_client is None:
            if self._notification_worker is not None:
                self._notification_worker.stop()
            self._stop_bot_runtime()

    def start_background_services(self) -> None:
        """Start configured bot intake and its optional Navigator outbox worker.

        中文:启动已配置的 bot intake 与可选 Navigator outbox worker。
        """
        config = self._require_configured()
        if config.mode not in {"bot", "hybrid"} or self._bot_runtime is None:
            raise ConnectorError(
                "UNAVAILABLE", "WeCom background service requires bot or hybrid mode"
            )
        self._bot_runtime.start()
        if self._notification_worker is not None:
            self._notification_worker.start()

    def on_shutdown(self, grace_period_ms: int) -> None:
        """Stop the bot WebSocket within the runtime shutdown budget."""
        timeout = max(0.1, min(grace_period_ms / 1000, 5.0))
        if self._notification_worker is not None:
            self._notification_worker.stop(timeout_seconds=timeout)
        self._stop_bot_runtime(timeout_seconds=timeout)

    def close(self) -> None:
        """Close bot resources for tests and embedding hosts."""
        if self._notification_worker is not None:
            self._notification_worker.stop()
        self._stop_bot_runtime()
        self._subscriptions.clear()

    def connection_health(self) -> dict[str, Any]:
        """Return separate readiness diagnostics for each configured identity.

        中文:分别报告 app、bot、human CLI 和 Navigator 的就绪状态,不返回凭据。
        """
        config = self._config
        bot_configured = bool(
            config and config.mode in {"bot", "hybrid"} and config.bot_id
        )
        app_configured = bool(
            config and config.mode in {"app", "hybrid"} and config.corp_id
        )
        cli_status = (
            self._cli_client.get_status()
            if config and config.cli_enabled
            else None
        )
        bot_client = self._bot_client
        return {
            "binding_id": config.binding_id if config else "",
            "application": {
                "configured": app_configured,
                "account_id": (
                    f"agent:{config.agent_id}" if app_configured and config else ""
                ),
                "status": "configured" if app_configured else "not_configured",
            },
            "bot": {
                "configured": bot_configured,
                "account_id": (
                    f"bot:{config.bot_id}" if bot_configured and config else ""
                ),
                "status": (
                    "connected"
                    if bot_client is not None and bot_client.is_connected
                    else "reconnecting"
                    if bot_client is not None and bot_client.reconnect_count > 0
                    else "disconnected"
                    if bot_configured
                    else "not_configured"
                ),
                "reconnect_count": bot_client.reconnect_count if bot_client else 0,
                "last_error": bot_client.last_error if bot_client else None,
            },
            "human_cli": {
                "enabled": bool(config and config.cli_enabled),
                "config_dir_configured": bool(config and config.cli_config_dir),
                "available": cli_status.available if cli_status else False,
                "version": cli_status.version if cli_status else "",
                "auth_status": cli_status.auth_status if cli_status else "disabled",
            },
            "navigator": {
                "configured": self._navigator_client is not None,
                "outbox_worker_running": (
                    self._notification_worker.is_running
                    if self._notification_worker is not None
                    else False
                ),
                "trusted_conversation_count": (
                    len(config.navigator.trusted_conversations)
                    if config and config.navigator
                    else 0
                ),
            },
        }

    def _stop_bot_runtime(self, *, timeout_seconds: float = 5.0) -> None:
        runtime = self._bot_runtime
        if runtime is not None:
            runtime.stop(timeout_seconds=timeout_seconds)

    async def _handle_bot_inbound(self, message: InboundMessage) -> None:
        """Normalize one vendor callback off the WebSocket event loop."""
        await asyncio.to_thread(self.publish_inbound_message, message)

    def _send_navigator_notification(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Deliver one validated outbox item through canonical send_message.

        中文:通过规范 send_message contract 投递一条已校验的 outbox 通知。
        """
        return self.send_message(request)

    def publish_inbound_message(self, message: InboundMessage) -> dict[str, Any]:
        """Normalize, emit, and optionally admit one bot message into Navigator.

        中文:规范化并发出一条 bot 消息;仅可信映射允许其进入 Navigator。
        """
        config = self._require_configured()
        normalized = _normalize_bot_inbound(message, config)
        event_key = (
            normalized["conversation"]["account_id"],
            normalized["message_id"],
        )
        with self._event_lock:
            already_emitted = event_key in self._seen_events
            if not already_emitted:
                self._seen_events[event_key] = None
                self._seen_events.move_to_end(event_key)
                while len(self._seen_events) > 2048:
                    self._seen_events.popitem(last=False)

        emitted = 0
        if not already_emitted:
            encoded = _inbound_message_to_proto(normalized).SerializeToString()
            stale_subscriptions: list[str] = []
            for subscription_id, subscription in tuple(self._subscriptions.items()):
                if not _event_filter_matches(subscription.filter, normalized):
                    continue
                try:
                    if subscription.emitter.emit(
                        INBOUND_MESSAGE_EVENT_TYPE,
                        encoded,
                        INBOUND_MESSAGE_TYPE_URL,
                    ):
                        emitted += 1
                    else:
                        stale_subscriptions.append(subscription_id)
                except Exception:
                    stale_subscriptions.append(subscription_id)
            for subscription_id in stale_subscriptions:
                self._subscriptions.pop(subscription_id, None)

        receipt: Mapping[str, Any] | None = None
        navigator = self._navigator_client
        navigator_config = config.navigator
        if navigator is not None and navigator_config is not None:
            trust = navigator_config.trusted_rule(normalized)
            if trust is not None:
                receipt = navigator.submit_message(normalized, trust)
            else:
                logger.info(
                    "Ignoring WeCom inbound task without an exact trusted sender map"
                )
        return {
            "message_id": normalized["message_id"],
            "event_emitted": emitted > 0,
            "navigator_receipt": dict(receipt) if receipt is not None else None,
        }

    def send_message(
        self,
        request: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Invoke canonical ``message.connector.v1/send_message``."""
        config = self._require_configured()
        if not isinstance(request, Mapping):
            raise ConnectorError(
                "INVALID_REQUEST", "send_message request must be an object"
            )
        if "binding_id" in request:
            raise ConnectorError(
                "INVALID_REQUEST",
                "binding_id is endpoint selection metadata, "
                "not a message payload field",
            )
        _raise_if_cancelled(cancellation)

        conversation = _conversation_for_send(request, config)
        account_id = conversation["account_id"]
        kind = conversation["kind"]

        # Routing decision based on configuration mode & conversation
        use_bot = config.mode == "bot" or (
            config.mode == "hybrid"
            and (account_id.startswith("bot:") or kind == "channel")
        )

        if use_bot:
            return self._send_via_bot(
                request, conversation, config, cancellation, request_id=request_id
            )

        # Standard application-message REST route
        if kind not in _TARGET_KEYS:
            return {
                "status": "rejected",
                "vendor_message_id": "",
                "reason": (
                    f"conversation kind {kind!r} is not supported by the wecom.app "
                    "application-message surface"
                ),
            }

        body = _build_agent_message(
            request,
            conversation,
            config,
            media_resolver=lambda media_type, part: self._resolve_media_id(
                media_type, part, config, cancellation
            ),
        )
        response = self._send_with_token_retry(body, config, cancellation)
        return _delivery_result(response, conversation.get("reply_message_id"))

    def _send_via_bot(
        self,
        request: Mapping[str, Any],
        conversation: Mapping[str, Any],
        config: WeComInstanceConfig,
        cancellation: CancellationToken | None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch message via WeCom smart robot gateway."""
        if self._bot_client is None:
            raise ConnectorError(
                "UNAVAILABLE", "WeCom bot client is not configured for bot mode"
            )

        chat_id = conversation["conversation_id"]
        chat_type = "group" if conversation["kind"] == "group" else "single"
        content_parts = request.get("content", [])
        text_lines: list[str] = []
        for p in content_parts:
            if isinstance(p, Mapping) and p.get("kind") == "text":
                text_lines.append(str(p.get("text", "")))
        content = "".join(text_lines) or " "

        reply_id = conversation.get("reply_message_id")
        try:
            if self._bot_runtime is None:
                raise ConnectorError(
                    "UNAVAILABLE", "WeCom bot runtime is not configured"
                )
            req_id = self._bot_runtime.send(
                chat_id,
                content,
                chat_type,
                reply_id,
                timeout_seconds=config.timeout_seconds + 22.0,
                request_id=request_id,
                is_cancelled=(cancellation.is_cancelled if cancellation else None),
            )
            return {
                "status": "accepted",
                "vendor_message_id": req_id,
                "reason": "WeCom OpenWS acknowledged the request",
            }
        except WeComBotRequestError as exc:
            errcode = _numeric_errcode(exc.errcode)
            status = "rate_limited" if errcode in RATE_LIMIT_CODES else "rejected"
            return {
                "status": status,
                "vendor_message_id": "",
                "reason": (
                    "WeCom bot rejected request: "
                    f"errcode={exc.errcode} errmsg={exc.errmsg}"
                ),
            }
        except ConnectorError:
            raise
        except Exception as exc:
            if cancellation is not None and cancellation.is_cancelled():
                raise ConnectorError("CANCELLED", "operation cancelled") from exc
            raise ConnectorError(
                "UNAVAILABLE",
                "WeCom bot request failed before a vendor receipt was received",
            ) from exc

    # ── DirectPluginRuntime adapter ────────────────────────────────────

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: CancellationToken | None = None,
        request_type_url: str | None = None,
        request_id: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, Any]:
        """Adapt the direct Plugin runtime call to the typed connector contract."""
        try:
            if capability == CAPABILITY_ID:
                if action != SEND_MESSAGE_METHOD:
                    raise ConnectorError(
                        "METHOD_NOT_FOUND",
                        f"unsupported method {action!r}; wecom implements "
                        f"{SEND_MESSAGE_METHOD!r}",
                    )
                if stream_results:
                    raise ConnectorError(
                        "METHOD_NOT_SUPPORTED", "send_message is not streaming"
                    )
                if request_type_url not in (None, SEND_MESSAGE_REQUEST_TYPE_URL):
                    raise ConnectorError(
                        "INVALID_REQUEST",
                        f"request_type_url must be {SEND_MESSAGE_REQUEST_TYPE_URL}",
                    )
                _raise_if_cancelled(cancellation)

                request = message_contract.SendMessageRequest()
                try:
                    request.ParseFromString(payload)
                except DecodeError as exc:
                    raise ConnectorError(
                        "INVALID_REQUEST", "payload is not a SendMessageRequest"
                    ) from exc

                result = self.send_message(
                    _send_request_to_mapping(request),
                    cancellation=cancellation,
                    request_id=request_id,
                )
                delivery = message_contract.DeliveryResult()
                _apply_delivery_result(delivery, result)
                return True, _TypedPayload(
                    delivery.SerializeToString(), DELIVERY_RESULT_TYPE_URL
                )

            elif capability == TOOL_PROVIDER_CAPABILITY_ID:
                if action == LIST_TOOLS_METHOD:
                    if request_type_url not in (None, LIST_TOOLS_REQUEST_TYPE_URL):
                        raise ConnectorError(
                            "INVALID_REQUEST",
                            f"request_type_url must be {LIST_TOOLS_REQUEST_TYPE_URL}",
                        )
                    _raise_if_cancelled(cancellation)
                    result_bytes = self.tool_provider.list_tools_proto(payload)
                    return True, _TypedPayload(
                        result_bytes, LIST_TOOLS_RESPONSE_TYPE_URL
                    )
                elif action == CALL_TOOL_METHOD:
                    if request_type_url not in (None, CALL_TOOL_REQUEST_TYPE_URL):
                        raise ConnectorError(
                            "INVALID_REQUEST",
                            f"request_type_url must be {CALL_TOOL_REQUEST_TYPE_URL}",
                        )
                    _raise_if_cancelled(cancellation)
                    result_bytes = self.tool_provider.call_tool_proto(payload)
                    return True, _TypedPayload(
                        result_bytes, CALL_TOOL_RESPONSE_TYPE_URL
                    )
                else:
                    raise ConnectorError(
                        "METHOD_NOT_FOUND",
                        f"unsupported tool method {action!r}; "
                        f"implements {LIST_TOOLS_METHOD!r} and {CALL_TOOL_METHOD!r}",
                    )

            else:
                raise ConnectorError(
                    "INVALID_REQUEST", f"unsupported capability {capability!r}"
                )

        except ConnectorError as exc:
            return False, f"{exc.code}: {exc.message}"

    # ── internals ──────────────────────────────────────────────────────

    def on_cancel(self, request_id: str, reason: str) -> None:
        """Cancel one outstanding bot send associated with a runtime request.

        中文:取消一个与 runtime request ID 关联的 bot 发送操作。
        """
        del reason
        runtime = self._bot_runtime
        if runtime is not None:
            runtime.cancel(request_id)

    def _require_configured(self) -> WeComInstanceConfig:
        if self._config is None:
            raise ConnectorError(
                "UNAVAILABLE", "wecom connector requires an activation configuration"
            )
        return self._config

    def _transport_for(self, config: WeComInstanceConfig) -> WeComTransport:
        if self._transport is None:
            self._transport = UrllibWeComTransport()
        return self._transport

    def _send_with_token_retry(
        self,
        body: Mapping[str, Any],
        config: WeComInstanceConfig,
        cancellation: CancellationToken | None,
    ) -> Mapping[str, Any]:
        transport = self._transport_for(config)
        token = self._access_token(transport, config, cancellation)
        response = transport.send_agent_message(
            token, body, config, cancellation=cancellation
        )
        if _errcode(response) in INVALID_TOKEN_CODES:
            token = self._access_token(
                transport, config, cancellation, force_refresh=True
            )
            response = transport.send_agent_message(
                token, body, config, cancellation=cancellation
            )
        return response

    def _resolve_media_id(
        self,
        media_type: str,
        part: Mapping[str, Any],
        config: WeComInstanceConfig,
        cancellation: CancellationToken | None,
    ) -> str:
        reference = part.get("reference")
        if not isinstance(reference, Mapping):
            raise ConnectorError(
                "INVALID_REQUEST", f"{media_type}.reference must be an object"
            )
        remote_uri = reference.get("remote_uri")
        vendor_media = reference.get("vendor_media")
        if (remote_uri is None) == (vendor_media is None):
            raise ConnectorError(
                "INVALID_REQUEST",
                f"{media_type}.reference must contain exactly one location",
            )

        if vendor_media is not None:
            if not isinstance(vendor_media, Mapping):
                raise ConnectorError(
                    "INVALID_REQUEST", f"{media_type}.reference.vendor_media is invalid"
                )
            vendor = _required_text(
                vendor_media.get("vendor"),
                f"{media_type}.reference.vendor_media.vendor",
            )
            account_id = _required_text(
                vendor_media.get("account_id"),
                f"{media_type}.reference.vendor_media.account_id",
            )
            if vendor != VENDOR or account_id != f"agent:{config.agent_id}":
                raise ConnectorError(
                    "INVALID_REQUEST",
                    "vendor media must belong to this wecom.app agent binding",
                )
            return _required_text(
                vendor_media.get("media_id"),
                f"{media_type}.reference.vendor_media.media_id",
            )

        validated_uri = _validated_remote_uri(
            remote_uri, f"{media_type}.reference.remote_uri"
        )
        file_name = part.get("file_name") or ""
        mime_type = part.get("mime_type") or ""
        if not isinstance(file_name, str):
            raise ConnectorError(
                "INVALID_REQUEST", f"{media_type}.file_name must be a string"
            )
        if not isinstance(mime_type, str):
            raise ConnectorError(
                "INVALID_REQUEST", f"{media_type}.mime_type must be a string"
            )

        transport = self._transport_for(config)
        token = self._access_token(transport, config, cancellation)
        response = transport.upload_media(
            token,
            media_type,
            validated_uri,
            file_name,
            mime_type,
            config,
            cancellation=cancellation,
        )
        if _errcode(response) in INVALID_TOKEN_CODES:
            token = self._access_token(
                transport, config, cancellation, force_refresh=True
            )
            response = transport.upload_media(
                token,
                media_type,
                validated_uri,
                file_name,
                mime_type,
                config,
                cancellation=cancellation,
            )
        if _errcode(response) != 0:
            raise ConnectorError(
                "UNAVAILABLE",
                "wecom media upload failed: "
                f"errcode={_errcode(response)} errmsg={response.get('errmsg', '')}",
            )
        media_id = response.get("media_id")
        if not isinstance(media_id, str) or not media_id:
            raise ConnectorError(
                "UNAVAILABLE", "wecom media upload response has no media_id"
            )
        return media_id

    def _access_token(
        self,
        transport: WeComTransport,
        config: WeComInstanceConfig,
        cancellation: CancellationToken | None,
        *,
        force_refresh: bool = False,
    ) -> str:
        with self._token_lock:
            now = time.monotonic()
            if not force_refresh and self._token and now < self._token_expires_at:
                return self._token
            response = transport.get_token(config, cancellation=cancellation)
            if _errcode(response) != 0:
                raise ConnectorError(
                    "UNAVAILABLE",
                    "wecom gettoken failed: "
                    f"errcode={_errcode(response)} errmsg={response.get('errmsg', '')}",
                )
            token = response.get("access_token")
            if not isinstance(token, str) or not token:
                raise ConnectorError(
                    "UNAVAILABLE", "wecom gettoken response has no access_token"
                )
            expires_in = response.get("expires_in", 7200)
            if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
                expires_in = 7200
            lifetime = max(float(expires_in) - config.token_refresh_skew_seconds, 1.0)
            self._token = token
            self._token_expires_at = now + lifetime
            return token


@dataclass(frozen=True, slots=True)
class _TypedPayload:
    value: bytes
    type_url: str


@dataclass(frozen=True, slots=True)
class _EventSubscription:
    emitter: Any
    filter: Mapping[str, Any]


def _environment_settings() -> dict[str, str] | None:
    """Read one binding through the standard plugin activation environment."""
    from cyrene_plugin_runtime.configuration import read_environment_settings

    return read_environment_settings()


def _raise_if_cancelled(cancellation: CancellationToken | None) -> None:
    if cancellation is not None and cancellation.is_cancelled():
        raise ConnectorError("CANCELLED", "operation cancelled")


def _secret_env_ref(value: Any, field: str) -> str:
    """Validate one environment-variable name used as a secret reference."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", value):
        raise ConnectorError(
            "INVALID_REQUEST", f"{field} must name an uppercase environment variable"
        )
    return value


def _configured_secret(value: Any, env_ref: str, field: str) -> str:
    """Resolve a secret from process ENV while retaining the legacy direct form."""
    if isinstance(value, str) and value:
        return value
    if value is not None:
        raise ConnectorError("INVALID_REQUEST", f"{field} must be a string")
    secret = os.environ.get(env_ref, "")
    if not secret:
        raise ConnectorError(
            "INVALID_REQUEST",
            f"{field} or its secret environment reference is required",
        )
    return secret


def _decode_event_filter(payload: bytes) -> dict[str, Any]:
    """Decode the bounded event filter accepted by message.connector.v1."""
    if not payload:
        return {}
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorError(
            "INVALID_REQUEST", "subscription filter must be JSON"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise ConnectorError("INVALID_REQUEST", "subscription filter must be an object")
    supported = {"event_type", "account_id", "conversation_id", "kind", "sender_id"}
    unsupported = sorted(set(decoded) - supported)
    if unsupported:
        raise ConnectorError(
            "INVALID_REQUEST", f"unsupported subscription filter keys: {unsupported}"
        )
    if decoded.get("event_type") not in (None, INBOUND_MESSAGE_EVENT_TYPE):
        raise ConnectorError(
            "INVALID_REQUEST", "event_type must be inbound_message when specified"
        )
    for key in supported - {"event_type"}:
        value = decoded.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ConnectorError(
                "INVALID_REQUEST", f"subscription filter {key} must be non-empty text"
            )
    if decoded.get("kind") not in (None, "private", "group"):
        raise ConnectorError(
            "INVALID_REQUEST", "subscription filter kind must be private or group"
        )
    return dict(decoded)


def _event_filter_matches(
    event_filter: Mapping[str, Any], payload: Mapping[str, Any]
) -> bool:
    """Match one canonical inbound event against the explicit filter fields."""
    conversation = payload["conversation"]
    values = {
        "event_type": INBOUND_MESSAGE_EVENT_TYPE,
        "account_id": conversation["account_id"],
        "conversation_id": conversation["conversation_id"],
        "kind": conversation["kind"],
        "sender_id": payload["sender_id"],
    }
    return all(values[key] == value for key, value in event_filter.items())


def _normalize_bot_inbound(
    message: InboundMessage, config: WeComInstanceConfig
) -> dict[str, Any]:
    """Convert a WeCom OpenWS callback into the canonical inbound event mapping.

    中文:将 WeCom OpenWS callback 转换为规范入站事件映射。
    """
    if not config.bot_id:
        raise ConnectorError("UNAVAILABLE", "WeCom bot account is not configured")
    raw_payload = message.raw_payload
    body = raw_payload.get("body", {}) if isinstance(raw_payload, Mapping) else {}
    if not isinstance(body, Mapping):
        body = {}
    message_id = _required_text(message.msg_id or body.get("msg_id"), "message_id")
    chat_id = _required_text(message.chat_id or body.get("chat_id"), "chat_id")
    sender_id = _required_text(message.sender_id, "sender_id")
    chat_type = message.chat_type or str(body.get("chat_type", "single"))
    if chat_type not in {"single", "group"}:
        raise ConnectorError(
            "INVALID_REQUEST", f"unsupported WeCom chat_type {chat_type!r}"
        )
    kind = "group" if chat_type == "group" else "private"
    msg_type = message.msg_type or str(body.get("msg_type", "text"))
    source = body.get(msg_type, {})
    if not isinstance(source, Mapping):
        source = {}
    content: list[dict[str, Any]] = []
    if msg_type == "text":
        text = source.get("content", message.content)
        if isinstance(text, str) and text:
            content.append({"text": {"text": text}})
    elif msg_type in {"image", "file"}:
        content.append(
            {
                msg_type: _inbound_attachment(
                    msg_type, source, account_id=f"bot:{config.bot_id}"
                )
            }
        )
    elif msg_type == "voice":
        transcript = source.get("recognition", source.get("content", ""))
        if isinstance(transcript, str) and transcript:
            content.append({"text": {"text": transcript}})
    payload: dict[str, Any] = {
        "message_id": message_id,
        "conversation": {
            "vendor": BOT_VENDOR,
            "account_id": f"bot:{config.bot_id}",
            "conversation_id": chat_id,
            "kind": kind,
        },
        "sender_id": sender_id,
        "sender_display_name": message.sender_name or sender_id,
        "content": content,
        "vendor_extension": {
            "vendor": BOT_VENDOR,
            "facts": [
                {"name": "message_type", "value": msg_type},
                {"name": "chat_type", "value": chat_type},
            ],
        },
    }
    reply = body.get("reply_to")
    if isinstance(reply, Mapping) and reply.get("msg_id"):
        payload["reply"] = {
            "message_id": _required_text(reply.get("msg_id"), "reply.message_id")
        }
    if msg_type not in {"text", "image", "file", "voice"}:
        payload["vendor_extension"]["facts"].append(
            {"name": "unsupported_message_type", "value": msg_type[:256]}
        )
    return payload


def _inbound_attachment(
    media_type: str, source: Mapping[str, Any], *, account_id: str
) -> dict[str, Any]:
    """Build one canonical image/file part without retaining the raw callback."""
    file_name_value = source.get("file_name", source.get("name", ""))
    file_name = file_name_value if isinstance(file_name_value, str) else ""
    media_id = next(
        (
            source[key]
            for key in ("media_id", "file_id", "fileid")
            if isinstance(source.get(key), str) and source[key]
        ),
        None,
    )
    uri = next(
        (
            source[key]
            for key in ("image_url", "file_url", "url")
            if isinstance(source.get(key), str) and source[key]
        ),
        None,
    )
    if media_id is not None:
        reference = {
            "vendor_media": {
                "vendor": BOT_VENDOR,
                "account_id": account_id,
                "media_id": media_id,
            }
        }
    elif uri is not None:
        reference = {"remote_uri": _validated_remote_uri(uri, f"{media_type}.url")}
        if not file_name:
            file_name = _remote_file_name(uri, media_type)
    else:
        raise ConnectorError(
            "INVALID_REQUEST",
            f"WeCom {media_type} attachment has no safe media id or absolute URL",
        )
    mime_value = source.get("mime_type") or source.get("content_type") or ""
    mime_type = mime_value if isinstance(mime_value, str) else ""
    if not mime_type and file_name:
        mime_type = mimetypes.guess_type(file_name)[0] or ""
    result: dict[str, Any] = {"reference": reference, "mime_type": mime_type}
    if media_type == "file":
        result["file_name"] = file_name or "attachment.bin"
    return result


def _inbound_message_to_proto(
    payload: Mapping[str, Any],
) -> message_contract.InboundMessagePayload:
    """Encode a normalized inbound event using message.connector.v1 protobuf."""
    response = message_contract.InboundMessagePayload()
    response.message_id = _required_text(payload.get("message_id"), "message_id")
    conversation = payload.get("conversation")
    if not isinstance(conversation, Mapping):
        raise ConnectorError("INVALID_REQUEST", "conversation must be an object")
    kind = conversation.get("kind")
    kind_value = {
        "private": message_contract.CONVERSATION_KIND_PRIVATE,
        "group": message_contract.CONVERSATION_KIND_GROUP,
    }.get(kind)
    if kind_value is None:
        raise ConnectorError("INVALID_REQUEST", "inbound conversation kind is invalid")
    response.conversation.vendor = _required_text(
        conversation.get("vendor"), "conversation.vendor"
    )
    response.conversation.account_id = _required_text(
        conversation.get("account_id"), "conversation.account_id"
    )
    response.conversation.conversation_id = _required_text(
        conversation.get("conversation_id"), "conversation.conversation_id"
    )
    response.conversation.kind = kind_value
    response.sender_id = _required_text(payload.get("sender_id"), "sender_id")
    response.sender_display_name = _required_text(
        payload.get("sender_display_name"), "sender_display_name"
    )
    content = payload.get("content", ())
    if not isinstance(content, (list, tuple)):
        raise ConnectorError("INVALID_REQUEST", "content must be an ordered list")
    for index, raw_part in enumerate(content):
        if not isinstance(raw_part, Mapping) or len(raw_part) != 1:
            raise ConnectorError("INVALID_REQUEST", f"content[{index}] is invalid")
        kind_name, item = next(iter(raw_part.items()))
        if not isinstance(item, Mapping):
            raise ConnectorError("INVALID_REQUEST", f"content[{index}] is invalid")
        part = response.content.add()
        if kind_name == "text":
            part.text.text = str(item.get("text", ""))
        elif kind_name in {"image", "file"}:
            destination = getattr(part, kind_name)
            reference = item.get("reference")
            if not isinstance(reference, Mapping):
                raise ConnectorError(
                    "INVALID_REQUEST", f"content[{index}].reference is invalid"
                )
            if "remote_uri" in reference:
                destination.reference.remote_uri = _required_text(
                    reference.get("remote_uri"), f"content[{index}].remote_uri"
                )
            elif "vendor_media" in reference:
                vendor_media = reference["vendor_media"]
                if not isinstance(vendor_media, Mapping):
                    raise ConnectorError(
                        "INVALID_REQUEST", f"content[{index}].vendor_media is invalid"
                    )
                destination.reference.vendor_media.vendor = _required_text(
                    vendor_media.get("vendor"), f"content[{index}].vendor"
                )
                destination.reference.vendor_media.account_id = _required_text(
                    vendor_media.get("account_id"), f"content[{index}].account_id"
                )
                destination.reference.vendor_media.media_id = _required_text(
                    vendor_media.get("media_id"), f"content[{index}].media_id"
                )
            else:
                raise ConnectorError(
                    "INVALID_REQUEST", f"content[{index}].reference has no location"
                )
            if kind_name == "image":
                destination.mime_type = str(item.get("mime_type", ""))
            else:
                destination.file_name = str(item.get("file_name", ""))
                destination.mime_type = str(item.get("mime_type", ""))
        else:
            raise ConnectorError(
                "INVALID_REQUEST",
                f"content[{index}] has unsupported kind {kind_name!r}",
            )
    reply = payload.get("reply")
    if reply is not None:
        if not isinstance(reply, Mapping):
            raise ConnectorError("INVALID_REQUEST", "reply must be an object")
        response.reply.message_id = _required_text(
            reply.get("message_id"), "reply.message_id"
        )
    extension = payload.get("vendor_extension", {})
    if isinstance(extension, Mapping):
        response.vendor_extension.vendor = str(extension.get("vendor", BOT_VENDOR))
        facts = extension.get("facts", ())
        if isinstance(facts, Sequence) and not isinstance(facts, (str, bytes)):
            for fact in facts:
                if not isinstance(fact, Mapping):
                    continue
                entry = response.vendor_extension.facts.add()
                entry.name = str(fact.get("name", ""))
                entry.value = str(fact.get("value", ""))
    return response


def _numeric_errcode(value: int | str) -> int | None:
    """Convert a vendor acknowledgement code to an integer when possible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConnectorError("INVALID_REQUEST", f"{field} must be a non-empty string")
    return value


def _validated_remote_uri(value: Any, field: str) -> str:
    uri = _required_text(value, field)
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConnectorError(
            "INVALID_REQUEST", f"{field} must be an absolute http(s) URL"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ConnectorError("INVALID_REQUEST", f"{field} must not contain credentials")
    return uri


def _remote_file_name(remote_uri: str, media_type: str) -> str:
    name = PurePosixPath(
        urllib.parse.unquote(urllib.parse.urlsplit(remote_uri).path)
    ).name
    return name or ("image.png" if media_type == "image" else "attachment.bin")


def _multipart_filename(value: str) -> str:
    name = PurePosixPath(value.replace("\\", "/")).name.strip()
    if not name or name in {".", ".."}:
        raise ConnectorError("INVALID_REQUEST", "media file name is invalid")
    if "\r" in name or "\n" in name or '"' in name:
        raise ConnectorError("INVALID_REQUEST", "media file name is invalid")
    return name


def _conversation_for_send(
    request: Mapping[str, Any], config: WeComInstanceConfig
) -> dict[str, Any]:
    conversation = request.get("conversation")
    if not isinstance(conversation, Mapping):
        raise ConnectorError("INVALID_REQUEST", "conversation must be an object")
    vendor = _required_text(conversation.get("vendor"), "conversation.vendor")
    if vendor != VENDOR:
        raise ConnectorError(
            "INVALID_REQUEST",
            f"conversation.vendor must be {VENDOR!r} for this connector",
        )
    account_id = _required_text(
        conversation.get("account_id"), "conversation.account_id"
    )
    if (
        config.mode in {"app", "hybrid"}
        and config.agent_id
        and account_id.startswith("agent:")
        and account_id != f"agent:{config.agent_id}"
    ):
        raise ConnectorError(
            "INVALID_REQUEST",
            f"conversation.account_id {account_id!r} is not this binding's agent",
        )
    conversation_id = _required_text(
        conversation.get("conversation_id"), "conversation.conversation_id"
    )
    kind = conversation.get("kind", "unspecified")
    if not isinstance(kind, str):
        raise ConnectorError("INVALID_REQUEST", "conversation.kind must be a string")
    reply = request.get("reply")
    reply_message_id = None
    if reply is not None:
        if not isinstance(reply, Mapping):
            raise ConnectorError("INVALID_REQUEST", "reply must be an object")
        reply_message_id = _required_text(reply.get("message_id"), "reply.message_id")
    return {
        "vendor": vendor,
        "account_id": account_id,
        "conversation_id": conversation_id,
        "kind": kind,
        "reply_message_id": reply_message_id,
    }


def _build_agent_message(
    request: Mapping[str, Any],
    conversation: Mapping[str, Any],
    config: WeComInstanceConfig,
    *,
    media_resolver: Callable[[str, Mapping[str, Any]], str],
) -> dict[str, Any]:
    raw_content = request.get("content")
    if not isinstance(raw_content, list) or not raw_content:
        raise ConnectorError("INVALID_REQUEST", "content must be a non-empty array")

    vendor_facts = request.get("vendor_facts", {})
    if vendor_facts is None:
        vendor_facts = {}
    if not isinstance(vendor_facts, Mapping):
        raise ConnectorError("INVALID_REQUEST", "vendor facts must be an object")
    unsupported = sorted(set(vendor_facts) - {"msgtype"})
    if unsupported:
        raise ConnectorError(
            "INVALID_REQUEST",
            f"unsupported vendor fact(s) {unsupported}; wecom.app accepts 'msgtype'",
        )
    msgtype = vendor_facts.get("msgtype", "text")
    if msgtype not in SUPPORTED_TEXT_MSG_TYPES:
        raise ConnectorError(
            "INVALID_REQUEST",
            f"msgtype must be one of {SUPPORTED_TEXT_MSG_TYPES}",
        )

    media_parts = [
        part
        for part in raw_content
        if isinstance(part, Mapping) and part.get("kind") in {"image", "file"}
    ]
    if media_parts:
        if len(raw_content) != 1 or len(media_parts) != 1:
            raise ConnectorError(
                "INVALID_REQUEST",
                "WeCom application messages require one media part per message",
            )
        if "msgtype" in vendor_facts:
            raise ConnectorError(
                "INVALID_REQUEST", "media content determines msgtype automatically"
            )
        part = media_parts[0]
        media_type = str(part["kind"])
        media_id = media_resolver(media_type, part)
        return {
            _TARGET_KEYS[conversation["kind"]]: conversation["conversation_id"],
            "msgtype": media_type,
            "agentid": config.agent_id,
            media_type: {"media_id": media_id},
        }

    text_parts: list[str] = []
    mentioned: list[str] = []
    for part in raw_content:
        if not isinstance(part, Mapping):
            raise ConnectorError(
                "INVALID_REQUEST", "each content part must be an object"
            )
        kind = part.get("kind")
        if kind == "text":
            text_parts.append(_required_text(part.get("text"), "content.text"))
        elif kind == "mention":
            if msgtype == "markdown":
                raise ConnectorError(
                    "INVALID_REQUEST", "markdown messages cannot carry mention parts"
                )
            target = part.get("target", "user")
            if target == "everyone":
                text_parts.append("@all")
                mentioned.append("@all")
            elif target == "user":
                target_id = _required_text(part.get("target_id"), "mention.target_id")
                display = part.get("display_name") or target_id
                text_parts.append(f"@{display}")
                mentioned.append(target_id)
            else:
                raise ConnectorError(
                    "INVALID_REQUEST", f"unsupported mention target {target!r}"
                )
        else:
            raise ConnectorError(
                "INVALID_REQUEST", f"unsupported content part kind {kind!r}"
            )

    content = "".join(text_parts)
    if not content:
        raise ConnectorError("INVALID_REQUEST", "rendered message content is empty")

    body: dict[str, Any] = {
        _TARGET_KEYS[conversation["kind"]]: conversation["conversation_id"],
        "msgtype": msgtype,
        "agentid": config.agent_id,
    }
    if msgtype == "markdown":
        body["markdown"] = {"content": content}
    else:
        text: dict[str, Any] = {"content": content}
        if mentioned:
            text["mentioned_list"] = mentioned
        body["text"] = text
    return body


def _errcode(response: Mapping[str, Any]) -> int:
    errcode = response.get("errcode", 0)
    if isinstance(errcode, bool) or not isinstance(errcode, int):
        return -1
    return errcode


def _delivery_result(
    response: Mapping[str, Any], reply_message_id: str | None
) -> dict[str, Any]:
    errcode = _errcode(response)
    errmsg = response.get("errmsg", "")
    facts: list[dict[str, str]] = []
    if reply_message_id is not None:
        facts.append({"name": "reply_reference_ignored", "value": reply_message_id})
    if errcode == 0:
        return {
            "status": "accepted",
            "vendor_message_id": str(response.get("msgid", "") or ""),
            "vendor_facts": facts,
        }
    if errcode in RATE_LIMIT_CODES:
        return {
            "status": "rate_limited",
            "vendor_message_id": "",
            "reason": f"errcode={errcode} errmsg={errmsg}",
            "vendor_facts": facts,
        }
    return {
        "status": "rejected",
        "vendor_message_id": "",
        "reason": f"errcode={errcode} errmsg={errmsg}",
        "vendor_facts": facts,
    }


def _send_request_to_mapping(request: Any) -> dict[str, Any]:
    conversation = {
        "vendor": request.conversation.vendor,
        "account_id": request.conversation.account_id,
        "conversation_id": request.conversation.conversation_id,
        "kind": _KIND_NAMES.get(request.conversation.kind, "unspecified"),
    }
    content: list[dict[str, Any]] = []
    for part in request.content:
        kind = part.WhichOneof("kind")
        if kind == "text":
            content.append({"kind": "text", "text": part.text.text})
        elif kind == "mention":
            target = part.mention.target
            content.append(
                {
                    "kind": "mention",
                    "target": (
                        "everyone"
                        if target == message_contract.MENTION_TARGET_EVERYONE
                        else "user"
                    ),
                    "target_id": part.mention.target_id,
                    "display_name": part.mention.display_name,
                }
            )
        elif kind == "image":
            content.append(
                {
                    "kind": "image",
                    "reference": _attachment_reference_to_mapping(part.image.reference),
                    "mime_type": part.image.mime_type,
                }
            )
        elif kind == "file":
            content.append(
                {
                    "kind": "file",
                    "reference": _attachment_reference_to_mapping(part.file.reference),
                    "file_name": part.file.file_name,
                    "mime_type": part.file.mime_type,
                }
            )
        else:
            content.append({"kind": "unspecified"})
    mapping: dict[str, Any] = {
        "conversation": conversation,
        "content": content,
    }
    reply = request.reply
    if reply.message_id:
        mapping["reply"] = {"message_id": reply.message_id}
    if request.vendor_extension.vendor == VENDOR:
        mapping["vendor_facts"] = {
            fact.name: fact.value for fact in request.vendor_extension.facts
        }
    return mapping


def _attachment_reference_to_mapping(reference: Any) -> dict[str, Any]:
    location = reference.WhichOneof("location")
    if location == "remote_uri":
        return {"remote_uri": reference.remote_uri}
    if location == "vendor_media":
        return {
            "vendor_media": {
                "vendor": reference.vendor_media.vendor,
                "account_id": reference.vendor_media.account_id,
                "media_id": reference.vendor_media.media_id,
            }
        }
    return {}


def _apply_delivery_result(target: Any, result: Mapping[str, Any]) -> None:
    status = {
        "accepted": message_contract.DELIVERY_STATUS_ACCEPTED,
        "rejected": message_contract.DELIVERY_STATUS_REJECTED,
        "rate_limited": message_contract.DELIVERY_STATUS_RATE_LIMITED,
    }.get(
        str(result.get("status", "rejected")), message_contract.DELIVERY_STATUS_REJECTED
    )
    target.status = status
    target.vendor_message_id = str(result.get("vendor_message_id", "") or "")
    target.reason = str(result.get("reason", "") or "")
    target.vendor_extension.vendor = VENDOR
    for fact in result.get("vendor_facts", []):
        entry = target.vendor_extension.facts.add()
        entry.name = fact["name"]
        entry.value = fact["value"]
