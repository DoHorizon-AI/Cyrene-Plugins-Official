"""Typed internal records for account-center operations.

This module keeps trusted identity and credential ownership explicit at the
plugin boundary. 中文：该模块显式表达可信身份与凭据归属。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class AccountCenterError(RuntimeError):
    """Sanitized, stable failure returned by Account Center.

    The message contains no provider response, token, or private storage data.
    中文：错误信息不包含供应商响应、令牌或私有存储数据。
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Anchor:
    """Primary human owner key supplied only by verified caller context."""

    issuer: str
    subject: str
    organization_id: str
    workspace_id: str | None


@dataclass(frozen=True, slots=True)
class OAuthCredential:
    """Short-lived in-memory GitHub OAuth credential material."""

    access_token: str
    refresh_token: str | None
    expires_at: int | None
    refresh_expires_at: int | None


def require_object(value: Any, *, code: str = "invalid_request") -> dict[str, Any]:
    """Return a shallow dict for a JSON object or raise a fixed error."""

    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise AccountCenterError(code)
    return value
