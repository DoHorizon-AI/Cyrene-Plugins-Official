"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_wecom_authorization.py                                     │
│  Package: wecom_connector.tests                                     │
│  Role: Verify signed authorization health and renewal boundaries.   │
│                                                                     │
│  模块职责：验证签名授权健康检查与续期边界。                             │
│  · 仅使用本地 CLI 子进程和厂商协议 stub                                  │
│  · 不模拟成功登录，不保存原始租户或 actor 字段                           │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import queue
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import cyrene_integration_contracts as contracts
import pytest

from wecom_connector import authorization as authorization_module
from wecom_connector.authorization import (
    AuthorizationSessionError,
    AuthorizationTrustConfig,
    WeComAuthorizationSessions,
    owner_fingerprint,
    profile_authorization_url,
)
from wecom_connector.cli import WeComCliClient, WeComCliError, WeComCliStatus
from wecom_connector.connector import ConnectorError, WeComInstanceConfig
from wecom_connector.tool_provider import WeComToolProvider

_QR_URL = (
    "https://work.weixin.qq.com/ai/qc/gen"
    "?source=wecom_cli_external&scode=abcdefgh12345678"
)


class PendingProcess:
    """Pending subprocess stub that exposes official CLI output without login."""

    def __init__(self, *events: tuple[str, str | None]) -> None:
        self.authorization_events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        for event in events:
            self.authorization_events.put(event)
        self._finished = threading.Event()

    def poll(self) -> int | None:
        return None if not self._finished.is_set() else -15

    def terminate(self) -> None:
        self._finished.set()

    def kill(self) -> None:
        self._finished.set()

    def wait(self, timeout: float | None = None) -> int:
        if not self._finished.wait(timeout):
            raise TimeoutError("pending CLI process is still active")
        return -15

    @property
    def terminated(self) -> bool:
        return self._finished.is_set()


def _cli_status(auth_status: str) -> WeComCliStatus:
    return WeComCliStatus(
        available=True,
        version="1.3.4",
        auth_status=auth_status,
        executable_path="wecom-cli",
    )


def _owner() -> tuple[str, SimpleNamespace]:
    caller = SimpleNamespace(
        caller_workload="cyrene-exchange",
        audience="cyrene.connectors.wecom",
        issuer="trusted-issuer",
        subject="operator-subject-private",
        organization_id="organization-private",
        workspace_id="workspace-private",
    )
    return owner_fingerprint(caller, "binding-private"), caller


def _trust_config(key: bytes) -> AuthorizationTrustConfig:
    return AuthorizationTrustConfig(
        authorization_ref="configured-authorization-ref",
        key=key,
        caller_workload="cyrene-exchange",
        audience="cyrene.connectors.wecom",
        issuer="cyrene-exchange",
        organization_id="configured-organization",
        workspace_id="configured-workspace",
        operator_subjects=("authorized-operator",),
    )


def _signed_envelope(
    key: bytes,
    *,
    tool_id: str,
    arguments: dict[str, object],
    subject: str = "authorized-operator",
    request_id: str = "signed-request",
) -> dict[str, object]:
    context = contracts.sign_context(
        method=tool_id,
        operation_id=contracts.TOOL_OPERATIONS[tool_id],
        payload=arguments,
        caller_workload="cyrene-exchange",
        audience="cyrene.connectors.wecom",
        issuer="cyrene-exchange",
        subject=subject,
        organization_id="configured-organization",
        workspace_id="configured-workspace",
        binding_id="wecom-signed",
        request_id=request_id,
        key=key,
    )
    return {
        "caller_context": contracts.serialize_trusted_caller_context(context),
        "arguments": arguments,
    }


def test_cli_only_activation_needs_no_app_or_bot_secrets(tmp_path: Path) -> None:
    config = WeComInstanceConfig.from_mapping(
        {
            "binding_id": "wecom-cli-only",
            "mode": "cli",
            "cli_enabled": True,
            "cli_config_dir": str(tmp_path / "profile"),
        }
    )

    assert config.mode == "cli"
    assert config.cli_enabled is True
    assert config.corp_id is None
    assert config.corp_secret is None
    assert config.agent_id is None
    assert config.bot_id is None
    assert config.bot_secret is None

    with pytest.raises(ConnectorError, match="cli_enabled=true"):
        WeComInstanceConfig.from_mapping(
            {
                "binding_id": "wecom-cli-only",
                "mode": "cli",
                "cli_enabled": False,
                "cli_config_dir": str(tmp_path / "profile"),
            }
        )


def test_signed_health_tool_checks_exact_binding_context_and_typed_result(
    tmp_path: Path,
) -> None:
    key = bytes.fromhex("cd" * 32)
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("authorized")
    cli.get_profile_identity_fingerprint.return_value = "profile-fingerprint"
    cli.execute.return_value = {"sessions": []}
    provider = WeComToolProvider(
        binding_id="wecom-signed",
        cli_client=cli,
        cli_enabled=True,
        authorization_config=_trust_config(key),
        authorization_profile_dir=str(tmp_path / "profile"),
    )
    arguments = {"authorization_ref": "configured-authorization-ref"}
    envelope = _signed_envelope(
        key,
        tool_id="wecom_authorization_health",
        arguments=arguments,
    )
    result = provider.call_tool(
        {
            "provider_tool_id": "wecom_authorization_health",
            "arguments_json": json.dumps(envelope),
        }
    )

    assert result["is_error"] is False
    health = json.loads(result["content_json"])
    assert health["authorization_ref"] == "configured-authorization-ref"
    assert health["authorization_state"] == "VALID"
    assert health["expires_at"] is None
    cli.execute.assert_called_once_with("message", args=["aibot", "sessions", "list"])
    provider.close()


def test_signed_health_rejects_changed_payload_and_extra_context_fields(
    tmp_path: Path,
) -> None:
    key = bytes.fromhex("ef" * 32)
    cli = MagicMock(spec=WeComCliClient)
    provider = WeComToolProvider(
        binding_id="wecom-signed",
        cli_client=cli,
        cli_enabled=True,
        authorization_config=_trust_config(key),
        authorization_profile_dir=str(tmp_path / "profile"),
    )
    signed_arguments = {"authorization_ref": "configured-authorization-ref"}
    envelope = _signed_envelope(
        key,
        tool_id="wecom_authorization_health",
        arguments=signed_arguments,
    )
    changed_payload = {
        **envelope,
        "arguments": {"authorization_ref": "other-authorization"},
    }
    changed_context = {
        **envelope,
        "caller_context": {
            **envelope["caller_context"],
            "unexpected": "must be rejected",
        },
    }

    for request in (changed_payload, changed_context):
        result = provider.call_tool(
            {
                "provider_tool_id": "wecom_authorization_health",
                "arguments_json": json.dumps(request),
            }
        )
        assert result["is_error"] is True
        assert json.loads(result["content_json"])["code"] == ("caller_context_invalid")
    cli.get_status.assert_not_called()
    cli.execute.assert_not_called()
    provider.close()


def test_signed_renewal_start_requires_configured_operator_subject(
    tmp_path: Path,
) -> None:
    key = bytes.fromhex("12" * 32)
    cli = MagicMock(spec=WeComCliClient)
    provider = WeComToolProvider(
        binding_id="wecom-signed",
        cli_client=cli,
        cli_enabled=True,
        authorization_config=_trust_config(key),
        authorization_profile_dir=str(tmp_path / "profile"),
    )
    arguments = {"authorization_ref": "configured-authorization-ref"}
    envelope = _signed_envelope(
        key,
        tool_id="wecom_reauthorization_start",
        arguments=arguments,
        subject="unapproved-operator",
    )
    result = provider.call_tool(
        {
            "provider_tool_id": "wecom_reauthorization_start",
            "arguments_json": json.dumps(envelope),
        }
    )

    assert result["is_error"] is True
    assert json.loads(result["content_json"])["code"] == "operator_not_authorized"
    cli.get_status.assert_not_called()
    cli.start_auth_init.assert_not_called()
    provider.close()


def test_signed_start_retry_returns_the_same_pending_session_without_restart(
    tmp_path: Path,
) -> None:
    key = bytes.fromhex("34" * 32)
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("unauthorized")
    pending = PendingProcess(("line", f"Scan this page: {_QR_URL}"))
    cli.start_auth_init.return_value = pending
    provider = WeComToolProvider(
        binding_id="wecom-signed",
        cli_client=cli,
        cli_enabled=True,
        authorization_config=_trust_config(key),
        authorization_profile_dir=str(tmp_path / "profile"),
    )
    arguments = {"authorization_ref": "configured-authorization-ref"}
    envelope = _signed_envelope(
        key,
        tool_id="wecom_reauthorization_start",
        arguments=arguments,
        request_id="exchange-durable-session-ref",
    )
    request = {
        "provider_tool_id": "wecom_reauthorization_start",
        "arguments_json": json.dumps(envelope),
    }

    first = provider.call_tool(request)
    retry = provider.call_tool(request)

    assert first["is_error"] is False, first["content_json"]
    assert retry["is_error"] is False, retry["content_json"]
    first_session = json.loads(first["content_json"])
    retry_session = json.loads(retry["content_json"])
    assert first_session["session_ref"] == retry_session["session_ref"]
    assert first_session["state"] == "WAITING_USER"
    assert first_session["authorization_url"] == _QR_URL
    cli.get_status.assert_called_once()
    cli.start_auth_init.assert_called_once()

    provider.close()
    assert pending.terminated is True


def test_signed_status_can_recover_by_original_start_request_id(
    tmp_path: Path,
) -> None:
    key = bytes.fromhex("56" * 32)
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("unauthorized")
    pending = PendingProcess(("line", f"Scan this page: {_QR_URL}"))
    cli.start_auth_init.return_value = pending
    provider = WeComToolProvider(
        binding_id="wecom-signed",
        cli_client=cli,
        cli_enabled=True,
        authorization_config=_trust_config(key),
        authorization_profile_dir=str(tmp_path / "profile"),
    )
    arguments = {"authorization_ref": "configured-authorization-ref"}
    start_request_id = "exchange-session-ref-20261014"
    started = provider.call_tool(
        {
            "provider_tool_id": "wecom_reauthorization_start",
            "arguments_json": json.dumps(
                _signed_envelope(
                    key,
                    tool_id="wecom_reauthorization_start",
                    arguments=arguments,
                    request_id=start_request_id,
                )
            ),
        }
    )
    original = json.loads(started["content_json"])
    status_arguments = {"session_ref": start_request_id}
    polled = provider.call_tool(
        {
            "provider_tool_id": "wecom_reauthorization_status",
            "arguments_json": json.dumps(
                _signed_envelope(
                    key,
                    tool_id="wecom_reauthorization_status",
                    arguments=status_arguments,
                    request_id="poll-request",
                )
            ),
        }
    )

    assert started["is_error"] is False
    assert polled["is_error"] is False
    recovered = json.loads(polled["content_json"])
    assert recovered["session_ref"] == original["session_ref"]
    assert recovered["state"] == "WAITING_USER"
    assert recovered["authorization_url"] == _QR_URL
    assert cli.start_auth_init.call_count == 1

    provider.close()


def test_status_alias_recovers_lost_start_after_restart_and_stays_actor_scoped(
    tmp_path: Path,
) -> None:
    key = bytes.fromhex("78" * 32)
    profile_dir = tmp_path / "profile"
    start_cli = MagicMock(spec=WeComCliClient)
    start_cli.get_status.return_value = _cli_status("unauthorized")
    start_process = PendingProcess(("line", f"Scan this page: {_QR_URL}"))
    start_cli.start_auth_init.return_value = start_process
    original_provider = WeComToolProvider(
        binding_id="wecom-signed",
        cli_client=start_cli,
        cli_enabled=True,
        authorization_config=_trust_config(key),
        authorization_profile_dir=str(profile_dir),
    )
    authorization_ref = "configured-authorization-ref"
    start_request_id = "exchange-recovery-ref-20261014"
    started = original_provider.call_tool(
        {
            "provider_tool_id": "wecom_reauthorization_start",
            "arguments_json": json.dumps(
                _signed_envelope(
                    key,
                    tool_id="wecom_reauthorization_start",
                    arguments={"authorization_ref": authorization_ref},
                    request_id=start_request_id,
                )
            ),
        }
    )
    assert started["is_error"] is False
    expected_session_ref = json.loads(started["content_json"])["session_ref"]

    restarted_cli = MagicMock(spec=WeComCliClient)
    restarted_cli.get_status.return_value = _cli_status("unauthorized")
    restarted_provider = WeComToolProvider(
        binding_id="wecom-signed",
        cli_client=restarted_cli,
        cli_enabled=True,
        authorization_config=_trust_config(key),
        authorization_profile_dir=str(profile_dir),
    )
    recovered = restarted_provider.call_tool(
        {
            "provider_tool_id": "wecom_reauthorization_status",
            "arguments_json": json.dumps(
                _signed_envelope(
                    key,
                    tool_id="wecom_reauthorization_status",
                    arguments={"session_ref": start_request_id},
                    request_id="recovery-poll",
                )
            ),
        }
    )

    assert recovered["is_error"] is False
    recovery_session = json.loads(recovered["content_json"])
    assert recovery_session["session_ref"] == expected_session_ref
    assert recovery_session["state"] == "VERIFYING"
    assert recovery_session["authorization_url"] == _QR_URL
    restarted_cli.start_auth_init.assert_not_called()

    unauthorized_poll = restarted_provider.call_tool(
        {
            "provider_tool_id": "wecom_reauthorization_status",
            "arguments_json": json.dumps(
                _signed_envelope(
                    key,
                    tool_id="wecom_reauthorization_status",
                    arguments={"session_ref": start_request_id},
                    subject="different-operator",
                    request_id="other-actor-poll",
                )
            ),
        }
    )
    assert unauthorized_poll["is_error"] is True
    assert json.loads(unauthorized_poll["content_json"])["code"] == (
        "session_not_found"
    )
    restarted_cli.get_status.assert_called_once()

    original_provider.close()
    restarted_provider.close()
    assert start_process.terminated is True


def test_authorization_binding_loads_only_a_32_byte_hex_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_hex = "ab" * 32
    monkeypatch.setenv("WECOM_AUTHORIZATION_CONTEXT_KEY", key_hex)
    config = WeComInstanceConfig.from_mapping(
        {
            "binding_id": "wecom-cli-only",
            "mode": "cli",
            "cli_enabled": True,
            "cli_config_dir": str(tmp_path / "profile"),
            "authorization": {
                "enabled": True,
                "authorization_ref": "configured-auth-ref",
                "caller_workload": "cyrene-exchange",
                "audience": "cyrene.connectors.wecom",
                "issuer": "cyrene-exchange",
                "organization_id": "configured-organization",
                "workspace_id": "configured-workspace",
                "operator_subjects": ["configured-operator"],
            },
        }
    )

    assert config.authorization is not None
    assert config.authorization.key == bytes.fromhex(key_hex)
    assert config.authorization.operator_subjects == ("configured-operator",)


def test_authorization_binding_rejects_missing_verification_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WECOM_AUTHORIZATION_CONTEXT_KEY", raising=False)

    with pytest.raises(ConnectorError, match="32-byte hex"):
        WeComInstanceConfig.from_mapping(
            {
                "binding_id": "wecom-cli-only",
                "mode": "cli",
                "cli_enabled": True,
                "cli_config_dir": str(tmp_path / "profile"),
                "authorization": {
                    "enabled": True,
                    "authorization_ref": "configured-auth-ref",
                    "caller_workload": "cyrene-exchange",
                    "audience": "cyrene.connectors.wecom",
                    "issuer": "cyrene-exchange",
                    "organization_id": "configured-organization",
                    "workspace_id": "configured-workspace",
                    "operator_subjects": ["configured-operator"],
                },
            }
        )


def test_health_proves_only_the_fixed_read_probe_and_discards_response_data(
    tmp_path: Path,
) -> None:
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("authorized")
    cli.get_profile_identity_fingerprint.return_value = "profile-fingerprint"
    cli.execute.return_value = {
        "sessions": [{"id": "tenant-session-private", "title": "private"}]
    }
    sessions = WeComAuthorizationSessions(
        "binding-private",
        cli,
        cli_enabled=True,
        profile_dir=tmp_path / "profile",
    )

    health = sessions.health()

    assert health["authorization_state"] == "VALID"
    assert health["login_state"] == "PRESENT"
    assert health["expires_at"] is None
    assert health["capabilities"][0]["capability_id"] == (
        "wecom.message.aibot.sessions.list"
    )
    assert health["capabilities"][0]["state"] == "VALID"
    cli.execute.assert_called_once_with("message", args=["aibot", "sessions", "list"])
    assert "tenant-session-private" not in json.dumps(health, default=str)
    sessions.close()


@pytest.mark.parametrize(
    ("payload", "expected_state", "expected_reason"),
    [
        (
            {"errcode": 850001, "errmsg": "invalid api key"},
            "PERMISSION_DENIED",
            "permission_denied",
        ),
        (
            {"errcode": 850003, "errmsg": "authorization expired"},
            "REAUTHORIZATION_REQUIRED",
            "authorization_expired",
        ),
        (
            {"error": {"code": 853004, "message": "cli token expired"}},
            "REAUTHORIZATION_REQUIRED",
            "cli_token_expired",
        ),
    ],
)
def test_health_classifies_confirmed_vendor_auth_errors_without_leaking_body(
    tmp_path: Path,
    payload: dict[str, object],
    expected_state: str,
    expected_reason: str,
) -> None:
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("authorized")
    cli.get_profile_identity_fingerprint.return_value = "profile-fingerprint"
    cli.execute.side_effect = WeComCliError(
        "vendor failure",
        stdout=json.dumps(
            {**payload, "tenant": "tenant-private", "member": "member-private"}
        ),
    )
    sessions = WeComAuthorizationSessions(
        "binding-private",
        cli,
        cli_enabled=True,
        profile_dir=tmp_path / "profile",
    )

    health = sessions.health()

    assert health["authorization_state"] == expected_state
    assert health["expires_at"] is None
    assert health["capabilities"][0]["reason_code"] == expected_reason
    assert "tenant-private" not in json.dumps(health, default=str)
    assert "member-private" not in json.dumps(health, default=str)
    sessions.close()


def test_network_and_unclassified_errors_remain_unknown(tmp_path: Path) -> None:
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("authorized")
    cli.get_profile_identity_fingerprint.return_value = "profile-fingerprint"
    cli.execute.side_effect = WeComCliError(
        "transport failed",
        stderr="tenant-private transport detail",
    )
    sessions = WeComAuthorizationSessions(
        "binding-private",
        cli,
        cli_enabled=True,
        profile_dir=tmp_path / "profile",
    )

    health = sessions.health()

    assert health["authorization_state"] == "UNKNOWN"
    assert health["capabilities"][0]["state"] == "UNKNOWN"
    assert health["expires_at"] is None
    assert "tenant-private" not in json.dumps(health, default=str)
    sessions.close()


def test_vendor_authorization_url_requires_exact_official_origin_and_shape() -> None:
    assert profile_authorization_url(f"Open this page: {_QR_URL}") == _QR_URL
    assert (
        profile_authorization_url(
            "https://work.weixin.qq.com.evil.example/ai/qc/gen"
            "?source=wecom_cli_external&scode=abcdefgh"
        )
        is None
    )
    assert (
        profile_authorization_url(
            "https://work.weixin.qq.com/ai/qc/gen?source=other&scode=abcdefgh"
        )
        is None
    )
    assert (
        profile_authorization_url(
            "https://user@work.weixin.qq.com/ai/qc/gen"
            "?source=wecom_cli_external&scode=abcdefgh"
        )
        is None
    )
    assert (
        profile_authorization_url(
            "https://work.weixin.qq.com/ai/qc/gen"
            "?source=wecom_cli_external&scode=abcdefgh&redirect=https://evil.example"
        )
        is None
    )


def test_existing_profile_without_official_grant_url_never_runs_auth_init(
    tmp_path: Path,
) -> None:
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("authorized")
    cli.get_profile_identity_fingerprint.return_value = "profile-fingerprint"
    cli.execute.side_effect = WeComCliError(
        "permission denied",
        stdout=json.dumps({"errcode": 850001, "errmsg": "invalid api key"}),
    )
    sessions = WeComAuthorizationSessions(
        "binding-private",
        cli,
        cli_enabled=True,
        profile_dir=tmp_path / "profile",
    )
    owner_hash, _ = _owner()

    with pytest.raises(
        AuthorizationSessionError, match="official_grant_url_unavailable"
    ):
        sessions.start(
            owner_hash,
            idempotency_key="request-key-1",
            payload_hash="payload-hash",
            request_alias="request-alias-1",
        )

    cli.start_auth_init.assert_not_called()
    sessions.close()


def test_supported_vendor_grant_url_waits_without_reinitializing_profile(
    tmp_path: Path,
) -> None:
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("authorized")
    cli.get_profile_identity_fingerprint.return_value = "profile-fingerprint"
    cli.execute.side_effect = WeComCliError(
        "permission denied",
        stdout=json.dumps(
            {
                "errcode": 850001,
                "errmsg": "invalid api key",
                "authorization_url": _QR_URL,
            }
        ),
    )
    sessions = WeComAuthorizationSessions(
        "binding-private",
        cli,
        cli_enabled=True,
        profile_dir=tmp_path / "profile",
    )
    owner_hash, _ = _owner()

    result = sessions.start(
        owner_hash,
        idempotency_key="grant-request-key",
        payload_hash="grant-payload-hash",
        request_alias="grant-request-alias",
    )

    assert result["state"] == "WAITING_USER"
    assert result["authorization_url"] == _QR_URL
    cli.start_auth_init.assert_not_called()
    sessions.close()


def test_grant_poll_fails_when_the_bound_profile_identity_changes(
    tmp_path: Path,
) -> None:
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("authorized")
    cli.get_profile_identity_fingerprint.side_effect = [
        "profile-before",
        "profile-after",
        "profile-after",
    ]
    cli.execute.side_effect = WeComCliError(
        "permission denied",
        stdout=json.dumps(
            {
                "errcode": 850001,
                "errmsg": "invalid api key",
                "authorization_url": _QR_URL,
            }
        ),
    )
    sessions = WeComAuthorizationSessions(
        "binding-private",
        cli,
        cli_enabled=True,
        profile_dir=tmp_path / "profile",
    )
    owner_hash, _ = _owner()

    result = sessions.start(
        owner_hash,
        idempotency_key="changed-profile-request",
        payload_hash="same-payload",
        request_alias="changed-profile-alias",
    )

    assert result["state"] == "FAILED"
    assert result["authorization_url"] is None
    assert cli.execute.call_count == 1
    cli.start_auth_init.assert_not_called()
    health = sessions.health()
    assert health["authorization_state"] == "REAUTHORIZATION_REQUIRED"
    assert health["capabilities"][0]["reason_code"] == "profile_identity_changed"
    sessions.close()


def test_pending_activation_is_idempotent_private_and_cancelled_on_close(
    tmp_path: Path,
) -> None:
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("unauthorized")
    pending = PendingProcess(("line", f"Scan this page: {_QR_URL}"))
    cli.start_auth_init.return_value = pending
    owner_hash, caller = _owner()
    profile_dir = tmp_path / "profile"
    sessions = WeComAuthorizationSessions(
        "binding-private",
        cli,
        cli_enabled=True,
        profile_dir=profile_dir,
    )

    first = sessions.start(
        owner_hash,
        idempotency_key="request-key-stable",
        payload_hash="payload-hash",
        request_alias="request-stable-id",
    )
    retry = sessions.start(
        owner_hash,
        idempotency_key="request-key-stable",
        payload_hash="payload-hash",
        request_alias="request-stable-id",
    )

    assert first["state"] == "WAITING_USER"
    assert first["authorization_url"] == _QR_URL
    assert retry["session_ref"] == first["session_ref"]
    assert cli.start_auth_init.call_count == 1
    store_directory = (
        profile_dir
        / ".cyrene"
        / "authorization"
        / sessions.authorization_ref.removeprefix("wecom-cli:")
    )
    store_text = (store_directory / "sessions.json").read_text(encoding="utf-8")
    assert caller.subject not in store_text
    assert caller.organization_id not in store_text
    assert caller.workspace_id not in store_text

    sessions.close()
    stored = json.loads((store_directory / "sessions.json").read_text(encoding="utf-8"))
    assert stored["sessions"][0]["state"] == "CANCELLED"
    assert stored["sessions"][0]["authorization_url"] is None
    assert pending.terminated is True


def test_session_poll_by_another_actor_does_not_probe_or_mutate_state(
    tmp_path: Path,
) -> None:
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("unauthorized")
    pending = PendingProcess(("line", f"Scan this page: {_QR_URL}"))
    cli.start_auth_init.return_value = pending
    owner_hash, _ = _owner()
    other_owner = owner_fingerprint(
        SimpleNamespace(
            caller_workload="cyrene-exchange",
            audience="cyrene.connectors.wecom",
            issuer="trusted-issuer",
            subject="other-operator",
            organization_id="organization-private",
            workspace_id="workspace-private",
        ),
        "binding-private",
    )
    sessions = WeComAuthorizationSessions(
        "binding-private",
        cli,
        cli_enabled=True,
        profile_dir=tmp_path / "profile",
    )
    started = sessions.start(
        owner_hash,
        idempotency_key="owner-poll-key",
        payload_hash="owner-poll-payload",
        request_alias="exchange-session-ref-20261014",
    )
    store_directory = (
        tmp_path
        / "profile"
        / ".cyrene"
        / "authorization"
        / sessions.authorization_ref.removeprefix("wecom-cli:")
    )
    before = (store_directory / "sessions.json").read_text(encoding="utf-8")

    by_start_request = sessions.status(owner_hash, "exchange-session-ref-20261014")
    assert by_start_request["session_ref"] == started["session_ref"]

    with pytest.raises(AuthorizationSessionError, match="session_not_found"):
        sessions.status(other_owner, "exchange-session-ref-20261014")

    after = (store_directory / "sessions.json").read_text(encoding="utf-8")
    assert after == before
    cli.get_status.assert_called_once()
    assert pending.terminated is False
    sessions.close()


def test_expired_renewal_terminates_pending_cli_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(authorization_module, "_utc_now", lambda: now)
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("unauthorized")
    pending_process = PendingProcess(("line", f"Scan this page: {_QR_URL}"))
    cli.start_auth_init.return_value = pending_process
    owner_hash, _ = _owner()
    sessions = WeComAuthorizationSessions(
        "binding-private",
        cli,
        cli_enabled=True,
        profile_dir=tmp_path / "profile",
    )
    started = sessions.start(
        owner_hash,
        idempotency_key="expiring-start",
        payload_hash="expiring-payload",
        request_alias="expiring-start-id",
    )

    monkeypatch.setattr(
        authorization_module, "_utc_now", lambda: now + timedelta(seconds=301)
    )
    result = sessions.status(owner_hash, started["session_ref"])

    assert result["state"] == "EXPIRED"
    assert result["authorization_url"] is None
    assert pending_process.terminated is True
    sessions.close()


def test_untracked_activation_after_restart_is_unknown_and_never_restarted(
    tmp_path: Path,
) -> None:
    owner_hash, _ = _owner()
    profile_dir = tmp_path / "profile"
    original_cli = MagicMock(spec=WeComCliClient)
    original_cli.get_status.return_value = _cli_status("unauthorized")
    original_process = PendingProcess(("line", f"Scan this page: {_QR_URL}"))
    original_cli.start_auth_init.return_value = original_process
    original = WeComAuthorizationSessions(
        "binding-private",
        original_cli,
        cli_enabled=True,
        profile_dir=profile_dir,
    )
    original_session = original.start(
        owner_hash,
        idempotency_key="persisted-activation-key",
        payload_hash="persisted-activation-payload",
        request_alias="persisted-product-session-ref",
    )

    restarted_cli = MagicMock(spec=WeComCliClient)
    restarted_cli.get_status.return_value = _cli_status("unauthorized")
    restarted = WeComAuthorizationSessions(
        "binding-private",
        restarted_cli,
        cli_enabled=True,
        profile_dir=profile_dir,
    )

    health = restarted.health()
    replay = restarted.start(
        owner_hash,
        idempotency_key="persisted-activation-key",
        payload_hash="persisted-activation-payload",
        request_alias="persisted-product-session-ref",
    )

    assert health["authorization_state"] == "UNKNOWN"
    assert health["capabilities"][0]["reason_code"] == ("authorization_start_ambiguous")
    assert replay["session_ref"] == original_session["session_ref"]
    assert replay["state"] == "VERIFYING"
    assert replay["authorization_url"] == _QR_URL
    with pytest.raises(
        AuthorizationSessionError, match="authorization_start_ambiguous"
    ):
        restarted.start(
            owner_hash,
            idempotency_key="new-activation-key",
            payload_hash="new-activation-payload",
            request_alias="new-product-session-ref",
        )
    restarted_cli.start_auth_init.assert_not_called()

    original.close()
    restarted.close()


def test_changed_start_payload_with_same_request_id_is_rejected(
    tmp_path: Path,
) -> None:
    cli = MagicMock(spec=WeComCliClient)
    cli.get_status.return_value = _cli_status("unauthorized")
    cli.start_auth_init.return_value = PendingProcess()
    owner_hash, _ = _owner()
    sessions = WeComAuthorizationSessions(
        "binding-private",
        cli,
        cli_enabled=True,
        profile_dir=tmp_path / "profile",
    )
    sessions.start(
        owner_hash,
        idempotency_key="same-request-key",
        payload_hash="first-payload",
        request_alias="same-request-id",
    )

    with pytest.raises(AuthorizationSessionError, match="idempotency_conflict"):
        sessions.start(
            owner_hash,
            idempotency_key="same-request-key",
            payload_hash="changed-payload",
            request_alias="same-request-id",
        )

    assert cli.start_auth_init.call_count == 1
    sessions.close()


def test_subprocess_start_uses_only_fixed_official_arguments_and_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "wecom-cli-stub"
    marker = tmp_path / "invocation.json"
    profile = tmp_path / "native-profile"
    script = f"""#!/usr/bin/env python3
import json
import os
import sys
import time

args = sys.argv[1:]
if args == ["--version"]:
    print("wecom-cli 1.3.4 (stub)")
elif args == ["auth", "show", "--status"]:
    print("unauthorized")
elif args == ["auth", "init", "--noninteractive", "--no-browser"]:
    marker = {str(marker)!r}
    with open(marker, "w", encoding="utf-8") as output:
        json.dump({{
            "args": args,
            "profile": os.getenv("WECOM_CLI_CONFIG_DIR"),
            "access_token": os.getenv("WECOM_CLI_ACCESS_TOKEN"),
            "headers": os.getenv("WECOM_CLI_ADDITIONAL_HEADERS"),
        }}, output)
    print("Please open this QR page:")
    print({_QR_URL!r}, flush=True)
    time.sleep(60)
"""
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setenv("WECOM_CLI_ACCESS_TOKEN", "ambient-private-token")
    monkeypatch.setenv("WECOM_CLI_ADDITIONAL_HEADERS", '{"X-Caller":"other"}')
    monkeypatch.setenv("WECOM_CLI_ADDITIONAL_HEADERS_EXTRA", '{"X-Extra":"other"}')
    cli = WeComCliClient(executable_path=executable, config_dir=profile)
    sessions = WeComAuthorizationSessions(
        "binding-private",
        cli,
        cli_enabled=True,
        profile_dir=profile,
    )
    owner_hash, _ = _owner()

    pending = sessions.start(
        owner_hash,
        idempotency_key="subprocess-request-key",
        payload_hash="subprocess-payload",
        request_alias="subprocess-product-session-ref",
    )
    process = next(iter(sessions._processes.values()))
    invocation = json.loads(marker.read_text(encoding="utf-8"))

    assert pending["state"] == "WAITING_USER"
    assert pending["authorization_url"] == _QR_URL
    assert invocation == {
        "args": ["auth", "init", "--noninteractive", "--no-browser"],
        "profile": str(profile),
        "access_token": None,
        "headers": None,
    }
    sessions.close()
    assert process.poll() is not None
