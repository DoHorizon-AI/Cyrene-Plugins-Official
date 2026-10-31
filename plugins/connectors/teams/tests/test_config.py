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
    assert settings.personal_chat_enabled is False
    assert settings.port == 3978
    assert not hasattr(settings, "allowed_actor_aad_object_ids")
    assert settings.delivery_state_dir is None
    assert settings.delivery_admin_token is None


def test_personal_chat_flag_requires_explicit_boolean_value(monkeypatch, tmp_path):
    """Keep private chat disabled unless the host opts in explicitly.

    中文：只有主机显式 opt-in 才启用个人聊天。
    """

    secret_path = tmp_path / "navigator-hmac"
    secret_path.write_bytes(b"private-test-key-material-at-least-32-bytes")
    _environment(monkeypatch, secret_path, TEAMS_PERSONAL_CHAT_ENABLED="true")
    assert IngressSettings.from_environment().personal_chat_enabled is True

    _environment(monkeypatch, secret_path, TEAMS_PERSONAL_CHAT_ENABLED="enabled")
    with pytest.raises(ConfigurationError, match="must be true or false"):
        IngressSettings.from_environment()


def test_optional_durable_delivery_requires_private_independent_admin_token(
    monkeypatch, tmp_path
):
    """Enable the spool only when state and its separate private token are set.

    中文：只有 state 与独立私有 token 同时配置时才启用持久队列。
    """

    hmac_path = tmp_path / "navigator-hmac"
    hmac_path.write_bytes(b"private-test-key-material-at-least-32-bytes")
    admin_path = tmp_path / "delivery-admin"
    admin_path.write_bytes(b"0123456789abcdef0123456789abcdef\n")
    admin_path.chmod(0o400)
    state_dir = tmp_path / "delivery-state"
    _environment(
        monkeypatch,
        hmac_path,
        TEAMS_NAVIGATOR_DELIVERY_STATE_DIR=str(state_dir),
        TEAMS_DELIVERY_ADMIN_TOKEN_FILE=str(admin_path),
    )

    settings = IngressSettings.from_environment()

    assert settings.delivery_state_dir == state_dir
    assert settings.delivery_admin_token == b"0123456789abcdef0123456789abcdef"
    assert settings.navigator_hmac_key != settings.delivery_admin_token
    assert settings.delivery_admin_token.decode("ascii") not in repr(settings)


@pytest.mark.parametrize(
    "overrides",
    [
        {"TEAMS_NAVIGATOR_DELIVERY_STATE_DIR": "/var/lib/cyrene/teams-delivery"},
        {"TEAMS_DELIVERY_ADMIN_TOKEN_FILE": "/run/secrets/teams-delivery-admin-token"},
    ],
)
def test_partial_durable_delivery_configuration_fails_closed(
    monkeypatch, tmp_path, overrides
):
    """Never silently fall back to direct mode after partial spool config.

    中文：部分 spool 配置不能静默退回 direct 模式。
    """

    secret_path = tmp_path / "navigator-hmac"
    secret_path.write_bytes(b"private-test-key-material-at-least-32-bytes")
    _environment(monkeypatch, secret_path, **overrides)

    with pytest.raises(ConfigurationError, match="delivery"):
        IngressSettings.from_environment()


def test_admin_token_file_rejects_group_readable_mode(monkeypatch, tmp_path):
    """Require owner-only mode for the separate internal admin token.

    中文：内部管理 token 必须使用 owner-only 文件权限。
    """

    hmac_path = tmp_path / "navigator-hmac"
    hmac_path.write_bytes(b"private-test-key-material-at-least-32-bytes")
    admin_path = tmp_path / "delivery-admin"
    admin_path.write_bytes(b"0123456789abcdef0123456789abcdef")
    admin_path.chmod(0o640)
    _environment(
        monkeypatch,
        hmac_path,
        TEAMS_NAVIGATOR_DELIVERY_STATE_DIR=str(tmp_path / "delivery-state"),
        TEAMS_DELIVERY_ADMIN_TOKEN_FILE=str(admin_path),
    )

    with pytest.raises(ConfigurationError, match="owner-only"):
        IngressSettings.from_environment()


def test_admin_token_file_rejects_symlink(monkeypatch, tmp_path):
    """A mounted admin credential may not be redirected through a symlink.

    中文：挂载的 admin credential 不能经由符号链接重定向。
    """

    hmac_path = tmp_path / "navigator-hmac"
    hmac_path.write_bytes(b"private-test-key-material-at-least-32-bytes")
    actual_path = tmp_path / "actual-admin-token"
    actual_path.write_bytes(b"0123456789abcdef0123456789abcdef")
    actual_path.chmod(0o400)
    symlink_path = tmp_path / "delivery-admin"
    symlink_path.symlink_to(actual_path)
    _environment(
        monkeypatch,
        hmac_path,
        TEAMS_NAVIGATOR_DELIVERY_STATE_DIR=str(tmp_path / "delivery-state"),
        TEAMS_DELIVERY_ADMIN_TOKEN_FILE=str(symlink_path),
    )

    with pytest.raises(ConfigurationError, match="could not be read"):
        IngressSettings.from_environment()


def test_delivery_state_directory_must_be_absolute(monkeypatch, tmp_path):
    """Reject process-working-directory dependent durability paths.

    中文：持久 state 路径不能依赖进程工作目录。
    """

    hmac_path = tmp_path / "navigator-hmac"
    hmac_path.write_bytes(b"private-test-key-material-at-least-32-bytes")
    admin_path = tmp_path / "delivery-admin"
    admin_path.write_bytes(b"0123456789abcdef0123456789abcdef")
    admin_path.chmod(0o400)
    _environment(
        monkeypatch,
        hmac_path,
        TEAMS_NAVIGATOR_DELIVERY_STATE_DIR="relative-state",
        TEAMS_DELIVERY_ADMIN_TOKEN_FILE=str(admin_path),
    )

    with pytest.raises(ConfigurationError, match="must be absolute"):
        IngressSettings.from_environment()


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
