from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from cyrene_account_center_contracts import AuthMaterial
from cyrene_integration_contracts import (
    ToolEnvelope,
    payload_sha256,
    serialize_tool_envelope,
    sign_context,
)
from cyrene_plugin_runtime import DirectPayload
from cyrene_tool_provider_contracts import (
    CAPABILITY_ID,
    METHOD_CALL_TOOL,
    METHOD_LIST_TOOLS,
    REQUEST_TYPE_URLS,
    decode_response,
    encode_request,
)
from cyrene_tool_provider_contracts import (
    tool_provider_pb2 as wire,
)

from cyrene_bilibili.account_center import AccountCenterDirectClient
from cyrene_bilibili.api import QrPoll, QrTicket
from cyrene_bilibili.plugin import BilibiliProvider
from cyrene_bilibili.settings import ProviderSettings, TrustedCaller
from cyrene_bilibili.tool_catalog import PLUGIN_VERSION
from cyrene_bilibili.trust import expected_target

_CALLER_WORKLOAD = "cyrene.exchange.integrations"
_AUDIENCE = "cyrene.connectors.bilibili"
_ISSUER = "https://id.example.test"
_SUBJECT = "actor-123"
_ORGANIZATION = "org-one"
_WORKSPACE = "workspace-one"
_BINDING = "bilibili-binding"
_ACCOUNT_BINDING = "account-center-binding"
_CALLER_KEY = b"c" * 32
_AC_KEY = b"a" * 32
_PROFILE = "00000000-0000-4000-8000-000000000010"
_LEASE = "00000000-0000-4000-8000-000000000011"
_SESSION = "00000000-0000-4000-8000-000000000012"
_COOKIE_JAR = json.dumps(
    {
        "schemaVersion": 1,
        "cookies": [
            {
                "name": "SESSDATA",
                "value": "sensitive-cookie",
                "domain": ".bilibili.com",
                "path": "/",
            },
            {"name": "bili_jct", "value": "csrf-secret", "domain": ".bilibili.com", "path": "/"},
            {"name": "DedeUserID", "value": "12345", "domain": ".bilibili.com", "path": "/"},
        ],
    },
    separators=(",", ":"),
)


def _settings(state_dir: Path) -> ProviderSettings:
    return ProviderSettings(
        binding_id=_BINDING,
        trusted_callers=(
            TrustedCaller(
                caller_workload=_CALLER_WORKLOAD,
                audience=_AUDIENCE,
                organization_id=_ORGANIZATION,
                workspace_id=_WORKSPACE,
                issuer=_ISSUER,
                verification_key=_CALLER_KEY,
            ),
        ),
        account_center_binding_id=_ACCOUNT_BINDING,
        account_center_connection_ref="grpc://127.0.0.1:50051",
        account_center_transport="local",
        provider_caller_id="cyrene.connectors.bilibili",
        provider_signing_key=_AC_KEY,
        account_center_root_cert_ref=None,
        account_center_client_cert_ref=None,
        account_center_client_key_ref=None,
        state_dir=state_dir,
        host_secret_names={},
    )


class _FakeAccountCenter:
    def __init__(self) -> None:
        self.redemptions: list[tuple[dict[str, Any], dict[str, str | None]]] = []
        self.begins: list[tuple[str, str, dict[str, str | None]]] = []
        self.completions: list[tuple[dict[str, Any], dict[str, str | None]]] = []
        self.completion_extras: dict[str, Any] = {}

    def redeem_provider_credential_lease(self, payload, actor):
        self.redemptions.append((dict(payload), dict(actor)))
        return AuthMaterial(
            access_token=_COOKIE_JAR,
            token_type="BilibiliCookieJar",
            external_subject="12345",
            expires_at=int(time.time()) + 3600,
        )

    def begin_bilibili_link(self, profile_ref, idempotency_key, actor):
        self.begins.append((profile_ref, idempotency_key, dict(actor)))
        return {
            "profile_ref": profile_ref,
            "link_session_ref": _SESSION,
            "expires_at": int(time.time()) + 600,
        }

    def complete_bilibili_link(self, payload, actor):
        self.completions.append((dict(payload), dict(actor)))
        return {
            "profile_ref": payload["profile_ref"],
            "provider": "bilibili",
            "auth_status": "linked",
            "external_subject": "12345",
            "expires_at": int(time.time()) + 3600,
            "credential_revision": 1,
            **self.completion_extras,
        }

    def close(self) -> None:
        pass


class _FakeApi:
    def __init__(self) -> None:
        self.send_count = 0
        self.user_reads = 0
        self.poll_result = QrPoll("PENDING", "{}")

    def close(self) -> None:
        pass

    def send_dm(self, peer_user_id, content, cookies_json, subject):
        self.send_count += 1
        assert cookies_json == _COOKIE_JAR
        assert subject == "12345"
        return {
            "external_id": "887766",
            "readback_verified": True,
            "readback_sha256": "d" * 64,
        }

    def user_find(self, user_id, cookies_json):
        self.user_reads += 1
        return {
            "user_id": user_id,
            "name": "test-user",
            "signature": "",
            "level": 1,
            "is_following": False,
        }

    def generate_qr(self):
        png = b"\x89PNG\r\n\x1a\nprovider-test"
        import hashlib

        return QrTicket(
            qrcode_key="qr-key-1",
            qr_url="https://passport.bilibili.com/h5-app/passport/login?x=y",
            qr_png=png,
            qr_png_sha256=hashlib.sha256(png).hexdigest(),
            cookies_json="{}",
            expires_at=int(time.time()) + 600,
        )

    def poll_qr(self, qrcode_key, cookies_json):
        return self.poll_result


def _invoke(provider, tool_id: str, arguments: dict[str, Any], *, request_id: str | None = None):
    context = sign_context(
        method=tool_id,
        operation_id=tool_id,
        payload=arguments,
        caller_workload=_CALLER_WORKLOAD,
        audience=_AUDIENCE,
        issuer=_ISSUER,
        subject=_SUBJECT,
        organization_id=_ORGANIZATION,
        workspace_id=_WORKSPACE,
        binding_id=_BINDING,
        request_id=request_id or str(uuid.uuid4()),
        key=_CALLER_KEY,
    )
    encoded_envelope = serialize_tool_envelope(
        ToolEnvelope(caller_context=context, arguments=arguments)
    )
    request = wire.CallToolRequest(
        binding_id=_BINDING,
        provider_tool_id=tool_id,
        catalog_version=PLUGIN_VERSION,
        arguments_json=json.dumps(encoded_envelope, separators=(",", ":")),
    )
    typed_request = encode_request(METHOD_CALL_TOOL, request)
    ok, response = provider.on_invoke(
        CAPABILITY_ID,
        METHOD_CALL_TOOL,
        typed_request.value,
        request_type_url=typed_request.type_url,
    )
    assert ok is True
    decoded = decode_response(METHOD_CALL_TOOL, response)
    parts = decoded.outcome.content
    assert len(parts) == 1
    return decoded.outcome.is_error, json.loads(parts[0].json.json)


def _write_arguments(
    operation_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    lease_ref: str = _LEASE,
) -> dict[str, Any]:
    return {
        "profile_ref": _PROFILE,
        "lease_ref": lease_ref,
        "target": expected_target(operation_id, payload),
        "arguments_sha256": payload_sha256(payload),
        "payload": payload,
        "idempotency_key": idempotency_key,
    }


def test_account_center_complete_always_sends_nullable_cookie_expiry(tmp_path: Path) -> None:
    """Keep the completion wire closed when session cookies have no expiry."""

    class _CapturingClient:
        def __init__(self) -> None:
            self.envelope: dict[str, Any] | None = None

        def invoke(self, **kwargs: Any) -> DirectPayload:
            request = kwargs["request"]
            self.envelope = json.loads(request.value)
            profile = {
                "profile_ref": _PROFILE,
                "provider": "bilibili",
                "auth_status": "healthy",
                "external_subject": "12345",
                "auth_mode": "session_cookie",
                "access_mode": "actor_owned",
                "credential_revision": 1,
            }
            return DirectPayload(
                type_url="type.googleapis.com/cyrene.account.center.v1.LinkedProviderProfileResponse",
                value=json.dumps(profile, separators=(",", ":")).encode("utf-8"),
            )

    actor = {
        "issuer": _ISSUER,
        "subject": _SUBJECT,
        "organization_id": _ORGANIZATION,
        "workspace_id": _WORKSPACE,
    }
    client = _CapturingClient()
    account_center = AccountCenterDirectClient(_settings(tmp_path), client=client)
    result = account_center.complete_bilibili_link(
        {
            "profile_ref": _PROFILE,
            "link_session_ref": _SESSION,
            "external_subject": "12345",
            "credential_json": _COOKIE_JAR,
            "credential_expires_at": None,
            "idempotency_key": "complete:nullable-expiry",
        },
        actor,
    )

    assert result["external_subject"] == "12345"
    assert client.envelope is not None
    assert client.envelope["payload"]["credential_expires_at"] is None
    assert _COOKIE_JAR not in json.dumps(result)


def test_real_shared_tool_envelope_and_idempotent_write_replay(tmp_path) -> None:
    account_center = _FakeAccountCenter()
    api = _FakeApi()
    provider = BilibiliProvider(
        settings=_settings(tmp_path),
        account_center=account_center,
        api=api,
    )
    payload = {"peer_user_id": "9988", "content": "hello"}
    idem = "work-operation:00000000-0000-4000-8000-000000000001"
    first = _write_arguments("bilibili.dm.send", payload, idempotency_key=idem)
    first_error, first_result = _invoke(provider, "bilibili.dm.send", first)
    replay = _write_arguments(
        "bilibili.dm.send",
        payload,
        idempotency_key=idem,
        lease_ref="00000000-0000-4000-8000-000000000099",
    )
    replay_error, replay_result = _invoke(provider, "bilibili.dm.send", replay)

    assert first_error is False
    assert replay_error is False
    assert first_result == replay_result
    assert first_result["status"] == "SUCCEEDED"
    assert first_result["operation_ref"] == idem
    assert first_result["receipt_id"]
    assert first_result["readback_verified"] is True
    assert "hello" not in json.dumps(first_result)
    assert api.send_count == 1
    assert len(account_center.redemptions) == 1
    assert account_center.redemptions[0][0]["arguments_sha256"] == payload_sha256(payload)


def test_read_envelope_has_no_model_profile_or_idempotency_fields(tmp_path) -> None:
    account_center = _FakeAccountCenter()
    api = _FakeApi()
    provider = BilibiliProvider(
        settings=_settings(tmp_path), account_center=account_center, api=api
    )
    payload = {"user_id": "9988"}
    arguments = {
        "profile_ref": _PROFILE,
        "lease_ref": _LEASE,
        "target": expected_target("bilibili.user.find", payload),
        "arguments_sha256": payload_sha256(payload),
        "payload": payload,
    }
    is_error, result = _invoke(provider, "bilibili.user.find", arguments)
    assert is_error is False
    assert result["item"]["user_id"] == "9988"
    assert len(account_center.redemptions) == 1
    assert api.user_reads == 1


def test_catalog_does_not_expose_account_link_operations_to_models(tmp_path) -> None:
    provider = BilibiliProvider(
        settings=_settings(tmp_path), account_center=_FakeAccountCenter(), api=_FakeApi()
    )
    request = encode_request(
        METHOD_LIST_TOOLS,
        wire.ListToolsRequest(binding_id=_BINDING),
    )
    ok, response = provider.on_invoke(
        CAPABILITY_ID,
        METHOD_LIST_TOOLS,
        request.value,
        request_type_url=REQUEST_TYPE_URLS[METHOD_LIST_TOOLS],
    )
    assert ok is True
    decoded = decode_response(METHOD_LIST_TOOLS, response)
    ids = {item.provider_tool_id for item in decoded.catalog.tools}
    assert len(ids) == 11
    assert not any(item.startswith("bilibili.auth.qr.") for item in ids)


def test_qr_flow_keeps_account_center_refs_and_cookie_out_of_tool_response(tmp_path) -> None:
    account_center = _FakeAccountCenter()
    api = _FakeApi()
    provider = BilibiliProvider(
        settings=_settings(tmp_path), account_center=account_center, api=api
    )
    begin_error, begin_result = _invoke(
        provider,
        "bilibili.auth.qr.begin",
        {"profile_ref": _PROFILE, "idempotency_key": "qr-request:00000001"},
    )
    assert begin_error is False
    assert begin_result["status"] == "PENDING"
    assert len(begin_result["qr_png_base64"]) > 12
    assert "link_session_ref" not in json.dumps(begin_result)
    assert "cookie" not in json.dumps(begin_result).lower()
    assert "sensitive-cookie" not in json.dumps(begin_result)

    api.poll_result = QrPoll("LINKED", _COOKIE_JAR, "12345")
    status_error, status_result = _invoke(
        provider,
        "bilibili.auth.qr.status",
        {"login_flow_id": begin_result["login_flow_id"]},
    )
    assert status_error is False, status_result
    assert status_result["status"] == "LINKED"
    assert status_result["profile"]["external_subject"] == "12345"
    assert len(account_center.completions) == 1
    completion = account_center.completions[0][0]
    assert completion["credential_json"] == _COOKIE_JAR
    assert completion["link_session_ref"] == _SESSION
    assert "sensitive-cookie" not in json.dumps(status_result)

    account_center.completion_extras = {
        "access_token": "must-never-persist",
        "credential_json": "must-never-persist",
    }
    api.poll_result = QrPoll("LINKED", _COOKIE_JAR, "12345")
    replay_error, replay_result = _invoke(
        provider,
        "bilibili.auth.qr.status",
        {"login_flow_id": begin_result["login_flow_id"]},
    )
    assert replay_error is False
    assert replay_result == status_result
    assert "must-never-persist" not in json.dumps(replay_result)
    scope = provider._store.scope_key(
        {
            "issuer": _ISSUER,
            "subject": _SUBJECT,
            "organization_id": _ORGANIZATION,
            "workspace_id": _WORKSPACE,
            "binding_id": _BINDING,
        }
    )
    stored_profile = provider._store.get_linked_profile(scope, begin_result["login_flow_id"])
    assert stored_profile == status_result["profile"]
    assert "access_token" not in stored_profile
    assert "credential_json" not in stored_profile


def test_qr_flow_id_is_bound_to_the_authenticated_actor(tmp_path) -> None:
    account_center = _FakeAccountCenter()
    provider = BilibiliProvider(
        settings=_settings(tmp_path), account_center=account_center, api=_FakeApi()
    )
    _, begin_result = _invoke(
        provider,
        "bilibili.auth.qr.begin",
        {"profile_ref": _PROFILE, "idempotency_key": "qr-request:00000002"},
    )

    context = sign_context(
        method="bilibili.auth.qr.status",
        operation_id="bilibili.auth.qr.status",
        payload={"login_flow_id": begin_result["login_flow_id"]},
        caller_workload=_CALLER_WORKLOAD,
        audience=_AUDIENCE,
        issuer=_ISSUER,
        subject="other-actor",
        organization_id=_ORGANIZATION,
        workspace_id=_WORKSPACE,
        binding_id=_BINDING,
        request_id=str(uuid.uuid4()),
        key=_CALLER_KEY,
    )
    envelope = serialize_tool_envelope(
        ToolEnvelope(
            caller_context=context,
            arguments={"login_flow_id": begin_result["login_flow_id"]},
        )
    )
    request = wire.CallToolRequest(
        binding_id=_BINDING,
        provider_tool_id="bilibili.auth.qr.status",
        catalog_version=PLUGIN_VERSION,
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )
    typed = encode_request(METHOD_CALL_TOOL, request)
    ok, response = provider.on_invoke(
        CAPABILITY_ID,
        METHOD_CALL_TOOL,
        typed.value,
        request_type_url=typed.type_url,
    )
    decoded = decode_response(METHOD_CALL_TOOL, response)
    assert ok is True
    assert decoded.outcome.is_error is False
    assert json.loads(decoded.outcome.content[0].json.json) == {"status": "NOT_FOUND"}
