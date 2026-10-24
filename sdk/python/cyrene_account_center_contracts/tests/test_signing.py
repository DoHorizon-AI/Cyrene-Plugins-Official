"""Canonical signing contract tests shared by Account Center clients."""

import hashlib
import hmac

import pytest
from cyrene_account_center_contracts import (
    canonical_json,
    payload_sha256,
    sign_context,
    verify_context,
)


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
