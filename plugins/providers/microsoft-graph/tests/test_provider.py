"""
┌────────────────────────────────────────────────────────────────────┐
│  Provider trust and Account Center scope tests                       │
│  验证用户配置租约、固定资源范围与权限拒绝                              │
└────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from cyrene_account_center_contracts import (
    AuthMaterial,
    ProviderProfile,
    payload_sha256,
    sign_context,
    verify_context,
)

from microsoft_graph_provider._generated import tool_provider_pb2 as wire
from microsoft_graph_provider.account_center import AccountCenterUnavailable
from microsoft_graph_provider.microsoft_graph_api import HttpResponse, MicrosoftGraphApi
from microsoft_graph_provider.plugin import (
    MicrosoftGraphProvider,
    _arguments_for_lease,
    _target,
)
from microsoft_graph_provider.settings import PROVIDER_CALLER_ID, ProviderSettings

TENANT_ID = "11111111-1111-4111-8111-111111111111"
ACTOR_ID = "22222222-2222-4222-8222-222222222222"
PROFILE_REF = "44444444-4444-4444-8444-444444444444"
LEASE_REF = "55555555-5555-4555-8555-555555555555"
PLAN_ID = "plan_01"
SYNTHETIC_TOKEN = "synthetic-delegated-token"
EXCHANGE_CALLER_ID = "exchange-test-caller"
CYRENE_ACTOR = {
    "issuer": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
    "subject": ACTOR_ID,
    "organization_id": "org-test-1",
    "workspace_id": "workspace-test-1",
    "binding_id": "graph-binding-1",
}


def _scope(
    *,
    plans: list[str] | None = None,
    tasks_write: bool = False,
    sharepoint_enabled: bool = False,
    site_id: str | None = None,
    list_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "tenant_id": TENANT_ID,
        "planner": {
            "plan_ids": [PLAN_ID] if plans is None else plans,
            "tasks_read": True,
            "tasks_write": tasks_write,
        },
        "sharepoint": {
            "enabled": sharepoint_enabled,
            "site_id": site_id,
            "list_ids": [] if list_ids is None else list_ids,
        },
    }


def _profile(
    scope: dict[str, Any],
    *,
    auth_status: str = "healthy",
    access_mode: str = "actor_owned",
    auth_mode: str = "delegated_oauth",
    oauth_scopes: list[str] | None = None,
    external_subject: str | None = None,
) -> ProviderProfile:
    external_subject = external_subject or f"{TENANT_ID}:{ACTOR_ID}"
    expected_scopes = ["User.Read", "Tasks.Read"]
    if scope["planner"]["tasks_write"]:
        expected_scopes.append("Tasks.ReadWrite")
    if scope["sharepoint"]["enabled"]:
        expected_scopes.append("Sites.Selected")
    return ProviderProfile(
        profile_ref=PROFILE_REF,
        provider="microsoft_graph",
        identity_ref=external_subject,
        target_scope=scope,
        auth_status=auth_status,
        auth_mode=auth_mode,
        oauth_scopes=expected_scopes if oauth_scopes is None else oauth_scopes,
        access_mode=access_mode,
        external_subject=external_subject,
        expires_at=int(time.time()) + 3600,
    )


def _settings(
    *,
    read_only: bool = False,
    sharepoint_site_id: str | None = None,
    sharepoint_list_ids: tuple[str, ...] = (),
) -> ProviderSettings:
    return ProviderSettings(
        binding_id=CYRENE_ACTOR["binding_id"],
        exchange_caller_id=EXCHANGE_CALLER_ID,
        exchange_verification_key=b"e" * 32,
        account_center_binding_id="account-center-binding-1",
        account_center_connection_ref="grpc://127.0.0.1:9000",
        account_center_transport="local",
        provider_caller_id=PROVIDER_CALLER_ID,
        provider_signing_key=b"p" * 32,
        account_center_root_cert_ref=None,
        account_center_client_cert_ref=None,
        account_center_client_key_ref=None,
        host_secret_names={},
        read_only=read_only,
        timeout_seconds=3.0,
        max_page_size=25,
        sharepoint_site_id=sharepoint_site_id,
        sharepoint_list_ids=sharepoint_list_ids,
    )


class FakeAccountCenter:
    """Return only synthetic actor profiles and lease material."""

    def __init__(
        self,
        profile: ProviderProfile,
        *,
        scope_digest: str | None = None,
        token_external_subject: str | None = None,
    ) -> None:
        self.profile = profile
        self.scope_digest = scope_digest or payload_sha256(profile.target_scope)
        self.token_external_subject = token_external_subject or profile.external_subject
        self.list_actors: list[dict[str, str | None]] = []
        self.redeem_payloads: list[dict[str, object]] = []
        self.redeem_contexts: list[dict[str, Any]] = []

    def list_provider_profiles(self, trusted_context):
        self.list_actors.append(dict(trusted_context))
        return (self.profile,)

    def redeem_provider_credential_lease(self, payload, trusted_context):
        self.redeem_payloads.append(dict(payload))
        self.redeem_contexts.append(dict(trusted_context))
        return AuthMaterial(
            SYNTHETIC_TOKEN,
            "Bearer",
            self.token_external_subject,
            int(time.time()) + 600,
            self.scope_digest,
        )

    def close(self) -> None:
        return None


class FakeTransport:
    """Return deterministic Graph responses without network access."""

    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, str, dict[str, str]]] = []

    def request(self, method, host, path, headers, body, timeout_seconds):
        del body, timeout_seconds
        self.calls.append((method, host, path, dict(headers)))
        if not self.responses:
            raise AssertionError("unexpected Graph request")
        return self.responses.pop(0)


def _response(value: object) -> HttpResponse:
    return HttpResponse(200, {"content-type": "application/json"}, json.dumps(value).encode())


def _trusted_call(
    tool_id: str,
    arguments: dict[str, Any],
    *,
    issuer: str = CYRENE_ACTOR["issuer"],
    subject: str = CYRENE_ACTOR["subject"],
    scope: dict[str, Any] | None = None,
) -> bytes:
    profile_ref = PROFILE_REF
    lease_ref = LEASE_REF
    selected_scope = _scope() if scope is None else scope
    signed_payload = {
        "payload": arguments,
        "profile_ref": profile_ref,
        "lease_ref": lease_ref,
        "target": _target(selected_scope, tool_id, arguments),
        "arguments_sha256": payload_sha256(_arguments_for_lease(tool_id, arguments)),
        "profile_scope_sha256": payload_sha256(selected_scope),
    }
    now = int(time.time())
    context = sign_context(
        method="call_tool",
        payload=signed_payload,
        caller_id=EXCHANGE_CALLER_ID,
        issuer=issuer,
        subject=subject,
        organization_id=CYRENE_ACTOR["organization_id"],
        workspace_id=CYRENE_ACTOR["workspace_id"],
        binding_id=CYRENE_ACTOR["binding_id"],
        request_id=str(uuid.uuid4()),
        operation_id=tool_id,
        key=b"e" * 32,
        issued_at=now,
        expires_at=now + 60,
    )
    envelope = {
        **signed_payload,
        "trusted_context": context.as_dict(),
    }
    request = wire.CallToolRequest(
        binding_id=CYRENE_ACTOR["binding_id"],
        provider_tool_id=tool_id,
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )
    return request.SerializeToString()


def _tool_result(result: tuple[bool, Any]) -> dict[str, Any]:
    ok, payload = result
    assert ok is True
    response = wire.CallToolResponse.FromString(payload.value)
    return json.loads(response.outcome.content[0].json.json)


def test_actor_owned_profile_lease_calls_me_before_selected_plan() -> None:
    scope = _scope()
    account_center = FakeAccountCenter(_profile(scope))
    transport = FakeTransport(
        [
            _response({"id": ACTOR_ID}),
            _response({"id": PLAN_ID, "title": "Pilot plan"}),
        ]
    )
    graph_api = MicrosoftGraphApi(cursor_key=b"p" * 32, transport=transport)
    provider = MicrosoftGraphProvider(
        settings=_settings(),
        account_center=account_center,
        graph_api=graph_api,
    )

    result = _tool_result(
        provider._call_tool(_trusted_call("microsoft_graph.planner.plan.get", {"plan_id": PLAN_ID}))
    )

    assert result == {
        "plan_id": PLAN_ID,
        "title": "Pilot plan",
        "created_date_time": None,
        "etag": None,
    }
    assert account_center.list_actors[0]["subject"] == CYRENE_ACTOR["subject"]
    assert account_center.redeem_payloads[0]["target"] == {
        "tenant_id": TENANT_ID,
        "plan_id": PLAN_ID,
    }
    assert account_center.redeem_payloads[0]["arguments_sha256"] == payload_sha256({})
    assert verify_context(
        method="redeem_provider_credential_lease",
        payload=account_center.redeem_payloads[0],
        context=account_center.redeem_contexts[0],
        keys={PROVIDER_CALLER_ID: b"p" * 32},
        expected_caller_id=PROVIDER_CALLER_ID,
    )
    assert [url.rsplit("?", 1)[0] for _, _, url, _ in transport.calls] == [
        "/v1.0/me",
        f"/v1.0/planner/plans/{PLAN_ID}",
    ]
    assert all(call[3]["Authorization"] == f"Bearer {SYNTHETIC_TOKEN}" for call in transport.calls)


def test_unselected_plan_is_denied_before_token_redemption_or_graph_access() -> None:
    account_center = FakeAccountCenter(_profile(_scope(plans=["other_plan"])))
    transport = FakeTransport([])
    graph_api = MicrosoftGraphApi(cursor_key=b"p" * 32, transport=transport)
    provider = MicrosoftGraphProvider(
        settings=_settings(),
        account_center=account_center,
        graph_api=graph_api,
    )

    result = _tool_result(
        provider._call_tool(_trusted_call("microsoft_graph.planner.plan.get", {"plan_id": PLAN_ID}))
    )

    assert result["error"]["code"] == "AUTHORIZATION_DENIED"
    assert account_center.redeem_payloads == []
    assert transport.calls == []


def test_outer_trusted_context_still_binds_target_ids_when_lease_digest_excludes_them() -> None:
    account_center = FakeAccountCenter(_profile(_scope()))
    transport = FakeTransport([])
    provider = MicrosoftGraphProvider(
        settings=_settings(),
        account_center=account_center,
        graph_api=MicrosoftGraphApi(cursor_key=b"p" * 32, transport=transport),
    )
    request = wire.CallToolRequest.FromString(
        _trusted_call("microsoft_graph.planner.plan.get", {"plan_id": PLAN_ID})
    )
    envelope = json.loads(request.arguments_json)
    envelope["payload"]["plan_id"] = "different_plan"
    request.arguments_json = json.dumps(envelope, separators=(",", ":"))

    result = _tool_result(provider._call_tool(request.SerializeToString()))

    assert result["error"]["code"] == "AUTHENTICATION_DENIED"
    assert account_center.list_actors == []
    assert account_center.redeem_payloads == []
    assert transport.calls == []


def test_actor_a_cannot_use_actor_b_profile_even_when_account_center_returns_it() -> None:
    actor_b_subject = f"{TENANT_ID}:88888888-8888-4888-8888-888888888888"
    account_center = FakeAccountCenter(_profile(_scope(), external_subject=actor_b_subject))
    transport = FakeTransport([])
    provider = MicrosoftGraphProvider(
        settings=_settings(),
        account_center=account_center,
        graph_api=MicrosoftGraphApi(cursor_key=b"p" * 32, transport=transport),
    )

    result = _tool_result(
        provider._call_tool(_trusted_call("microsoft_graph.planner.plan.get", {"plan_id": PLAN_ID}))
    )

    assert result["error"]["code"] == "TRUSTED_ACTOR_PROFILE_MISMATCH"
    assert account_center.redeem_payloads == []
    assert transport.calls == []


def test_missing_canonical_entra_mapping_is_denied_before_account_or_graph_calls() -> None:
    account_center = FakeAccountCenter(_profile(_scope()))
    transport = FakeTransport([])
    provider = MicrosoftGraphProvider(
        settings=_settings(),
        account_center=account_center,
        graph_api=MicrosoftGraphApi(cursor_key=b"p" * 32, transport=transport),
    )

    result = _tool_result(
        provider._call_tool(
            _trusted_call(
                "microsoft_graph.planner.plan.get",
                {"plan_id": PLAN_ID},
                issuer="https://identity.cyrene.example",
                subject="actor-without-entra-mapping",
            )
        )
    )

    assert result["error"]["code"] == "TRUSTED_ACTOR_MAPPING_REQUIRED"
    assert account_center.list_actors == []
    assert transport.calls == []


def test_revoked_or_workspace_operator_profile_is_denied_before_redemption() -> None:
    for profile in (
        _profile(_scope(), auth_status="revoked"),
        _profile(_scope(), access_mode="workspace_operator"),
        _profile(_scope(), auth_mode="host_identity"),
    ):
        account_center = FakeAccountCenter(profile)
        provider = MicrosoftGraphProvider(
            settings=_settings(),
            account_center=account_center,
            graph_api=MicrosoftGraphApi(cursor_key=b"p" * 32, transport=FakeTransport([])),
        )
        result = _tool_result(
            provider._call_tool(
                _trusted_call(
                    "microsoft_graph.planner.plan.get",
                    {"plan_id": PLAN_ID},
                    scope=_scope(plans=["other_plan"]),
                )
            )
        )
        assert result["error"]["code"] == "AUTHORIZATION_DENIED"
        assert account_center.redeem_payloads == []


def test_profile_scope_digest_mismatch_stops_before_graph_request() -> None:
    profile = _profile(_scope())
    account_center = FakeAccountCenter(profile, scope_digest="0" * 64)
    transport = FakeTransport([])
    provider = MicrosoftGraphProvider(
        settings=_settings(),
        account_center=account_center,
        graph_api=MicrosoftGraphApi(cursor_key=b"p" * 32, transport=transport),
    )

    result = _tool_result(
        provider._call_tool(_trusted_call("microsoft_graph.planner.plan.get", {"plan_id": PLAN_ID}))
    )

    assert result["error"]["code"] == "PROFILE_SCOPE_MISMATCH"
    assert len(account_center.redeem_payloads) == 1
    assert transport.calls == []


def test_profile_with_unapproved_mail_scope_is_rejected_before_redemption() -> None:
    account_center = FakeAccountCenter(
        _profile(_scope(), oauth_scopes=["User.Read", "Tasks.Read", "Mail.Read"])
    )
    transport = FakeTransport([])
    provider = MicrosoftGraphProvider(
        settings=_settings(),
        account_center=account_center,
        graph_api=MicrosoftGraphApi(cursor_key=b"p" * 32, transport=transport),
    )

    result = _tool_result(
        provider._call_tool(_trusted_call("microsoft_graph.planner.plan.get", {"plan_id": PLAN_ID}))
    )

    assert result["error"]["code"] == "PROFILE_SCOPE_MISMATCH"
    assert account_center.redeem_payloads == []
    assert transport.calls == []


def test_token_for_different_profile_subject_fails_without_resource_read() -> None:
    profile = _profile(_scope())
    other_subject = f"{TENANT_ID}:88888888-8888-4888-8888-888888888888"
    account_center = FakeAccountCenter(profile, token_external_subject=other_subject)
    transport = FakeTransport([])
    provider = MicrosoftGraphProvider(
        settings=_settings(),
        account_center=account_center,
        graph_api=MicrosoftGraphApi(cursor_key=b"p" * 32, transport=transport),
    )

    result = _tool_result(
        provider._call_tool(_trusted_call("microsoft_graph.planner.plan.get", {"plan_id": PLAN_ID}))
    )

    assert result["error"]["code"] == "TRUSTED_ACTOR_PROFILE_MISMATCH"
    assert transport.calls == []


def test_sharepoint_unselected_list_is_denied_before_redemption() -> None:
    site_id = (
        "tenant.sharepoint.com,"
        "55555555-5555-4555-8555-555555555555,"
        "66666666-6666-4666-8666-666666666666"
    )
    list_id = "77777777-7777-4777-8777-777777777777"
    selected_list = "99999999-9999-4999-8999-999999999999"
    scope = _scope(
        sharepoint_enabled=True,
        site_id=site_id,
        list_ids=[selected_list],
    )
    account_center = FakeAccountCenter(_profile(scope))
    transport = FakeTransport([])
    provider = MicrosoftGraphProvider(
        settings=_settings(
            sharepoint_site_id=site_id,
            sharepoint_list_ids=(selected_list,),
        ),
        account_center=account_center,
        graph_api=MicrosoftGraphApi(cursor_key=b"p" * 32, transport=transport),
    )

    result = _tool_result(
        provider._call_tool(
            _trusted_call(
                "microsoft_graph.sharepoint.list.get",
                {"site_id": site_id, "list_id": list_id},
                scope=scope,
            )
        )
    )

    assert result["error"]["code"] == "AUTHORIZATION_DENIED"
    assert account_center.redeem_payloads == []
    assert transport.calls == []


def test_sharepoint_profile_must_match_optional_host_binding_allowlist() -> None:
    site_id = (
        "tenant.sharepoint.com,"
        "55555555-5555-4555-8555-555555555555,"
        "66666666-6666-4666-8666-666666666666"
    )
    list_id = "77777777-7777-4777-8777-777777777777"
    scope = _scope(sharepoint_enabled=True, site_id=site_id, list_ids=[list_id])
    different_list = "88888888-8888-4888-8888-888888888888"

    for settings in (
        _settings(),
        _settings(
            sharepoint_site_id=site_id,
            sharepoint_list_ids=(different_list,),
        ),
    ):
        account_center = FakeAccountCenter(_profile(scope))
        transport = FakeTransport([])
        provider = MicrosoftGraphProvider(
            settings=settings,
            account_center=account_center,
            graph_api=MicrosoftGraphApi(cursor_key=b"p" * 32, transport=transport),
        )

        result = _tool_result(
            provider._call_tool(
                _trusted_call(
                    "microsoft_graph.sharepoint.site.get",
                    {"site_id": site_id},
                    scope=scope,
                )
            )
        )

        assert result["error"]["code"] == "PROFILE_SCOPE_MISMATCH"
        assert account_center.redeem_payloads == []
        assert transport.calls == []


def test_read_only_binding_hides_write_tools() -> None:
    provider = MicrosoftGraphProvider(
        settings=_settings(read_only=True),
        account_center=FakeAccountCenter(_profile(_scope(tasks_write=True))),
        graph_api=MicrosoftGraphApi(cursor_key=b"p" * 32, transport=FakeTransport([])),
    )
    request = wire.ListToolsRequest(binding_id=CYRENE_ACTOR["binding_id"])

    ok, payload = provider._list_tools(request.SerializeToString())

    assert ok is True
    response = wire.ListToolsResponse.FromString(payload.value)
    tool_ids = {tool.provider_tool_id for tool in response.catalog.tools}
    assert "microsoft_graph.planner.task.create" not in tool_ids
    assert "microsoft_graph.planner.task.update" not in tool_ids
    assert "microsoft_graph.planner.tasks.list" in tool_ids


def test_account_center_channel_failure_is_sanitized_and_retryable() -> None:
    class FailedAccountCenter(FakeAccountCenter):
        def list_provider_profiles(self, trusted_context):
            raise AccountCenterUnavailable("synthetic internal transport detail")

    provider = MicrosoftGraphProvider(
        settings=_settings(),
        account_center=FailedAccountCenter(_profile(_scope())),
        graph_api=MicrosoftGraphApi(cursor_key=b"p" * 32, transport=FakeTransport([])),
    )

    result = _tool_result(
        provider._call_tool(_trusted_call("microsoft_graph.planner.plan.get", {"plan_id": PLAN_ID}))
    )

    assert result == {"error": {"code": "AUTHORIZATION_UNAVAILABLE", "retryable": True}}
