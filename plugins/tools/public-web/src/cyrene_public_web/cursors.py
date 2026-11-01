"""Authenticated, actor-scoped pagination cursors for source-backed reads."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cyrene_integration_contracts import canonical_json


class CursorError(ValueError):
    """A cursor is malformed, tampered with, or belongs to another request."""


def actor_scope_digest(
    *,
    issuer: str,
    subject: str,
    organization_id: str,
    workspace_id: str | None,
    binding_id: str,
    key: bytes,
) -> str:
    value = canonical_json(
        {
            "issuer": issuer,
            "subject": subject,
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "binding_id": binding_id,
        }
    )
    return hmac.new(key, value, hashlib.sha256).hexdigest()


def encode_cursor(payload: Mapping[str, Any], key: bytes) -> str:
    if len(key) != 32:
        raise ValueError("cursor key must contain 32 bytes")
    body = canonical_json(dict(payload))
    if len(body) > 4096:
        raise ValueError("cursor payload exceeds its size limit")
    nonce = os.urandom(12)
    encrypted = AESGCM(_encryption_key(key)).encrypt(
        nonce,
        body,
        b"cyrene.public-web.pagination.v1",
    )
    token = base64.urlsafe_b64encode(nonce + encrypted).rstrip(b"=").decode("ascii")
    return "v1." + token


def decode_cursor(
    value: str,
    *,
    key: bytes,
    expected_kind: str,
    expected_scope: str,
    expected_query_digest: str,
) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) > 8192:
        raise CursorError("cursor is invalid")
    parts = value.split(".")
    if (
        len(parts) != 2
        or parts[0] != "v1"
        or re.fullmatch(r"[A-Za-z0-9_-]{1,8192}", parts[1]) is None
    ):
        raise CursorError("cursor version is unsupported")
    try:
        encrypted = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        if not 28 <= len(encrypted) <= 4096 + 28:
            raise ValueError("cursor ciphertext is invalid")
        nonce, ciphertext = encrypted[:12], encrypted[12:]
        raw = AESGCM(_encryption_key(key)).decrypt(
            nonce,
            ciphertext,
            b"cyrene.public-web.pagination.v1",
        )
        payload = json.loads(raw, object_pairs_hook=_unique_object)
    except InvalidTag:
        raise CursorError("cursor authentication failed") from None
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise CursorError("cursor payload is invalid") from None
    if not isinstance(payload, dict) or set(payload) != {
        "kind",
        "scope_digest",
        "query_digest",
        "position",
        "expires_at",
    }:
        raise CursorError("cursor payload shape is invalid")
    if (
        payload["kind"] != expected_kind
        or payload["scope_digest"] != expected_scope
        or payload["query_digest"] != expected_query_digest
    ):
        raise CursorError("cursor is outside its actor or source scope")
    position = payload["position"]
    expires_at = payload["expires_at"]
    if (
        isinstance(position, bool)
        or not isinstance(position, (str, int))
        or (isinstance(position, str) and len(position) > 2048)
        or (isinstance(position, int) and not 0 <= position <= 1_000_000)
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at <= int(time.time())
    ):
        raise CursorError("cursor position or expiry is invalid")
    return payload


def query_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(value))).hexdigest()


def _encryption_key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("cursor key must contain 32 bytes")
    return hashlib.sha256(b"cyrene-public-web/cursor-key/v1\0" + key).digest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate cursor field")
        result[key] = value
    return result
