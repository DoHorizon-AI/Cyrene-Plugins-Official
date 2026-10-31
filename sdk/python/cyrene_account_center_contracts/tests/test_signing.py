"""Canonical signing contract tests shared by Account Center clients."""

import hashlib
import hmac
import json
from dataclasses import asdict

import pytest
from cyrene_account_center_contracts import (
    AzureDevOpsOperation,
    BeginAzureDevOpsLinkRequest,
    BeginHuggingFaceRenewalRequest,
    BeginMicrosoftGraphLinkRequest,
    BeginMicrosoftGraphScopeUpgradeRequest,
    CompleteAzureDevOpsLinkRequest,
    CompleteMicrosoftGraphLinkRequest,
    CompleteMicrosoftGraphScopeUpgradeRequest,
    HuggingFaceRenewalSession,
    MicrosoftGraphMailFeatures,
    MicrosoftGraphOperation,
    MicrosoftGraphPermissionUpgrade,
    PollHuggingFaceRenewalRequest,
    RedeemProviderCredentialLeaseRequest,
    UpdateMicrosoftGraphV2ScopeRequest,
    canonical_json,
    payload_sha256,
    sign_context,
    verify_context,
)


def test_huggingface_renewal_dtos_round_trip_as_public_wire_json() -> None:
    """Keep the new renewal DTO field names stable for Exchange serialization."""

    values = (
        (
            BeginHuggingFaceRenewalRequest(
                profile_ref="profile-1",
                session_ref="session-1",
            ),
            {"profile_ref": "profile-1", "session_ref": "session-1"},
        ),
        (
            PollHuggingFaceRenewalRequest(
                profile_ref="profile-1",
                session_ref="session-1",
            ),
            {"profile_ref": "profile-1", "session_ref": "session-1"},
        ),
        (
            HuggingFaceRenewalSession(
                profile_ref="profile-1",
                session_ref="session-1",
                state="waiting_user",
                user_code="L94P-HPL8",
                verification_uri="https://hf.co/oauth/device",
                expires_at=1_800_000_000,
                interval_seconds=5,
                generation=7,
                reason_code=None,
            ),
            {
                "profile_ref": "profile-1",
                "session_ref": "session-1",
                "state": "waiting_user",
                "user_code": "L94P-HPL8",
                "verification_uri": "https://hf.co/oauth/device",
                "expires_at": 1_800_000_000,
                "interval_seconds": 5,
                "generation": 7,
                "reason_code": None,
            },
        ),
    )

    for dto, expected in values:
        serialized = asdict(dto)
        assert serialized == expected
        decoded = json.loads(canonical_json(serialized))
        assert decoded == expected
        assert canonical_json(decoded) == canonical_json(expected)


def test_provider_lease_request_contract_keeps_profile_scope() -> None:
    request = RedeemProviderCredentialLeaseRequest(
        lease_ref="00000000-0000-4000-8000-000000000002",
        profile_ref="00000000-0000-4000-8000-000000000001",
        operation_id="github.repository.get",
        target={"repository_id": "123", "repository": "owner/repo"},
        arguments_sha256="a" * 64,
    )

    assert asdict(request) == {
        "lease_ref": "00000000-0000-4000-8000-000000000002",
        "profile_ref": "00000000-0000-4000-8000-000000000001",
        "operation_id": "github.repository.get",
        "target": {"repository_id": "123", "repository": "owner/repo"},
        "arguments_sha256": "a" * 64,
    }


def test_graph_scope_upgrade_completion_dto_contains_only_callback_fields() -> None:
    request = CompleteMicrosoftGraphScopeUpgradeRequest(
        state="synthetic-state",
        code="synthetic-code",
    )

    serialized = asdict(request)
    assert serialized == {"state": "synthetic-state", "code": "synthetic-code"}
    assert json.loads(canonical_json(serialized)) == serialized


def test_rfc8785_canonical_vector_matches_javascript_key_and_number_order() -> None:
    """Share the same UTF-16 key ordering and number spellings with Exchange and Navigator."""

    value = {
        "\ue000": "bmp",
        "𐀀": "supplementary",
        "small": 1e-7,
        "decimal": 1e-6,
    }
    expected = (
        '{"decimal":0.000001,"small":1e-7,"𐀀":"supplementary","":"bmp"}'
    ).encode()

    assert canonical_json(value) == expected
    assert payload_sha256(value) == (
        "201897b3c1db3ba8f346a7cd54066c3879fb96d13c7bd04b59e1c4e0a1992bd5"
    )


def test_teams_resource_and_arguments_hashes_match_exchange_navigator_vector() -> None:
    """Bind the Teams root-thread route and payload digests to the shared JCS bytes."""

    route = {
        "tenantId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "teamId": "team-1",
        "channelId": "channel-1",
        "conversationId": "19:channel-conversation",
        "rootThreadId": "1698765432101",
        "serviceUrl": "https://smba.trafficmanager.net/emea/",
    }
    arguments = {"conversation": route, "text": "The task completed."}

    assert payload_sha256(route) == (
        "78c889cfc0c6f06fa00005ce2bb2e017a1008a4f38694bfc7e1c3bfb2155e375"
    )
    assert payload_sha256(arguments) == (
        "813a88c0637c1de73e4a74757216a62e3fd84e7e98d1f6f97de923f1d42e251f"
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2**53, -(2**53)])
def test_rfc8785_rejects_non_finite_and_unsafe_numeric_values(value: object) -> None:
    """Never round numbers outside the interoperable I-JSON/JCS domain."""

    with pytest.raises(ValueError):
        canonical_json(value)


def test_context_binds_method_body_actor_and_binding() -> None:
    key = b"k" * 32
    payload = {"account_ref": "ref", "operation_id": "github.issue.get"}
    context = sign_context(
        method="create_credential_lease",
        payload=payload,
        caller_id="exchange",
        issuer="https://platform.example",
        subject="user-1",
        organization_id="org-1",
        workspace_id=None,
        binding_id="binding-1",
        request_id="request-1",
        operation_id="create_credential_lease",
        key=key,
        issued_at=100,
        expires_at=200,
    )
    assert verify_context(
        method="create_credential_lease",
        payload=payload,
        context=context,
        keys={"exchange": key},
        now=150,
    )
    assert not verify_context(
        method="create_credential_lease",
        payload={**payload, "account_ref": "other"},
        context=context,
        keys={"exchange": key},
        now=150,
    )
    assert not verify_context(
        method="create_credential_lease",
        payload=payload,
        context=context,
        keys={"exchange": key},
        now=150,
        expected_caller_id="github-provider",
    )
    assert not verify_context(
        method="redeem_credential_lease",
        payload=payload,
        context=context,
        keys={"exchange": key},
        now=150,
    )


def test_context_expiry_and_future_skew_are_bounded() -> None:
    key = b"k" * 32
    kwargs = {
        "method": "list_links",
        "payload": {},
        "caller_id": "exchange",
        "issuer": "issuer",
        "subject": "subject",
        "organization_id": "org",
        "workspace_id": None,
        "binding_id": "binding",
        "request_id": "request",
        "operation_id": "list_links",
        "key": key,
    }
    with pytest.raises(ValueError, match="context lifetime"):
        sign_context(**kwargs, issued_at=100, expires_at=401)
    future = sign_context(**kwargs, issued_at=181, expires_at=200)
    assert not verify_context(
        method="list_links", payload={}, context=future, keys={"exchange": key}, now=150
    )


def test_provider_profile_lease_signature_binds_target_and_argument_digest() -> None:
    key = b"p" * 32
    payload = {
        "profile_ref": "00000000-0000-4000-8000-000000000001",
        "operation_id": "huggingface.file.upload",
        "target": {
            "repo_type": "model",
            "repo_id": "owner/model",
            "path": "artifacts/config.json",
        },
        "arguments_sha256": "a" * 64,
    }
    context = sign_context(
        method="create_provider_credential_lease",
        payload=payload,
        caller_id="exchange",
        issuer="https://platform.example",
        subject="user-1",
        organization_id="org-1",
        workspace_id="workspace-1",
        binding_id="account-center-binding",
        request_id="request-lease-1",
        operation_id="create_provider_credential_lease",
        key=key,
        issued_at=100,
        expires_at=200,
    )

    assert verify_context(
        method="create_provider_credential_lease",
        payload=payload,
        context=context,
        keys={"exchange": key},
        now=150,
    )
    for altered in (
        {**payload, "profile_ref": "00000000-0000-4000-8000-000000000002"},
        {**payload, "target": {**payload["target"], "path": "other.json"}},
        {**payload, "arguments_sha256": "b" * 64},
    ):
        assert not verify_context(
            method="create_provider_credential_lease",
            payload=altered,
            context=context,
            keys={"exchange": key},
            now=150,
        )


def test_provider_oauth_dtos_keep_scopes_and_credentials_out_of_requests() -> None:
    """Keep Entra link requests limited to selected targets and callback values."""

    graph = BeginMicrosoftGraphLinkRequest(
        profile_ref="00000000-0000-4000-8000-000000000401",
        planner_plan_ids=["plan_opaque-1"],
        idempotency_key="graph-link-1",
    )
    graph_upgrade = BeginMicrosoftGraphScopeUpgradeRequest(
        profile_ref=graph.profile_ref,
        permission=MicrosoftGraphPermissionUpgrade.PLANNER_TASKS_WRITE,
        idempotency_key="graph-upgrade-1",
    )
    ado = BeginAzureDevOpsLinkRequest(
        profile_ref="00000000-0000-4000-8000-000000000402",
        tenant_id="11111111-2222-4333-8444-555555555555",
        organization="cyrene-devops",
        project_id="12345678-1234-4234-8234-123456789abc",
        source_id="pipeline-source-1",
        source_kind="pipeline_runs",
        pipeline_id=42,
        idempotency_key="ado-link-1",
    )
    callbacks = (
        CompleteMicrosoftGraphLinkRequest(state="random-state", code="one-time-code"),
        CompleteAzureDevOpsLinkRequest(state="random-state", code="one-time-code"),
    )

    assert set(asdict(graph)) == {
        "profile_ref",
        "planner_plan_ids",
        "idempotency_key",
        "office_v2_scope",
    }
    assert asdict(graph)["office_v2_scope"] is None
    assert set(asdict(graph_upgrade)) == {
        "profile_ref",
        "permission",
        "idempotency_key",
        "site_id",
        "list_ids",
        "mail_features",
    }
    assert asdict(graph_upgrade)["mail_features"] is None
    assert set(asdict(ado)) == {
        "profile_ref",
        "tenant_id",
        "organization",
        "project_id",
        "source_id",
        "source_kind",
        "idempotency_key",
        "pipeline_id",
        "definition_id",
        "definition_environment_id",
    }
    assert all(set(asdict(callback)) == {"state", "code"} for callback in callbacks)
    assert MicrosoftGraphOperation.PLANNER_TASK_CREATE == (
        "microsoft_graph.planner.task.create"
    )
    assert MicrosoftGraphOperation.MAIL_MESSAGE_SEND == "microsoft_graph.mail.message.send"
    assert AzureDevOpsOperation.RELEASE_ENVIRONMENT_GET == (
        "azure_devops.release_environment.get"
    )


def test_mail_scope_upgrade_dto_is_one_closed_feature_selection() -> None:
    """Serialize one combined Mail consent without exposing raw scopes."""

    features = MicrosoftGraphMailFeatures(
        metadata_read=True,
        drafts_write=False,
        send=True,
    )
    request = BeginMicrosoftGraphScopeUpgradeRequest(
        profile_ref="00000000-0000-4000-8000-000000000401",
        permission=MicrosoftGraphPermissionUpgrade.MAIL,
        idempotency_key="graph-mail-upgrade-1",
        mail_features=features,
    )

    payload = asdict(request)
    assert payload["permission"] == "mail"
    assert payload["mail_features"] == {
        "metadata_read": True,
        "drafts_write": False,
        "send": True,
    }
    assert "scopes" not in payload
    with pytest.raises(ValueError, match="at least one Mail feature"):
        MicrosoftGraphMailFeatures(False, False, False)
    with pytest.raises(ValueError, match="mail_features is valid only"):
        BeginMicrosoftGraphScopeUpgradeRequest(
            profile_ref="00000000-0000-4000-8000-000000000401",
            permission=MicrosoftGraphPermissionUpgrade.PLANNER_TASKS_WRITE,
            idempotency_key="graph-write-upgrade-1",
            mail_features=features,
        )


def test_microsoft_graph_link_dto_preserves_v1_and_v2_wire_selectors() -> None:
    """Keep the v1 positional contract and make v2 requests omit the v1 selector."""

    v1 = BeginMicrosoftGraphLinkRequest(
        "00000000-0000-4000-8000-000000000401",
        ["plan_opaque-1"],
        "graph-link-v1",
    )
    scope = {
        "schema_version": 2,
        "tenant_id": "11111111-2222-4333-8444-555555555555",
        "workspace_id": "22222222-3333-4444-8555-666666666666",
        "planner": {"plan_ids": [], "tasks_read": False, "tasks_write": False},
        "sharepoint": {
            "enabled": False,
            "site_id": None,
            "list_ids": [],
            "drive_ids": [],
            "metadata_read": False,
            "content_read": False,
        },
        "todo": {
            "enabled": False,
            "task_list_ids": [],
            "tasks_read": False,
            "tasks_write": False,
        },
        "calendar": {
            "enabled": False,
            "calendar_ids": [],
            "events_read": False,
            "freebusy_read": False,
            "events_write": False,
            "freebusy_subjects": [],
            "attendees_write": False,
        },
        "mail": {
            "enabled": False,
            "folder_ids": [],
            "metadata_read": False,
            "body_read": False,
            "drafts_write": False,
            "send_enabled": False,
        },
    }
    v2 = BeginMicrosoftGraphLinkRequest(
        profile_ref="00000000-0000-4000-8000-000000000401",
        idempotency_key="graph-link-v2",
        office_v2_scope=scope,
    )

    assert asdict(v1) == {
        "profile_ref": "00000000-0000-4000-8000-000000000401",
        "planner_plan_ids": ["plan_opaque-1"],
        "idempotency_key": "graph-link-v1",
        "office_v2_scope": None,
    }
    assert asdict(v2) == {
        "profile_ref": "00000000-0000-4000-8000-000000000401",
        "planner_plan_ids": None,
        "idempotency_key": "graph-link-v2",
        "office_v2_scope": scope,
    }


def test_microsoft_graph_v2_scope_update_dto_has_exact_cas_fields() -> None:
    """Expose the exact owner/workspace/revision-bound resource-selection request."""

    scope = {
        "schema_version": 2,
        "tenant_id": "11111111-2222-4333-8444-555555555555",
        "workspace_id": "22222222-3333-4444-8555-666666666666",
    }
    update = UpdateMicrosoftGraphV2ScopeRequest(
        profile_ref="00000000-0000-4000-8000-000000000401",
        workspace_id=scope["workspace_id"],
        expected_credential_revision=3,
        target_scope=scope,
        idempotency_key="graph-scope-update-1",
    )

    assert set(asdict(update)) == {
        "profile_ref",
        "workspace_id",
        "expected_credential_revision",
        "target_scope",
        "idempotency_key",
    }
    with pytest.raises(ValueError, match="exactly one Graph link scope"):
        BeginMicrosoftGraphLinkRequest(
            profile_ref="profile-1",
            idempotency_key="graph-link-invalid",
        )
    with pytest.raises(ValueError, match="exactly one Graph link scope"):
        BeginMicrosoftGraphLinkRequest(
            profile_ref="profile-1",
            planner_plan_ids=[],
            idempotency_key="graph-link-invalid",
            office_v2_scope=scope,
        )
    with pytest.raises(ValueError, match="non-negative"):
        UpdateMicrosoftGraphV2ScopeRequest(
            profile_ref="profile-1",
            workspace_id=scope["workspace_id"],
            expected_credential_revision=True,
            target_scope=scope,
            idempotency_key="graph-scope-update-invalid",
        )


def test_sdk_signer_and_verifier_reject_empty_trusted_claims() -> None:
    key = b"k" * 32
    kwargs = {
        "method": "list_links",
        "payload": {},
        "caller_id": "exchange",
        "issuer": "issuer",
        "subject": "subject",
        "organization_id": "org",
        "workspace_id": None,
        "binding_id": "binding",
        "request_id": "request",
        "operation_id": "list_links",
        "key": key,
        "issued_at": 100,
        "expires_at": 200,
    }
    for field in (
        "method",
        "caller_id",
        "issuer",
        "subject",
        "organization_id",
        "binding_id",
        "request_id",
        "operation_id",
    ):
        with pytest.raises(ValueError, match="claims"):
            sign_context(**{**kwargs, field: ""})
    with pytest.raises(ValueError, match="claims"):
        sign_context(**{**kwargs, "workspace_id": ""})

    valid = sign_context(**kwargs).as_dict()
    for field in (
        "method",
        "caller_id",
        "issuer",
        "subject",
        "organization_id",
        "binding_id",
        "request_id",
        "operation_id",
        "workspace_id",
    ):
        malformed = dict(valid)
        malformed[field] = ""
        signed_fields = {
            name: value for name, value in malformed.items() if name != "signature"
        }
        malformed["signature"] = hmac.new(
            key, canonical_json(signed_fields), hashlib.sha256
        ).hexdigest()
        assert not verify_context(
            method="list_links",
            payload={},
            context=malformed,
            keys={"exchange": key},
            now=150,
        )
