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


def _settings(
    *, delivery_state_dir=None, delivery_admin_token=None, personal_chat_enabled=False
):
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
        personal_chat_enabled=personal_chat_enabled,
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


def _personal_install_activity(action, *, activity_id):
    """Return one fabricated install lifecycle event from personal scope.

    中文：返回一条伪造的 personal scope 安装生命周期 event。
    """

    return {
        "type": "installationUpdate",
        "action": action,
        "id": activity_id,
        "timestamp": "2026-10-30T04:00:00Z",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": "29:actor", "aadObjectId": ACTOR_ID, "name": "Actor"},
        "recipient": {"id": "28:cyrene-bot", "type": "bot"},
        "conversation": {
            "id": "a:personal-install-conversation",
            "tenantId": TENANT_ID,
            "conversationType": "personal",
        },
        "channelData": {"tenant": {"id": TENANT_ID}},
    }


def _personal_message_activity(*, activity_id="personal-message-1"):
    """Return one fabricated one-person Teams bot message.

    中文：返回一条伪造的一对一 Teams bot 消息。
    """

    return {
        "type": "message",
        "id": activity_id,
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": "29:actor", "aadObjectId": ACTOR_ID, "name": "Actor"},
        "recipient": {"id": "28:cyrene-bot", "type": "bot"},
        "conversation": {
            "id": "a:personal-conversation",
            "tenantId": TENANT_ID,
            "conversationType": "personal",
        },
        "channelData": {"tenant": {"id": TENANT_ID}},
        "text": "private request",
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
async def test_personal_install_and_removal_require_sdk_auth_and_forward_binding_events(
    monkeypatch,
):
    """Forward personal route registration and revocation only after SDK auth.

    中文：只有 SDK 鉴权成功后才转发个人路由注册与撤销。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)
    navigator = AdmissionNavigator()
    service = await build_ingress_service(
        _settings(personal_chat_enabled=True), navigator=navigator
    )
    token = _service_token(private_key)
    try:
        with TestClient(service.http_app) as client:
            headers = {"Authorization": f"Bearer {token}"}
            added = client.post(
                "/api/messages",
                json=_personal_install_activity("add", activity_id="install-add-1"),
                headers=headers,
            )
            removed = client.post(
                "/api/messages",
                json=_personal_install_activity(
                    "remove", activity_id="install-remove-1"
                ),
                headers=headers,
            )
            missing_timestamp_activity = _personal_install_activity(
                "add", activity_id="install-add-missing-time"
            )
            del missing_timestamp_activity["timestamp"]
            missing_timestamp = client.post(
                "/api/messages",
                json=missing_timestamp_activity,
                headers=headers,
            )
            rejected = client.post(
                "/api/messages",
                json=_personal_install_activity("add", activity_id="install-add-2"),
                headers={
                    "Authorization": _service_token(
                        private_key,
                        service_url="https://smba.trafficmanager.net/teams/",
                    )
                },
            )
    finally:
        await service.close()

    assert added.status_code == 200
    assert removed.status_code == 200
    assert missing_timestamp.status_code == 200
    assert rejected.status_code == 401
    assert [event["trust"]["installation_action"] for event in navigator.events] == [
        "add",
        "remove",
    ]
    assert all(
        event["event_type"] == "personal_installation" for event in navigator.events
    )
    assert all(
        event["trust"]["actor_aad_object_id"] == ACTOR_ID for event in navigator.events
    )


@pytest.mark.asyncio
async def test_personal_messages_stay_disabled_until_explicit_host_opt_in(monkeypatch):
    """Drop personal messages by default and preserve identity after opt-in.

    中文：默认忽略个人消息；显式 opt-in 后保留已验证身份。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)
    token = _service_token(private_key)
    activity = _personal_message_activity()

    disabled_navigator = AdmissionNavigator()
    disabled_service = await build_ingress_service(
        _settings(), navigator=disabled_navigator
    )
    try:
        with TestClient(disabled_service.http_app) as client:
            disabled = client.post(
                "/api/messages",
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        await disabled_service.close()

    enabled_navigator = AdmissionNavigator()
    enabled_service = await build_ingress_service(
        _settings(personal_chat_enabled=True), navigator=enabled_navigator
    )
    try:
        with TestClient(enabled_service.http_app) as client:
            enabled = client.post(
                "/api/messages",
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        await enabled_service.close()

    assert disabled.status_code == 200
    assert disabled_navigator.events == []
    assert enabled.status_code == 200
    assert len(enabled_navigator.events) == 1
    event = enabled_navigator.events[0]
    assert event["payload"]["conversation"]["kind"] == "private"
    assert event["trust"]["actor_aad_object_id"] == ACTOR_ID
    assert event["trust"]["conversation_kind"] == "private"


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
async def test_durable_mode_rejects_unverified_activity_before_sqlite_enqueue(
    monkeypatch, tmp_path
):
    """An SDK auth rejection cannot create a durable queue row or receive success.

    中文：SDK 鉴权拒绝时不能写入 SQLite，也不能返回成功 ACK。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)

    class UnusedNavigator:
        async def submit_body(self, _body: bytes):
            raise AssertionError("unauthenticated activity must not be forwarded")

        async def aclose(self):
            return None

    service = await build_ingress_service(
        _settings(
            delivery_state_dir=tmp_path / "delivery-state",
            delivery_admin_token=b"0123456789abcdef0123456789abcdef",
        ),
        navigator=UnusedNavigator(),
    )
    mismatched_token = _service_token(
        private_key,
        service_url="https://smba.trafficmanager.net/teams/",
    )
    try:
        with TestClient(service.http_app) as client:
            response = client.post(
                "/api/messages",
                json=_activity(activity_id="1749000000009"),
                headers={"Authorization": f"Bearer {mismatched_token}"},
            )
        assert response.status_code == 401
        with sqlite3.connect(service.delivery_spool.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        assert count == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_durable_mode_rejects_personal_routes_before_sqlite_enqueue(
    monkeypatch, tmp_path
):
    """Reject unordered or unsendable private identities before durable admission."""

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)

    class UnusedNavigator:
        async def submit_body(self, _body: bytes):
            raise AssertionError("rejected personal events must never be queued")

        async def aclose(self):
            return None

    service = await build_ingress_service(
        _settings(
            delivery_state_dir=tmp_path / "delivery-state",
            delivery_admin_token=b"0123456789abcdef0123456789abcdef",
            personal_chat_enabled=True,
        ),
        navigator=UnusedNavigator(),
    )
    headers = {"Authorization": f"Bearer {_service_token(private_key)}"}
    missing_timestamp = _personal_install_activity(
        "add", activity_id="install-add-missing-time"
    )
    del missing_timestamp["timestamp"]
    invalid_install_id = _personal_install_activity(
        "add", activity_id="install-add-invalid-id"
    )
    invalid_install_id["conversation"]["id"] = "19:personal@thread.v2"
    invalid_message_id = _personal_message_activity(
        activity_id="personal-message-invalid-id"
    )
    invalid_message_id["conversation"]["id"] = "19:personal@thread.v2"

    try:
        with TestClient(service.http_app) as client:
            responses = [
                client.post("/api/messages", json=missing_timestamp, headers=headers),
                client.post("/api/messages", json=invalid_install_id, headers=headers),
                client.post("/api/messages", json=invalid_message_id, headers=headers),
            ]
        assert [response.status_code for response in responses] == [200, 200, 200]
        with sqlite3.connect(service.delivery_spool.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        assert count == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_durable_mode_rejects_personal_routes_without_order_or_sendable_id(
    monkeypatch, tmp_path
):
    """Do not persist installation events lacking official time or sendable IDs."""

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)

    class UnusedNavigator:
        async def submit_body(self, _body: bytes):
            raise AssertionError("rejected personal events must never be queued")

        async def aclose(self):
            return None

    service = await build_ingress_service(
        _settings(
            delivery_state_dir=tmp_path / "delivery-state",
            delivery_admin_token=b"0123456789abcdef0123456789abcdef",
            personal_chat_enabled=True,
        ),
        navigator=UnusedNavigator(),
    )
    token = _service_token(private_key)
    headers = {"Authorization": f"Bearer {token}"}
    missing_timestamp = _personal_install_activity(
        "add", activity_id="install-add-missing-time"
    )
    del missing_timestamp["timestamp"]
    invalid_install_id = _personal_install_activity(
        "add", activity_id="install-add-invalid-id"
    )
    invalid_install_id["conversation"]["id"] = "19:personal@thread.v2"
    invalid_message_id = _personal_message_activity(
        activity_id="personal-message-invalid-id"
    )
    invalid_message_id["conversation"]["id"] = "19:personal@thread.v2"

    try:
        with TestClient(service.http_app) as client:
            responses = [
                client.post("/api/messages", json=missing_timestamp, headers=headers),
                client.post("/api/messages", json=invalid_install_id, headers=headers),
                client.post("/api/messages", json=invalid_message_id, headers=headers),
            ]
        assert [response.status_code for response in responses] == [200, 200, 200]
        with sqlite3.connect(service.delivery_spool.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        assert count == 0
    finally:
        await service.close()


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
async def test_sqlite_commit_failure_cannot_acknowledge_teams(monkeypatch, tmp_path):
    """A failed durable commit rolls back and returns non-2xx to Teams.

    中文：SQLite commit 失败时事务回滚，Teams 收到非 2xx。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)

    class UnusedNavigator:
        async def submit_body(self, _body: bytes):
            raise AssertionError("uncommitted event must not be forwarded")

        async def aclose(self):
            return None

    service = await build_ingress_service(
        _settings(
            delivery_state_dir=tmp_path / "delivery-state",
            delivery_admin_token=b"0123456789abcdef0123456789abcdef",
        ),
        navigator=UnusedNavigator(),
    )
    original_connect = service.delivery_spool._connect

    class CommitFailureConnection(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")

    def failing_connect():
        connection = sqlite3.connect(
            service.delivery_spool.database_path,
            timeout=5.0,
            isolation_level=None,
            factory=CommitFailureConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    try:
        monkeypatch.setattr(service.delivery_spool, "_connect", failing_connect)
        with TestClient(service.http_app) as client:
            response = client.post(
                "/api/messages",
                json=_activity(activity_id="1749000000008"),
                headers={"Authorization": f"Bearer {_service_token(private_key)}"},
            )
            monkeypatch.setattr(service.delivery_spool, "_connect", original_connect)
            assert not 200 <= response.status_code < 300
            with sqlite3.connect(service.delivery_spool.database_path) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM deliveries"
                ).fetchone()[0]
            assert count == 0
    finally:
        monkeypatch.setattr(service.delivery_spool, "_connect", original_connect)
        await service.close()


@pytest.mark.asyncio
async def test_sqlite_writer_lock_failure_cannot_acknowledge_teams(
    monkeypatch, tmp_path
):
    """A held SQLite write lock makes the authenticated callback return non-2xx.

    中文：SQLite writer lock 未释放时，经过认证的 callback 返回非 2xx。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _install_test_jwks(monkeypatch, private_key)

    class UnusedNavigator:
        async def submit_body(self, _body: bytes):
            raise AssertionError("locked database must not forward")

        async def aclose(self):
            return None

    service = await build_ingress_service(
        _settings(
            delivery_state_dir=tmp_path / "delivery-state",
            delivery_admin_token=b"0123456789abcdef0123456789abcdef",
        ),
        navigator=UnusedNavigator(),
    )
    database_path = service.delivery_spool.database_path
    lock_connection = sqlite3.connect(database_path, timeout=0.1, isolation_level=None)
    lock_connection.execute("BEGIN IMMEDIATE")

    def short_busy_connect():
        connection = sqlite3.connect(
            database_path,
            timeout=0.02,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    original_connect = service.delivery_spool._connect
    try:
        monkeypatch.setattr(service.delivery_spool, "_connect", short_busy_connect)
        with TestClient(service.http_app) as client:
            response = client.post(
                "/api/messages",
                json=_activity(activity_id="1749000000010"),
                headers={"Authorization": f"Bearer {_service_token(private_key)}"},
            )
            assert not 200 <= response.status_code < 300
            lock_connection.rollback()
            monkeypatch.setattr(service.delivery_spool, "_connect", original_connect)
            with sqlite3.connect(database_path) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM deliveries"
                ).fetchone()[0]
            assert count == 0
    finally:
        lock_connection.rollback()
        lock_connection.close()
        monkeypatch.setattr(service.delivery_spool, "_connect", original_connect)
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
            assert status.json()["retained_body_bytes"] == 0
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
            assert any(
                route.path.endswith("/retry-dead") for route in service.http_app.routes
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_retry_dead_admin_route_requires_paused_holder_and_requeues_same_body(
    tmp_path,
):
    """Recover permanent failures only under the current drained pause holder.

    中文：只有当前暂停 holder 可在 active=0 时恢复 dead-letter，正文保持原样。
    """

    token_value = b"0123456789abcdef0123456789abcdef"
    navigator = AdmissionNavigator()
    service = await build_ingress_service(
        _settings(
            delivery_state_dir=tmp_path / "delivery-state",
            delivery_admin_token=token_value,
        ),
        navigator=navigator,
    )
    event = {
        "event_type": "inbound_message",
        "payload": {
            "message_id": "1749000000007",
            "conversation": {
                "vendor": "msteams",
                "account_id": BOT_APP_ID,
                "conversation_id": CONVERSATION_ID,
                "kind": "channel",
            },
            "sender_id": ACTOR_ID,
            "content": [{"text": {"text": "recover this private dead letter"}}],
        },
        "trust": {
            "version": 1,
            "provider": "teams",
            "tenant_id": TENANT_ID,
            "actor_aad_object_id": ACTOR_ID,
            "team_id": TEAM_ID,
            "channel_id": CHANNEL_ID,
            "root_thread_id": "1749000000007",
            "message_id": "1749000000007",
            "bot_app_id": BOT_APP_ID,
            "service_url": SERVICE_URL,
        },
    }
    expected_body = json.dumps(
        event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    await service.delivery_spool.enqueue(event)
    claim = await service.delivery_spool.claim_next(lease_seconds=5)
    assert claim is not None
    await service.delivery_spool.mark_dead(claim, status_code=422, code="INVALID_EVENT")
    headers = {"Authorization": f"Bearer {token_value.decode('ascii')}"}
    cas = {"holder": "manual-recovery-1", "expected_revision": 0}
    try:
        with TestClient(service.http_app) as client:
            unpaused = client.post(
                "/_internal/teams/delivery/retry-dead", headers=headers, json=cas
            )
            assert unpaused.status_code == 409

            paused = client.post(
                "/_internal/teams/delivery/pause", headers=headers, json=cas
            )
            assert paused.status_code == 200
            assert paused.json()["active_delivery_count"] == 0
            assert paused.json()["dead_count"] == 1
            assert paused.json()["retained_body_bytes"] == len(expected_body)

            retry = client.post(
                "/_internal/teams/delivery/retry-dead",
                headers=headers,
                json={"holder": "manual-recovery-1", "expected_revision": 1},
            )
            assert retry.status_code == 200
            assert retry.json()["retried_count"] == 1
            assert retry.json()["dead_count"] == 0
            assert retry.json()["queued_count"] == 1

            replay = client.post(
                "/_internal/teams/delivery/retry-dead",
                headers=headers,
                json={"holder": "manual-recovery-1", "expected_revision": 1},
            )
            assert replay.status_code == 200
            assert replay.json()["retried_count"] == 0

            resumed = client.post(
                "/_internal/teams/delivery/resume",
                headers=headers,
                json={"holder": "manual-recovery-1", "expected_revision": 1},
            )
            assert resumed.status_code == 200
            deadline = time.monotonic() + 2
            while not navigator.events and time.monotonic() < deadline:
                time.sleep(0.01)
            assert navigator.events == [event]
            while (
                await service.delivery_spool.status()
            ).delivered_count == 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert (await service.delivery_spool.status()).delivered_count == 1
            with sqlite3.connect(service.delivery_spool.database_path) as connection:
                row = connection.execute(
                    "SELECT body, state FROM deliveries"
                ).fetchone()
            assert row == (None, "delivered")
    finally:
        await service.close()
