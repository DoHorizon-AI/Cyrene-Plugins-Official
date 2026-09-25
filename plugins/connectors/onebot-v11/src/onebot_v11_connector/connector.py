###############################################################################
# 📄 File: plugins/connectors/onebot-v11/src/onebot_v11_connector/connector.py
# Module: Cyrene Plugins Official
# Role: Protocol or message connector implementation.
#
# This file documents an active plugin boundary; runtime behavior is unchanged.
#
# 模块：Cyrene Plugins Official
# 职责：协议或消息连接器实现。
# 本文件属于活动插件边界；运行时行为保持不变。
###############################################################################
"""Generic OneBot v11 implementation of ``message.connector.v1``.

One host activation receives one configured binding, so this package never
selects an instance by adapter type or resolver order. Platform may resolve and
authorize the endpoint, then the Product invokes this package directly. Binding
identity is configuration; worker/runtime generation is an independent
lifecycle observation owned by the host.

The connector uses the packaged Cyrene direct runtime for activation settings.
A host can load ``OneBotV11Connector`` through the generic Python worker and call
``send_message`` or ``on_subscribe``.  HTTP and forward-WebSocket transport
profiles are selected from the configured binding; the OneBot protocol parser
remains independent of Product session policy.

中文：提供通用 OneBot v11 `message.connector.v1` 实现。
一次 Host activation 只接收一个已配置 binding,
因此此 package 绝不会按 adapter 类型或解析顺序选择实例。
Platform 可以解析并授权 Endpoint,随后 Product 直接调用本 package。binding 身份来自配置;
Worker/Runtime 代次则是由 Host 管理的独立生命周期观测值。

connector 使用打包的 Cyrene direct runtime 获取 activation 设置。
Host 可以通过通用 Python Worker 加载 `OneBotV11Connector`,
并调用 `send_message` 或 `on_subscribe`。HTTP 与 forward-WebSocket 传输 profile
根据已配置 binding 选择;OneBot 协议解析器独立于 Product session 策略。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from cyrene_plugin_runtime.configuration import read_environment_settings
from google.protobuf.message import DecodeError

from ._generated import message_connector_pb2 as message_contract

CAPABILITY_ID = "message.connector.v1"
INTERFACE_VERSION = "1"
SEND_MESSAGE_METHOD = "send_message"
RESPOND_REQUEST_METHOD = "respond_request"
INBOUND_MESSAGE_EVENT_TYPE = "inbound_message"
INBOUND_REQUEST_EVENT_TYPE = "inbound_request"
ONEBOT_VENDOR = "onebot.v11"
MESSAGE_CONNECTOR_TYPE_PREFIX = "type.cyrene.io/cyrene.message.connector.v1."
SEND_MESSAGE_REQUEST_TYPE_URL = f"{MESSAGE_CONNECTOR_TYPE_PREFIX}SendMessageRequest"
INBOUND_MESSAGE_TYPE_URL = f"{MESSAGE_CONNECTOR_TYPE_PREFIX}InboundMessagePayload"
DELIVERY_RESULT_TYPE_URL = f"{MESSAGE_CONNECTOR_TYPE_PREFIX}DeliveryResult"
RESPOND_REQUEST_TYPE_URL = "type.cyrene.io/message.connector.v1.respond_request.request"
RESPOND_RESULT_TYPE_URL = "type.cyrene.io/message.connector.v1.respond_request.response"
INBOUND_REQUEST_TYPE_URL = "type.cyrene.io/message.connector.v1.InboundRequestPayload"
MAX_RESPOND_REQUEST_BYTES = 16 * 1024

MAX_VENDOR_FACTS = 32
MAX_VENDOR_FACT_NAME_BYTES = 64
MAX_VENDOR_FACT_VALUE_BYTES = 2_048
MAX_VENDOR_FACT_TOTAL_BYTES = 8_192


class ConnectorError(RuntimeError):
    """Structured failure returned at the generic capability boundary.

        中文:在通用 capability 边界返回的结构化失败。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CancellationToken(Protocol):
    """Minimal cancellation seam provided by the generic worker.

        中文:由通用 Worker 提供的精简取消接口。
    """

    def is_cancelled(self) -> bool:
        """Return whether the current direct invocation has been cancelled.

            中文:返回当前 direct invocation 是否已取消。
        """

        ...


class ApplicationEventEmitter(Protocol):
    """Worker-owned bounded emitter for one application-event subscription.

        中文:由 Worker 拥有、用于单个 application-event 订阅的有界 emitter。
    """

    def emit(self, event_type: str, payload: bytes, type_url: str = "") -> bool:
        """Deliver one typed event and report whether the subscriber accepted it.

            中文:投递一个类型化事件,并报告订阅者是否接受。
        """

        ...


class OneBotTransport(Protocol):
    """Transport abstraction for OneBot actions.

    A transport belongs to one configured binding.  Implementations may expose
    an optional ``start`` and ``set_event_handler`` hook for inbound events; the
    connector uses those hooks without making them part of the required legacy
    transport surface.

        中文：用于 OneBot action 的传输抽象。传输实例属于单个已配置 binding。
        实现可以提供可选的 `start` 和 `set_event_handler` hook 来接收入站事件;
        connector 会使用这些 hook,但不会将其设为旧版传输接口的必需成员。
    """

    def call(
        self,
        action: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float,
        cancellation: CancellationToken | None,
    ) -> Mapping[str, Any]:
        """Execute one binding-scoped OneBot action and return its result object.

            中文:执行一个作用域限定在单个 binding 的 OneBot action,并返回其结果对象。
        """

        ...

    def close(self) -> None:
        """Close the transport and release all binding-local resources.

            中文:关闭传输并释放该 binding 的全部本地资源。
        """

        ...


@dataclass(frozen=True, slots=True)
class OneBotInstanceConfig:
    """Configuration for one host-provided capability binding.

    ``binding_id`` is the stable identity selected by the Product or resolved
    through the control plane. It is not an account or vendor identity and is
    not regenerated when a worker restarts. ``runtime_profile`` is only an
    external transport label; it does not change connector identity.

        中文：由 Host 提供、用于单个 capability binding 的配置。
        `binding_id` 是由 Product 选择或通过控制平面解析得到的稳定身份。
        它不是账户或供应商身份,Worker 重启时也不会重新生成。
        `runtime_profile` 只是外部传输标签,不会改变 connector 身份。
    """

    binding_id: str
    http_base_url: str | None = None
    access_token: str = ""
    runtime_profile: str = "onebot-v11"
    transport_profile: str = "http_api"
    websocket_url: str | None = None
    reverse_listen_host: str = "127.0.0.1"
    reverse_listen_port: int = 6199
    self_account_id: str | None = None
    timeout_seconds: float = 10.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> OneBotInstanceConfig:
        """Validate a host-provided configured binding mapping.

            中文:校验由 Host 提供的已配置 binding 映射。
        """

        binding_id = _required_identifier(value.get("binding_id"), "binding_id")
        raw_base_url = value.get("http_base_url")
        base_url: str | None = None
        if raw_base_url is not None:
            base_url = _required_text(raw_base_url, "http_base_url")
            parsed = urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ConnectorError(
                    "INVALID_REQUEST",
                    "http_base_url must be an absolute http(s) URL",
                )

        raw_websocket_url = value.get("websocket_url")
        websocket_url: str | None = None
        if raw_websocket_url is not None:
            websocket_url = _required_text(raw_websocket_url, "websocket_url")
            parsed = urlparse(websocket_url)
            if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
                raise ConnectorError(
                    "INVALID_REQUEST",
                    "websocket_url must be an absolute ws(s) URL",
                )

        transport_profile = value.get("transport_profile")
        if transport_profile is None:
            transport_profile = "forward_websocket" if websocket_url else "http_api"
        if not isinstance(transport_profile, str):
            raise ConnectorError("INVALID_REQUEST", "transport_profile must be text")
        transport_profile = transport_profile.strip()
        if transport_profile not in {
            "http_api",
            "forward_websocket",
            "reverse_websocket",
        }:
            raise ConnectorError(
                "INVALID_REQUEST",
                "transport_profile must be http_api, forward_websocket, "
                "or reverse_websocket",
            )
        if transport_profile == "http_api" and base_url is None:
            raise ConnectorError(
                "INVALID_REQUEST",
                "http_base_url is required for the http_api transport",
            )
        if transport_profile == "forward_websocket" and websocket_url is None:
            raise ConnectorError(
                "INVALID_REQUEST",
                "websocket_url is required for the forward_websocket transport",
            )

        reverse_listen_host = value.get("reverse_listen_host", "127.0.0.1")
        if not isinstance(reverse_listen_host, str) or not reverse_listen_host.strip():
            raise ConnectorError(
                "INVALID_REQUEST", "reverse_listen_host must be non-empty text"
            )
        reverse_listen_port = value.get("reverse_listen_port", 6199)
        if isinstance(reverse_listen_port, str):
            try:
                reverse_listen_port = int(reverse_listen_port)
            except ValueError as exc:
                raise ConnectorError(
                    "INVALID_REQUEST", "reverse_listen_port must be numeric"
                ) from exc
        if (
            isinstance(reverse_listen_port, bool)
            or not isinstance(reverse_listen_port, int)
            or not 0 <= reverse_listen_port <= 65_535
        ):
            raise ConnectorError(
                "INVALID_REQUEST",
                "reverse_listen_port must be between 0 and 65535",
            )

        access_token = value.get("access_token", "")
        if (
            not isinstance(access_token, str)
            or "\r" in access_token
            or "\n" in access_token
        ):
            raise ConnectorError("INVALID_REQUEST", "access_token must be text")

        runtime_profile = value.get("runtime_profile", "onebot-v11")
        if not isinstance(runtime_profile, str) or not runtime_profile.strip():
            raise ConnectorError(
                "INVALID_REQUEST",
                "runtime_profile must be a non-empty string",
            )
        runtime_profile = runtime_profile.strip()
        if runtime_profile != "onebot-v11":
            raise ConnectorError(
                "INVALID_REQUEST",
                "unsupported OneBot runtime_profile; use onebot-v11",
            )

        account_id = value.get("self_account_id")
        if account_id is not None:
            account_id = _required_identifier(account_id, "self_account_id")

        timeout_seconds = value.get("timeout_seconds", 10.0)
        if isinstance(timeout_seconds, str):
            try:
                timeout_seconds = float(timeout_seconds)
            except ValueError as exc:
                raise ConnectorError(
                    "INVALID_REQUEST", "timeout_seconds must be numeric"
                ) from exc
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ConnectorError(
                "INVALID_REQUEST",
                "timeout_seconds must be a positive number",
            )

        return cls(
            binding_id=binding_id,
            http_base_url=base_url.rstrip("/") if base_url is not None else None,
            access_token=access_token,
            runtime_profile=runtime_profile,
            transport_profile=transport_profile,
            websocket_url=websocket_url,
            reverse_listen_host=reverse_listen_host.strip(),
            reverse_listen_port=reverse_listen_port,
            self_account_id=account_id,
            timeout_seconds=float(timeout_seconds),
        )

    @classmethod
    def from_settings(cls, settings: Mapping[str, str]) -> OneBotInstanceConfig:
        """Build configuration from the generic worker Configure mapping.

            中文:根据通用 Worker 的 Configure 映射构造配置。
        """

        config: dict[str, Any] = {}
        encoded = settings.get("config")
        if encoded:
            try:
                decoded = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise ConnectorError(
                    "INVALID_REQUEST", "config must contain valid JSON"
                ) from exc
            if not isinstance(decoded, Mapping):
                raise ConnectorError("INVALID_REQUEST", "config must be an object")
            config.update(decoded)

        for key in (
            "binding_id",
            "http_base_url",
            "access_token",
            "runtime_profile",
            "transport_profile",
            "websocket_url",
            "reverse_listen_host",
            "reverse_listen_port",
            "self_account_id",
        ):
            if key in settings:
                config[key] = settings[key]
        if "timeout_seconds" in settings:
            try:
                config["timeout_seconds"] = float(settings["timeout_seconds"])
            except ValueError as exc:
                raise ConnectorError(
                    "INVALID_REQUEST", "timeout_seconds must be numeric"
                ) from exc
        return cls.from_mapping(config)


class UrllibOneBotTransport:
    """Small dependency-free OneBot v11 HTTP action transport.

        中文:依赖精简、无需额外依赖的 OneBot v11 HTTP action 传输。
    """

    def __init__(self, config: OneBotInstanceConfig) -> None:
        if config.http_base_url is None:
            raise ConnectorError(
                "INVALID_REQUEST",
                "http_base_url is required for the http_api transport",
            )
        self._base_url = config.http_base_url
        self._access_token = config.access_token

    def call(
        self,
        action: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float,
        cancellation: CancellationToken | None,
    ) -> Mapping[str, Any]:
        """POST one action to the configured OneBot HTTP endpoint.

            中文:向已配置的 OneBot HTTP Endpoint 发送一个 action。
        """

        _raise_if_cancelled(cancellation)
        request = urllib.request.Request(
            f"{self._base_url}/{action}",
            data=json.dumps(dict(params), separators=(",", ":")).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise ConnectorError(
                "EXECUTION_FAILED",
                f"OneBot action {action} returned HTTP {exc.code}",
            ) from exc
        except TimeoutError as exc:
            raise ConnectorError(
                "TIMEOUT", f"OneBot action {action} timed out"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ConnectorError(
                "CAPABILITY_UNAVAILABLE",
                f"OneBot action {action} could not reach its runtime",
            ) from exc

        _raise_if_cancelled(cancellation)
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorError(
                "PROTOCOL_MISMATCH",
                f"OneBot action {action} returned malformed JSON",
            ) from exc
        if not isinstance(decoded, Mapping):
            raise ConnectorError(
                "PROTOCOL_MISMATCH",
                f"OneBot action {action} returned a non-object response",
            )
        if decoded.get("status") != "ok" or decoded.get("retcode") != 0:
            raise ConnectorError(
                "EXECUTION_FAILED",
                f"OneBot action {action} was rejected by the runtime",
            )
        data = decoded.get("data", {})
        if not isinstance(data, Mapping):
            raise ConnectorError(
                "PROTOCOL_MISMATCH",
                f"OneBot action {action} returned non-object data",
            )
        return dict(data)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    def close(self) -> None:
        """There is no persistent resource in the stdlib transport.

            中文:标准库传输中没有持久化资源需要清理。
        """


@dataclass(slots=True)
class _Subscription:
    emitter: ApplicationEventEmitter
    filter: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _DirectTypedPayload:
    value: bytes
    type_url: str


class OneBotV11Connector:
    """Official generic OneBot v11 connector for one configured binding.

        中文:供单个已配置 binding 使用的官方通用 OneBot v11 connector。
    """

    plugin_id = "cyrene.connectors.onebot-v11"
    version = "0.1.0"
    capabilities = (CAPABILITY_ID,)

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        transport: OneBotTransport | None = None,
    ) -> None:
        self._config: OneBotInstanceConfig | None = None
        self._transport = transport
        self._transport_server: Any | None = None
        self._subscriptions: dict[str, _Subscription] = {}
        if config is not None:
            self.configure(config, transport=transport)
        else:
            environment_config = _environment_config()
            if environment_config is not None:
                self.configure(
                    OneBotInstanceConfig.from_settings(environment_config),
                    transport=transport,
                )

    @property
    def configured_binding_id(self) -> str | None:
        """Return stable configured identity, never a runtime generation.

            中文:返回稳定的已配置身份,不返回 Runtime 代次。
        """

        return self._config.binding_id if self._config is not None else None

    @property
    def runtime_profile(self) -> str | None:
        """Return the external transport profile label, if configured.

            中文:如果已配置,则返回外部传输 profile 标签。
        """

        return self._config.runtime_profile if self._config is not None else None

    @property
    def reverse_listen_address(self) -> tuple[str, int] | None:
        """Return the bound reverse-WebSocket address, when that profile is used.

            中文:使用该 profile 时,返回绑定的反向 WebSocket 地址。
        """

        if self._transport_server is None:
            return None
        return self._transport_server.listen_address

    def configure(
        self,
        config: Mapping[str, Any] | OneBotInstanceConfig,
        *,
        transport: OneBotTransport | None = None,
    ) -> None:
        """Configure one worker activation with one stable binding identity.

            中文:使用一个稳定 binding 身份配置一次 Worker activation。
        """

        parsed = (
            config
            if isinstance(config, OneBotInstanceConfig)
            else OneBotInstanceConfig.from_mapping(config)
        )
        if self._config is not None and self._config.binding_id != parsed.binding_id:
            raise ConnectorError(
                "INVALID_REQUEST",
                "a worker activation cannot switch configured binding_id",
            )
        self._config = parsed
        if transport is not None:
            self._transport = transport
        elif self._transport is None:
            if parsed.transport_profile == "forward_websocket":
                from .transport import OneBotWebSocketTransport

                self._transport = OneBotWebSocketTransport(
                    parsed, event_handler=self.publish_inbound_event
                )
            elif parsed.transport_profile == "reverse_websocket":
                from .transport import (
                    OneBotReverseWebSocketServer,
                    OneBotWebSocketTransport,
                )

                self._transport = OneBotWebSocketTransport(
                    parsed, event_handler=self.publish_inbound_event
                )
                self._transport_server = OneBotReverseWebSocketServer(
                    parsed, self._transport
                )
                self._transport_server.start()
            else:
                self._transport = UrllibOneBotTransport(parsed)
        event_handler_setter = getattr(self._transport, "set_event_handler", None)
        if event_handler_setter is not None:
            event_handler_setter(self.publish_inbound_event)

    def on_configure(self, settings: Mapping[str, str]) -> str | None:
        """Generic worker lifecycle hook; ``None`` means configured.

            中文:通用 Worker 生命周期 hook;返回 `None` 表示配置成功。
        """

        try:
            self.configure(OneBotInstanceConfig.from_settings(settings))
        except ConnectorError as exc:
            return f"{exc.code}: {exc.message}"
        return None

    # ════════════════════════════════════════════════════════════════════════
    # 🔧 FUNCTION: OneBotV11Connector.send_message
    #
    #   Converts one canonical message request into a OneBot action while
    #   keeping the configured binding identity and cancellation boundary.
    #
    #   将一次规范消息请求转换为 OneBot action，同时保持配置 binding 身份与取消边界。
    # ════════════════════════════════════════════════════════════════════════
    def send_message(
        self,
        request: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """Invoke canonical ``message.connector.v1/send_message``.

            中文:调用规范的 `message.connector.v1/send_message`。
        """

        config = self._require_configured()
        transport = self._require_transport()
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
        segments = build_onebot_segments(request)
        action = (
            "send_group_msg" if conversation["kind"] == "group" else "send_private_msg"
        )
        target_key = "group_id" if conversation["kind"] == "group" else "user_id"
        result = transport.call(
            action,
            {
                target_key: _numeric_identifier(
                    conversation["conversation_id"], "conversation_id"
                ),
                "message": segments,
            },
            timeout_seconds=config.timeout_seconds,
            cancellation=cancellation,
        )
        _raise_if_cancelled(cancellation)
        vendor_message_id = result.get("message_id")
        if vendor_message_id is not None:
            vendor_message_id = _required_identifier(vendor_message_id, "message_id")
        return {
            "status": "accepted",
            "vendor_message_id": vendor_message_id or "",
        }

    def respond_request(
        self,
        request: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """Approve or reject a normalized OneBot friend/group request.

        The Product supplies the opaque OneBot ``flag`` as ``request_id``.
        ``friend`` maps to ``set_friend_add_request`` and ``group_invite`` maps
        to ``set_group_add_request`` with ``sub_type=invite``.  The connector
        never derives a target from Product conversation state.

            中文：批准或拒绝经过规范化的 OneBot 好友／群组请求。
            Product 会将不透明的 OneBot `flag` 作为 `request_id` 传入。
            `friend` 映射为 `set_friend_add_request`;
            `group_invite` 则映射为带有 `sub_type=invite` 的 `set_group_add_request`。
            connector 绝不会根据 Product 会话状态推导目标对象。
        """

        config = self._require_configured()
        transport = self._require_transport()
        value = _mapping(request, "respond_request")
        unknown = set(value).difference(
            {"request_id", "request_kind", "decision", "comment", "vendor_request"}
        )
        if unknown:
            raise ConnectorError(
                "INVALID_REQUEST", "respond_request contains unknown fields"
            )
        request_id = _required_identifier(value.get("request_id"), "request_id")
        request_kind = value.get("request_kind")
        if request_kind not in {"friend", "group_invite"}:
            raise ConnectorError(
                "INVALID_REQUEST",
                "request_kind must be friend or group_invite",
            )
        decision = value.get("decision")
        if decision not in {"approve", "reject"}:
            raise ConnectorError(
                "INVALID_REQUEST", "decision must be approve or reject"
            )
        comment = value.get("comment", "")
        if not isinstance(comment, str) or len(comment.encode("utf-8")) > 2_048:
            raise ConnectorError(
                "INVALID_REQUEST", "comment must be at most 2048 UTF-8 bytes"
            )
        vendor_request = value.get("vendor_request", {})
        vendor_request = _mapping(vendor_request, "vendor_request")
        unknown_vendor = set(vendor_request).difference(
            {"sub_type", "group_id", "user_id"}
        )
        if unknown_vendor:
            raise ConnectorError(
                "INVALID_REQUEST", "vendor_request contains unknown fields"
            )
        sub_type = vendor_request.get(
            "sub_type", "invite" if request_kind == "group_invite" else "add"
        )
        if sub_type not in {"add", "invite"}:
            raise ConnectorError(
                "INVALID_REQUEST", "vendor_request.sub_type must be add or invite"
            )
        _raise_if_cancelled(cancellation)
        if request_kind == "friend":
            action = "set_friend_add_request"
            params: dict[str, Any] = {
                "flag": request_id,
                "approve": decision == "approve",
            }
            if comment:
                params["remark"] = comment
        else:
            action = "set_group_add_request"
            params = {
                "flag": request_id,
                "sub_type": sub_type,
                "approve": decision == "approve",
            }
            if comment:
                params["reason"] = comment
        transport.call(
            action,
            params,
            timeout_seconds=config.timeout_seconds,
            cancellation=cancellation,
        )
        _raise_if_cancelled(cancellation)
        return {
            "status": "accepted",
            "request_id": request_id,
            "request_kind": request_kind,
            "decision": decision,
        }

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: CancellationToken | None = None,
        request_type_url: str | None = None,
    ) -> tuple[bool, Any]:
        """Adapt the direct Plugin runtime call to the typed connector contract.

            中文:将 direct Plugin runtime 调用适配为类型化 connector contract。
        """

        try:
            if capability != CAPABILITY_ID:
                raise ConnectorError(
                    "INVALID_REQUEST", f"unsupported capability {capability!r}"
                )
            if action not in {SEND_MESSAGE_METHOD, RESPOND_REQUEST_METHOD}:
                raise ConnectorError(
                    "INVALID_REQUEST", f"unsupported method {action!r}"
                )
            if action == RESPOND_REQUEST_METHOD:
                if request_type_url != RESPOND_REQUEST_TYPE_URL:
                    raise ConnectorError(
                        "INVALID_REQUEST",
                        f"request_type_url must be {RESPOND_REQUEST_TYPE_URL}",
                    )
                if len(payload) > MAX_RESPOND_REQUEST_BYTES:
                    raise ConnectorError(
                        "INVALID_REQUEST",
                        "respond_request payload exceeds 16384 bytes",
                    )
                request = json.loads(payload.decode("utf-8"))
                result = self.respond_request(request, cancellation=cancellation)
                return True, _direct_typed_payload(
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    RESPOND_RESULT_TYPE_URL,
                )
            if (
                request_type_url is not None
                and request_type_url != SEND_MESSAGE_REQUEST_TYPE_URL
            ):
                raise ConnectorError(
                    "INVALID_REQUEST",
                    f"request_type_url must be {SEND_MESSAGE_REQUEST_TYPE_URL}",
                )
            request = message_contract.SendMessageRequest()
            request.ParseFromString(payload)
            result = self.send_message(
                _send_request_from_proto(request), cancellation=cancellation
            )
            response = _delivery_result_from_mapping(result)
            return True, _direct_typed_payload(
                response.SerializeToString(), DELIVERY_RESULT_TYPE_URL
            )
        except DecodeError as exc:
            return False, _direct_error(
                ConnectorError(
                    "INVALID_REQUEST",
                    f"send_message payload is invalid protobuf: {exc}",
                )
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return False, _direct_error(
                ConnectorError(
                    "INVALID_REQUEST", f"respond_request JSON is invalid: {exc}"
                )
            )
        except ConnectorError as exc:
            return False, _direct_error(exc)
        except Exception as exc:  # Keep errors inside the worker protocol.
        # 中文:将错误保留在 Worker 协议内。
            return False, _direct_error(
                ConnectorError("EXECUTION_FAILED", f"OneBot invocation failed: {exc}")
            )

    # ════════════════════════════════════════════════════════════════════════
    # 🔧 FUNCTION: OneBotV11Connector.on_subscribe
    #
    #   Attaches an event emitter to this activation's configured binding and
    #   starts the transport only after the subscription is accepted.
    #
    #   将事件 emitter 绑定到当前 activation 的配置身份，并在订阅接受后启动传输。
    # ════════════════════════════════════════════════════════════════════════
    def on_subscribe(
        self,
        subscription_id: str,
        capability: str,
        filter_payload: bytes,
        emitter: ApplicationEventEmitter | None = None,
    ) -> str | None:
        """Attach one event stream to this activation's configured binding.

            中文:为此 activation 附加一个属于已配置 binding 的事件流。
        """

        self._require_configured()
        if capability != CAPABILITY_ID:
            return f"INVALID_REQUEST: unsupported capability {capability!r}"
        if not subscription_id.strip():
            return "INVALID_REQUEST: subscription_id must not be empty"
        if emitter is None:
            return "CAPABILITY_UNAVAILABLE: application event emitter is required"
        try:
            event_filter = _decode_filter(filter_payload)
        except ConnectorError as exc:
            return f"{exc.code}: {exc.message}"
        self._subscriptions[subscription_id] = _Subscription(emitter, event_filter)
        starter = getattr(self._transport, "start", None)
        if starter is not None:
            starter()
        return None

    def on_unsubscribe(self, subscription_id: str, reason: str) -> None:
        """Detach one binding-local subscription without affecting the transport.

            中文:分离一个 binding 本地订阅,不影响传输。
        """

        del reason
        self._subscriptions.pop(subscription_id, None)

    def publish_inbound_event(self, event: Mapping[str, Any]) -> int:
        """Normalize one OneBot message event and emit it to this binding only.

            中文:规范化一条 OneBot 消息事件,并且只发给此 binding。
        """

        config = self._require_configured()
        if event.get("post_type") == "request":
            payload = normalize_request_event(event)
            if (
                config.self_account_id
                and payload["account_id"] != config.self_account_id
            ):
                raise ConnectorError(
                    "INVALID_REQUEST",
                    "inbound event self account does not match configured instance",
                )
            return self._publish_json_event(
                payload,
                INBOUND_REQUEST_EVENT_TYPE,
                INBOUND_REQUEST_TYPE_URL,
            )
        payload = normalize_inbound_event(event)
        account_id = payload["conversation"]["account_id"]
        if config.self_account_id and account_id != config.self_account_id:
            raise ConnectorError(
                "INVALID_REQUEST",
                "inbound event self account does not match configured instance",
            )
        encoded = _inbound_message_to_proto(payload).SerializeToString()
        return self._publish_encoded_event(
            payload, encoded, INBOUND_MESSAGE_EVENT_TYPE, INBOUND_MESSAGE_TYPE_URL
        )

    def _publish_json_event(
        self, payload: Mapping[str, Any], event_type: str, type_url: str
    ) -> int:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self._publish_encoded_event(payload, encoded, event_type, type_url)

    def _publish_encoded_event(
        self,
        payload: Mapping[str, Any],
        encoded: bytes,
        event_type: str,
        type_url: str,
    ) -> int:
        delivered = 0
        for subscription_id, subscription in list(self._subscriptions.items()):
            if not _filter_matches(subscription.filter, payload, event_type):
                continue
            if subscription.emitter.emit(event_type, encoded, type_url):
                delivered += 1
            else:
                self._subscriptions.pop(subscription_id, None)
        return delivered

    def close(self) -> None:
        """Close the configured transport and detach application streams.

            中文:关闭已配置的传输并分离 application event 流。
        """

        self._subscriptions.clear()
        if self._transport_server is not None:
            self._transport_server.close()
            self._transport_server = None
        if self._transport is not None:
            self._transport.close()

    def on_shutdown(self, grace_period_ms: int) -> None:
        """Stop the configured transport within the worker shutdown callback.

            中文:在 Worker 关闭回调的时限内停止已配置的传输。
        """

        del grace_period_ms
        self.close()

    def _require_configured(self) -> OneBotInstanceConfig:
        if self._config is None:
            raise ConnectorError(
                "CAPABILITY_UNAVAILABLE",
                "OneBot connector has no configured capability binding",
            )
        return self._config

    def _require_transport(self) -> OneBotTransport:
        if self._transport is None:
            raise ConnectorError(
                "CAPABILITY_UNAVAILABLE",
                "OneBot connector transport is unavailable",
            )
        return self._transport


def _send_request_from_proto(
    request: message_contract.SendMessageRequest,
) -> dict[str, Any]:
    if not request.HasField("conversation"):
        raise ConnectorError("INVALID_REQUEST", "send_message conversation is required")
    payload: dict[str, Any] = {
        "conversation": _conversation_from_proto(request.conversation),
        "content": [
            _content_part_from_proto(part, f"content[{index}]")
            for index, part in enumerate(request.content)
        ],
    }
    if request.HasField("reply"):
        payload["reply"] = {"message_id": request.reply.message_id}
    if request.HasField("vendor_extension"):
        payload["vendor_extension"] = _vendor_extension_from_proto(
            request.vendor_extension
        )
    return payload


def _delivery_result_from_mapping(
    result: Mapping[str, Any],
) -> message_contract.DeliveryResult:
    response = message_contract.DeliveryResult()
    status = result.get("status", "accepted")
    status_values = {
        "accepted": message_contract.DELIVERY_STATUS_ACCEPTED,
        "rejected": message_contract.DELIVERY_STATUS_REJECTED,
        "rate_limited": message_contract.DELIVERY_STATUS_RATE_LIMITED,
    }
    if isinstance(status, str):
        status = status_values.get(status)
    if not isinstance(status, int) or status not in status_values.values():
        raise ConnectorError("INVALID_REQUEST", "delivery status is unsupported")
    response.status = status
    vendor_message_id = result.get("vendor_message_id", "")
    if vendor_message_id:
        response.vendor_message_id = _required_identifier(
            vendor_message_id, "vendor_message_id"
        )
    reason = result.get("reason", "")
    if reason:
        response.reason = _required_text(reason, "reason")
    return response


def _inbound_message_to_proto(
    payload: Mapping[str, Any],
) -> message_contract.InboundMessagePayload:
    response = message_contract.InboundMessagePayload()
    response.message_id = _required_identifier(payload.get("message_id"), "message_id")
    response.conversation.CopyFrom(
        _conversation_to_proto(_mapping(payload.get("conversation"), "conversation"))
    )
    response.sender_id = _required_identifier(payload.get("sender_id"), "sender_id")
    response.sender_display_name = _required_text(
        payload.get("sender_display_name"), "sender_display_name"
    )
    content = payload.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        raise ConnectorError("INVALID_REQUEST", "content must be an ordered list")
    for index, value in enumerate(content):
        response.content.add().CopyFrom(
            _content_part_to_proto(
                _mapping(value, f"content[{index}]"), f"content[{index}]"
            )
        )
    if payload.get("reply") is not None:
        reply = _mapping(payload["reply"], "reply")
        response.reply.message_id = _required_identifier(
            reply.get("message_id"), "reply.message_id"
        )
    if payload.get("vendor_extension") is not None:
        response.vendor_extension.CopyFrom(
            _vendor_extension_to_proto(
                _mapping(payload["vendor_extension"], "vendor_extension")
            )
        )
    return response


def _conversation_from_proto(
    value: message_contract.ConversationScope,
) -> dict[str, Any]:
    kind = {
        message_contract.CONVERSATION_KIND_PRIVATE: "private",
        message_contract.CONVERSATION_KIND_GROUP: "group",
        message_contract.CONVERSATION_KIND_CHANNEL: "channel",
    }.get(value.kind)
    if kind is None:
        raise ConnectorError("INVALID_REQUEST", "unsupported conversation kind")
    return {
        "vendor": value.vendor,
        "account_id": value.account_id,
        "conversation_id": value.conversation_id,
        "kind": kind,
    }


def _conversation_to_proto(
    value: Mapping[str, Any],
) -> message_contract.ConversationScope:
    response = message_contract.ConversationScope()
    response.vendor = _required_text(value.get("vendor"), "conversation.vendor")
    response.account_id = _required_identifier(
        value.get("account_id"), "conversation.account_id"
    )
    response.conversation_id = _required_identifier(
        value.get("conversation_id"), "conversation.conversation_id"
    )
    response.kind = _conversation_kind_to_proto(value.get("kind"))
    return response


def _conversation_kind_to_proto(value: Any) -> int:
    if value in {"private", "CONVERSATION_KIND_PRIVATE", 1}:
        return message_contract.CONVERSATION_KIND_PRIVATE
    if value in {"group", "CONVERSATION_KIND_GROUP", 2}:
        return message_contract.CONVERSATION_KIND_GROUP
    if value in {"channel", "CONVERSATION_KIND_CHANNEL", 3}:
        return message_contract.CONVERSATION_KIND_CHANNEL
    raise ConnectorError("INVALID_REQUEST", "unsupported conversation kind")


def _content_part_from_proto(
    value: message_contract.MessageContentPart,
    field: str,
) -> dict[str, Any]:
    kind = value.WhichOneof("kind")
    if kind == "text":
        return {"text": {"text": value.text.text}}
    if kind == "mention":
        target = {
            message_contract.MENTION_TARGET_USER: "user",
            message_contract.MENTION_TARGET_EVERYONE: "everyone",
        }.get(value.mention.target)
        if target is None:
            raise ConnectorError(
                "INVALID_REQUEST", f"{field} mention target is unsupported"
            )
        return {
            "mention": {
                "target": target,
                "target_id": value.mention.target_id,
                "display_name": value.mention.display_name,
            }
        }
    if kind == "image":
        return {
            "image": {
                "reference": _attachment_reference_from_proto(value.image.reference),
                "mime_type": value.image.mime_type,
            }
        }
    if kind == "file":
        return {
            "file": {
                "reference": _attachment_reference_from_proto(value.file.reference),
                "file_name": value.file.file_name,
                "mime_type": value.file.mime_type,
            }
        }
    raise ConnectorError("INVALID_REQUEST", f"{field} has no content kind")


def _content_part_to_proto(
    value: Mapping[str, Any],
    field: str,
) -> message_contract.MessageContentPart:
    if len(value) != 1:
        raise ConnectorError(
            "INVALID_REQUEST", f"{field} must contain exactly one content kind"
        )
    kind, raw = next(iter(value.items()))
    item = _mapping(raw, f"{field}.{kind}")
    response = message_contract.MessageContentPart()
    if kind == "text":
        text = item.get("text")
        if not isinstance(text, str):
            raise ConnectorError("INVALID_REQUEST", f"{field}.text must be text")
        response.text.text = text
    elif kind == "mention":
        response.mention.target = _mention_target_to_proto(item.get("target", "user"))
        if response.mention.target == message_contract.MENTION_TARGET_USER:
            response.mention.target_id = _required_identifier(
                item.get("target_id"), f"{field}.mention.target_id"
            )
        response.mention.display_name = str(item.get("display_name", ""))
    elif kind in {"image", "file"}:
        reference = _mapping(item.get("reference"), f"{field}.{kind}.reference")
        target = response.image if kind == "image" else response.file
        target.reference.CopyFrom(
            _attachment_reference_to_proto(reference, f"{field}.{kind}.reference")
        )
        if kind == "image":
            target.mime_type = str(item.get("mime_type", ""))
        else:
            target.file_name = str(item.get("file_name", ""))
            target.mime_type = str(item.get("mime_type", ""))
    else:
        raise ConnectorError("INVALID_REQUEST", f"{field} has unsupported content kind")
    return response


def _mention_target_to_proto(value: Any) -> int:
    if value in {"user", "MENTION_TARGET_USER", 1}:
        return message_contract.MENTION_TARGET_USER
    if value in {"everyone", "MENTION_TARGET_EVERYONE", 2}:
        return message_contract.MENTION_TARGET_EVERYONE
    raise ConnectorError("INVALID_REQUEST", "unsupported mention target")


def _attachment_reference_from_proto(
    value: message_contract.AttachmentReference,
) -> dict[str, Any]:
    location = value.WhichOneof("location")
    if location == "remote_uri":
        return {"remote_uri": value.remote_uri}
    if location == "vendor_media":
        return {
            "vendor_media": {
                "vendor": value.vendor_media.vendor,
                "account_id": value.vendor_media.account_id,
                "media_id": value.vendor_media.media_id,
            }
        }
    raise ConnectorError("INVALID_REQUEST", "attachment reference location is required")


def _attachment_reference_to_proto(
    value: Mapping[str, Any],
    field: str,
) -> message_contract.AttachmentReference:
    response = message_contract.AttachmentReference()
    remote_uri = value.get("remote_uri")
    vendor_media = value.get("vendor_media")
    if remote_uri is not None and vendor_media is not None:
        raise ConnectorError("INVALID_REQUEST", f"{field} has multiple locations")
    if remote_uri is not None:
        response.remote_uri = _required_text(remote_uri, f"{field}.remote_uri")
        parsed = urlparse(response.remote_uri)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConnectorError(
                "INVALID_REQUEST", f"{field}.remote_uri must be http(s)"
            )
        return response
    media = _mapping(vendor_media, f"{field}.vendor_media")
    response.vendor_media.vendor = _required_text(
        media.get("vendor"), f"{field}.vendor_media.vendor"
    )
    response.vendor_media.account_id = _required_identifier(
        media.get("account_id"), f"{field}.vendor_media.account_id"
    )
    response.vendor_media.media_id = _required_identifier(
        media.get("media_id"), f"{field}.vendor_media.media_id"
    )
    return response


def _vendor_extension_from_proto(
    value: message_contract.VendorExtension,
) -> dict[str, Any]:
    return {
        "vendor": value.vendor,
        "facts": [{"name": fact.name, "value": fact.value} for fact in value.facts],
    }


def _vendor_extension_to_proto(
    value: Mapping[str, Any],
) -> message_contract.VendorExtension:
    response = message_contract.VendorExtension()
    response.vendor = _required_text(value.get("vendor"), "vendor_extension.vendor")
    facts = value.get("facts", [])
    if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes)):
        raise ConnectorError("INVALID_REQUEST", "vendor_extension.facts must be a list")
    for index, raw_fact in enumerate(facts):
        fact = _mapping(raw_fact, f"vendor_extension.facts[{index}]")
        target = response.facts.add()
        target.name = _required_text(
            fact.get("name"), f"vendor_extension.facts[{index}].name"
        )
        target.value = _required_text(
            fact.get("value"), f"vendor_extension.facts[{index}].value"
        )
    return response


def _direct_typed_payload(value: bytes, type_url: str) -> Any:
    """Return one direct-runtime structural payload.

        中文:返回一个 direct-runtime 结构化负载。
    """

    return _DirectTypedPayload(value, type_url)


def _direct_error(error: ConnectorError) -> str:
    """Return one direct-runtime structured error prefix and message.

        中文:返回一个 direct-runtime 结构化错误前缀和消息。
    """

    return f"{error.code}: {error.message}"


# ════════════════════════════════════════════════════════════════════════
# 🔧 FUNCTION: build_onebot_segments
#
#   Maps ordered canonical content parts to OneBot segments without changing
#   the caller's ordering or treating reply metadata as ordinary content.
#
#   将有序的规范内容片段映射为 OneBot segment，不改变顺序，
#   也不把 reply 元数据当作普通内容。
# ════════════════════════════════════════════════════════════════════════
def build_onebot_segments(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Map canonical ordered content parts to ordered OneBot v11 segments.

        中文:将规范的有序内容部分映射为有序的 OneBot v11 segment。
    """

    content = request.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        raise ConnectorError("INVALID_REQUEST", "content must be an ordered list")

    segments: list[dict[str, Any]] = []
    reply = request.get("reply")
    if reply is not None:
        reply_map = _mapping(reply, "reply")
        message_id = _numeric_identifier(
            reply_map.get("message_id"), "reply.message_id"
        )
        segments.append({"type": "reply", "data": {"id": message_id}})

    for index, value in enumerate(content):
        part = _mapping(value, f"content[{index}]")
        if len(part) != 1:
            raise ConnectorError(
                "INVALID_REQUEST",
                f"content[{index}] must contain exactly one content kind",
            )
        kind, raw = next(iter(part.items()))
        item = _mapping(raw, f"content[{index}].{kind}")
        if kind == "text":
            text = item.get("text")
            if not isinstance(text, str):
                raise ConnectorError("INVALID_REQUEST", "text content must be text")
            segments.append({"type": "text", "data": {"text": text}})
        elif kind == "mention":
            target = item.get("target", "user")
            if target in {"everyone", "MENTION_TARGET_EVERYONE", 2}:
                target_id = "all"
            else:
                if target not in {"user", "MENTION_TARGET_USER", 1}:
                    raise ConnectorError(
                        "INVALID_REQUEST", "unsupported mention target"
                    )
                target_id = _required_identifier(
                    item.get("target_id"), "mention.target_id"
                )
            segments.append({"type": "at", "data": {"qq": target_id}})
        elif kind in {"image", "file"}:
            reference = _mapping(item.get("reference"), f"{kind}.reference")
            source_key, source = _attachment_source(reference, f"{kind}.reference")
            data: dict[str, Any] = {source_key: source}
            if kind == "file" and item.get("file_name"):
                data["name"] = _required_text(item["file_name"], "file.file_name")
            segments.append({"type": kind, "data": data})
        else:
            raise ConnectorError(
                "INVALID_REQUEST",
                f"unsupported message content kind {kind!r}",
            )

    if not segments:
        raise ConnectorError("INVALID_REQUEST", "message content must not be empty")
    return segments


# ════════════════════════════════════════════════════════════════════════
# 🔧 FUNCTION: normalize_inbound_event
#
#   Converts an inbound OneBot message event into the connector's canonical
#   event shape and preserves the distinction between content and metadata.
#
#   将 OneBot 入站消息事件转换为 connector 的规范事件形态，并保留内容与元数据的区别。
# ════════════════════════════════════════════════════════════════════════
def normalize_inbound_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Map a OneBot v11 message event to canonical connector JSON semantics.

        中文:将 OneBot v11 消息事件映射为规范 connector JSON 语义。
    """

    if not isinstance(event, Mapping) or event.get("post_type") != "message":
        raise ConnectorError("INVALID_REQUEST", "event is not a OneBot message event")
    message_type = event.get("message_type")
    if message_type not in {"private", "group"}:
        raise ConnectorError("INVALID_REQUEST", "unsupported OneBot message type")

    self_id = _required_identifier(event.get("self_id"), "self_id")
    sender = event.get("sender", {})
    sender_map = _mapping(sender, "sender")
    sender_id = _required_identifier(
        sender_map.get("user_id", event.get("user_id")), "sender.user_id"
    )
    group_id = _optional_identifier(event.get("group_id"))
    conversation_id = group_id if message_type == "group" else sender_id
    if conversation_id is None:
        raise ConnectorError("INVALID_REQUEST", "conversation identity is missing")

    raw_message = event.get("message")
    if not isinstance(raw_message, Sequence) or isinstance(raw_message, (str, bytes)):
        raise ConnectorError(
            "INVALID_REQUEST", "OneBot message must be an ordered list"
        )

    content: list[dict[str, Any]] = []
    reply: dict[str, str] | None = None
    vendor_facts: list[dict[str, str]] = []
    for index, raw_segment in enumerate(raw_message):
        segment = _mapping(raw_segment, f"message[{index}]")
        segment_type = _required_text(segment.get("type"), f"message[{index}].type")
        data = _mapping(segment.get("data", {}), f"message[{index}].data")
        if segment_type == "text":
            text = data.get("text")
            if not isinstance(text, str):
                raise ConnectorError(
                    "INVALID_REQUEST", "OneBot text segment is invalid"
                )
            content.append({"text": {"text": text}})
        elif segment_type == "at":
            target = _required_identifier(data.get("qq"), "at.qq")
            if target == "all":
                content.append({"mention": {"target": "everyone", "target_id": ""}})
            else:
                content.append(
                    {
                        "mention": {
                            "target": "user",
                            "target_id": target,
                            "display_name": str(data.get("name", "")),
                        }
                    }
                )
        elif segment_type in {"image", "file"}:
            content.append(_inbound_attachment_part(segment_type, data, self_id))
        elif segment_type == "reply":
            if reply is not None:
                raise ConnectorError(
                    "INVALID_REQUEST", "OneBot event contains duplicate replies"
                )
            reply = {"message_id": _required_identifier(data.get("id"), "reply.id")}
        else:
            _collect_vendor_facts(vendor_facts, index, segment_type, data)

    if not content and reply is None:
        raise ConnectorError("INVALID_REQUEST", "OneBot event has no supported content")

    sender_name = sender_map.get("card") or sender_map.get("nickname") or sender_id
    if not isinstance(sender_name, str):
        sender_name = sender_id
    payload: dict[str, Any] = {
        "message_id": _required_identifier(event.get("message_id"), "message_id"),
        "conversation": {
            "vendor": ONEBOT_VENDOR,
            "account_id": self_id,
            "conversation_id": conversation_id,
            "kind": message_type,
        },
        "sender_id": sender_id,
        "sender_display_name": sender_name,
        "content": content,
    }
    if reply is not None:
        payload["reply"] = reply
    if vendor_facts:
        payload["vendor_extension"] = {
            "vendor": ONEBOT_VENDOR,
            "facts": vendor_facts,
        }
    return payload


def normalize_request_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a OneBot friend or group request for Product auto-approval.

        中文:规范化 OneBot 好友或群组请求,供 Product 自动审批。
    """

    if not isinstance(event, Mapping) or event.get("post_type") != "request":
        raise ConnectorError("INVALID_REQUEST", "event is not a OneBot request event")
    request_type = event.get("request_type")
    sub_type = event.get("sub_type")
    if request_type == "friend":
        request_kind = "friend"
        normalized_sub_type = "add"
    elif request_type == "group" and sub_type in {"add", "invite"}:
        request_kind = "group_invite"
        normalized_sub_type = sub_type
    else:
        raise ConnectorError(
            "INVALID_REQUEST",
            "only friend and group add/invite requests are supported",
        )
    self_id = _required_identifier(event.get("self_id"), "self_id")
    request_id = _required_identifier(event.get("flag"), "flag")
    vendor_request: dict[str, str] = {"sub_type": normalized_sub_type}
    for source, target in (("group_id", "group_id"), ("user_id", "user_id")):
        value = event.get(source)
        if value is not None:
            vendor_request[target] = _required_identifier(value, source)
    return {
        "account_id": self_id,
        "vendor": ONEBOT_VENDOR,
        "request_id": request_id,
        "request_kind": request_kind,
        "vendor_request": vendor_request,
    }


def _conversation_for_send(
    request: Mapping[str, Any], config: OneBotInstanceConfig
) -> dict[str, str]:
    conversation = _mapping(request.get("conversation"), "conversation")
    vendor = conversation.get("vendor")
    if vendor != ONEBOT_VENDOR:
        raise ConnectorError(
            "INVALID_REQUEST",
            f"conversation.vendor must be {ONEBOT_VENDOR!r}",
        )
    account_id = _required_identifier(conversation.get("account_id"), "account_id")
    if config.self_account_id and account_id != config.self_account_id:
        raise ConnectorError(
            "INVALID_REQUEST",
            "conversation account does not match configured instance",
        )
    kind = conversation.get("kind")
    if kind in {"CONVERSATION_KIND_PRIVATE", "private", 1}:
        normalized_kind = "private"
    elif kind in {"CONVERSATION_KIND_GROUP", "group", 2}:
        normalized_kind = "group"
    else:
        raise ConnectorError("INVALID_REQUEST", "unsupported conversation kind")
    return {
        "account_id": account_id,
        "conversation_id": _required_identifier(
            conversation.get("conversation_id"), "conversation_id"
        ),
        "kind": normalized_kind,
    }


def _inbound_attachment_part(
    segment_type: str,
    data: Mapping[str, Any],
    account_id: str,
) -> dict[str, Any]:
    source_value = data.get("url") or data.get("file")
    if not isinstance(source_value, str) or not source_value:
        raise ConnectorError(
            "INVALID_REQUEST",
            f"OneBot {segment_type} segment has no attachment reference",
        )
    reference: dict[str, Any]
    parsed = urlparse(source_value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        reference = {"remote_uri": source_value}
    else:
        reference = {
            "vendor_media": {
                "vendor": ONEBOT_VENDOR,
                "account_id": account_id,
                "media_id": source_value,
            }
        }
    if segment_type == "image":
        return {"image": {"reference": reference, "mime_type": ""}}
    return {
        "file": {
            "reference": reference,
            "file_name": str(data.get("name", "")),
            "mime_type": "",
        }
    }


def _attachment_source(reference: Mapping[str, Any], field: str) -> tuple[str, str]:
    remote_uri = reference.get("remote_uri")
    if remote_uri is not None:
        remote_uri = _required_text(remote_uri, f"{field}.remote_uri")
        parsed = urlparse(remote_uri)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConnectorError(
                "INVALID_REQUEST", f"{field}.remote_uri must be http(s)"
            )
        return "url", remote_uri
    vendor_media = _mapping(reference.get("vendor_media"), f"{field}.vendor_media")
    return "file", _required_identifier(
        vendor_media.get("media_id"), f"{field}.media_id"
    )


def _decode_filter(filter_payload: bytes) -> dict[str, Any]:
    if not filter_payload:
        return {}
    try:
        value = json.loads(filter_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorError(
            "INVALID_REQUEST", "subscription filter is invalid JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise ConnectorError("INVALID_REQUEST", "subscription filter must be an object")
    unknown = set(value).difference(
        {"event_type", "conversation_id", "kind", "request_kind"}
    )
    if unknown:
        raise ConnectorError(
            "INVALID_REQUEST", "subscription filter contains unknown fields"
        )
    if "event_type" in value and value["event_type"] not in {
        INBOUND_MESSAGE_EVENT_TYPE,
        INBOUND_REQUEST_EVENT_TYPE,
    }:
        raise ConnectorError(
            "INVALID_REQUEST", "subscription filter event_type is unsupported"
        )
    if "request_kind" in value and value["request_kind"] not in {
        "friend",
        "group_invite",
    }:
        raise ConnectorError(
            "INVALID_REQUEST", "subscription filter request_kind is unsupported"
        )
    for field in ("conversation_id", "kind", "request_kind"):
        if field in value and not isinstance(value[field], str):
            raise ConnectorError(
                "INVALID_REQUEST", f"subscription filter {field} must be text"
            )
    return dict(value)


def _filter_matches(
    filter_value: Mapping[str, Any],
    payload: Mapping[str, Any],
    event_type: str | None = None,
) -> bool:
    if (
        event_type is not None
        and "event_type" in filter_value
        and filter_value["event_type"] != event_type
    ):
        return False
    conversation = payload.get("conversation")
    if conversation is None:
        if "conversation_id" in filter_value or "kind" in filter_value:
            return False
        return not (
            "request_kind" in filter_value
            and filter_value["request_kind"] != payload.get("request_kind")
        )
    if (
        "conversation_id" in filter_value
        and filter_value["conversation_id"] != conversation["conversation_id"]
    ):
        return False
    return not ("kind" in filter_value and filter_value["kind"] != conversation["kind"])


def _collect_vendor_facts(
    facts: list[dict[str, str]],
    index: int,
    segment_type: str,
    data: Mapping[str, Any],
) -> None:
    """Keep unknown segments bounded without forwarding raw vendor payloads.

        中文:对未知 segment 进行有界保留,不转发原始供应商负载。
    """

    values: list[tuple[str, Any]] = [("type", segment_type)]
    values.extend(
        (str(key), value)
        for key, value in data.items()
        if isinstance(value, (str, int, float, bool))
    )
    for key, value in values:
        if len(facts) >= MAX_VENDOR_FACTS:
            return
        name = f"segment_{index}_{key}"[:MAX_VENDOR_FACT_NAME_BYTES]
        text = str(value)[:MAX_VENDOR_FACT_VALUE_BYTES]
        proposed = (
            sum(
                len(item["name"].encode("utf-8")) + len(item["value"].encode("utf-8"))
                for item in facts
            )
            + len(name.encode("utf-8"))
            + len(text.encode("utf-8"))
        )
        if proposed > MAX_VENDOR_FACT_TOTAL_BYTES:
            return
        facts.append({"name": name, "value": text})


def _raise_if_cancelled(cancellation: CancellationToken | None) -> None:
    if cancellation is not None and cancellation.is_cancelled():
        raise ConnectorError("CANCELLED", "OneBot operation was cancelled")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConnectorError("INVALID_REQUEST", f"{field} must be an object")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConnectorError("INVALID_REQUEST", f"{field} must be non-empty text")
    return value.strip()


def _required_identifier(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConnectorError("INVALID_REQUEST", f"{field} must be a string or integer")
    text = str(value).strip()
    if not text:
        raise ConnectorError("INVALID_REQUEST", f"{field} must be non-empty")
    return text


def _optional_identifier(value: Any) -> str | None:
    if value is None:
        return None
    return _required_identifier(value, "identifier")


def _numeric_identifier(value: Any, field: str) -> int:
    text = _required_identifier(value, field)
    if not text.isdecimal():
        raise ConnectorError("INVALID_REQUEST", f"{field} must be a numeric OneBot id")
    return int(text)


def _environment_config() -> dict[str, str] | None:
    """Read one binding through the standard Plugin activation environment.

        中文:通过标准 Plugin activation 环境读取一个 binding。
    """

    return read_environment_settings()
