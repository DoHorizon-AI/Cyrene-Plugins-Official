"""
WeCom-to-Exchange fixed integration sink and its durable local transport outbox.

This module owns only signed admission transport state. Exchange owns the
business inbox, command parsing, actor binding, and delegated execution.
模块职责：向固定 Exchange 入口发送规范消息，并持久化本地传输重试状态。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from ._generated import message_connector_pb2 as message_contract

_ENV_REF = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_MAX_PROTO_BYTES = 1024 * 1024
_MAX_BODY_BYTES = 2 * 1024 * 1024
_REPLY_CONTEXT_TTL_SECONDS = 300
_ACKED = "ACKED"
_PENDING = "PENDING"
_RETAINED = "RETAINED"


@dataclass(frozen=True, slots=True)
class WeComIntegrationConfig:
    """Immutable, host-resolved identity and transport settings for one sink.

    The HMAC key is injected from an environment reference. It is never read
    from the activation JSON or persisted in the SQLite outbox.
    中文：固定绑定与主机注入密钥；密钥不会写入激活配置或本地队列。
    """

    binding_id: str
    exchange_base_url: str
    organization_id: str
    workspace_id: str
    source_authority: str
    bot_namespace: str
    account_id: str
    group_conversation_id: str
    source_hmac_key: bytes = field(repr=False)
    outbox_path: Path
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        """Reject mutable, weak, or caller-selected destinations."""

        for name in (
            "binding_id",
            "organization_id",
            "workspace_id",
            "source_authority",
            "bot_namespace",
            "account_id",
            "group_conversation_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        limits = {
            "organization_id": 200,
            "workspace_id": 200,
            "source_authority": 300,
            "bot_namespace": 300,
            "account_id": 300,
            "group_conversation_id": 292,
        }
        if any(len(getattr(self, name)) > limit for name, limit in limits.items()):
            raise ValueError(
                "configured integration source fact exceeds its size limit"
            )
        try:
            parsed_binding_id = UUID(self.binding_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("binding_id must be an Exchange SyncBinding UUID") from exc
        if str(parsed_binding_id) != self.binding_id:
            raise ValueError("binding_id must use canonical lowercase UUID form")
        if not self.account_id.startswith("bot:"):
            raise ValueError("account_id must identify the configured WeCom bot")
        if (
            not isinstance(self.source_hmac_key, bytes)
            or len(self.source_hmac_key) < 32
        ):
            raise ValueError("source HMAC key must contain at least 32 bytes")
        if not isinstance(self.outbox_path, Path) or not self.outbox_path.is_absolute():
            raise ValueError("outbox_path must be an absolute local filesystem path")
        if not 0 < self.timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be greater than 0 and at most 60")
        validate_exchange_base_url(self.exchange_base_url)

    @property
    def endpoint(self) -> str:
        """Return the fixed per-binding Exchange intake route."""

        return (
            f"{self.exchange_base_url.rstrip('/')}/api/v1/integrations/hooks/wecom/"
            f"{self.binding_id}"
        )


class HttpTransport(Protocol):
    """Narrow POST boundary used for deterministic transport tests."""

    def post(
        self, url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep signed message bodies on the one configured Exchange endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class UrllibHttpTransport:
    """POST JSON to one fixed HTTPS or loopback Exchange endpoint."""

    def post(
        self, url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=timeout) as response:
                return int(response.status), response.read(64 * 1024)
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read(64 * 1024)


def validate_exchange_base_url(value: str) -> str:
    """Validate and normalize a fixed endpoint origin without URL parameters."""

    if not isinstance(value, str):
        raise ValueError("exchange_base_url must be text")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        or parsed.scheme == "http"
        and _is_loopback(parsed.hostname)
    ):
        pass
    else:
        raise ValueError("exchange_base_url must use HTTPS or loopback HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "exchange_base_url cannot contain credentials, query, or fragment"
        )
    if parsed.path not in ("", "/"):
        raise ValueError("exchange_base_url must be an origin without a path")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("exchange_base_url port is invalid")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def load_hmac_key(settings: Mapping[str, str], env_ref: str) -> bytes:
    """Resolve a private source key by an allowlisted environment variable name."""

    if not isinstance(env_ref, str) or _ENV_REF.fullmatch(env_ref) is None:
        raise ValueError("source_hmac_env_ref must be an uppercase environment name")
    value = settings.get(env_ref)
    if not isinstance(value, str) or len(value.encode("utf-8")) < 32:
        raise ValueError("configured source HMAC key must contain at least 32 bytes")
    return value.encode("utf-8")


class WeComIntegrationForwarder:
    """Persist canonical inbound events and retry Exchange admission safely.

    Retries repeat only the idempotent Exchange inbox admission. They never
    invoke a vendor write. Unacknowledged and explicitly rejected rows remain
    available in the local queue for diagnosis.
    中文：重试只重复 Exchange 幂等收件箱准入，不执行企业微信业务写入。
    """

    def __init__(
        self,
        config: WeComIntegrationConfig,
        *,
        transport: HttpTransport | None = None,
        clock: Callable[[], float] = time.time,
        minimum_retry_seconds: float = 0.5,
        maximum_retry_seconds: float = 60.0,
    ) -> None:
        if minimum_retry_seconds <= 0 or maximum_retry_seconds < minimum_retry_seconds:
            raise ValueError("retry interval bounds are invalid")
        self.config = config
        self._transport = transport or UrllibHttpTransport()
        self._clock = clock
        self._minimum_retry_seconds = minimum_retry_seconds
        self._maximum_retry_seconds = maximum_retry_seconds
        self._lock = threading.RLock()
        self._drain_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        config.outbox_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(config.outbox_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        self._db = sqlite3.connect(
            config.outbox_path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS wecom_exchange_outbox (
                    binding_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    body BLOB NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('PENDING','ACKED','RETAINED')
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    last_status_code INTEGER,
                    PRIMARY KEY(binding_id, source_event_id)
                )"""
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS ix_wecom_exchange_outbox_due "
                "ON wecom_exchange_outbox(status, next_attempt_at, source_event_id)"
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS wecom_reply_contexts (
                    binding_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    conversation_kind TEXT NOT NULL,
                    callback_req_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY(binding_id, message_id)
                )"""
            )
            self._db.execute(
                "DELETE FROM wecom_reply_contexts WHERE expires_at<=?",
                (float(self._clock()),),
            )

    def enqueue(
        self,
        *,
        source_event_id: str,
        sender_id: str,
        conversation_id: str,
        conversation_kind: str,
        from_self: bool,
        message_proto: bytes,
        source_transport_request_id: str = "",
    ) -> bool:
        """Durably add one actual normalized message before network admission.

        Returns true for a new event and false for a byte-identical duplicate.
        Raises on conflicting reuse of the source event ID.
        中文：先写入 SQLite 再尝试网络发送；冲突的事件 ID 会被拒绝。
        """

        _require_text(source_event_id, "source_event_id", maximum=300)
        _require_text(sender_id, "sender_id", maximum=200)
        _require_text(conversation_id, "conversation_id", maximum=292)
        if conversation_kind not in {"private", "group"}:
            raise ValueError("conversation_kind must be private or group")
        if type(from_self) is not bool:
            raise ValueError("from_self must be a boolean")
        if (
            not isinstance(message_proto, bytes)
            or not 1 <= len(message_proto) <= _MAX_PROTO_BYTES
        ):
            raise ValueError("message_proto must contain 1 to 1048576 bytes")
        _require_text(
            source_transport_request_id,
            "source_transport_request_id",
            maximum=300,
        )
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in source_transport_request_id
        ):
            raise ValueError("source_transport_request_id contains a control character")
        normalized = message_contract.InboundMessagePayload()
        try:
            normalized.ParseFromString(message_proto)
        except Exception as exc:
            raise ValueError(
                "message_proto is not a valid message.connector.v1 payload"
            ) from exc
        if (
            normalized.message_id != source_event_id
            or normalized.sender_id != sender_id
            or normalized.conversation.vendor != "wecom.bot"
            or normalized.conversation.account_id != self.config.account_id
            or normalized.conversation.conversation_id != conversation_id
            or _conversation_kind_name(normalized.conversation.kind)
            != conversation_kind
            or normalized.vendor_extension.vendor != "wecom.bot"
            or [
                fact.value
                for fact in normalized.vendor_extension.facts
                if fact.name == "transport_request_id"
            ]
            != [source_transport_request_id]
        ):
            raise ValueError("source facts do not match the canonical inbound protobuf")

        envelope = {
            "schemaVersion": 1,
            "bindingId": self.config.binding_id,
            "organizationId": self.config.organization_id,
            "workspaceId": self.config.workspace_id,
            "sourceAuthority": self.config.source_authority,
            "botNamespace": self.config.bot_namespace,
            "accountId": self.config.account_id,
            "conversationId": conversation_id,
            "conversationKind": conversation_kind,
            "sourceEventId": source_event_id,
            "senderId": sender_id,
            "fromSelf": from_self,
            "sourceTransportRequestId": source_transport_request_id,
            "messageProtoB64": base64.b64encode(message_proto).decode("ascii"),
        }
        body = json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(body) > _MAX_BODY_BYTES:
            raise ValueError("integration envelope exceeds the size limit")
        body_hash = hashlib.sha256(body).hexdigest()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT body_sha256 FROM wecom_exchange_outbox "
                    "WHERE binding_id=? AND source_event_id=?",
                    (self.config.binding_id, source_event_id),
                ).fetchone()
                if existing is not None:
                    if str(existing["body_sha256"]) != body_hash:
                        raise ValueError(
                            "source event ID was reused with different content"
                        )
                    self._db.execute("COMMIT")
                    return False
                self._db.execute(
                    "INSERT INTO wecom_exchange_outbox "
                    "(binding_id,source_event_id,body,body_sha256,status,"
                    "attempts,next_attempt_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        self.config.binding_id,
                        source_event_id,
                        body,
                        body_hash,
                        _PENDING,
                        0,
                        float(self._clock()),
                    ),
                )
                self._db.execute(
                    "DELETE FROM wecom_reply_contexts WHERE expires_at<=?",
                    (float(self._clock()),),
                )
                self._db.execute(
                    "INSERT OR REPLACE INTO wecom_reply_contexts "
                    "(binding_id,message_id,account_id,conversation_id,"
                    "conversation_kind,callback_req_id,expires_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        self.config.binding_id,
                        source_event_id,
                        normalized.conversation.account_id,
                        normalized.conversation.conversation_id,
                        conversation_kind,
                        source_transport_request_id,
                        float(self._clock()) + _REPLY_CONTEXT_TTL_SECONDS,
                    ),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        self._wake.set()
        return True

    def start(self, *, poll_interval_seconds: float = 0.5) -> None:
        """Start one background sender while preserving pending local rows."""

        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(poll_interval_seconds,),
                name="wecom-exchange-outbox",
                daemon=True,
            )
            self._thread.start()
        self._wake.set()

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        """Stop the background sender without deleting unacknowledged rows."""

        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.1, timeout_seconds))
        with self._lock:
            if thread is None or not thread.is_alive():
                self._thread = None

    def close(self) -> None:
        """Stop sending and close the local SQLite transport state database."""

        self.stop()
        with self._lock:
            self._db.close()

    def drain_once(self) -> bool:
        """Attempt the next due Exchange admission; return whether one was found."""

        if not self._drain_lock.acquire(blocking=False):
            return False
        try:
            now = float(self._clock())
            with self._lock:
                row = self._db.execute(
                    "SELECT source_event_id,body,body_sha256,attempts FROM "
                    "wecom_exchange_outbox WHERE status=? AND next_attempt_at<=? "
                    "ORDER BY next_attempt_at,source_event_id LIMIT 1",
                    (_PENDING, now),
                ).fetchone()
                if row is None:
                    return False
                event_id = str(row["source_event_id"])
                body = bytes(row["body"])
                body_hash = str(row["body_sha256"])
                attempts = int(row["attempts"]) + 1
                self._db.execute(
                    "UPDATE wecom_exchange_outbox SET attempts=? "
                    "WHERE binding_id=? AND source_event_id=? AND status=?",
                    (attempts, self.config.binding_id, event_id, _PENDING),
                )
            self._send_one(event_id, body, body_hash, attempts)
            return True
        finally:
            self._drain_lock.release()

    def queue_status(self) -> dict[str, int]:
        """Return aggregate transport counts without exposing message content."""

        with self._lock:
            rows = self._db.execute(
                "SELECT status,COUNT(*) AS count FROM wecom_exchange_outbox "
                "GROUP BY status"
            ).fetchall()
        result = {_PENDING: 0, _ACKED: 0, _RETAINED: 0}
        result.update({str(row["status"]): int(row["count"]) for row in rows})
        return result

    def resolve_reply_context(
        self,
        *,
        message_id: str,
        account_id: str,
        conversation_id: str,
        conversation_kind: str,
    ) -> str | None:
        """Resolve only a fresh reply ID captured from this bot's SDK callback."""

        if (
            not isinstance(message_id, str)
            or not message_id
            or account_id != self.config.account_id
            or conversation_kind not in {"private", "group"}
            or not conversation_id
        ):
            return None
        now = float(self._clock())
        with self._lock:
            self._db.execute(
                "DELETE FROM wecom_reply_contexts WHERE expires_at<=?", (now,)
            )
            row = self._db.execute(
                "SELECT context.callback_req_id FROM wecom_reply_contexts AS context "
                "JOIN wecom_exchange_outbox AS outbox "
                "ON outbox.binding_id=context.binding_id "
                "AND outbox.source_event_id=context.message_id "
                "WHERE context.binding_id=? AND context.message_id=? "
                "AND context.account_id=? AND context.conversation_id=? "
                "AND context.conversation_kind=? AND context.expires_at>? "
                "AND outbox.status IN (?,?)",
                (
                    self.config.binding_id,
                    message_id,
                    account_id,
                    conversation_id,
                    conversation_kind,
                    now,
                    _PENDING,
                    _ACKED,
                ),
            ).fetchone()
        return str(row["callback_req_id"]) if row is not None else None

    def _send_one(
        self, event_id: str, body: bytes, body_hash: str, attempts: int
    ) -> None:
        timestamp = str(int(self._clock()))
        request_id = str(uuid4())
        path = urllib.parse.urlsplit(self.config.endpoint).path
        signing_input = (
            f"v1\nPOST\n{path}\n{timestamp}\n{request_id}\n"
            f"{self.config.binding_id}\n{body_hash}"
        ).encode()
        signature = hmac.new(
            self.config.source_hmac_key, signing_input, hashlib.sha256
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Cyrene-WeCom-Timestamp": timestamp,
            "X-Cyrene-WeCom-Request-Id": request_id,
            "X-Cyrene-WeCom-Body-SHA256": body_hash,
            "X-Cyrene-WeCom-Source-Binding-Id": self.config.binding_id,
            "X-Cyrene-WeCom-Signature": f"sha256={signature}",
        }
        try:
            status, response_body = self._transport.post(
                self.config.endpoint,
                body,
                headers,
                self.config.timeout_seconds,
            )
        except Exception:
            self._schedule_retry(event_id, attempts, None)
            return
        if 200 <= status < 300 and _is_durable_ack(response_body):
            with self._lock:
                self._db.execute(
                    "UPDATE wecom_exchange_outbox SET status=?,body=X'',"
                    "last_status_code=? "
                    "WHERE binding_id=? AND source_event_id=?",
                    (_ACKED, status, self.config.binding_id, event_id),
                )
            return
        if 400 <= status < 500 and status not in {408, 425, 429}:
            with self._lock:
                self._db.execute(
                    "UPDATE wecom_exchange_outbox SET status=?,last_status_code=? "
                    "WHERE binding_id=? AND source_event_id=?",
                    (_RETAINED, status, self.config.binding_id, event_id),
                )
            return
        self._schedule_retry(event_id, attempts, status)

    def _schedule_retry(self, event_id: str, attempts: int, status: int | None) -> None:
        delay = min(
            self._maximum_retry_seconds,
            self._minimum_retry_seconds * (2 ** min(attempts - 1, 10)),
        )
        with self._lock:
            self._db.execute(
                "UPDATE wecom_exchange_outbox SET next_attempt_at=?,last_status_code=? "
                "WHERE binding_id=? AND source_event_id=? AND status=?",
                (
                    float(self._clock()) + delay,
                    status,
                    self.config.binding_id,
                    event_id,
                    _PENDING,
                ),
            )

    def _run(self, poll_interval_seconds: float) -> None:
        while not self._stop.is_set():
            if not self.drain_once():
                self._wake.wait(poll_interval_seconds)
                self._wake.clear()


def _is_durable_ack(body: bytes) -> bool:
    """Accept only the explicit inbox-persisted response contract."""

    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, Mapping)
        and value.get("accepted") is True
        and value.get("persisted") is True
    )


def _conversation_kind_name(value: int) -> str:
    if value == message_contract.CONVERSATION_KIND_PRIVATE:
        return "private"
    if value == message_contract.CONVERSATION_KIND_GROUP:
        return "group"
    return ""


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _require_text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")
    return value
