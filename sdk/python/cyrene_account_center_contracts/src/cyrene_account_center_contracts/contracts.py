"""DTOs and canonical signed-caller context for ``account.center.v1``.

This package contains contracts only; credentials and account lifecycle remain
owned by the Account Center Plugin. 中文：此软件包仅定义契约。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import asdict, dataclass
from typing import Any


def canonical_json(value: Any) -> bytes:
    """Encode JSON values using the capability's canonical UTF-8 form."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def payload_sha256(value: Any) -> str:
    """Return a SHA-256 digest for exact canonical JSON payload semantics."""

    return hashlib.sha256(canonical_json(value)).hexdigest()


def _valid_text_claim(value: Any) -> bool:
    """Return whether one identity or operation claim is non-empty text."""

    return isinstance(value, str) and bool(value)


@dataclass(frozen=True, slots=True)
class TrustedCallerContext:
    """HMAC-authenticated actor, binding, operation, and freshness claims."""

    method: str
    caller_id: str
    issuer: str
    subject: str
    organization_id: str
    workspace_id: str | None
    binding_id: str
    request_id: str
    issued_at: int
    expires_at: int
    operation_id: str
    payload_sha256: str
    signature: str

    def as_dict(self) -> dict[str, Any]:
        """Return the wire representation with no role or provider-selected fields."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class Resource:
    """Stable GitHub resource key; no arbitrary URL or shell target."""

    repository_id: str
    repository: str
    issue_number: int


@dataclass(frozen=True, slots=True)
class BeginLinkRequest:
    provider: str
    idempotency_key: str
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompleteGitHubLinkRequest:
    state: str
    code: str


@dataclass(frozen=True, slots=True)
class ObserveWeComChallengeRequest:
    bot_namespace: str
    source_authority: str
    stable_subject: str
    challenge_id: str
    challenge_value: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class ConfirmWeComLinkRequest:
    link_session_ref: str
    challenge_id: str
    challenge_value: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ListLinksRequest:
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class GetAuthorizationRequest:
    account_ref: str
    operation_id: str
    resource: Resource
    arguments_sha256: str


@dataclass(frozen=True, slots=True)
class UnlinkRequest:
    account_ref: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CreateCredentialLeaseRequest:
    account_ref: str
    operation_id: str
    resource: Resource
    arguments_sha256: str


@dataclass(frozen=True, slots=True)
class RedeemCredentialLeaseRequest:
    lease_ref: str
    operation_id: str
    resource: Resource
    arguments_sha256: str


@dataclass(frozen=True, slots=True)
class ResolveLinkedSenderRequest:
    """Exact authenticated WeCom source identity tuple for private lookup."""

    source_authority: str
    bot_namespace: str
    stable_subject: str


@dataclass(frozen=True, slots=True)
class ResolveLinkedSenderResponse:
    """Resolved primary owner anchor; fields are absent when not linked."""

    linked: bool
    account_ref: str | None = None
    provider: str | None = None
    issuer: str | None = None
    subject: str | None = None
    organization_id: str | None = None
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class CredentialLease:
    lease_ref: str
    provider: str
    operation_id: str
    resource: Resource
    arguments_sha256: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class AuthMaterial:
    """Private provider-only in-memory authorization value returned by IPC."""

    access_token: str
    token_type: str
    external_subject: str
    expires_at: int | None


def sign_context(
    *,
    method: str,
    payload: Any,
    caller_id: str,
    issuer: str,
    subject: str,
    organization_id: str,
    workspace_id: str | None,
    binding_id: str,
    request_id: str,
    operation_id: str,
    key: bytes,
    issued_at: int | None = None,
    expires_at: int | None = None,
) -> TrustedCallerContext:
    """Sign exact method and payload with an injected per-caller HMAC key."""

    issued = int(time.time()) if issued_at is None else issued_at
    expires = issued + 120 if expires_at is None else expires_at
    if (
        not isinstance(key, bytes)
        or len(key) != 32
        or isinstance(issued, bool)
        or not isinstance(issued, int)
        or isinstance(expires, bool)
        or not isinstance(expires, int)
        or issued < 0
        or expires < 0
        or expires <= issued
        or expires - issued > 300
    ):
        raise ValueError("caller key and context lifetime are invalid")
    fields: dict[str, Any] = {
        "method": method,
        "payload_sha256": payload_sha256(payload),
        "caller_id": caller_id,
        "issuer": issuer,
        "subject": subject,
        "organization_id": organization_id,
        "workspace_id": workspace_id,
        "binding_id": binding_id,
        "request_id": request_id,
        "issued_at": issued,
        "expires_at": expires,
        "operation_id": operation_id,
    }
    if any(
        not _valid_text_claim(fields[field])
        for field in (
            "method",
            "caller_id",
            "issuer",
            "subject",
            "organization_id",
            "binding_id",
            "request_id",
            "operation_id",
        )
    ) or (workspace_id is not None and not _valid_text_claim(workspace_id)):
        raise ValueError("caller context claims are invalid")
    signature = hmac.new(key, canonical_json(fields), hashlib.sha256).hexdigest()
    return TrustedCallerContext(
        method=method,
        caller_id=caller_id,
        issuer=issuer,
        subject=subject,
        organization_id=organization_id,
        workspace_id=workspace_id,
        binding_id=binding_id,
        request_id=request_id,
        issued_at=issued,
        expires_at=expires,
        operation_id=operation_id,
        payload_sha256=fields["payload_sha256"],
        signature=signature,
    )


def verify_context(
    *,
    method: str,
    payload: Any,
    context: TrustedCallerContext | dict[str, Any],
    keys: dict[str, bytes],
    now: int | None = None,
    expected_caller_id: str | None = None,
) -> bool:
    """Verify signature, exact body hash, caller, clock skew, and 300s maximum TTL."""

    values = (
        context.as_dict()
        if isinstance(context, TrustedCallerContext)
        else dict(context)
    )
    expected_fields = {
        "method",
        "caller_id",
        "issuer",
        "subject",
        "organization_id",
        "workspace_id",
        "binding_id",
        "request_id",
        "issued_at",
        "expires_at",
        "operation_id",
        "payload_sha256",
        "signature",
    }
    if set(values) != expected_fields:
        return False
    signature = values.pop("signature", None)
    caller_id = values.get("caller_id")
    key = keys.get(caller_id) if isinstance(caller_id, str) else None
    current = int(time.time()) if now is None else now
    issued = values.get("issued_at")
    expires = values.get("expires_at")
    if (
        key is None
        or len(key) != 32
        or (expected_caller_id is not None and caller_id != expected_caller_id)
    ):
        return False
    if (
        not isinstance(issued, int)
        or isinstance(issued, bool)
        or not isinstance(expires, int)
        or isinstance(expires, bool)
    ):
        return False
    if any(
        not _valid_text_claim(values.get(field))
        for field in (
            "method",
            "caller_id",
            "issuer",
            "subject",
            "organization_id",
            "binding_id",
            "request_id",
            "operation_id",
        )
    ):
        return False
    workspace = values.get("workspace_id")
    if workspace is not None and not _valid_text_claim(workspace):
        return False
    if (
        issued > current + 30
        or expires <= current
        or expires <= issued
        or expires - issued > 300
    ):
        return False
    if values.get("payload_sha256") != payload_sha256(payload):
        return False
    if values.get("method") != method:
        return False
    expected = hmac.new(key, canonical_json(values), hashlib.sha256).hexdigest()
    return isinstance(signature, str) and hmac.compare_digest(signature, expected)
