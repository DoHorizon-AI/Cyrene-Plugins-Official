"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_ingress.py                                                  │
│  Module: tests.test_ingress                                          │
│  Role: Verify durable admission gates the Teams HTTP ACK.            │
│                                                                      │
│  模块职责：验证 Teams HTTP ACK 必须等待持久接纳                        │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from microsoft_teams.api.auth.cloud_environment import PUBLIC

from teams_connector.config import IngressSettings
from teams_connector.ingress import build_ingress_service
from teams_connector.navigator import AdmissionResult, NavigatorAdmissionUnavailable

BOT_APP_ID = "11111111-1111-4111-8111-111111111111"
TENANT_ID = "22222222-2222-4222-8222-222222222222"
ACTOR_ID = "33333333-3333-4333-8333-333333333333"
TEAM_ID = "19:team@thread.tacv2"
CHANNEL_ID = "19:channel@thread.tacv2"
CONVERSATION_ID = "19:conversation@thread.tacv2"
SERVICE_URL = f"https://smba.trafficmanager.net/amer/{TENANT_ID}/"


class AdmissionNavigator:
    """Capture events and optionally simulate a transient host failure.

    中文：记录事件，并可模拟主机短暂不可用。
    """

    def __init__(self, fail_first=False, terminal=False):
        self.fail_first = fail_first
        self.terminal = terminal
        self.events = []
        self.closed = False

    async def submit(self, event):
        """Require the sidecar to forward before it can acknowledge Teams.

        中文：要求侧车先转发，之后才能向 Teams 返回 ACK。
        """

        self.events.append(event)
        if self.fail_first and len(self.events) == 1:
            raise NavigatorAdmissionUnavailable("host is unavailable")
        if self.terminal:
            return AdmissionResult(
                accepted=False,
                duplicate=False,
                status_code=403,
                terminal_rejection=True,
                code="TEAMS_TEAM_NOT_ALLOWED",
            )
        return AdmissionResult(
            accepted=True, duplicate=len(self.events) > 1, status_code=202
        )

    async def submit_body(self, body: bytes):
        """Decode the canonical test body and reuse the Navigator fake.

        中文：解析规范测试 body，并复用 Navigator fake 行为。
        """

        return await self.submit(json.loads(body))

    async def aclose(self):
        """Record client shutdown without external I/O.

        中文：记录 client 关闭，不进行外部 I/O。
        """

        self.closed = True


def _settings(*, delivery_state_dir=None, delivery_admin_token=None):
    """Return private sidecar settings for one approved test team.

    中文：返回单个允许测试团队的私有侧车配置。
    """

    return IngressSettings(
        bot_app_id=BOT_APP_ID,
        tenant_id=TENANT_ID,
        allowed_team_ids=frozenset({TEAM_ID}),
        navigator_base_url="http://navigator",
        navigator_hmac_key_id="teams-v1",
        navigator_hmac_key=b"fabricated-navigator-key-material-of-32-bytes",
        navigator_timeout_seconds=1,
        delivery_state_dir=delivery_state_dir,
        delivery_admin_token=delivery_admin_token,
    )


def _activity(*, actor_aad_id=ACTOR_ID, activity_id="1749000000000"):
    """Return one complete fabricated Teams channel activity body.

    中文：返回完整伪造 Teams 频道 activity body。
    """

    return {
        "type": "message",
        "id": activity_id,
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": "29:actor", "aadObjectId": actor_aad_id, "name": "Actor"},
        "recipient": {"id": "28:cyrene-bot", "type": "bot"},
        "conversation": {"id": CONVERSATION_ID, "tenantId": TENANT_ID},
        "channelData": {
            "tenant": {"id": TENANT_ID},
            "team": {"id": TEAM_ID},
            "channel": {"id": CHANNEL_ID},
        },
        "text": "@Cyrene inspect",
        "entities": [],
        "attachments": [],
    }


def _install_test_jwks(monkeypatch, private_key):
    """Use a generated test key for the official SDK validator.

    中文：为官方 SDK validator 使用生成的测试 key。
    """

    public_key = private_key.public_key()

    class FakeJwksClient:
        def __init__(self, _uri):
            pass

        def get_signing_key_from_jwt(self, _token):
            return SimpleNamespace(key=public_key)

    monkeypatch.setattr(
        "microsoft_teams.apps.auth.token_validator.jwt.PyJWKClient",
        FakeJwksClient,
    )


def _service_token(private_key, *, service_url=SERVICE_URL):
    """Sign a service token for the activity's exact service URL and app ID.

    中文：为 activity 的确切 service URL 与 app ID 签发 service token。
    """

    now = int(time.time())
    return jwt.encode(
        {
            "iss": PUBLIC.token_issuer,
            "aud": BOT_APP_ID,
            "iat": now - 1,
            "exp": now + 120,
            "serviceurl": service_url,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "fabricated-test-key"},
    )


@pytest.mark.asyncio
async def test_teams_ack_waits_for_navigator_admission_and_retry_reuses_message_id(
    monkeypatch,
):
    """A failed admission returns 500; the repeated activity is forwarded again.

    中文：接纳失败时返回 500；同一 activity 重投时再次转发。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)
    navigator = AdmissionNavigator(fail_first=True)
    service = await build_ingress_service(_settings(), navigator=navigator)
    token = _service_token(private_key)
    try:
        with TestClient(service.http_app) as client:
            headers = {"Authorization": f"Bearer {token}"}
            first = client.post("/api/messages", json=_activity(), headers=headers)
            second = client.post("/api/messages", json=_activity(), headers=headers)
    finally:
        await service.close()

    assert first.status_code == 500
    assert second.status_code == 200
    assert len(navigator.events) == 2
    assert navigator.events[0]["trust"]["message_id"] == "1749000000000"
    assert navigator.events[0]["trust"]["service_url"] == SERVICE_URL
    assert navigator.events[1]["trust"]["message_id"] == "1749000000000"
    assert navigator.closed is True


@pytest.mark.asyncio
async def test_explicit_authenticated_terminal_rejection_is_acked(monkeypatch):
    """Do not retry a host-verified and code-allowlisted scope refusal.

    中文：已通过主机鉴权且 code 在 allowlist 中的范围拒绝不重试。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)
    navigator = AdmissionNavigator(terminal=True)
    service = await build_ingress_service(_settings(), navigator=navigator)
    token = _service_token(private_key)
    try:
        with TestClient(service.http_app) as client:
            response = client.post(
                "/api/messages",
                json=_activity(),
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        await service.close()

    assert response.status_code == 200
    assert len(navigator.events) == 1


@pytest.mark.asyncio
async def test_sidecar_does_not_static_filter_future_directory_maintainers(monkeypatch):
    """Forward identities in the allowed team; Navigator owns the fresh role check.

    中文：允许团队内的身份都转发，由 Navigator 每次读取最新角色权限。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)
    navigator = AdmissionNavigator()
    service = await build_ingress_service(_settings(), navigator=navigator)
    token = _service_token(private_key)
    new_actor = "44444444-4444-4444-8444-444444444444"
    try:
        with TestClient(service.http_app) as client:
            headers = {"Authorization": f"Bearer {token}"}
            first = client.post(
                "/api/messages",
                json=_activity(activity_id="1749000000002"),
                headers=headers,
            )
            second = client.post(
                "/api/messages",
                json=_activity(actor_aad_id=new_actor, activity_id="1749000000003"),
                headers=headers,
            )
    finally:
        await service.close()

    assert first.status_code == 200
    assert second.status_code == 200
    assert [event["trust"]["actor_aad_object_id"] for event in navigator.events] == [
        ACTOR_ID,
        new_actor,
    ]


@pytest.mark.asyncio
async def test_sdk_rejects_activity_service_url_not_bound_to_signed_claim(monkeypatch):
    """Never persist or route an activity serviceUrl that disagrees with the JWT.

    中文：activity serviceUrl 与 JWT claim 不一致时，不能持久化或用于回复路由。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)
    navigator = AdmissionNavigator()
    service = await build_ingress_service(_settings(), navigator=navigator)
    mismatched_token = _service_token(
        private_key,
        service_url="https://smba.trafficmanager.net/teams/",
    )
    try:
        with TestClient(service.http_app) as client:
            response = client.post(
                "/api/messages",
                json=_activity(),
                headers={"Authorization": f"Bearer {mismatched_token}"},
            )
    finally:
        await service.close()

    assert response.status_code == 401
    assert navigator.events == []


@pytest.mark.asyncio
async def test_durable_ack_waits_for_sqlite_commit_not_navigator_forward(
    monkeypatch, tmp_path
):
    """The authenticated Teams ACK follows SQLite commit while Navigator is pending.

    中文：Teams ACK 等待 SQLite commit，但不等待 Navigator 后台投递。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)
    token_value = b"0123456789abcdef0123456789abcdef"
    forward_started = threading.Event()
    release_forward = threading.Event()

    class BlockingNavigator:
        async def submit_body(self, body: bytes):
            forward_started.set()
            await asyncio.to_thread(release_forward.wait, 5)
            return AdmissionResult(True, False, 202)

        async def aclose(self):
            return None

    service = await build_ingress_service(
        _settings(
            delivery_state_dir=tmp_path / "delivery-state",
            delivery_admin_token=token_value,
        ),
        navigator=BlockingNavigator(),
    )
    committed = threading.Event()
    release_ack = threading.Event()
    original_enqueue = service.delivery_spool.enqueue

    async def delay_after_commit(event):
        result = await original_enqueue(event)
        committed.set()
        await asyncio.to_thread(release_ack.wait, 5)
        return result

    service.delivery_spool.enqueue = delay_after_commit
    request_token = _service_token(private_key)
    try:
        with (
            TestClient(service.http_app) as client,
            concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor,
        ):
            response_future = executor.submit(
                client.post,
                "/api/messages",
                json=_activity(activity_id="1749000000004"),
                headers={"Authorization": f"Bearer {request_token}"},
            )
            assert committed.wait(timeout=5)
            assert response_future.done() is False
            with sqlite3.connect(service.delivery_spool.database_path) as connection:
                row = connection.execute(
                    "SELECT state, body FROM deliveries"
                ).fetchone()
            assert row is not None
            assert row[0] == "queued"
            assert b"@Cyrene inspect" in row[1]
            release_ack.set()
            response = response_future.result(timeout=5)
            assert response.status_code == 200
            assert forward_started.wait(timeout=2)
    finally:
        release_ack.set()
        release_forward.set()
        await service.close()


@pytest.mark.asyncio
async def test_spool_capacity_failure_returns_non_success_to_teams(
    monkeypatch, tmp_path
):
    """A full queue cannot be converted into a successful Teams ACK.

    中文：队列容量不足时必须向 Teams 返回非成功状态。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)
    token_value = b"0123456789abcdef0123456789abcdef"

    class UnusedNavigator:
        async def submit_body(self, _body: bytes):
            raise AssertionError("full queue must not forward")

        async def aclose(self):
            return None

    service = await build_ingress_service(
        _settings(
            delivery_state_dir=tmp_path / "delivery-state",
            delivery_admin_token=token_value,
        ),
        navigator=UnusedNavigator(),
    )
    service.delivery_spool._max_rows = 0
    try:
        with TestClient(service.http_app) as client:
            response = client.post(
                "/api/messages",
                json=_activity(activity_id="1749000000005"),
                headers={"Authorization": f"Bearer {_service_token(private_key)}"},
            )
        assert not 200 <= response.status_code < 300
        with sqlite3.connect(service.delivery_spool.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        assert count == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_duplicate_activity_is_idempotent_and_conflicting_body_is_rejected(
    monkeypatch, tmp_path
):
    """Exact webhook redelivery reuses one row; changed bytes under the same ID fail.

    中文：相同 webhook 正文重投只保留一行；同 ID 不同正文必须拒绝。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)
    navigator = AdmissionNavigator()
    service = await build_ingress_service(
        _settings(
            delivery_state_dir=tmp_path / "delivery-state",
            delivery_admin_token=b"0123456789abcdef0123456789abcdef",
        ),
        navigator=navigator,
    )
    token = _service_token(private_key)
    headers = {"Authorization": f"Bearer {token}"}
    original_activity = _activity(activity_id="1749000000006")
    conflicting_activity = {
        **original_activity,
        "text": "changed under same activity id",
    }
    try:
        with TestClient(service.http_app) as client:
            first = client.post(
                "/api/messages", json=original_activity, headers=headers
            )
            duplicate = client.post(
                "/api/messages", json=original_activity, headers=headers
            )
            conflict = client.post(
                "/api/messages", json=conflicting_activity, headers=headers
            )

        assert first.status_code == 200
        assert duplicate.status_code == 200
        assert not 200 <= conflict.status_code < 300
        with sqlite3.connect(service.delivery_spool.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        assert count == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_durable_admin_routes_are_private_and_use_holder_revision_cas(tmp_path):
    """Protect internal delivery control and return metadata-only status.

    中文：内部投递管理接口要求独立 token，并只返回元数据。
    """

    token_value = b"0123456789abcdef0123456789abcdef"
    service = await build_ingress_service(
        _settings(
            delivery_state_dir=tmp_path / "delivery-state",
            delivery_admin_token=token_value,
        ),
        navigator=AdmissionNavigator(),
    )
    headers = {"Authorization": f"Bearer {token_value.decode('ascii')}"}
    cas = {"holder": "rollout-1", "expected_revision": 0}
    try:
        with TestClient(service.http_app) as client:
            unauthorized = client.get("/_internal/teams/delivery/status")
            assert unauthorized.status_code == 401
            assert token_value.decode("ascii") not in unauthorized.text

            status = client.get("/_internal/teams/delivery/status", headers=headers)
            assert status.status_code == 200
            assert status.json()["paused"] is False
            assert status.json()["revision"] == 0
            assert "holder" not in status.json()
            assert "body" not in status.json()

            paused = client.post(
                "/_internal/teams/delivery/pause", headers=headers, json=cas
            )
            assert paused.status_code == 200
            assert paused.json()["paused"] is True
            assert paused.json()["revision"] == 1
            assert paused.json()["active_delivery_count"] == 0

            duplicate_pause = client.post(
                "/_internal/teams/delivery/pause", headers=headers, json=cas
            )
            assert duplicate_pause.status_code == 200
            assert duplicate_pause.json()["revision"] == 1

            conflict = client.post(
                "/_internal/teams/delivery/pause",
                headers=headers,
                json={"holder": "rollout-2", "expected_revision": 1},
            )
            assert conflict.status_code == 409
            wrong_resume = client.post(
                "/_internal/teams/delivery/resume",
                headers=headers,
                json={"holder": "rollout-2", "expected_revision": 1},
            )
            assert wrong_resume.status_code == 409

            resumed = client.post(
                "/_internal/teams/delivery/resume",
                headers=headers,
                json={"holder": "rollout-1", "expected_revision": 1},
            )
            assert resumed.status_code == 200
            assert resumed.json()["paused"] is False
            assert resumed.json()["revision"] == 2
            repeated_resume = client.post(
                "/_internal/teams/delivery/resume",
                headers=headers,
                json={"holder": "rollout-1", "expected_revision": 1},
            )
            assert repeated_resume.status_code == 200
            assert repeated_resume.json()["revision"] == 2
    finally:
        await service.close()
