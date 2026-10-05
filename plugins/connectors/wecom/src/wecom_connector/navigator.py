"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 navigator.py                                                    │
│  Package: wecom_connector                                           │
│  Role: Admit trusted WeCom messages into local Navigator work.      │
│                                                                     │
│  模块职责：将可信的 WeCom 入站消息提交给同宿主的 Navigator。             │
│  · 只接受 loopback Navigator URL 和 secret ENV 引用。                 │
│  · 仅在账户、会话、发送者和 owner/session map 完全匹配时创建任务。     │
│  · 使用原始消息 ID 作为远端 event dedup identity。                    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import ConnectorError

_ENVIRONMENT_VARIABLE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_MAX_EVENT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class TrustedConversation:
    """Configured sender, account, conversation, and Navigator owner scope.

    中文:显式配置的发送者、账户、会话与 Navigator owner 范围。
    """

    account_id: str
    conversation_id: str
    kind: str
    owner_id: str
    session_id: str
    trusted_senders: frozenset[str]

    def trusts(self, payload: Mapping[str, Any]) -> bool:
        """Return whether all inbound identity fields match this exact rule."""
        conversation = payload.get("conversation")
        if not isinstance(conversation, Mapping):
            return False
        return (
            conversation.get("account_id") == self.account_id
            and conversation.get("conversation_id") == self.conversation_id
            and conversation.get("kind") == self.kind
            and payload.get("sender_id") in self.trusted_senders
        )


@dataclass(frozen=True, slots=True)
class NavigatorIntegrationConfig:
    """Resolved same-host Navigator endpoint and trusted conversation rules.

    中文:已解析的同宿主 Navigator endpoint 与可信会话规则。
    """

    base_url: str
    workspace_id: str
    connector_id: str
    secret_env_ref: str
    bearer_token: str
    timeout_seconds: float
    retry_count: int
    trusted_conversations: tuple[TrustedConversation, ...]

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, binding_id: str
    ) -> NavigatorIntegrationConfig:
        """Validate Navigator settings and resolve its bearer token by ENV name.

        中文:校验 Navigator 设置,并通过 ENV 名称解析 bearer token。
        """
        base_url = _required_text(value.get("base_url"), "navigator.base_url")
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in _LOOPBACK_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ConnectorError(
                "INVALID_REQUEST",
                "navigator.base_url must be loopback-only http(s) without URL "
                "credentials or query",
            )
        if parsed.path not in {"", "/"}:
            raise ConnectorError(
                "INVALID_REQUEST", "navigator.base_url must not contain a path prefix"
            )
        workspace_id = _required_text(
            value.get("workspace_id"), "navigator.workspace_id"
        )
        connector_id = _required_text(
            value.get("connector_id", binding_id), "navigator.connector_id"
        )
        if connector_id != binding_id:
            raise ConnectorError(
                "INVALID_REQUEST",
                "navigator.connector_id must match the activation binding_id",
            )
        secret_env_ref = _required_text(
            value.get("secret_env_ref"), "navigator.secret_env_ref"
        )
        if not _ENVIRONMENT_VARIABLE.fullmatch(secret_env_ref):
            raise ConnectorError(
                "INVALID_REQUEST",
                "navigator.secret_env_ref must name an uppercase environment variable",
            )
        bearer_token = os.environ.get(secret_env_ref, "")
        if not bearer_token:
            raise ConnectorError(
                "UNAVAILABLE",
                "Navigator bearer token environment reference "
                f"{secret_env_ref!r} is unset",
            )
        timeout_seconds = value.get("timeout_seconds", 5.0)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 60
        ):
            raise ConnectorError(
                "INVALID_REQUEST",
                "navigator.timeout_seconds must be greater than 0 and at most 60",
            )
        retry_count = value.get("retry_count", 2)
        if (
            isinstance(retry_count, bool)
            or not isinstance(retry_count, int)
            or not 0 <= retry_count <= 5
        ):
            raise ConnectorError(
                "INVALID_REQUEST",
                "navigator.retry_count must be an integer from 0 through 5",
            )
        raw_trusted = value.get("trusted_conversations", ())
        if not isinstance(raw_trusted, Sequence) or isinstance(
            raw_trusted, (str, bytes)
        ):
            raise ConnectorError(
                "INVALID_REQUEST", "navigator.trusted_conversations must be a list"
            )
        trusted = tuple(
            _parse_trusted_conversation(item, index)
            for index, item in enumerate(raw_trusted)
        )
        keys = [(item.account_id, item.conversation_id, item.kind) for item in trusted]
        if len(keys) != len(set(keys)):
            raise ConnectorError(
                "INVALID_REQUEST", "navigator trusted conversation rules must be unique"
            )
        if not trusted:
            raise ConnectorError(
                "INVALID_REQUEST",
                "navigator.trusted_conversations must configure at least one "
                "owner/session map",
            )
        return cls(
            base_url=base_url.rstrip("/"),
            workspace_id=workspace_id,
            connector_id=connector_id,
            secret_env_ref=secret_env_ref,
            bearer_token=bearer_token,
            timeout_seconds=float(timeout_seconds),
            retry_count=retry_count,
            trusted_conversations=trusted,
        )

    def trusted_rule(self, payload: Mapping[str, Any]) -> TrustedConversation | None:
        """Return one exact configured rule for an inbound canonical message."""
        for rule in self.trusted_conversations:
            if rule.trusts(payload):
                return rule
        return None


class NavigatorTaskClient:
    """Submit one normalized inbound event and task through local Navigator."""

    def __init__(self, config: NavigatorIntegrationConfig) -> None:
        self._config = config

    def submit_message(
        self, payload: Mapping[str, Any], trust: TrustedConversation
    ) -> Mapping[str, Any]:
        """Atomically record the message and create/link its trusted task.

        The Navigator connector event endpoint owns durable message deduplication
        and task admission in one transaction. 中文:Navigator event 路由在一个事务中
        完成消息去重、事件记录和任务 admission。
        """
        message_id = _required_text(payload.get("message_id"), "message_id")
        conversation = payload.get("conversation")
        if not isinstance(conversation, Mapping):
            raise ConnectorError("INVALID_REQUEST", "conversation must be an object")
        account_id = _required_text(conversation.get("account_id"), "account_id")
        conversation_id = _required_text(
            conversation.get("conversation_id"), "conversation_id"
        )
        sender_id = _required_text(payload.get("sender_id"), "sender_id")
        if not trust.trusts(payload):
            raise ConnectorError(
                "INVALID_REQUEST",
                "inbound sender does not match configured trust scope",
            )
        task_prompt = _task_prompt(payload)
        event_body = {
            "messageId": message_id,
            "type": "message.inbound",
            "accountId": account_id,
            "conversationId": conversation_id,
            "senderId": sender_id,
            "payload": dict(payload),
            "task": {
                "id": _task_id(self._config.connector_id, account_id, message_id),
                "sessionId": trust.session_id,
                "prompt": task_prompt,
                "title": f"WeCom message from {payload['sender_display_name']}",
                "metadata": {
                    "source": "message.connector.v1",
                    "connectorId": self._config.connector_id,
                    "messageId": message_id,
                    "accountId": account_id,
                    "conversationId": conversation_id,
                    "senderId": sender_id,
                    "ownerId": trust.owner_id,
                    "sessionId": trust.session_id,
                    "notification": _notification_descriptor(
                        self._config.connector_id,
                        account_id,
                        conversation_id,
                        str(conversation.get("kind", "")),
                        sender_id,
                    ),
                },
            },
        }
        encoded = json.dumps(
            event_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_EVENT_BYTES:
            raise ConnectorError(
                "INVALID_REQUEST", "Navigator inbound event is too large"
            )
        route = (
            "/api/v1/workspaces/"
            f"{urllib.parse.quote(self._config.workspace_id, safe='')}"
            "/work/connectors/"
            f"{urllib.parse.quote(self._config.connector_id, safe='')}/events"
        )
        url = f"{self._config.base_url}{route}"
        last_error: ConnectorError | None = None
        for attempt in range(self._config.retry_count + 1):
            request = urllib.request.Request(
                url,
                data=encoded,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._config.bearer_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self._config.timeout_seconds
                ) as response:
                    status = getattr(response, "status", response.getcode())
                    raw = response.read(_MAX_EVENT_BYTES + 1)
                if status not in {200, 201, 202}:
                    raise ConnectorError(
                        "UNAVAILABLE", f"Navigator intake returned HTTP {status}"
                    )
                if len(raw) > _MAX_EVENT_BYTES:
                    raise ConnectorError(
                        "UNAVAILABLE", "Navigator intake returned an oversized receipt"
                    )
                try:
                    decoded = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ConnectorError(
                        "UNAVAILABLE", "Navigator intake returned invalid JSON"
                    ) from exc
                if not isinstance(decoded, Mapping) or not decoded.get("eventId"):
                    raise ConnectorError(
                        "UNAVAILABLE", "Navigator intake receipt has no eventId"
                    )
                return decoded
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    raise ConnectorError(
                        "INVALID_REQUEST",
                        "Navigator rejected a reused WeCom message ID with "
                        "different content",
                    ) from exc
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                last_error = ConnectorError(
                    "UNAVAILABLE" if retryable else "INVALID_REQUEST",
                    f"Navigator intake returned HTTP {exc.code}",
                )
                if not retryable or attempt >= self._config.retry_count:
                    raise last_error from exc
            except (TimeoutError, OSError, urllib.error.URLError) as exc:
                last_error = ConnectorError(
                    "UNAVAILABLE", "Navigator local intake request failed"
                )
                if attempt >= self._config.retry_count:
                    raise last_error from exc
            if last_error is not None:
                time.sleep(min(0.1 * (2**attempt), 1.0))
        raise last_error or ConnectorError("UNAVAILABLE", "Navigator intake failed")


def _parse_trusted_conversation(value: Any, index: int) -> TrustedConversation:
    """Validate one explicit owner and sender map.

    中文:校验一条显式 owner 与 sender 映射。
    """
    field = f"navigator.trusted_conversations[{index}]"
    if not isinstance(value, Mapping):
        raise ConnectorError("INVALID_REQUEST", f"{field} must be an object")
    account_id = _required_text(value.get("account_id"), f"{field}.account_id")
    conversation_id = _required_text(
        value.get("conversation_id"), f"{field}.conversation_id"
    )
    kind = _required_text(value.get("kind"), f"{field}.kind")
    if kind not in {"private", "group"}:
        raise ConnectorError(
            "INVALID_REQUEST", f"{field}.kind must be private or group"
        )
    owner_id = _required_text(value.get("owner_id"), f"{field}.owner_id")
    session_id = _required_text(value.get("session_id"), f"{field}.session_id")
    senders = value.get("trusted_senders")
    if not isinstance(senders, Sequence) or isinstance(senders, (str, bytes)):
        raise ConnectorError(
            "INVALID_REQUEST", f"{field}.trusted_senders must be a non-empty list"
        )
    normalized_senders = frozenset(
        _required_text(sender, f"{field}.trusted_senders[{sender_index}]")
        for sender_index, sender in enumerate(senders)
    )
    if not normalized_senders:
        raise ConnectorError(
            "INVALID_REQUEST", f"{field}.trusted_senders must be a non-empty list"
        )
    return TrustedConversation(
        account_id=account_id,
        conversation_id=conversation_id,
        kind=kind,
        owner_id=owner_id,
        session_id=session_id,
        trusted_senders=normalized_senders,
    )


def _task_id(connector_id: str, account_id: str, message_id: str) -> str:
    """Build a stable task identifier from binding-scoped vendor identities."""
    source = f"{connector_id}\0{account_id}\0{message_id}".encode()
    digest = hashlib.sha256(source).hexdigest()[:32]
    return f"wecom-{digest}"


def notification_recipient_key(
    account_id: str, conversation_id: str, chat_type: str, sender_id: str
) -> str:
    """Return a stable exact-route key for one trusted WeCom notification target.

    中文:为一个可信 WeCom 通知目标生成稳定且精确的 route key。
    """
    route = json.dumps(
        [account_id, conversation_id, chat_type, sender_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"wecom-message:{route}"


def _notification_descriptor(
    connector_id: str,
    account_id: str,
    conversation_id: str,
    kind: str,
    sender_id: str,
) -> dict[str, Any]:
    """Build the exact trusted route that terminal tasks may notify.

    中文:构造终态任务可使用的精确可信通知 route。
    """
    chat_type = "group" if kind == "group" else "single"
    return {
        "type": "wecom.message",
        "connectorId": connector_id,
        "recipient": notification_recipient_key(
            account_id, conversation_id, chat_type, sender_id
        ),
        "payload": {
            "accountId": account_id,
            "conversationId": conversation_id,
            "chatType": chat_type,
            "senderId": sender_id,
        },
    }


def _task_prompt(payload: Mapping[str, Any]) -> str:
    """Render normalized text and attachment labels into a bounded task prompt."""
    lines: list[str] = []
    for part in payload.get("content", ()):
        if not isinstance(part, Mapping) or len(part) != 1:
            continue
        kind, value = next(iter(part.items()))
        if kind == "text" and isinstance(value, Mapping):
            lines.append(str(value.get("text", "")))
        elif kind in {"image", "file"} and isinstance(value, Mapping):
            label = value.get("file_name") or kind
            lines.append(f"[{kind} attachment: {label}]")
    rendered = "\n".join(line for line in lines if line).strip()
    if not rendered:
        rendered = "Incoming WeCom message"
    return rendered[:16_000]


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConnectorError("INVALID_REQUEST", f"{field} must be non-empty text")
    return value
