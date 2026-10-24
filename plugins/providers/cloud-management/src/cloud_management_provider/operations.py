"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud_management_provider.operations                       │
│  Role: Sign and verify restart-stable opaque operation references.   │
│                                                                      │
│  模块职责：签署并校验重启后仍有效的不透明操作引用。                   │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from cyrene_integration_contracts import canonical_json

MAX_OPERATION_REF_BYTES = 8192


class InvalidOperationRef(ValueError):
    """The operation reference is malformed, expired, or signed by another binding."""


def create_operation_ref(payload: Mapping[str, Any], key: bytes) -> str:
    """Encode a small provider operation payload and bind it to the host HMAC key."""

    normalized = dict(payload)
    body = canonical_json(normalized)
    encoded = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    signature = hmac.new(key, body, hashlib.sha256).hexdigest()
    reference = f"op1.{encoded}.{signature}"
    if len(reference.encode("utf-8")) > MAX_OPERATION_REF_BYTES:
        raise InvalidOperationRef("operation reference exceeds its size limit")
    return reference


def parse_operation_ref(reference: object, key: bytes) -> dict[str, Any]:
    """Verify one operation reference without accepting user-controlled vendor URLs."""

    if not isinstance(reference, str) or len(reference.encode("utf-8")) > MAX_OPERATION_REF_BYTES:
        raise InvalidOperationRef("operation reference is malformed")
    parts = reference.split(".")
    if len(parts) != 3 or parts[0] != "op1" or len(parts[2]) != 64:
        raise InvalidOperationRef("operation reference is malformed")
    try:
        body = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
    except (ValueError, TypeError) as error:
        raise InvalidOperationRef("operation reference is malformed") from error
    expected = hmac.new(key, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts[2]):
        raise InvalidOperationRef("operation reference signature is invalid")
    try:
        payload = json.loads(body, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError) as error:
        raise InvalidOperationRef("operation reference payload is malformed") from error
    if not isinstance(payload, dict) or canonical_json(payload) != body:
        raise InvalidOperationRef("operation reference payload is not canonical")
    if set(payload) - {
        "profile_ref",
        "provider",
        "operation",
        "target",
        "request_id",
        "created_at",
        "vendor_operation",
    }:
        raise InvalidOperationRef("operation reference contains unsupported fields")
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous duplicate JSON properties in a signed reference."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("operation reference contains duplicate fields")
        result[key] = value
    return result
