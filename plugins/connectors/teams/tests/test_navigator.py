"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_navigator.py                                                │
│  Module: tests.test_navigator                                        │
│  Role: Verify HMAC signing and durable admission handling.           │
│                                                                      │
│  模块职责：验证 HMAC 签名与持久接纳响应处理                            │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from teams_connector.navigator import (
    NavigatorAdmissionUnavailable,
    NavigatorEventClient,
)

HMAC_KEY = b"teams-test-key-material-with-32-bytes-minimum"


@pytest.mark.asyncio
async def test_event_is_signed_over_exact_raw_body_and_accepts_duplicate_receipt():
    """Use the confirmed timestamp-dot-raw-body HMAC contract.

    中文：使用已确认的 timestamp-dot-raw-body HMAC 契约。
    """

    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["headers"] = request.headers
        return httpx.Response(200, json={"duplicate": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = NavigatorEventClient(
            base_url="http://navigator:8080",
            key_id="teams-v1",
            hmac_key=HMAC_KEY,
            client=http_client,
            clock=lambda: 1_790_000_000,
        )
        result = await client.submit(
            {
                "trust": {"message_id": "activity-1", "provider": "teams"},
                "event_type": "inbound_message",
                "payload": {"message_id": "activity-1", "sender_id": "actor"},
            }
        )

    timestamp = "1790000000"
    expected_body = json.dumps(
        {
            "trust": {"message_id": "activity-1", "provider": "teams"},
            "event_type": "inbound_message",
            "payload": {"message_id": "activity-1", "sender_id": "actor"},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_signature = hmac.new(
        HMAC_KEY,
        timestamp.encode("ascii") + b"." + expected_body,
        hashlib.sha256,
    ).hexdigest()

    assert captured["body"] == expected_body
    assert captured["headers"]["x-cyrene-source-key-id"] == "teams-v1"
    assert captured["headers"]["x-cyrene-source-timestamp"] == timestamp
    assert (
        captured["headers"]["x-cyrene-source-signature"] == f"v1={expected_signature}"
    )
    assert b"teams-test-key-material" not in captured["body"]
    assert result.accepted is True
    assert result.duplicate is True


@pytest.mark.asyncio
async def test_authenticated_terminal_team_rejection_is_not_an_admission():
    """Accept only an authenticated, code-allowlisted business rejection.

    中文：只接受已认证且 code 在 allowlist 中的业务终态拒绝。
    """

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "code": "TEAMS_TEAM_NOT_ALLOWED",
                "terminal": True,
                "sourceAuthenticated": True,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = NavigatorEventClient(
            base_url="http://navigator:8080",
            key_id="teams-v1",
            hmac_key=HMAC_KEY,
            client=http_client,
        )
        result = await client.submit({"event_type": "inbound_message"})

    assert result.accepted is False
    assert result.terminal_rejection is True
    assert result.code == "TEAMS_TEAM_NOT_ALLOWED"
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_source_authentication_401_is_permanent_for_durable_delivery():
    """A source-authentication 401 is terminal until its private key is fixed.

    中文：来源鉴权或 timestamp 401 在修复私有 key 前属于永久错误。
    """

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "code": "CONNECTOR_SOURCE_UNAUTHENTICATED",
                "terminal": False,
                "sourceAuthenticated": False,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = NavigatorEventClient(
            base_url="http://navigator:8080",
            key_id="teams-v1",
            hmac_key=HMAC_KEY,
            client=http_client,
        )
        with pytest.raises(NavigatorAdmissionUnavailable, match="status=401") as error:
            await client.submit({"event_type": "inbound_message"})
    assert error.value.retryable is False
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_unknown_or_malformed_four_xx_is_permanent_for_spool_retry_policy():
    """An unallowlisted 4xx is never ACKed and is not retried forever.

    中文：没有 allowlist 终态 code 的 4xx 不 ACK，也不无限重试。
    """

    replies = iter(
        [
            httpx.Response(
                403,
                json={
                    "code": "WORK_CONNECTOR_INVALID_EVENT",
                    "terminal": True,
                    "sourceAuthenticated": True,
                },
            ),
            httpx.Response(422, content=b"not-json"),
        ]
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return next(replies)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = NavigatorEventClient(
            base_url="http://navigator:8080",
            key_id="teams-v1",
            hmac_key=HMAC_KEY,
            client=http_client,
        )
        for status_code in (403, 422):
            with pytest.raises(
                NavigatorAdmissionUnavailable, match=f"status={status_code}"
            ) as error:
                await client.submit({"event_type": "inbound_message"})
            assert error.value.retryable is False


@pytest.mark.asyncio
async def test_rate_limit_is_transient_and_exposes_only_bounded_retry_after():
    """A 429 schedules a retry without persisting response headers.

    中文：429 按临时错误重试，只读取有界 Retry-After 元数据。
    """

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "17"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = NavigatorEventClient(
            base_url="http://navigator:8080",
            key_id="teams-v1",
            hmac_key=HMAC_KEY,
            client=http_client,
        )
        with pytest.raises(NavigatorAdmissionUnavailable) as error:
            await client.submit({"event_type": "inbound_message"})

    assert error.value.retryable is True
    assert error.value.status_code == 429
    assert error.value.retry_after_seconds == 17


@pytest.mark.asyncio
async def test_host_failure_is_generic_and_does_not_leak_callback_url():
    """Raise an opaque error so SDK logs cannot expose the internal endpoint.

    中文：以不透明错误上抛，避免 SDK 日志泄露内部 endpoint。
    """

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = NavigatorEventClient(
            base_url="https://private.navigator.example.invalid:8443",
            key_id="teams-v1",
            hmac_key=HMAC_KEY,
            client=http_client,
        )
        with pytest.raises(NavigatorAdmissionUnavailable) as error:
            await client.submit({"event_type": "inbound_message"})

    assert "private.navigator" not in str(error.value)
    assert "https://" not in str(error.value)
