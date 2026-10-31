"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_auth.py                                                     │
│  Module: tests.test_auth                                             │
│  Role: Verify SDK JWT validation and sidecar fail-closed behavior.   │
│                                                                      │
│  模块职责：验证 SDK JWT 校验与侧车 fail-closed 行为                    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from microsoft_teams.api.auth.cloud_environment import PUBLIC
from microsoft_teams.apps.auth.token_validator import InboundActivityTokenValidator

from teams_connector.config import IngressSettings
from teams_connector.ingress import build_ingress_service

BOT_APP_ID = "11111111-1111-4111-8111-111111111111"
TENANT_ID = "22222222-2222-4222-8222-222222222222"
TEAM_ID = "19:team@thread.tacv2"


class _NavigatorStub:
    """A no-network Navigator seam for ASGI authentication tests.

    中文：用于 ASGI 鉴权测试的无网络 Navigator seam。
    """

    async def submit(self, _event):
        """Fail the test if authentication unexpectedly reaches the host seam.

        中文：若鉴权测试意外到达 host seam，则让测试失败。
        """

        raise AssertionError("unauthenticated activity reached Navigator")

    async def aclose(self):
        """Match the Navigator client close contract.

        中文：符合 Navigator client 关闭契约。
        """


def _settings():
    """Return private test settings with no real endpoints or credentials.

    中文：返回不含真实 endpoint 或凭据的私有测试配置。
    """

    return IngressSettings(
        bot_app_id=BOT_APP_ID,
        tenant_id=TENANT_ID,
        allowed_team_ids=frozenset({TEAM_ID}),
        navigator_base_url="http://navigator",
        navigator_hmac_key_id="teams-v1",
        navigator_hmac_key=b"a-fabricated-test-key-with-at-least-32-bytes",
    )


def _keys(monkeypatch):
    """Install a deterministic JWKS seam backed by a generated test RSA key.

    中文：使用生成的测试 RSA key 安装确定性的 JWKS seam。
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
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
    return private_key


def _token(private_key, *, audience=BOT_APP_ID, issuer=None, expires=None):
    """Sign one fabricated Bot Framework service token.

    中文：签发一个伪造的 Bot Framework service token。
    """

    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer or PUBLIC.token_issuer,
            "aud": audience,
            "iat": now - 1,
            "exp": expires if expires is not None else now + 120,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "fabricated-test-key"},
    )


@pytest.mark.asyncio
async def test_official_sdk_accepts_matching_signature_audience_issuer(monkeypatch):
    """Exercise Microsoft's current inbound activity validator with a valid token.

    中文：使用有效 token 测试微软当前 inbound activity validator。
    """

    private_key = _keys(monkeypatch)
    validator = InboundActivityTokenValidator(BOT_APP_ID)

    claims = await validator.validate_token(_token(private_key))

    assert claims["aud"] == BOT_APP_ID
    assert claims["iss"] == PUBLIC.token_issuer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token_options", "use_other_key"),
    [
        ({"audience": "99999999-9999-4999-8999-999999999999"}, False),
        ({"issuer": "https://attacker.example.invalid/issuer"}, False),
        ({"expires": 1}, False),
        ({}, True),
    ],
)
async def test_official_sdk_rejects_bad_audience_issuer_expiry_and_forged_signature(
    monkeypatch, token_options, use_other_key
):
    """Reject token claim mismatches, expired tokens, and forged signatures.

    中文：拒绝 audience/issuer 错配、过期 token 与伪造签名。
    """

    trusted_private_key = _keys(monkeypatch)
    signing_key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if use_other_key
        else trusted_private_key
    )
    validator = InboundActivityTokenValidator(BOT_APP_ID)

    with pytest.raises(jwt.InvalidTokenError):
        await validator.validate_token(_token(signing_key, **token_options))


@pytest.mark.asyncio
async def test_public_callback_rejects_missing_and_malformed_authorization():
    """The actual SDK HTTP route rejects absent and malformed bearer tokens.

    中文：真实 SDK HTTP route 会拒绝缺少或格式错误的 bearer token。
    """

    service = await build_ingress_service(_settings(), navigator=_NavigatorStub())
    try:
        with TestClient(service.http_app) as client:
            missing = client.post("/api/messages", json={"type": "message"})
            malformed = client.post(
                "/api/messages",
                json={"type": "message"},
                headers={"Authorization": "Bearer not-a-jwt"},
            )
            assert client.get("/health/live").json() == {"status": "live"}
            assert client.get("/health/ready").json() == {"status": "ready"}

        assert missing.status_code == 401
        assert malformed.status_code == 401
    finally:
        await service.close()
