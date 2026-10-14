"""
┌──────────────────────────────────────────────────────────────────────┐
│  test_integration_support_contracts.py                              │
│  Module: contracts.tck.tool_provider_v1                              │
│  Role: Check integration support DTO, schema, and trust invariants.  │
│                                                                      │
│  模块职责：校验集成支持 DTO、schema 与可信上下文边界。                  │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from cyrene_integration_contracts import (
    TOOL_OPERATIONS,
    AuthorizationState,
    CloudQueryResult,
    CloudResourceKind,
    CloudResourceState,
    EmailSubmission,
    EmailSubmissionStatus,
    LoginState,
    RenewalState,
    parse_authorization_health,
    parse_cloud_query_result,
    parse_email_submission,
    parse_renewal_session,
    parse_tool_envelope,
    schema_for,
    serialize_authorization_health,
    serialize_cloud_query_result,
    serialize_email_submission,
    serialize_renewal_session,
    serialize_tool_envelope,
    sign_context,
    validate_schema,
    verify_context,
    write_replay_identity,
)

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = ROOT / "contracts/json/integration-support/v1/schema.json"
FIXED_NOW = datetime.now(UTC).replace(microsecond=0)
KEY = b"k" * 32
BASE_ARGUMENTS = {"target_ref": "configured-cloud-main"}


def _schema() -> dict[str, Any]:
    """Load the canonical repository schema used by tool-provider consumers."""

    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _context(
    *,
    method: str = "cloud_authorization_health",
    operation_id: str | None = None,
    payload: Any = BASE_ARGUMENTS,
    issued_at: datetime = FIXED_NOW - timedelta(seconds=5),
    expires_at: datetime = FIXED_NOW + timedelta(seconds=90),
    caller_workload: str = "exchange-worker",
    audience: str = "cloud-resources-provider",
    issuer: str = "https://identity.example",
    subject: str = "operator-subject-1",
    organization_id: str = "org-test-1",
    workspace_id: str | None = "workspace-test-1",
    binding_id: str = "cloud-binding-1",
    request_id: str = "request-test-1",
) -> Any:
    """Create deterministic test authorization using a private fixed key."""

    return sign_context(
        method=method,
        operation_id=operation_id or TOOL_OPERATIONS[method],
        payload=payload,
        caller_workload=caller_workload,
        audience=audience,
        issuer=issuer,
        subject=subject,
        organization_id=organization_id,
        workspace_id=workspace_id,
        binding_id=binding_id,
        request_id=request_id,
        key=KEY,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _envelope(arguments: dict[str, Any] = BASE_ARGUMENTS) -> dict[str, Any]:
    """Build one valid cloud authorization health envelope."""

    context = _context(payload=arguments)
    return {"caller_context": context.as_dict(), "arguments": arguments}


def test_schema_is_valid_packaged_and_closed() -> None:
    """Keep the public schema and installed SDK schema byte-identical."""

    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    bundled = files("cyrene_integration_contracts").joinpath("schema.json")
    assert bundled.read_bytes() == SCHEMA_PATH.read_bytes()
    assert schema_for("AuthorizationHealth")["$ref"] == "#/$defs/AuthorizationHealth"

    request = _envelope()
    validate_schema("ToolEnvelope", request)
    with pytest.raises(jsonschema.ValidationError):
        validate_schema(
            "CloudAuthorizationHealthArguments",
            {"target_ref": "x", "url": "https://bad.example"},
        )
    with pytest.raises(jsonschema.ValidationError):
        validate_schema(
            "TrustedCallerContext",
            {**request["caller_context"], "access_token": "secret"},
        )


def test_authorization_health_round_trip_preserves_unknown_expiry() -> None:
    """Unknown source expiry remains null instead of being guessed."""

    wire = {
        "schema_version": 1,
        "authorization_ref": "authorization-ref-1",
        "login_state": "PRESENT",
        "authorization_state": "UNKNOWN",
        "checked_at": "2030-01-02T03:04:05Z",
        "expires_at": None,
        "generation": 3,
        "capabilities": [
            {
                "capability_id": "wecom.message.send",
                "state": "UNKNOWN",
                "last_verified_at": None,
                "expires_at": None,
                "reason_code": "provider_introspection_unavailable",
            }
        ],
    }
    dto = parse_authorization_health(wire)
    assert dto.login_state is LoginState.PRESENT
    assert dto.authorization_state is AuthorizationState.UNKNOWN
    assert dto.expires_at is None
    assert dto.capabilities[0].expires_at is None
    assert serialize_authorization_health(dto) == wire

    with pytest.raises(jsonschema.ValidationError):
        parse_authorization_health({**wire, "untrusted_identity": "caller-selected"})


def test_renewal_and_cloud_result_dtos_round_trip_strictly() -> None:
    """Keep nullable renewal times and bounded cloud metadata typed."""

    renewal_wire = {
        "session_ref": "renewal-session-1",
        "authorization_ref": "authorization-ref-1",
        "state": "WAITING_USER",
        "created_at": "2030-01-02T03:04:05Z",
        "expires_at": None,
        "authorization_url": "https://login.example/authorize?state=opaque",
        "qr_attachment_ref": "attachment-ref-1",
        "generation": 4,
    }
    renewal = parse_renewal_session(renewal_wire)
    assert renewal.state is RenewalState.WAITING_USER
    assert renewal.expires_at is None
    assert serialize_renewal_session(renewal) == renewal_wire

    cloud_wire = {
        "provider": "azure",
        "checked_at": "2030-01-02T03:04:05Z",
        "resources": [
            {
                "external_ref": "opaque-external-ref",
                "kind": "azure.vm",
                "display_name": "worker-01",
                "status": "UNKNOWN",
                "provider_status": None,
                "collected_at": "2030-01-02T03:04:04Z",
                "observed_at": None,
                "attributes": {"region": "eastus", "vcpus": 4, "deallocated": None},
                "platform_resource_ref": None,
                "platform_generation": None,
            }
        ],
    }
    cloud = parse_cloud_query_result(cloud_wire)
    assert isinstance(cloud, CloudQueryResult)
    assert cloud.resources[0].kind is CloudResourceKind.AZURE_VM
    assert cloud.resources[0].status is CloudResourceState.UNKNOWN
    assert cloud.resources[0].attributes["vcpus"] == 4
    assert serialize_cloud_query_result(cloud) == cloud_wire

    with pytest.raises(jsonschema.ValidationError):
        parse_cloud_query_result(
            {
                **cloud_wire,
                "resources": [
                    {
                        **cloud_wire["resources"][0],
                        "attributes": {
                            "provider_error": {
                                "code": "TOKEN_REVOKED",
                                "message": "raw provider error",
                            }
                        },
                    }
                ],
            }
        )


def test_email_submission_does_not_claim_unproven_delivery() -> None:
    """Email DTO carries only bounded outcome facts and explicit evidence."""

    wire = {
        "request_ref": "notification-request-1",
        "status": "UNKNOWN",
        "vendor_receipt_ref": None,
        "delivery_confirmed": False,
    }
    dto = parse_email_submission(wire)
    assert isinstance(dto, EmailSubmission)
    assert dto.status is EmailSubmissionStatus.UNKNOWN
    assert not dto.delivery_confirmed
    assert serialize_email_submission(dto) == wire


def test_signed_tool_envelope_binds_scope_operation_and_raw_arguments() -> None:
    """Require the exact binding, audience, tenant, caller, and arguments."""

    envelope = _envelope()
    parsed = parse_tool_envelope(envelope)
    assert serialize_tool_envelope(parsed) == envelope
    arguments = dict(parsed.arguments)
    expected = {
        "method": "cloud_authorization_health",
        "operation_id": "cloud.authorization.health.read",
        "payload": arguments,
        "context": parsed.caller_context,
        "key": KEY,
        "expected_caller_workload": "exchange-worker",
        "expected_audience": "cloud-resources-provider",
        "expected_binding_id": "cloud-binding-1",
        "expected_organization_id": "org-test-1",
        "expected_workspace_id": "workspace-test-1",
        "expected_issuer": "https://identity.example",
        "expected_subject": "operator-subject-1",
        "now": FIXED_NOW,
    }
    assert verify_context(**expected)

    for changed in (
        {"expected_binding_id": "other-binding"},
        {"expected_audience": "other-provider"},
        {"expected_organization_id": "other-org"},
        {"expected_workspace_id": "other-workspace"},
        {"expected_caller_workload": "other-caller"},
        {"expected_issuer": "other-issuer"},
        {"expected_subject": "other-subject"},
        {"operation_id": "cloud.resources.query"},
        {"payload": {"target_ref": "other-configured-target"}},
    ):
        assert not verify_context(**{**expected, **changed})

    assert not verify_context(**{**expected, "now": FIXED_NOW + timedelta(seconds=91)})
    assert not verify_context(**{**expected, "key": b"short"})

    wrong_pair = dict(envelope)
    wrong_pair["caller_context"] = {
        **envelope["caller_context"],
        "operation_id": "cloud.resources.query",
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_schema("ToolEnvelope", wrong_pair)


def test_context_lifetime_and_missing_binding_key_fail_closed() -> None:
    """Reject unknown keys and overlong signatures before provider work."""

    with pytest.raises(ValueError, match="32 bytes"):
        sign_context(
            method="cloud_authorization_health",
            operation_id="cloud.authorization.health.read",
            payload=BASE_ARGUMENTS,
            caller_workload="exchange-worker",
            audience="cloud-resources-provider",
            issuer="https://identity.example",
            subject="operator-subject-1",
            organization_id="org-test-1",
            workspace_id="workspace-test-1",
            binding_id="cloud-binding-1",
            request_id="request-test-1",
            key=b"missing-config",
            issued_at=FIXED_NOW,
            expires_at=FIXED_NOW + timedelta(seconds=30),
        )
    with pytest.raises(ValueError, match="lifetime"):
        _context(expires_at=FIXED_NOW + timedelta(seconds=301))


def test_cloud_authorization_health_uses_configured_target_selector() -> None:
    """The health tool accepts the same opaque configured target_ref."""

    args = {"target_ref": "configured-cloud-main"}
    context = _context(method="cloud_authorization_health", payload=args)
    validate_schema(
        "CloudAuthorizationHealthEnvelope",
        {"caller_context": context.as_dict(), "arguments": args},
    )
    with pytest.raises(jsonschema.ValidationError):
        validate_schema(
            "CloudAuthorizationHealthEnvelope",
            {
                "caller_context": context.as_dict(),
                "arguments": {"target_ref": "x", "url": "https://arbitrary.example"},
            },
        )


def test_all_fixed_tool_envelopes_use_their_allowlisted_operation() -> None:
    """Validate every fixed tool ID against its paired operation and arguments."""

    examples = {
        "wecom_authorization_health": {"authorization_ref": "wecom-auth-1"},
        "wecom_reauthorization_start": {"authorization_ref": "wecom-auth-1"},
        "wecom_reauthorization_status": {"session_ref": "renewal-session-1"},
        "cloud_resources_query": {"target_ref": "configured-cloud-main"},
        "cloud_authorization_health": {"target_ref": "configured-cloud-main"},
        "email_notification_send": {
            "recipient_ref": "ops-oncall",
            "subject": "Connection requires attention",
            "text": "A configured connection needs operator review.",
            "request_ref": "notification-1",
        },
        "email_connection_health": {},
    }
    assert set(examples) == set(TOOL_OPERATIONS)
    for method, arguments in examples.items():
        context = _context(method=method, payload=arguments)
        validate_schema(
            "ToolEnvelope",
            {"caller_context": context.as_dict(), "arguments": arguments},
        )


def test_write_replay_identity_scopes_stable_refs_and_separates_payload_hash() -> None:
    """Deduplicate exact writes by trusted scope while rejecting changed bodies."""

    renewal_arguments = {"authorization_ref": "wecom-auth-1"}
    renewal_context = _context(
        method="wecom_reauthorization_start",
        payload=renewal_arguments,
        request_id="renewal-request-1",
    )
    renewal_key, renewal_hash = write_replay_identity(
        renewal_context, renewal_arguments
    )
    assert write_replay_identity(renewal_context, renewal_arguments) == (
        renewal_key,
        renewal_hash,
    )
    changed_renewal = {"authorization_ref": "wecom-auth-2"}
    changed_context = _context(
        method="wecom_reauthorization_start",
        payload=changed_renewal,
        request_id="renewal-request-1",
    )
    changed_key, changed_hash = write_replay_identity(changed_context, changed_renewal)
    assert changed_key == renewal_key
    assert changed_hash != renewal_hash
    new_request_context = _context(
        method="wecom_reauthorization_start",
        payload=renewal_arguments,
        request_id="renewal-request-2",
    )
    assert (
        write_replay_identity(new_request_context, renewal_arguments)[0] != renewal_key
    )

    email_arguments = {
        "recipient_ref": "ops-oncall",
        "subject": "Connection requires attention",
        "text": "A configured connection needs operator review.",
        "request_ref": "notification-request-1",
    }
    email_context = _context(method="email_notification_send", payload=email_arguments)
    email_key, email_hash = write_replay_identity(email_context, email_arguments)
    email_reissued_context = _context(
        method="email_notification_send",
        payload=email_arguments,
        request_id="email-retry-context-2",
    )
    assert write_replay_identity(email_reissued_context, email_arguments) == (
        email_key,
        email_hash,
    )
    changed_email = {**email_arguments, "text": "Changed notification body."}
    changed_email_context = _context(
        method="email_notification_send", payload=changed_email
    )
    changed_email_key, changed_email_hash = write_replay_identity(
        changed_email_context, changed_email
    )
    assert changed_email_key == email_key
    assert changed_email_hash != email_hash
    different_scope = _context(
        method="email_notification_send",
        payload=email_arguments,
        organization_id="org-test-2",
    )
    assert write_replay_identity(different_scope, email_arguments)[0] != email_key
    different_request_ref = {**email_arguments, "request_ref": "notification-request-2"}
    different_ref_context = _context(
        method="email_notification_send", payload=different_request_ref
    )
    assert (
        write_replay_identity(different_ref_context, different_request_ref)[0]
        != email_key
    )

    with pytest.raises(ValueError, match="write replay"):
        write_replay_identity(_context(), BASE_ARGUMENTS)
