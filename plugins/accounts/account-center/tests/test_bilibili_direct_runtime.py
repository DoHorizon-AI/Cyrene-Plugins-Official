"""Exercise Bilibili linking through Account Center's real local Plugin runtime."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from email.utils import formatdate, parsedate_to_datetime
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cyrene_account_center_contracts import (
    BilibiliOperation,
    canonical_json,
    sign_context,
)
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient
from cyrene_plugin_runtime.server import load_entrypoint, serve

from account_center import plugin as account_center_plugin_module

_ACCOUNT_CENTER = "account.center.v1"
_REQUEST_PREFIX = "type.googleapis.com/cyrene.account.center.v1."
_EXCHANGE = "cyrene.exchange.integrations"
_GITHUB = "cyrene.connectors.github-issues"
_BILIBILI = "cyrene.connectors.bilibili"
_ACCOUNT_BINDING = "account-center-bilibili-e2e"
_TOOL_BINDING = "bilibili-provider-e2e"
_ISSUER = "https://identity.example.test"
_SUBJECT = "account-center-e2e-owner"
_ORGANIZATION = "org-aether-e2e"
_WORKSPACE = "workspace-aether-e2e"
_PROFILE_REF = "00000000-0000-4000-8000-000000000581"
_MID = "12345"
_EXCHANGE_AC_KEY = b"e" * 32
_GITHUB_KEY = b"g" * 32
_BILIBILI_AC_KEY = b"b" * 32
_EXCHANGE_TOOL_KEY = b"t" * 32
_COOKIE_SESSDATA = "fixture-bili-sessdata-secret"
_COOKIE_CSRF = "fixture-bili-csrf-secret"


def _account_center_request(
    client: DirectPluginClient,
    method: str,
    payload: dict[str, object],
    *,
    caller_id: str,
    key: bytes,
    actor: dict[str, str | None],
    binding_id: str = _ACCOUNT_BINDING,
) -> dict[str, object]:
    """Sign and invoke one exact Account Center request over gRPC."""

    issued_at = int(time.time())
    context = sign_context(
        method=method,
        payload=payload,
        caller_id=caller_id,
        issuer=str(actor["issuer"]),
        subject=str(actor["subject"]),
        organization_id=str(actor["organization_id"]),
        workspace_id=actor["workspace_id"],
        binding_id=binding_id,
        request_id=str(uuid4()),
        operation_id=method,
        key=key,
        issued_at=issued_at,
        expires_at=issued_at + 120,
    )
    request_type = account_center_plugin_module._action_type_name(method, "Request")
    response_type = {
        "begin_bilibili_link": "BilibiliLinkSession",
        "complete_bilibili_link": "LinkedProviderProfileResponse",
    }.get(method, account_center_plugin_module._action_type_name(method, "Response"))
    result = client.invoke(
        capability=_ACCOUNT_CENTER,
        interface_version="1",
        method=method,
        request=DirectPayload(
            type_url=f"{_REQUEST_PREFIX}{request_type}",
            value=canonical_json(
                {"payload": payload, "trusted_context": context.as_dict()}
            ),
        ),
        deadline_seconds=5,
    )
    assert result.type_url == f"{_REQUEST_PREFIX}{response_type}"
    decoded = json.loads(result.value)
    assert isinstance(decoded, dict)
    return decoded


def _load_bilibili_source(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Add the sibling Bilibili source and official SDK sources to this test process."""

    repository_root = Path(__file__).resolve().parents[4]
    configured = os.environ.get("CYRENE_BILIBILI_PLUGIN_SOURCE")
    plugin_root = (
        Path(configured).resolve()
        if configured
        else repository_root / "plugins" / "connectors" / "bilibili"
    )
    source_root = plugin_root if plugin_root.name == "src" else plugin_root / "src"
    if not source_root.is_dir():
        pytest.skip(
            "Bilibili connector source is not present; set CYRENE_BILIBILI_PLUGIN_SOURCE "
            "for the cross-repository acceptance lane"
        )
    sdk_root = repository_root / "sdk" / "python"
    for path in (
        sdk_root / "cyrene_tool_provider_contracts" / "src",
        sdk_root / "cyrene_integration_contracts" / "src",
        source_root,
    ):
        if path.is_dir():
            monkeypatch.syspath_prepend(str(path))
    return source_root


def test_bilibili_qr_identity_lease_and_restart_use_real_account_center_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify QR, nav MID, encrypted persistence, all lease operations, and replay denial."""

    _load_bilibili_source(monkeypatch)
    tool_contracts = import_module("cyrene_tool_provider_contracts")
    integration_contracts = import_module("cyrene_integration_contracts")
    tool_wire = tool_contracts.tool_provider_pb2
    account_center_client_module = import_module("cyrene_bilibili.account_center")
    bilibili_api_module = import_module("cyrene_bilibili.api")
    bilibili_plugin_module = import_module("cyrene_bilibili.plugin")
    bilibili_settings_module = import_module("cyrene_bilibili.settings")
    bilibili_catalog_module = import_module("cyrene_bilibili.tool_catalog")

    lease_operations = {operation.value for operation in BilibiliOperation}
    expected_operations = {
        "bilibili.auth.qr.begin",
        "bilibili.auth.qr.status",
        *lease_operations,
    }
    assert len(lease_operations) == 11
    assert len(expected_operations) == 13
    assert expected_operations <= set(integration_contracts.TOOL_OPERATIONS.values())
    assert lease_operations == set(
        account_center_plugin_module.PROVIDER_OPERATIONS_BY_PROVIDER["bilibili"]
    )
    assert lease_operations == set(
        account_center_client_module._BILIBILI_LEASE_OPERATIONS
    )
    assert "begin_bilibili_link" in account_center_plugin_module.METHODS
    assert "complete_bilibili_link" in account_center_plugin_module.METHODS

    actor = {
        "issuer": _ISSUER,
        "subject": _SUBJECT,
        "organization_id": _ORGANIZATION,
        "workspace_id": _WORKSPACE,
    }
    caller_refs = {
        _EXCHANGE: "account-center/caller-hmac-keys/exchange",
        _GITHUB: "account-center/caller-hmac-keys/github",
        _BILIBILI: "account-center/caller-hmac-keys/bilibili",
    }
    secret_refs = {
        "account-center/encryption-key": "TEST_BILIBILI_AC_ENCRYPTION_KEY",
        **{
            reference: f"TEST_BILIBILI_AC_CALLER_{index}"
            for index, reference in enumerate(caller_refs.values())
        },
    }
    secret_values = {
        "TEST_BILIBILI_AC_ENCRYPTION_KEY": b"e" * 32,
        "TEST_BILIBILI_AC_CALLER_0": _EXCHANGE_AC_KEY,
        "TEST_BILIBILI_AC_CALLER_1": _GITHUB_KEY,
        "TEST_BILIBILI_AC_CALLER_2": _BILIBILI_AC_KEY,
    }
    for name, value in secret_values.items():
        monkeypatch.setenv(name, value.hex())
    monkeypatch.setenv("CYRENE_PLUGIN_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", _ACCOUNT_BINDING)
    configuration = {
        "encryption_key_ref": "account-center/encryption-key",
        "caller_key_refs": caller_refs,
        "hostSecretRefs": secret_refs,
        "exchange_caller_id": _EXCHANGE,
        "github_provider_caller_id": _GITHUB,
        "bilibili_provider_caller_id": _BILIBILI,
        "method_callers": {},
        "wecom_sources": {},
    }
    monkeypatch.setenv(
        "CYRENE_CAPABILITY_CONFIGURATION_JSON", json.dumps(configuration)
    )

    account_plugin = load_entrypoint("account_center.plugin:AccountCenterPlugin")
    database_path = account_plugin._store.path
    ac_server, ac_connection = serve(
        account_plugin,
        "account.center.v1",
        "1",
        "127.0.0.1:0",
    )
    ac_client = DirectPluginClient.for_local_connection_ref(ac_connection)

    cookie_expiry = formatdate(2_116_000_000, usegmt=True)
    api_calls: list[str] = []

    def fixture_bilibili_api(request: httpx.Request) -> httpx.Response:
        api_calls.append(request.url.path)
        if request.url.path == "/x/passport-login/web/qrcode/generate":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "qrcode_key": "fixture-qr-key",
                        "url": "https://passport.bilibili.com/h5-app/passport/login?fixture=1",
                    },
                },
                headers=[
                    (
                        "set-cookie",
                        "buvid3=fixture-visitor; Domain=.bilibili.com; Path=/; Secure",
                    )
                ],
            )
        if request.url.path == "/x/passport-login/web/qrcode/poll":
            assert request.url.params.get("qrcode_key") == "fixture-qr-key"
            return httpx.Response(
                200,
                json={"code": 0, "data": {"url": "https://ignored.invalid/"}},
                headers=[
                    (
                        "set-cookie",
                        (
                            f"SESSDATA={_COOKIE_SESSDATA}; Domain=.bilibili.com; Path=/; "
                            f"Expires={cookie_expiry}; Secure"
                        ),
                    ),
                    (
                        "set-cookie",
                        (
                            f"bili_jct={_COOKIE_CSRF}; Domain=.bilibili.com; Path=/; "
                            f"Expires={cookie_expiry}; Secure"
                        ),
                    ),
                    (
                        "set-cookie",
                        (
                            f"DedeUserID={_MID}; Domain=.bilibili.com; Path=/; "
                            f"Expires={cookie_expiry}; Secure"
                        ),
                    ),
                ],
            )
        if request.url.path == "/x/web-interface/nav":
            assert f"SESSDATA={_COOKIE_SESSDATA}" in request.headers.get("cookie", "")
            return httpx.Response(200, json={"code": 0, "data": {"mid": _MID}})
        raise AssertionError(
            f"unexpected fixture Bilibili endpoint: {request.url.path}"
        )

    http_client = httpx.Client(
        transport=httpx.MockTransport(fixture_bilibili_api),
        timeout=httpx.Timeout(5),
        trust_env=False,
        follow_redirects=False,
    )
    trusted_caller = bilibili_settings_module.TrustedCaller(
        caller_workload=_EXCHANGE,
        audience=_BILIBILI,
        organization_id=_ORGANIZATION,
        workspace_id=_WORKSPACE,
        issuer=_ISSUER,
        verification_key=_EXCHANGE_TOOL_KEY,
    )
    bili_state_dir = tmp_path / "bilibili-state"
    bili_state_dir.mkdir(mode=0o700)
    settings = bilibili_settings_module.ProviderSettings(
        binding_id=_TOOL_BINDING,
        trusted_callers=(trusted_caller,),
        account_center_binding_id=_ACCOUNT_BINDING,
        account_center_connection_ref="grpc://127.0.0.1:1",
        account_center_transport="local",
        provider_caller_id=_BILIBILI,
        provider_signing_key=_BILIBILI_AC_KEY,
        account_center_root_cert_ref=None,
        account_center_client_cert_ref=None,
        account_center_client_key_ref=None,
        state_dir=bili_state_dir,
        host_secret_names={},
        timeout_seconds=5,
    )
    account_center_client = account_center_client_module.AccountCenterDirectClient(
        settings,
        client=ac_client,
    )
    provider = bilibili_plugin_module.BilibiliProvider(
        settings=settings,
        account_center=account_center_client,
        api=bilibili_api_module.BilibiliApi(timeout_seconds=5, client=http_client),
    )
    bili_server, bili_connection = serve(
        provider,
        tool_contracts.CAPABILITY_ID,
        "1",
        "127.0.0.1:0",
    )
    bili_client = DirectPluginClient.for_local_connection_ref(bili_connection)

    def invoke_bilibili_tool(
        operation_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        context = integration_contracts.sign_context(
            method=operation_id,
            operation_id=operation_id,
            payload=arguments,
            caller_workload=_EXCHANGE,
            audience=_BILIBILI,
            issuer=_ISSUER,
            subject=_SUBJECT,
            organization_id=_ORGANIZATION,
            workspace_id=_WORKSPACE,
            binding_id=_TOOL_BINDING,
            request_id=str(uuid4()),
            key=_EXCHANGE_TOOL_KEY,
        )
        envelope = integration_contracts.serialize_tool_envelope(
            integration_contracts.ToolEnvelope(
                caller_context=context, arguments=arguments
            )
        )
        request = tool_wire.CallToolRequest(
            binding_id=_TOOL_BINDING,
            provider_tool_id=operation_id,
            catalog_version=bilibili_catalog_module.PLUGIN_VERSION,
            arguments_json=json.dumps(envelope, separators=(",", ":")),
        )
        typed_request = tool_contracts.encode_request(
            tool_contracts.METHOD_CALL_TOOL, request
        )
        response = bili_client.invoke(
            capability=tool_contracts.CAPABILITY_ID,
            interface_version="1",
            method=tool_contracts.METHOD_CALL_TOOL,
            request=DirectPayload(
                type_url=typed_request.type_url, value=typed_request.value
            ),
            deadline_seconds=5,
        )
        decoded = tool_contracts.decode_response(
            tool_contracts.METHOD_CALL_TOOL,
            tool_contracts.TypedPayload(response.type_url, response.value),
        )
        assert decoded.outcome.is_error is False
        result = json.loads(decoded.outcome.content[0].json.json)
        assert isinstance(result, dict)
        return result

    try:
        catalog_request = tool_contracts.encode_request(
            tool_contracts.METHOD_LIST_TOOLS,
            tool_wire.ListToolsRequest(binding_id=_TOOL_BINDING),
        )
        catalog_response = bili_client.invoke(
            capability=tool_contracts.CAPABILITY_ID,
            interface_version="1",
            method=tool_contracts.METHOD_LIST_TOOLS,
            request=DirectPayload(
                type_url=catalog_request.type_url, value=catalog_request.value
            ),
            deadline_seconds=5,
        )
        catalog = tool_contracts.decode_response(
            tool_contracts.METHOD_LIST_TOOLS,
            tool_contracts.TypedPayload(
                catalog_response.type_url, catalog_response.value
            ),
        )
        visible_tools = {entry.provider_tool_id for entry in catalog.catalog.tools}
        assert len(visible_tools) == 11
        assert not {"bilibili.auth.qr.begin", "bilibili.auth.qr.status"} & visible_tools

        begin = invoke_bilibili_tool(
            "bilibili.auth.qr.begin",
            {
                "profile_ref": _PROFILE_REF,
                "idempotency_key": "qr-request:runtime-e2e-1",
            },
        )
        assert begin["status"] == "PENDING"
        assert "qr_png_base64" in begin
        assert "link_session_ref" not in begin
        assert _COOKIE_SESSDATA not in json.dumps(begin)

        linked = invoke_bilibili_tool(
            "bilibili.auth.qr.status",
            {"login_flow_id": begin["login_flow_id"]},
        )
        assert linked["status"] == "LINKED"
        profile = linked["profile"]
        assert profile["provider"] == "bilibili"
        assert profile["external_subject"] == _MID
        assert profile["credential_revision"] == 1
        assert _COOKIE_SESSDATA not in json.dumps(linked)
        assert _COOKIE_CSRF not in json.dumps(linked)
        assert api_calls == [
            "/x/passport-login/web/qrcode/generate",
            "/x/passport-login/web/qrcode/poll",
            "/x/web-interface/nav",
        ]

        listed = _account_center_request(
            ac_client,
            "list_provider_profiles",
            {},
            caller_id=_EXCHANGE,
            key=_EXCHANGE_AC_KEY,
            actor=actor,
        )
        assert len(listed["profiles"]) == 1
        assert listed["profiles"][0]["profile_ref"] == _PROFILE_REF
        assert listed["profiles"][0]["target_scope"] == {
            "schema_version": 1,
            "account_id": _MID,
        }
        assert _COOKIE_SESSDATA not in json.dumps(listed)

        with sqlite3.connect(database_path) as db:
            stored = db.execute(
                "SELECT credential_blob FROM provider_profiles WHERE profile_ref=?",
                (_PROFILE_REF,),
            ).fetchone()
        assert stored is not None and stored[0]
        assert _COOKIE_SESSDATA.encode() not in stored[0]
        assert _COOKIE_CSRF.encode() not in stored[0]
        assert os.stat(database_path).st_mode & 0o777 == 0o600
        decrypted_cookie_jar, persisted_expiry = account_plugin._decrypt_provider_token(
            _PROFILE_REF,
            bytes(stored[0]),
        )
        assert _COOKIE_SESSDATA in decrypted_cookie_jar
        assert _COOKIE_CSRF in decrypted_cookie_jar
        assert persisted_expiry == int(parsedate_to_datetime(cookie_expiry).timestamp())

        targets = {
            "bilibili.search": {"search_type": "video"},
            "bilibili.user.find": {"user_id": _MID},
            "bilibili.video.read": {"video_id": "BV1234567890"},
            "bilibili.comments.list": {
                "video_id": "BV1234567890",
                "root_comment_id": "0",
            },
            "bilibili.dynamics.list": {"user_id": _MID},
            "bilibili.dynamic.read": {"dynamic_id": "42"},
            "bilibili.dm.list": {"peer_user_id": "67890"},
            "bilibili.dm.read": {"peer_user_id": "67890", "message_id": "77"},
            "bilibili.comment.create": {
                "video_id": "BV1234567890",
                "root_comment_id": "0",
                "parent_comment_id": "0",
            },
            "bilibili.dynamic.publish": {"user_id": _MID},
            "bilibili.dm.send": {"peer_user_id": "67890"},
        }
        assert set(targets) == lease_operations
        replay_payload: dict[str, object] | None = None
        for index, operation_id in enumerate(sorted(lease_operations)):
            target = targets[operation_id]
            arguments_digest = integration_contracts.payload_sha256(
                {"operation_id": operation_id, "target": target}
            )
            lease = _account_center_request(
                ac_client,
                "create_provider_credential_lease",
                {
                    "profile_ref": _PROFILE_REF,
                    "operation_id": operation_id,
                    "target": target,
                    "arguments_sha256": arguments_digest,
                },
                caller_id=_EXCHANGE,
                key=_EXCHANGE_AC_KEY,
                actor=actor,
            )
            redemption = {
                "lease_ref": lease["lease_ref"],
                "profile_ref": _PROFILE_REF,
                "operation_id": operation_id,
                "target": target,
                "arguments_sha256": arguments_digest,
            }
            if replay_payload is None:
                replay_payload = redemption
                stranger = {**actor, "subject": "another-owner"}
                with pytest.raises(
                    account_center_client_module.AccountCenterUnavailable
                ):
                    account_center_client.redeem_provider_credential_lease(
                        redemption,
                        stranger,
                    )
            credential = account_center_client.redeem_provider_credential_lease(
                redemption,
                actor,
            )
            assert credential.token_type == "BilibiliCookieJar"
            assert credential.external_subject == _MID
            assert json.loads(credential.access_token)["cookies"]
            if index == 0:
                with pytest.raises(
                    account_center_client_module.AccountCenterUnavailable
                ):
                    account_center_client.redeem_provider_credential_lease(
                        redemption,
                        actor,
                    )
        assert replay_payload is not None

        replayed_status = invoke_bilibili_tool(
            "bilibili.auth.qr.status",
            {"login_flow_id": begin["login_flow_id"]},
        )
        assert replayed_status == linked
        assert _COOKIE_SESSDATA not in json.dumps(replayed_status)
    finally:
        bili_client.close()
        bili_server.stop(grace=0).wait(timeout=5)
        provider.close()
        http_client.close()
        ac_client.close()
        ac_server.stop(grace=0).wait(timeout=5)

    restarted_plugin = load_entrypoint("account_center.plugin:AccountCenterPlugin")
    restarted_server, restarted_connection = serve(
        restarted_plugin,
        "account.center.v1",
        "1",
        "127.0.0.1:0",
    )
    restarted_client = DirectPluginClient.for_local_connection_ref(restarted_connection)
    try:
        after_restart = _account_center_request(
            restarted_client,
            "list_provider_profiles",
            {},
            caller_id=_EXCHANGE,
            key=_EXCHANGE_AC_KEY,
            actor=actor,
        )
        assert after_restart == listed
        assert _COOKIE_SESSDATA not in json.dumps(after_restart)
        assert _COOKIE_CSRF not in json.dumps(after_restart)
    finally:
        restarted_client.close()
        restarted_server.stop(grace=0).wait(timeout=5)
