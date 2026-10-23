"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_config.py                                                   │
│  Module: tests.test_config                                           │
│  Role: Verify fail-closed sidecar and host-secret configuration.     │
│                                                                      │
│  模块职责：验证侧车 fail-closed 与主机 secret 配置                     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json

import pytest

from teams_connector.config import ConfigurationError, IngressSettings

BOT_APP_ID = "11111111-1111-4111-8111-111111111111"
TENANT_ID = "22222222-2222-4222-8222-222222222222"
TEAM_ID = "19:approved-team@thread.tacv2"


def _environment(monkeypatch, key_path, **overrides):
    """Set the required private settings for one sidecar configuration.

    中文：为一次侧车配置设置必需的私有参数。
    """

    values = {
        "TEAMS_BOT_APP_ID": BOT_APP_ID,
        "TEAMS_TENANT_ID": TENANT_ID,
        "TEAMS_ALLOWED_TEAM_IDS": json.dumps([TEAM_ID]),
        "NAVIGATOR_INTERNAL_BASE_URL": "http://navigator:8080",
        "TEAMS_NAVIGATOR_HMAC_KEY_FILE": str(key_path),
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_secret_is_loaded_from_file_reference_and_team_scope_is_explicit(
    monkeypatch, tmp_path
):
    """Load HMAC bytes from a mounted file and require the approved Team ID.

    中文：从挂载文件读取 HMAC bytes，并要求显式配置允许 Team ID。
    """

    secret_path = tmp_path / "navigator-hmac"
    secret_path.write_bytes(b"private-test-key-material-at-least-32-bytes\n")
    _environment(monkeypatch, secret_path)

    settings = IngressSettings.from_environment()

    assert settings.navigator_hmac_key == b"private-test-key-material-at-least-32-bytes"
    assert settings.allowed_team_ids == frozenset({TEAM_ID})
    assert settings.port == 3978
    assert not hasattr(settings, "allowed_actor_aad_object_ids")


def test_empty_team_allowlist_fails_closed(monkeypatch, tmp_path):
    """An unscoped sidecar cannot start with an empty team allowlist.

    中文：没有团队范围的侧车不能以空 allowlist 启动。
    """

    secret_path = tmp_path / "navigator-hmac"
    secret_path.write_bytes(b"private-test-key-material-at-least-32-bytes")
    _environment(monkeypatch, secret_path, TEAMS_ALLOWED_TEAM_IDS="[]")

    with pytest.raises(ConfigurationError, match="empty or invalid"):
        IngressSettings.from_environment()


@pytest.mark.parametrize(
    "url",
    [
        "http://navigator.example.invalid:8080",
        "https://user:password@navigator.example.invalid",
        "https://navigator.example.invalid/callback?token=secret",
    ],
)
def test_public_plain_http_credentials_and_path_urls_are_rejected(
    monkeypatch, tmp_path, url
):
    """Require TLS for dotted DNS and do not accept credentials/path secrets.

    中文：点分 DNS 要求 TLS，并拒绝凭据或带敏感路径的 URL。
    """

    secret_path = tmp_path / "navigator-hmac"
    secret_path.write_bytes(b"private-test-key-material-at-least-32-bytes")
    _environment(monkeypatch, secret_path, NAVIGATOR_INTERNAL_BASE_URL=url)

    with pytest.raises(ConfigurationError):
        IngressSettings.from_environment()
