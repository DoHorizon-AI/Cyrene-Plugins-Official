"""Authenticated opaque cursors for bounded Azure DevOps source pages.

Cursor scope includes the configured source and fixed polling window. 中文：
游标不能跨项目、来源类型或时间窗重用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from cyrene_account_center_contracts import canonical_json


class CursorError(ValueError):
    """A caller supplied cursor is invalid or belongs to another source."""


def encode_cursor(payload: Mapping[str, Any], key: bytes) -> str:
    """Serialize and authenticate bounded cursor state."""

    body = canonical_json(dict(payload))
    encoded = base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")
    signed = f"v1.{encoded}"
    signature = hmac.new(key, signed.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{signed}.{signature}"


def decode_cursor(value: str, key: bytes) -> dict[str, Any]:
    """Verify an opaque cursor signature and decode a unique-key object."""

    if not isinstance(value, str) or len(value) > 16_384:
        raise CursorError("cursor is invalid")
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise CursorError("cursor version is unsupported")
    signed = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(key, signed.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts[2]):
        raise CursorError("cursor signature is invalid")
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        decoded = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CursorError("cursor payload is invalid") from error
    if not isinstance(decoded, dict):
        raise CursorError("cursor payload must be an object")
    return decoded


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate cursor fields before trusting their values."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate cursor field")
        result[key] = value
    return result
