"""Capability-specific signed caller context and credential encryption.

Only configured service identities may call this capability. Credentials use
AES-GCM with a separately injected 256-bit key. 中文：仅允许配置的服务身份调用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .models import AccountCenterError, Anchor

CONTEXT_FIELDS = (
    "method",
    "payload_sha256",
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
)


def canonical_json(value: Any) -> bytes:
    """Encode JSON values with the shared deterministic UTF-8 representation."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def payload_digest(payload: Mapping[str, Any]) -> str:
    """Hash the exact semantic request payload that the context authorizes."""

    return hashlib.sha256(canonical_json(payload)).hexdigest()


def decode_key(value: str, *, length: int) -> bytes:
    """Decode injected hex or base64 key material and enforce its size."""

    try:
        decoded = (
            bytes.fromhex(value)
            if len(value) == length * 2
            else base64.b64decode(value, validate=True)
        )
    except (ValueError, TypeError):
        raise AccountCenterError("configuration_unavailable") from None
    if len(decoded) != length:
        raise AccountCenterError("configuration_unavailable")
    return decoded


def context_signed_bytes(method: str, context: Mapping[str, Any]) -> bytes:
    """Return canonical HMAC input including the exact method and payload hash."""

    if context.get("method") != method:
        raise AccountCenterError("untrusted_caller")
    return canonical_json({field: context[field] for field in CONTEXT_FIELDS})


def verify_context(
    method: str,
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
    caller_keys: Mapping[str, bytes],
    *,
    now: int | None = None,
) -> Anchor:
    """Verify exact-body HMAC, freshness, and fixed configured caller identity."""

    if set(context) != {*CONTEXT_FIELDS, "signature"}:
        raise AccountCenterError("untrusted_caller")
    caller_id = context.get("caller_id")
    key = caller_keys.get(caller_id) if isinstance(caller_id, str) else None
    if key is None:
        raise AccountCenterError("untrusted_caller")
    timestamp = int(time.time()) if now is None else now
    issued_at = context.get("issued_at")
    expires_at = context.get("expires_at")
    if (
        not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or issued_at > timestamp + 30
        or expires_at <= timestamp
        or expires_at <= issued_at
        or expires_at - issued_at > 300
    ):
        raise AccountCenterError("untrusted_caller")
    expected_digest = payload_digest(payload)
    if not hmac.compare_digest(str(context.get("payload_sha256", "")), expected_digest):
        raise AccountCenterError("untrusted_caller")
    try:
        supplied = bytes.fromhex(str(context.get("signature", "")))
    except ValueError:
        raise AccountCenterError("untrusted_caller") from None
    expected = hmac.new(
        key, context_signed_bytes(method, context), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(supplied, expected):
        raise AccountCenterError("untrusted_caller")
    for field in (
        "caller_id",
        "issuer",
        "subject",
        "organization_id",
        "binding_id",
        "request_id",
        "operation_id",
    ):
        if not isinstance(context.get(field), str) or not context[field]:
            raise AccountCenterError("untrusted_caller")
    workspace = context.get("workspace_id")
    if workspace is not None and (
        not isinstance(workspace, str) or not workspace
    ):
        raise AccountCenterError("untrusted_caller")
    return Anchor(
        issuer=context["issuer"],
        subject=context["subject"],
        organization_id=context["organization_id"],
        workspace_id=workspace,
    )


def encrypt(key: bytes, plaintext: bytes, associated_data: bytes) -> bytes:
    """Encrypt private material with an authenticated AES-GCM envelope."""

    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, associated_data)


def decrypt(key: bytes, ciphertext: bytes, associated_data: bytes) -> bytes:
    """Decrypt a stored authenticated AES-GCM envelope or fail closed."""

    try:
        return AESGCM(key).decrypt(ciphertext[:12], ciphertext[12:], associated_data)
    except (InvalidTag, ValueError):
        raise AccountCenterError("credential_unavailable") from None
