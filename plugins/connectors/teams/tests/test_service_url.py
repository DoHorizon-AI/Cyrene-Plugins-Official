"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_service_url.py                                              │
│  Module: tests.test_service_url                                      │
│  Role: Verify Microsoft Bot Framework service URL routing safety.     │
│                                                                      │
│  模块职责：验证 Microsoft Bot Framework service URL 路由安全             │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from microsoft_teams.apps import App

from teams_connector.service_url import ServiceUrlError, validate_bot_service_url

TENANT_ID = "22222222-2222-4222-8222-222222222222"


@pytest.mark.parametrize(
    "service_url",
    [
        "https://smba.trafficmanager.net/teams/",
        "https://smba.trafficmanager.net/amer/",
        "https://smba.trafficmanager.net/emea/",
        "https://smba.trafficmanager.net:443/teams/",
        f"https://smba.trafficmanager.net/amer/{TENANT_ID}/",
        "https://smba.infra.teams.microsoft.com/teams/",
    ],
)
def test_known_bot_framework_routes_are_preserved_exactly(service_url):
    """Preserve global, regional, and tenant-specific routing paths.

    中文：原样保留 global、regional 与 tenant-specific 路由路径。
    """

    assert validate_bot_service_url(service_url, tenant_id=TENANT_ID) == service_url


@pytest.mark.parametrize(
    "service_url",
    [
        "http://smba.trafficmanager.net/amer/",
        "https://attacker.example.invalid/teams/",
        "https://user@smba.trafficmanager.net/teams/",
        "https://smba.trafficmanager.net:8443/teams/",
        "https://smba.trafficmanager.net:/teams/",
        "https://smba.trafficmanager.net/teams/?token=private",
        "https://smba.trafficmanager.net/teams/?",
        "https://smba.trafficmanager.net/teams/#fragment",
        "https://smba.trafficmanager.net/teams/#",
        "https://smba.trafficmanager.net/amer/%2e%2e/",
        "https://smba.trafficmanager.net/amer/../teams/",
        "https://smba.trafficmanager.net/amer//",
        "https://smba.infra.teams.microsoft.com/amer/",
    ],
)
def test_untrusted_or_unclean_service_urls_are_rejected(service_url):
    """Reject non-HTTPS hosts, credentials, query/fragment, and unsafe paths.

    中文：拒绝非 HTTPS host、凭据、query/fragment 与不安全路径。
    """

    with pytest.raises(ServiceUrlError):
        validate_bot_service_url(service_url, tenant_id=TENANT_ID)


def test_regional_tenant_segment_must_match_verified_tenant():
    """Do not route a message through a region URL bound to another tenant.

    中文：不允许使用绑定到另一租户的区域 service URL。
    """

    other_tenant = "33333333-3333-4333-8333-333333333333"
    value = f"https://smba.trafficmanager.net/amer/{other_tenant}/"

    with pytest.raises(ServiceUrlError, match="does not match"):
        validate_bot_service_url(value, tenant_id=TENANT_ID)


@pytest.mark.asyncio
async def test_official_sdk_default_and_explicit_regional_reply_routing(monkeypatch):
    """Prove `App.reply` uses the app default only when no service URL is passed.

    The Bot Framework SDK defaults to the public `/teams` base. Supplying the
    validated inbound region URL must override it and preserve its route path.

    中文：验证未传 service URL 时 SDK 使用 app 默认 `/teams`，传入已验证区域 URL 时保留
    该区域路径并覆盖默认值。
    """

    app_module = importlib.import_module("microsoft_teams.apps.app")
    captured = []

    async def send_without_network(api, _activity, reference, *, agentic_identity=None):
        captured.append((reference.conversation.id, reference.service_url))
        return SimpleNamespace(id="synthetic-sdk-send-result")

    monkeypatch.setattr(app_module, "send_or_update_activity", send_without_network)
    app = App(
        client_id="11111111-1111-4111-8111-111111111111",
        client_secret="synthetic-test-secret",
        tenant_id=TENANT_ID,
        dangerously_allow_unauthenticated_requests=False,
        telemetry={"agent365": False},
    )
    app._initialized = True
    regional_url = f"https://smba.trafficmanager.net/amer/{TENANT_ID}/"
    root_thread_id = "1749000000000"
    try:
        await app.reply("conversation-1", root_thread_id, "default route")
        await app.reply(
            "conversation-1",
            root_thread_id,
            "regional route",
            service_url=validate_bot_service_url(regional_url, tenant_id=TENANT_ID),
        )
    finally:
        await app.api.http.http.aclose()
        await app.http_client.http.aclose()

    assert app.api.service_url == "https://smba.trafficmanager.net/teams"
    assert captured[0][1] == "https://smba.trafficmanager.net/teams"
    assert captured[1][1] == regional_url
    assert f";messageid={root_thread_id}" in captured[1][0]
