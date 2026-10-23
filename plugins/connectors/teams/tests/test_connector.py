"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_connector.py                                                │
│  Module: tests.test_connector                                        │
│  Role: Verify scoped Teams replies and explicit delivery outcomes.   │
│                                                                      │
│  模块职责：验证 Teams 频道范围与明确的投递结果                         │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from teams_connector._generated import message_connector_pb2 as message_wire
from teams_connector.connector import TeamsConnector

BOT_APP_ID = "11111111-1111-4111-8111-111111111111"
TENANT_ID = "22222222-2222-4222-8222-222222222222"
TEAM_ID = "19:team@thread.tacv2"
CHANNEL_ID = "19:channel@thread.tacv2"
CONVERSATION_ID = "19:conversation@thread.tacv2"
SERVICE_URL = "https://smba.trafficmanager.net/amer/"


class FakeTeamsApp:
    """Capture SDK reply calls without contacting Microsoft Teams.

    中文：捕获 SDK reply 调用，不连接 Microsoft Teams。
    """

    def __init__(self, *, response_id="outbound-1", exception=None):
        self.response_id = response_id
        self.exception = exception
        self.calls = []

    async def reply(self, conversation_id, root_thread_id, text, *, service_url=None):
        """Capture the exact target and return a fabricated accepted response.

        中文：捕获精确目标并返回伪造的 accepted 响应。
        """

        self.calls.append((conversation_id, root_thread_id, text, service_url))
        if self.exception is not None:
            raise self.exception
        return SimpleNamespace(id=self.response_id)


def _config():
    """Return a private single-Team binding fixture.

    中文：返回私有单 Team binding 测试样例。
    """

    return {
        "binding_id": "teams-v1",
        "bot_app_id": BOT_APP_ID,
        "tenant_id": TENANT_ID,
        "allowed_team_ids": [TEAM_ID],
        "client_secret_env_ref": "TEAMS_BOT_APP_SECRET",
        "timeout_seconds": 10,
    }


def _request(
    *, team_id=TEAM_ID, service_url=SERVICE_URL, root_thread_id="1749000000000"
):
    """Return one canonical text reply request for the configured root thread.

    中文：返回针对已配置根线程的一条规范文本回复请求。
    """

    request = message_wire.SendMessageRequest()
    request.conversation.vendor = "msteams"
    request.conversation.account_id = BOT_APP_ID
    request.conversation.conversation_id = CONVERSATION_ID
    request.conversation.kind = message_wire.CONVERSATION_KIND_CHANNEL
    request.content.add().text.text = "Repository scan completed."
    request.reply.message_id = root_thread_id
    request.vendor_extension.vendor = "msteams"
    for name, value in (
        ("tenant_id", TENANT_ID),
        ("team_id", team_id),
        ("channel_id", CHANNEL_ID),
        ("service_url", service_url),
    ):
        fact = request.vendor_extension.facts.add()
        fact.name = name
        fact.value = value
    return request


def _invoke(connector, request):
    """Invoke the formal protobuf action and return its structural result.

    中文：调用规范 protobuf action 并返回结构化结果。
    """

    return connector.on_invoke(
        "message.connector.v1",
        "send_message",
        request.SerializeToString(deterministic=True),
        request_type_url="type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest",
    )


def test_send_message_uses_configured_bot_and_root_thread_without_network(monkeypatch):
    """Call the official SDK seam with the exact conversation and thread IDs.

    中文：通过官方 SDK seam 使用精确 conversation 与 thread ID。
    """

    monkeypatch.setenv("TEAMS_BOT_APP_SECRET", "test-secret-value-not-config")
    fake = FakeTeamsApp()
    connector = TeamsConnector(_config(), sdk_app_factory=lambda _config, _secret: fake)
    try:
        ok, result = _invoke(connector, _request())
        assert ok is True
        assert (
            result.type_url
            == "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult"
        )
        delivered = message_wire.DeliveryResult.FromString(result.value)
    finally:
        connector.on_shutdown()

    assert delivered.status == message_wire.DELIVERY_STATUS_ACCEPTED
    assert delivered.vendor_message_id == "outbound-1"
    assert fake.calls == [
        (
            CONVERSATION_ID,
            "1749000000000",
            "Repository scan completed.",
            SERVICE_URL,
        )
    ]


def test_send_message_rejects_unbound_team_before_sdk_call(monkeypatch):
    """Do not let a valid Exchange call escape the configured team allowlist.

    中文：不允许有效 Exchange 调用超出已配置 Team allowlist。
    """

    monkeypatch.setenv("TEAMS_BOT_APP_SECRET", "test-secret-value-not-config")
    fake = FakeTeamsApp()
    connector = TeamsConnector(_config(), sdk_app_factory=lambda _config, _secret: fake)
    try:
        ok, error = _invoke(connector, _request(team_id="another-team"))
    finally:
        connector.on_shutdown()

    assert ok is False
    assert error.startswith("PERMISSION_DENIED:")
    assert fake.calls == []


def test_send_message_rejects_untrusted_service_url_before_sdk_call(monkeypatch):
    """Never send bot credentials to a service URL outside the Microsoft allowlist.

    中文：不会向 Microsoft allowlist 之外的 service URL 发送 bot credential。
    """

    monkeypatch.setenv("TEAMS_BOT_APP_SECRET", "test-secret-value-not-config")
    fake = FakeTeamsApp()
    connector = TeamsConnector(_config(), sdk_app_factory=lambda _config, _secret: fake)
    try:
        ok, error = _invoke(
            connector,
            _request(service_url="https://attacker.example.invalid/teams/"),
        )
    finally:
        connector.on_shutdown()

    assert ok is False
    assert error.startswith("PERMISSION_DENIED:")
    assert "attacker.example.invalid" not in error
    assert fake.calls == []


@pytest.mark.parametrize("root_thread_id", ["١٢٣", "０", "0000"])
def test_send_message_requires_ascii_nonzero_root_thread_id(
    monkeypatch, root_thread_id
):
    """Reject IDs that Exchange's canonical route cannot represent safely.

    中文：拒绝无法由 Exchange 规范路由安全表达的根线程 ID。
    """

    monkeypatch.setenv("TEAMS_BOT_APP_SECRET", "test-secret-value-not-config")
    fake = FakeTeamsApp()
    connector = TeamsConnector(_config(), sdk_app_factory=lambda _config, _secret: fake)
    try:
        ok, error = _invoke(connector, _request(root_thread_id=root_thread_id))
    finally:
        connector.on_shutdown()

    assert ok is False
    assert error.startswith("INVALID_REQUEST:")
    assert fake.calls == []


def test_provider_auth_rejection_is_reported_without_success(monkeypatch):
    """Map a known authentication rejection to explicit DeliveryResult status.

    中文：将已知鉴权拒绝映射为明确 DeliveryResult 状态。
    """

    monkeypatch.setenv("TEAMS_BOT_APP_SECRET", "test-secret-value-not-config")
    response = httpx.Response(
        401, request=httpx.Request("POST", "https://provider.invalid")
    )
    fake = FakeTeamsApp(
        exception=httpx.HTTPStatusError(
            "unauthorized", request=response.request, response=response
        )
    )
    connector = TeamsConnector(_config(), sdk_app_factory=lambda _config, _secret: fake)
    try:
        ok, result = _invoke(connector, _request())
        delivered = message_wire.DeliveryResult.FromString(result.value)
    finally:
        connector.on_shutdown()

    assert ok is True
    assert delivered.status == message_wire.DELIVERY_STATUS_REJECTED
    assert delivered.reason == "auth_expired"


def test_unknown_provider_result_is_uncertain_not_success(monkeypatch):
    """A provider 5xx cannot be reported as accepted or blindly retried.

    中文：供应商 5xx 不能报告为 accepted，也不能盲目重试。
    """

    monkeypatch.setenv("TEAMS_BOT_APP_SECRET", "test-secret-value-not-config")
    response = httpx.Response(
        503, request=httpx.Request("POST", "https://provider.invalid")
    )
    fake = FakeTeamsApp(
        exception=httpx.HTTPStatusError(
            "temporary failure", request=response.request, response=response
        )
    )
    connector = TeamsConnector(_config(), sdk_app_factory=lambda _config, _secret: fake)
    try:
        ok, error = _invoke(connector, _request())
    finally:
        connector.on_shutdown()

    assert ok is False
    assert error.startswith("DELIVERY_UNCERTAIN:")
    assert "provider.invalid" not in error


def test_plaintext_secret_in_config_is_rejected():
    """Accept a host secret reference, never a secret value in plugin config.

    中文：接受主机 secret 引用，不接受插件配置中的 secret 明文。
    """

    config = {**_config(), "client_secret": "not-allowed"}

    with pytest.raises(Exception, match="host secret reference"):
        TeamsConnector(config)
