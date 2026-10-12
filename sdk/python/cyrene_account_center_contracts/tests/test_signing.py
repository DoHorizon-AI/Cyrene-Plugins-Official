"""Canonical signing contract tests shared by Account Center clients."""

import hashlib
import hmac

import pytest
from cyrene_account_center_contracts import (
    canonical_json,
    sign_context,
    verify_context,
)


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
