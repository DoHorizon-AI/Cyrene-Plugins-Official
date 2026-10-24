"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 connector.py                                                     │
│  Module: teams_connector.connector                                  │
│  Role: Implement the Teams message.connector.v1 transport.           │
│                                                                      │
│  模块职责：实现 Teams message.connector.v1 消息传输                     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from google.protobuf.message import DecodeError

from ._generated import message_connector_pb2 as message_contract
from .service_url import ServiceUrlError, validate_bot_service_url

os.environ["PYTHON_DOTENV_DISABLED"] = "true"

CAPABILITY_ID = "message.connector.v1"
SEND_MESSAGE_METHOD = "send_message"
SEND_MESSAGE_REQUEST_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest"
)
DELIVERY_RESULT_TYPE_URL = "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult"
TEAMS_VENDOR = "msteams"
MAX_MESSAGE_CHARS = 16_384


class TeamsConnectorError(RuntimeError):
    """Safe structured failure returned at the Plugin boundary.

    中文：在 Plugin 边界返回的安全结构化错误。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TeamsBindingConfig:
    """Resolved non-secret binding facts for one Teams bot.

    中文：一个 Teams bot binding 的非密钥配置事实。
    """

    binding_id: str
    bot_app_id: str
    tenant_id: str
    allowed_team_ids: frozenset[str]
    client_secret_env_ref: str
    service_url: str | None
    timeout_seconds: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> TeamsBindingConfig:
        """Validate one activation object without accepting plaintext secrets.

        中文：校验一个 activation object，拒绝明文 secret 字段。
        """

        if "client_secret" in value or "bot_secret" in value:
            raise TeamsConnectorError(
                "INVALID_REQUEST",
                "Teams secrets must be supplied by a host secret reference",
            )
        binding_id = _required_text(value.get("binding_id"), "binding_id", limit=200)
        bot_app_id = _uuid_text(value.get("bot_app_id"), "bot_app_id")
        tenant_id = _uuid_text(value.get("tenant_id"), "tenant_id")
        raw_team_ids = value.get("allowed_team_ids")
        if (
            not isinstance(raw_team_ids, Sequence)
            or isinstance(raw_team_ids, (str, bytes))
            or not raw_team_ids
        ):
            raise TeamsConnectorError(
                "INVALID_REQUEST", "allowed_team_ids must be a non-empty list"
            )
        team_ids = frozenset(
            _required_text(item, "allowed_team_ids[]", limit=512)
            for item in raw_team_ids
        )
        if len(team_ids) != len(raw_team_ids):
            raise TeamsConnectorError(
                "INVALID_REQUEST", "allowed_team_ids contains duplicates"
            )
        secret_ref = _required_text(
            value.get("client_secret_env_ref", "TEAMS_BOT_APP_SECRET"),
            "client_secret_env_ref",
            limit=128,
        )
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", secret_ref):
            raise TeamsConnectorError(
                "INVALID_REQUEST", "client_secret_env_ref is invalid"
            )
        service_url = value.get("service_url")
        if service_url is not None:
            try:
                service_url = validate_bot_service_url(
                    _required_text(service_url, "service_url", limit=512),
                    tenant_id=tenant_id,
                )
            except ServiceUrlError as exc:
                raise TeamsConnectorError(
                    "INVALID_REQUEST",
                    "service_url is outside the Microsoft Teams allowlist",
                ) from exc
        try:
            timeout = float(value.get("timeout_seconds", 10))
        except (TypeError, ValueError) as exc:
            raise TeamsConnectorError(
                "INVALID_REQUEST", "timeout_seconds must be numeric"
            ) from exc
        if not 0.1 <= timeout <= 30:
            raise TeamsConnectorError(
                "INVALID_REQUEST", "timeout_seconds is outside the supported range"
            )
        return cls(
            binding_id=binding_id,
            bot_app_id=bot_app_id,
            tenant_id=tenant_id,
            allowed_team_ids=team_ids,
            client_secret_env_ref=secret_ref,
            service_url=service_url,
            timeout_seconds=timeout,
        )


@dataclass(frozen=True)
class _DirectTypedPayload:
    """Structural response understood by DirectPluginRuntime.

    中文：DirectPluginRuntime 可识别的结构化响应。
    """

    value: bytes
    type_url: str


class TeamsConnector:
    """One binding-scoped `message.connector.v1` Teams transport.

    The Exchange host invokes this connector only after verifying a fresh
    `ConnectorOperationGrantV1`. This class still enforces the fixed app,
    tenant, and installed-Team binding before sending to Teams.

    中文：绑定范围内的 `message.connector.v1` Teams 传输。Exchange host 验证新鲜的
    `ConnectorOperationGrantV1` 后才调用；本类仍会校验固定 app、tenant 和 Team。
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        sdk_app_factory: Callable[[TeamsBindingConfig, str], Any] | None = None,
    ) -> None:
        self.config = TeamsBindingConfig.from_mapping(config)
        self._sdk_app_factory = sdk_app_factory
        self._sdk_app: Any | None = None
        self._runner: asyncio.Runner | None = None
        self._send_lock = threading.Lock()

    def send_message(
        self, request: message_contract.SendMessageRequest
    ) -> message_contract.DeliveryResult:
        """Send text to the exact authorized Teams channel thread.

        中文：向精确授权的 Teams 频道线程发送文本。
        """

        conversation = _conversation_for_send(request, self.config)
        text = _text_for_send(request)
        with self._send_lock:
            try:
                runner = self._require_runner()
                app = self._require_sdk_app(runner)
                sent = runner.run(
                    app.reply(
                        conversation["conversation_id"],
                        conversation["root_thread_id"],
                        text,
                        service_url=conversation["service_url"],
                    )
                )
            except TeamsConnectorError:
                raise
            except httpx.HTTPStatusError as exc:
                return _result_for_http_status(
                    exc.response.status_code, exc.response.headers
                )
            except (httpx.TimeoutException, httpx.NetworkError, TimeoutError):
                raise TeamsConnectorError(
                    "DELIVERY_UNCERTAIN",
                    "Teams transport did not confirm whether the message was accepted",
                ) from None
            except Exception:
                # SDK exception text may contain URLs or vendor response data.
                raise TeamsConnectorError(
                    "DELIVERY_UNCERTAIN",
                    "Teams transport did not confirm whether the message was accepted",
                ) from None

        result = message_contract.DeliveryResult(
            status=message_contract.DELIVERY_STATUS_ACCEPTED,
            vendor_message_id=_optional_text(getattr(sent, "id", None), limit=512)
            or "",
        )
        result.vendor_extension.vendor = TEAMS_VENDOR
        _append_fact(result.vendor_extension, "provider_status", "accepted")
        return result

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: Any | None = None,
        request_type_url: str | None = None,
        request_id: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, Any]:
        """Dispatch the canonical DirectPluginRuntime send method.

        中文：分发规范 DirectPluginRuntime send 方法。
        """

        del request_id, stream_results
        try:
            if capability != CAPABILITY_ID or action != SEND_MESSAGE_METHOD:
                raise TeamsConnectorError(
                    "INVALID_REQUEST", "unsupported Teams connector method"
                )
            if request_type_url not in {None, SEND_MESSAGE_REQUEST_TYPE_URL}:
                raise TeamsConnectorError(
                    "INVALID_REQUEST", "send_message request type is invalid"
                )
            if cancellation is not None and cancellation.is_cancelled():
                raise TeamsConnectorError(
                    "CANCELLED", "Teams message delivery was cancelled"
                )
            request = message_contract.SendMessageRequest()
            request.ParseFromString(payload)
            result = self.send_message(request)
            if cancellation is not None and cancellation.is_cancelled():
                raise TeamsConnectorError(
                    "DELIVERY_UNCERTAIN",
                    "Teams accepted-state could not be confirmed after cancellation",
                )
            return True, _DirectTypedPayload(
                value=result.SerializeToString(deterministic=True),
                type_url=DELIVERY_RESULT_TYPE_URL,
            )
        except DecodeError:
            return False, _direct_error(
                TeamsConnectorError(
                    "INVALID_REQUEST", "send_message payload is invalid protobuf"
                )
            )
        except TeamsConnectorError as exc:
            return False, _direct_error(exc)
        except Exception:
            return False, _direct_error(
                TeamsConnectorError(
                    "UPSTREAM_ERROR", "Teams connector invocation failed"
                )
            )

    def on_shutdown(self, grace_period_ms: int = 0) -> None:
        """Close the SDK HTTP clients on the connector's dedicated event loop.

        中文：在 connector 专用事件循环上关闭 SDK HTTP client。
        """

        del grace_period_ms
        with self._send_lock:
            if self._runner is None:
                return
            if self._sdk_app is not None:
                api_http = getattr(getattr(self._sdk_app, "api", None), "http", None)
                api_client = getattr(api_http, "http", None)
                app_http_client = getattr(self._sdk_app, "http_client", None)
                for client in (api_client, getattr(app_http_client, "http", None)):
                    close = getattr(client, "aclose", None)
                    if callable(close):
                        self._runner.run(close())
            self._runner.close()
            self._runner = None
            self._sdk_app = None

    def _require_runner(self) -> asyncio.Runner:
        """Create the connector's reusable event loop on first send.

        中文：首次发送时创建可复用的 connector 事件循环。
        """

        if self._runner is None:
            try:
                self._runner = asyncio.Runner()
            except RuntimeError as exc:
                raise TeamsConnectorError(
                    "UNAVAILABLE", "Teams runtime loop is unavailable"
                ) from exc
        return self._runner

    def _require_sdk_app(self, runner: asyncio.Runner) -> Any:
        """Resolve the host secret reference and build the official SDK client.

        中文：解析主机 secret 引用并构造微软官方 SDK client。
        """

        if self._sdk_app is not None:
            return self._sdk_app
        secret = os.environ.get(self.config.client_secret_env_ref)
        if not secret:
            raise TeamsConnectorError(
                "AUTH_NOT_CONFIGURED",
                "Teams bot credential host reference is unavailable",
            )
        if self._sdk_app_factory is not None:
            self._sdk_app = runner.run(
                _create_injected_app(self._sdk_app_factory, self.config, secret)
            )
        else:
            self._sdk_app = runner.run(_create_official_app(self.config, secret))
        return self._sdk_app


async def _create_official_app(config: TeamsBindingConfig, secret: str) -> Any:
    """Create a credentialed Microsoft Teams SDK application.

    中文：创建带有主机注入凭据的 Microsoft Teams SDK app。
    """

    from microsoft_teams.apps import App
    from microsoft_teams.common import ClientOptions

    from .logging_utils import install_sensitive_log_filters

    install_sensitive_log_filters()

    options: dict[str, Any] = {
        "client_id": config.bot_app_id,
        "client_secret": secret,
        "tenant_id": config.tenant_id,
        "client": ClientOptions(timeout=config.timeout_seconds),
        "dangerously_allow_unauthenticated_requests": False,
        "telemetry": {"agent365": False},
    }
    if config.service_url is not None:
        options["service_url"] = config.service_url
    app = App(**options)
    await app.initialize()
    return app


async def _create_injected_app(
    factory: Callable[[TeamsBindingConfig, str], Any],
    config: TeamsBindingConfig,
    secret: str,
) -> Any:
    """Build one test or host supplied SDK-compatible sender.

    中文：构造测试或宿主提供的 SDK 兼容 sender。
    """

    return factory(config, secret)


def _conversation_for_send(
    request: message_contract.SendMessageRequest,
    config: TeamsBindingConfig,
) -> dict[str, str]:
    """Validate provider, app, channel scope, and thread identity.

    中文：校验 provider、app、频道范围与线程标识。
    """

    if not request.HasField("conversation"):
        raise TeamsConnectorError("INVALID_REQUEST", "conversation is required")
    conversation = request.conversation
    if conversation.vendor != TEAMS_VENDOR:
        raise TeamsConnectorError(
            "INVALID_REQUEST", "conversation vendor is unsupported"
        )
    if conversation.account_id != config.bot_app_id:
        raise TeamsConnectorError(
            "PERMISSION_DENIED", "Teams account does not match this binding"
        )
    if conversation.kind != message_contract.CONVERSATION_KIND_CHANNEL:
        raise TeamsConnectorError(
            "INVALID_REQUEST", "only channel conversations are supported"
        )
    conversation_id = _required_text(
        conversation.conversation_id, "conversation_id", limit=512
    )
    if not request.HasField("reply"):
        raise TeamsConnectorError(
            "INVALID_REQUEST", "channel reply requires a root thread reference"
        )
    root_thread_id = _required_text(
        request.reply.message_id, "reply.message_id", limit=512
    )
    if re.fullmatch(r"[0-9]{1,512}", root_thread_id) is None or not any(
        character != "0" for character in root_thread_id
    ):
        raise TeamsConnectorError(
            "INVALID_REQUEST",
            "Teams channel root message ID must be a non-zero numeric value",
        )
    facts = _vendor_facts(request.vendor_extension, config.tenant_id)
    team_id = facts.get("team_id")
    channel_id = facts.get("channel_id")
    route_service_url = facts.get("service_url") or config.service_url
    if route_service_url is None:
        raise TeamsConnectorError(
            "INVALID_REQUEST", "verified Teams service_url is required"
        )
    try:
        service_url = validate_bot_service_url(
            route_service_url,
            tenant_id=config.tenant_id,
        )
    except ServiceUrlError as exc:
        raise TeamsConnectorError(
            "PERMISSION_DENIED", "Teams service_url is outside the provider allowlist"
        ) from exc
    if team_id not in config.allowed_team_ids:
        raise TeamsConnectorError(
            "PERMISSION_DENIED", "Teams team is outside this binding"
        )
    if channel_id is None:
        raise TeamsConnectorError(
            "INVALID_REQUEST", "Teams channel identity is missing"
        )
    return {
        "conversation_id": conversation_id,
        "root_thread_id": root_thread_id,
        "service_url": service_url,
        "team_id": team_id,
        "channel_id": channel_id,
    }


def _vendor_facts(
    extension: message_contract.VendorExtension, tenant_id: str
) -> dict[str, str]:
    """Read unique bounded Teams route facts from the canonical extension.

    中文：读取规范扩展中的唯一且有界的 Teams route facts。
    """

    if extension.vendor != TEAMS_VENDOR:
        raise TeamsConnectorError(
            "INVALID_REQUEST", "Teams route extension is required"
        )
    facts: dict[str, str] = {}
    for fact in extension.facts:
        if fact.name in facts:
            raise TeamsConnectorError(
                "INVALID_REQUEST", "Teams route facts must be unique"
            )
        facts[fact.name] = fact.value
    if facts.get("tenant_id") != tenant_id:
        raise TeamsConnectorError(
            "PERMISSION_DENIED", "Teams tenant is outside this binding"
        )
    if not facts.get("team_id") or not facts.get("channel_id"):
        raise TeamsConnectorError(
            "INVALID_REQUEST", "Teams team and channel facts are required"
        )
    return facts


def _text_for_send(request: message_contract.SendMessageRequest) -> str:
    """Extract non-empty text-only output for Teams channel replies.

    中文：提取非空且仅含文本的 Teams 频道回复。
    """

    texts: list[str] = []
    for part in request.content:
        kind = part.WhichOneof("kind")
        if kind != "text":
            raise TeamsConnectorError(
                "INVALID_REQUEST", "Teams outbound currently accepts text content only"
            )
        texts.append(part.text.text)
    text = "\n".join(item for item in texts if item.strip()).strip()
    if not text or len(text) > MAX_MESSAGE_CHARS:
        raise TeamsConnectorError(
            "INVALID_REQUEST", "Teams reply text is empty or too long"
        )
    return text


def _result_for_http_status(
    status_code: int, headers: Mapping[str, str]
) -> message_contract.DeliveryResult:
    """Convert known vendor HTTP rejections into canonical delivery status.

    中文：将已知供应商 HTTP 拒绝转换为规范 delivery status。
    """

    if status_code >= 500 or status_code == 408:
        raise TeamsConnectorError(
            "DELIVERY_UNCERTAIN",
            "Teams service did not confirm whether the message was accepted",
        )
    if status_code == 429:
        result = message_contract.DeliveryResult(
            status=message_contract.DELIVERY_STATUS_RATE_LIMITED,
            reason="rate_limited",
        )
        retry = headers.get("Retry-After")
        try:
            retry_seconds = max(0, min(int(retry or "0"), 86_400))
        except ValueError:
            retry_seconds = 0
        if retry_seconds:
            result.retry_after.seconds = retry_seconds
        result.vendor_extension.vendor = TEAMS_VENDOR
        _append_fact(result.vendor_extension, "provider_status", "rate_limited")
        return result

    reason = {
        401: "auth_expired",
        403: "permission_denied",
    }.get(status_code, "upstream_rejected")
    result = message_contract.DeliveryResult(
        status=message_contract.DELIVERY_STATUS_REJECTED,
        reason=reason,
    )
    result.vendor_extension.vendor = TEAMS_VENDOR
    _append_fact(result.vendor_extension, "provider_status", reason)
    return result


def _append_fact(
    extension: message_contract.VendorExtension, name: str, value: str
) -> None:
    """Append one bounded vendor fact to the result.

    中文：向结果扩展追加一个有界 vendor fact。
    """

    fact = extension.facts.add()
    fact.name = name
    fact.value = value


def _direct_error(error: TeamsConnectorError) -> str:
    """Return a DirectPluginRuntime error without vendor response details.

    中文：返回 DirectPluginRuntime 错误，不包含供应商响应细节。
    """

    return f"{error.code}: {error.message}"


def _required_text(value: Any, field: str, *, limit: int) -> str:
    """Return required bounded text without reflecting the supplied value.

    中文：返回必填且有界的文本，不回显输入值。
    """

    text = _optional_text(value, limit=limit)
    if text is None:
        raise TeamsConnectorError("INVALID_REQUEST", f"{field} is required or invalid")
    return text


def _optional_text(value: Any, *, limit: int) -> str | None:
    """Return one trimmed optional string under its field limit.

    中文：返回不超过字段长度限制的可选字符串。
    """

    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    return value.strip()


def _uuid_text(value: Any, field: str) -> str:
    """Validate one UUID field and return its canonical lowercase form.

    中文：校验一个 UUID 字段并返回小写规范形式。
    """

    text = _required_text(value, field, limit=64)
    try:
        return str(uuid.UUID(text))
    except ValueError as exc:
        raise TeamsConnectorError("INVALID_REQUEST", f"{field} must be a UUID") from exc
