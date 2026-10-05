"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 notification_worker.py                                          │
│  Package: wecom_connector                                           │
│  Role: Deliver scoped Navigator notifications through WeCom.        │
│                                                                     │
│  模块职责：通过 WeCom 投递受 workspace、binding 和 trusted route 约束的 │
│  Navigator 通知。                                                   │
│  · 只 claim 精确的 connector、recipient 与 wecom.message 通知。       │
│  · 发送前先持久化 started；未知回执标记 uncertain，绝不自动重发。      │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .errors import ConnectorError
from .navigator import (
    NavigatorIntegrationConfig,
    TrustedConversation,
    notification_recipient_key,
)

logger = logging.getLogger(__name__)

NOTIFICATION_TYPE = "wecom.message"
MAX_NOTIFICATION_BYTES = 64 * 1024
MAX_NOTIFICATION_TEXT = 16_000
CLAIM_LIMIT = 4
LEASE_SECONDS = 120


class NavigatorNotificationWorker:
    """Poll only this WeCom binding's explicitly trusted terminal notifications.

    中文:只轮询当前 WeCom binding 已显式信任的终态通知。
    """

    def __init__(
        self,
        config: NavigatorIntegrationConfig,
        send_message: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        *,
        poll_interval_seconds: float = 1.0,
        worker_id: str | None = None,
    ) -> None:
        if not 0.05 <= poll_interval_seconds <= 60:
            raise ValueError("poll_interval_seconds must be between 0.05 and 60")
        self._config = config
        self._send_message = send_message
        self._poll_interval_seconds = poll_interval_seconds
        self._worker_id = worker_id or f"wecom-{uuid.uuid4().hex}"
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """Return whether the background polling thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start one background polling thread for this configured binding."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"wecom-outbox-{self._config.connector_id}",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        """Stop polling and wait briefly for any current receipt transition."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout_seconds))
        with self._lock:
            if thread is None or not thread.is_alive():
                self._thread = None

    def poll_once(self) -> int:
        """Claim and process one bounded batch for each configured route.

        中文:为每个已配置 route claim 并处理一批有界通知。
        """
        processed = 0
        for rule in self._config.trusted_conversations:
            for sender_id in sorted(rule.trusted_senders):
                if self._stop_event.is_set():
                    return processed
                recipient = notification_recipient_key(
                    rule.account_id,
                    rule.conversation_id,
                    _chat_type(rule.kind),
                    sender_id,
                )
                claim = self._post(
                    "/notifications/claim",
                    {
                        "workerId": self._worker_id,
                        "limit": CLAIM_LIMIT,
                        "leaseSeconds": LEASE_SECONDS,
                        "type": NOTIFICATION_TYPE,
                        "connectorId": self._config.connector_id,
                        "recipient": recipient,
                    },
                )
                items = claim.get("items", [])
                if not isinstance(items, list):
                    raise ConnectorError(
                        "UNAVAILABLE", "Navigator claim receipt has invalid items"
                    )
                for item in items:
                    if self._stop_event.is_set():
                        return processed
                    self._process_claim(item, rule, sender_id, recipient)
                    processed += 1
        return processed

    def _process_claim(
        self,
        item: Any,
        rule: TrustedConversation,
        sender_id: str,
        recipient: str,
    ) -> None:
        if not isinstance(item, Mapping):
            raise ConnectorError("UNAVAILABLE", "Navigator claim item is invalid")
        notification = item.get("notification")
        lease_token = _text(item.get("leaseToken"), "leaseToken")
        if not isinstance(notification, Mapping):
            raise ConnectorError("UNAVAILABLE", "claimed notification is invalid")
        notification_id = _text(
            notification.get("id", notification.get("notificationId")),
            "notification.id",
        )

        encoded_id = urllib.parse.quote(notification_id, safe="")
        start_route = f"/notifications/{encoded_id}/start"
        finish_route = f"/notifications/{encoded_id}/finish"
        self._post(start_route, {"leaseToken": lease_token})
        # The claim was filtered by exact type, connector and recipient in the
        # Navigator transaction. Recheck the persisted row after start so a
        # malformed claimed item can be closed as failed instead of requeued.
        try:
            route = _trusted_notification_route(
                notification,
                self._config.connector_id,
                recipient,
                rule,
                sender_id,
            )
        except ConnectorError:
            self._post(
                finish_route,
                {
                    "leaseToken": lease_token,
                    "outcome": "failed",
                    "result": {"status": "invalid_notification_route"},
                },
            )
            return
        payload = route["payload"]
        text = payload.get("text")
        invalid_payload = (
            not isinstance(text, str)
            or not text.strip()
            or len(text.encode("utf-8")) > MAX_NOTIFICATION_TEXT
        )
        if invalid_payload:
            # Poison rows are terminally failed instead of being re-claimed forever.
            self._post(
                finish_route,
                {
                    "leaseToken": lease_token,
                    "outcome": "failed",
                    "result": {"status": "invalid_notification_text"},
                },
            )
            return

        request = {
            "conversation": {
                "vendor": "wecom.app",
                "account_id": rule.account_id,
                "conversation_id": rule.conversation_id,
                "kind": rule.kind,
            },
            "content": [{"kind": "text", "text": text}],
        }
        try:
            receipt = self._send_message(request)
        except Exception as exc:
            logger.warning(
                "WeCom notification result is unknown; it will not be retried",
                extra={
                    "notification_id": notification_id,
                    "error_type": type(exc).__name__,
                },
            )
            self._post(
                finish_route,
                {
                    "leaseToken": lease_token,
                    "outcome": "uncertain",
                    "result": {"status": "unknown_vendor_receipt"},
                },
            )
            return

        if not isinstance(receipt, Mapping):
            outcome = "uncertain"
            result = {"status": "invalid_connector_receipt"}
        else:
            status = receipt.get("status")
            if status == "accepted":
                outcome = "delivered"
                result = {
                    "status": "accepted",
                    "vendorMessageId": str(receipt.get("vendor_message_id", ""))[
                        :256
                    ],
                }
            elif status in {"rejected", "rate_limited"}:
                outcome = "failed"
                result = {"status": str(status)}
            else:
                outcome = "uncertain"
                result = {"status": "unknown_connector_status"}
        self._post(
            finish_route,
            {
                "leaseToken": lease_token,
                "outcome": outcome,
                "result": result,
            },
        )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                logger.warning(
                    "WeCom Navigator outbox poll failed",
                    extra={"error_type": type(exc).__name__},
                )
            self._stop_event.wait(self._poll_interval_seconds)

    def _post(self, suffix: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        route = (
            "/api/v1/workspaces/"
            f"{urllib.parse.quote(self._config.workspace_id, safe='')}"
            "/work/notifications"
            f"{suffix}"
        )
        url = f"{self._config.base_url}{route}"
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_NOTIFICATION_BYTES:
            raise ConnectorError(
                "INVALID_REQUEST", "Navigator notification request is too large"
            )
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
                raw = response.read(MAX_NOTIFICATION_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ConnectorError(
                "UNAVAILABLE", f"Navigator notification returned HTTP {exc.code}"
            ) from exc
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            raise ConnectorError(
                "UNAVAILABLE", "Navigator notification request failed"
            ) from exc
        if status not in {200, 201, 202, 204}:
            raise ConnectorError(
                "UNAVAILABLE", f"Navigator notification returned HTTP {status}"
            )
        if not raw:
            return {}
        if len(raw) > MAX_NOTIFICATION_BYTES:
            raise ConnectorError(
                "UNAVAILABLE", "Navigator notification response is too large"
            )
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorError(
                "UNAVAILABLE", "Navigator notification returned invalid JSON"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise ConnectorError(
                "UNAVAILABLE", "Navigator notification receipt is not an object"
            )
        return decoded


def _trusted_notification_route(
    notification: Mapping[str, Any],
    connector_id: str,
    recipient: str,
    rule: TrustedConversation,
    sender_id: str,
) -> dict[str, Any]:
    """Validate a claimed row against the route derived from trusted config."""
    if notification.get("type") != NOTIFICATION_TYPE:
        raise ConnectorError("INVALID_REQUEST", "notification type is not WeCom")
    if notification.get("connectorId") != connector_id:
        raise ConnectorError(
            "INVALID_REQUEST", "notification connector does not match this binding"
        )
    if notification.get("recipient") != recipient:
        raise ConnectorError("INVALID_REQUEST", "notification recipient is not trusted")
    payload = notification.get("payload")
    if not isinstance(payload, Mapping):
        raise ConnectorError("INVALID_REQUEST", "notification payload is invalid")
    expected = {
        "accountId": rule.account_id,
        "conversationId": rule.conversation_id,
        "chatType": _chat_type(rule.kind),
        "senderId": sender_id,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ConnectorError(
            "INVALID_REQUEST", "notification route is outside the trusted map"
        )
    return {"payload": payload}


def _chat_type(kind: str) -> str:
    """Map canonical WeCom conversation kinds to OpenWS chat types."""
    return "group" if kind == "group" else "single"


def _text(value: Any, field: str) -> str:
    """Require one bounded non-empty string from an outbox record."""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ConnectorError("UNAVAILABLE", f"Navigator {field} is invalid")
    return value
