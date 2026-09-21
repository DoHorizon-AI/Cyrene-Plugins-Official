"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 connector.py                                                    │
│  Package: wecom_connector                                           │
│  Role: WeCom application-message implementation of message.connector.v1.│
│                                                                     │
│  模块职责：企业微信应用消息的出站连接器实现（send_message）。            │
│  · 只做厂商 wire translation：token 获取/缓存、message/send 调用与      │
│    错误码到 DeliveryStatus 的确定性映射。                              │
│  · 入站回调（URL 验证、AES 解密、消息接收）不在 v1 范围，见 README。     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

from google.protobuf.message import DecodeError

from ._generated import message_connector_pb2 as message_contract

CAPABILITY_ID = "message.connector.v1"
SEND_MESSAGE_METHOD = "send_message"
VENDOR = "wecom.app"

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

_KIND_NAMES = {
    message_contract.CONVERSATION_KIND_UNSPECIFIED: "unspecified",
    message_contract.CONVERSATION_KIND_PRIVATE: "private",
    message_contract.CONVERSATION_KIND_GROUP: "group",
    message_contract.CONVERSATION_KIND_CHANNEL: "channel",
}
_TARGET_KEYS = {"private": "touser", "group": "toparty"}


class ConnectorError(RuntimeError):
    """Typed connector failure carried to DirectPluginRuntime."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CancellationToken(Protocol):
    """Minimal cooperative cancellation surface."""

    def is_cancelled(self) -> bool: ...


class WeComTransport(Protocol):
    """Injectable transport for token, upload, and message REST calls."""

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
    """One configured WeCom application binding."""

    binding_id: str
    corp_id: str
    corp_secret: str
    agent_id: int
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 10.0
    token_refresh_skew_seconds: float = 60.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> WeComInstanceConfig:
        """Validate one configuration mapping."""

        binding_id = _required_text(value.get("binding_id"), "binding_id")
        corp_id = _required_text(value.get("corp_id"), "corp_id")
        corp_secret = _required_text(value.get("corp_secret"), "corp_secret")
        agent_id = value.get("agent_id")
        if isinstance(agent_id, bool) or not isinstance(agent_id, int) or agent_id <= 0:
            raise ConnectorError(
                "INVALID_REQUEST", "agent_id must be a positive integer"
            )
        base_url = value.get("base_url", DEFAULT_BASE_URL)
        if not isinstance(base_url, str):
            raise ConnectorError("INVALID_REQUEST", "base_url must be a string")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme == "https":
            pass
        elif parsed.scheme == "http" and parsed.hostname in (
            "127.0.0.1",
            "::1",
            "localhost",
        ):
            # Plain HTTP is tolerated only for loopback test endpoints.
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
        return cls(
            binding_id=binding_id,
            corp_id=corp_id,
            corp_secret=corp_secret,
            agent_id=agent_id,
            base_url=base_url.rstrip("/"),
            timeout_seconds=float(timeout),
            token_refresh_skew_seconds=float(skew),
        )

    @classmethod
    def from_settings(cls, settings: Mapping[str, str]) -> WeComInstanceConfig:
        """Build configuration from the standard plugin activation environment."""

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
    """Small urllib transport; the only network surface this connector owns."""

    def get_token(
        self,
        config: WeComInstanceConfig,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Mapping[str, Any]:
        query = urllib.parse.urlencode(
            {"corpid": config.corp_id, "corpsecret": config.corp_secret}
        )
        return self._json_request(
            f"{config.base_url}/cgi-bin/gettoken?{query}", None, config, cancellation
        )

    def send_agent_message(
        self,
        token: str,
        payload: Mapping[str, Any],
        config: WeComInstanceConfig,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Mapping[str, Any]:
        query = urllib.parse.urlencode({"access_token": token})
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._json_request(
            f"{config.base_url}/cgi-bin/message/send?{query}",
            body,
            config,
            cancellation,
            content_type="application/json; charset=utf-8",
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
        """Fetch one canonical remote attachment and upload it as temporary media."""

        content, resolved_name, resolved_mime = self._download_media(
            remote_uri,
            media_type,
            file_name,
            mime_type,
            config,
            cancellation,
        )
        boundary = f"cyrene-{secrets.token_hex(16)}"
        disposition_name = _multipart_filename(resolved_name)
        prefix = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="media"; '
            f'filename="{disposition_name}"; filelength={len(content)}\r\n'
            f"Content-Type: {resolved_mime}\r\n\r\n"
        ).encode()
        body = prefix + content + f"\r\n--{boundary}--\r\n".encode()
        query = urllib.parse.urlencode({"access_token": token, "type": media_type})
        return self._json_request(
            f"{config.base_url}/cgi-bin/media/upload?{query}",
            body,
            config,
            cancellation,
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def _download_media(
        self,
        remote_uri: str,
        media_type: str,
        file_name: str,
        mime_type: str,
        config: WeComInstanceConfig,
        cancellation: CancellationToken | None,
    ) -> tuple[bytes, str, str]:
        _raise_if_cancelled(cancellation)
        request = urllib.request.Request(remote_uri, headers={"Accept": "*/*"})
        limit = MAX_IMAGE_BYTES if media_type == "image" else MAX_FILE_BYTES
        try:
            with urllib.request.urlopen(
                request, timeout=config.timeout_seconds
            ) as response:
                final_url = urllib.parse.urlsplit(response.geturl())
                if final_url.scheme not in {"http", "https"}:
                    raise ConnectorError(
                        "INVALID_REQUEST", "media redirect must remain http(s)"
                    )
                declared_length = response.headers.get("Content-Length")
                if declared_length is not None:
                    try:
                        declared_size = int(declared_length)
                    except ValueError as exc:
                        raise ConnectorError(
                            "UNAVAILABLE",
                            "media response has an invalid Content-Length",
                        ) from exc
                    if declared_size > limit:
                        raise ConnectorError(
                            "INVALID_REQUEST",
                            f"{media_type} exceeds the {limit}-byte upload limit",
                        )
                content = response.read(limit + 1)
                response_mime = response.headers.get_content_type()
        except ConnectorError:
            raise
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise ConnectorError(
                "UNAVAILABLE",
                f"media download failed: {type(exc).__name__}: {exc}",
            ) from exc
        _raise_if_cancelled(cancellation)
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
    """WeCom application-message connector for one configured binding."""

    plugin_id = "cyrene.connectors.wecom"
    version = "0.1.0"
    capabilities = (CAPABILITY_ID,)

    def __init__(
        self,
        config: WeComInstanceConfig | None = None,
        transport: WeComTransport | None = None,
    ) -> None:
        self._config: WeComInstanceConfig | None = None
        self._transport: WeComTransport | None = None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()
        if config is not None:
            self.configure(config, transport=transport)
        else:
            settings = _environment_settings()
            if settings is not None:
                self.configure(WeComInstanceConfig.from_settings(settings))

    # ── configuration ──────────────────────────────────────────────────

    @property
    def configured_binding_id(self) -> str | None:
        """Return the configured binding identity."""

        return self._config.binding_id if self._config is not None else None

    def configure(
        self,
        config: WeComInstanceConfig,
        *,
        transport: WeComTransport | None = None,
    ) -> None:
        """Bind one configuration; an explicit transport is used when given."""

        self._config = config
        self._transport = transport
        self._token = None
        self._token_expires_at = 0.0

    # ── canonical method ───────────────────────────────────────────────

    def send_message(
        self,
        request: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
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
        kind = conversation["kind"]
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

    # ── DirectPluginRuntime adapter ────────────────────────────────────

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: CancellationToken | None = None,
        request_type_url: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, Any]:
        """Adapt the direct Plugin runtime call to the typed connector contract."""

        try:
            if capability != CAPABILITY_ID:
                raise ConnectorError(
                    "INVALID_REQUEST", f"unsupported capability {capability!r}"
                )
            if action != SEND_MESSAGE_METHOD:
                raise ConnectorError(
                    "METHOD_NOT_FOUND",
                    f"unsupported method {action!r}; wecom.app v1 implements "
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
                _send_request_to_mapping(request), cancellation=cancellation
            )
            delivery = message_contract.DeliveryResult()
            _apply_delivery_result(delivery, result)
            return True, _TypedPayload(
                delivery.SerializeToString(), DELIVERY_RESULT_TYPE_URL
            )
        except ConnectorError as exc:
            return False, f"{exc.code}: {exc.message}"

    # ── internals ──────────────────────────────────────────────────────

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

        remote_uri = _validated_remote_uri(
            remote_uri, f"{media_type}.reference.remote_uri"
        )
        file_name = part.get("file_name", "") if media_type == "file" else ""
        if not isinstance(file_name, str):
            raise ConnectorError("INVALID_REQUEST", "file.file_name must be a string")
        mime_type = part.get("mime_type", "")
        if not isinstance(mime_type, str):
            raise ConnectorError(
                "INVALID_REQUEST", f"{media_type}.mime_type must be a string"
            )

        transport = self._transport_for(config)
        token = self._access_token(transport, config, cancellation)
        response = transport.upload_media(
            token,
            media_type,
            remote_uri,
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
                remote_uri,
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


def _environment_settings() -> dict[str, str] | None:
    """Read one binding through the standard plugin activation environment."""

    from cyrene_plugin_runtime.configuration import read_environment_settings

    return read_environment_settings()


def _raise_if_cancelled(cancellation: CancellationToken | None) -> None:
    if cancellation is not None and cancellation.is_cancelled():
        raise ConnectorError("CANCELLED", "operation cancelled")


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
    if account_id.startswith("agent:") and account_id != f"agent:{config.agent_id}":
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
            f"unsupported vendor fact(s) {unsupported}; wecom.app v1 accepts 'msgtype'",
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


# ── proto mapping ──────────────────────────────────────────────────────


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
