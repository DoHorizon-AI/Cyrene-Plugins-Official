"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 navigator.py                                                     │
│  Module: teams_connector.navigator                                  │
│  Role: Submit verified Teams events to durable Navigator admission.   │
│                                                                      │
│  模块职责：将已验证 Teams 事件提交给 Navigator 持久接纳入口             │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

EVENT_ROUTE = "/internal/v1/connectors/teams/events"
TERMINAL_BUSINESS_REJECTIONS = frozenset(
    {"TEAMS_TENANT_NOT_ALLOWED", "TEAMS_TEAM_NOT_ALLOWED"}
)


class NavigatorAdmissionUnavailable(RuntimeError):
    """Navigator could not confirm durable admission for this event.

    中文：Navigator 未能确认此事件已持久接纳。
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        status_code: int | None = None,
        code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.code = code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class AdmissionResult:
    """Result of forwarding one event to Navigator.

    中文：转发一个事件至 Navigator 的结果。
    """

    accepted: bool
    duplicate: bool
    status_code: int
    terminal_rejection: bool = False
    code: str | None = None


class NavigatorEventClient:
    """HMAC-sign exact canonical event bytes and require host admission ACK.

    中文：对规范事件原始字节计算 HMAC，并等待主机接纳确认。
    """

    def __init__(
        self,
        *,
        base_url: str,
        key_id: str,
        hmac_key: bytes,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
        clock: Any = time.time,
    ) -> None:
        if len(hmac_key) < 32:
            raise ValueError("Navigator HMAC key must contain at least 32 bytes")
        self._base_url = base_url.rstrip("/")
        self._key_id = key_id
        self._hmac_key = bytes(hmac_key)
        self._timeout_seconds = timeout_seconds
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None
        self._clock = clock

    async def submit(self, event: dict[str, Any]) -> AdmissionResult:
        """Forward one normalized event and distinguish host rejection from outage.

        The request body is persisted by Navigator before it acknowledges. A
        duplicate retry is therefore safe to forward again with the same
        Teams message ID; the host owns durable deduplication.

        中文：转发规范事件并区分主机拒绝与不可用。Navigator 会先持久化，再确认；
        因而可安全重发相同 Teams message ID，持久去重由主机负责。
        """

        return await self.submit_body(canonical_event_body(event))

    async def submit_body(self, raw_body: bytes) -> AdmissionResult:
        """Sign and submit exact persisted bytes with a fresh timestamp.

        The spool passes the same body on every retry. Only the timestamp and
        HMAC request headers are regenerated; no authorization headers enter
        the durable queue.

        中文：对持久化的精确字节签名并提交新 timestamp。每次重试使用相同 body，只重新
        生成 timestamp 与 HMAC headers；Authorization 不进入持久队列。
        """

        if not isinstance(raw_body, bytes) or not raw_body:
            raise ValueError("Navigator event body must be non-empty bytes")
        timestamp = str(int(self._clock()))
        signature = hmac.new(
            self._hmac_key,
            timestamp.encode("ascii") + b"." + raw_body,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Cyrene-Source-Key-Id": self._key_id,
            "X-Cyrene-Source-Timestamp": timestamp,
            "X-Cyrene-Source-Signature": f"v1={signature}",
        }
        url = urljoin(self._base_url + "/", EVENT_ROUTE.lstrip("/"))
        try:
            response = await self._client.post(
                url,
                content=raw_body,
                headers=headers,
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError:
            # Do not propagate HTTPX's exception text; it may include an internal URL.
            raise NavigatorAdmissionUnavailable(
                "Navigator event admission could not be confirmed",
                retryable=True,
            ) from None

        if 200 <= response.status_code < 300:
            duplicate = False
            try:
                decoded = response.json()
            except (ValueError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, dict):
                duplicate = decoded.get("duplicate") is True
            return AdmissionResult(True, duplicate, response.status_code)

        body = _response_error_facts(response)
        code = body.get("code")
        safe_code = (
            code
            if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_.-]{1,100}", code)
            else "UNKNOWN"
        )
        if (
            400 <= response.status_code < 500
            and body.get("terminal") is True
            and body.get("sourceAuthenticated") is True
            and safe_code in TERMINAL_BUSINESS_REJECTIONS
        ):
            return AdmissionResult(
                False,
                False,
                response.status_code,
                terminal_rejection=True,
                code=safe_code,
            )
        retryable = (
            response.status_code in {408, 425, 429} or response.status_code >= 500
        )
        retry_after_seconds = _retry_after_seconds(response.headers.get("Retry-After"))
        raise NavigatorAdmissionUnavailable(
            "Navigator event admission was not durable "
            f"(status={response.status_code}, code={safe_code})",
            retryable=retryable,
            status_code=response.status_code,
            code=safe_code,
            retry_after_seconds=retry_after_seconds,
        )

    async def aclose(self) -> None:
        """Close an internally created HTTP client.

        中文：关闭本类创建的 HTTP client。
        """

        if self._owns_client:
            await self._client.aclose()


def _response_error_facts(response: httpx.Response) -> dict[str, Any]:
    """Read only the bounded structured rejection fields used for ACK policy.

    中文：只读取 ACK 策略使用的有界结构化拒绝字段。
    """

    try:
        decoded = response.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {
        "code": decoded.get("code"),
        "terminal": decoded.get("terminal"),
        "sourceAuthenticated": decoded.get("sourceAuthenticated"),
    }


def canonical_event_body(event: dict[str, Any]) -> bytes:
    """Serialize one normalized Navigator event into its stable wire body.

    中文：将规范 Navigator event 序列化为稳定的 wire body。
    """

    return json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse only bounded numeric Retry-After values; ignore unsafe text.

    中文：只解析有界数值 Retry-After，忽略其他响应文本。
    """

    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds < 0 or seconds > 24 * 60 * 60:
        return None
    return seconds
