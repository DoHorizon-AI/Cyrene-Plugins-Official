"""Verify Exchange identity context and create Account Center signatures.

中文:使用 Account Center 公共 SDK 验证 Exchange 身份上下文并签名。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from typing import Any

from cyrene_account_center_contracts import sign_context, verify_context

from .settings import ProviderSettings

MAX_CONTEXT_LIFETIME_SECONDS = 300


class TrustedContextError(ValueError):
    """The invocation did not carry valid Exchange-authenticated context."""


def verify_exchange_context(
    settings: ProviderSettings,
    payload: Mapping[str, Any],
    context: Any,
    *,
    now: int | None = None,
) -> dict[str, str | None]:
    """Verify exact payload, actor, method, caller, binding, and freshness."""

    if not isinstance(context, Mapping):
        raise TrustedContextError("trusted context is required")
    try:
        valid = verify_context(
            method="call_tool",
            payload=payload,
            context=dict(context),
            keys={settings.exchange_caller_id: settings.exchange_verification_key},
            now=now,
            expected_caller_id=settings.exchange_caller_id,
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise TrustedContextError("trusted context is invalid")
    if context.get("operation_id") not in {
        "github.issue.get",
        "github.issue.comment.create",
        "github.issue.state.update",
        "github.repository.list",
        "github.repository.get",
        "github.branches.list",
        "github.pull_requests.list",
        "github.pull_request.get",
        "github.actions.runs.list",
        "github.ci.status",
    }:
        raise TrustedContextError("trusted operation is invalid")
    if context.get("binding_id") != settings.binding_id:
        raise TrustedContextError("trusted binding does not match invocation")
    actor: dict[str, str | None] = {}
    for field in ("issuer", "subject", "organization_id", "binding_id"):
        value = context.get(field)
        if not isinstance(value, str) or not value:
            raise TrustedContextError("trusted actor anchor is incomplete")
        actor[field] = value
    workspace_id = context.get("workspace_id")
    if workspace_id is not None and (not isinstance(workspace_id, str) or not workspace_id):
        raise TrustedContextError("trusted workspace scope is invalid")
    actor["workspace_id"] = workspace_id
    issued_at = context.get("issued_at")
    expires_at = context.get("expires_at")
    current_time = int(time.time()) if now is None else now
    if (
        isinstance(issued_at, bool)
        or not isinstance(issued_at, int)
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or issued_at > current_time + 30
        or expires_at <= current_time
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_CONTEXT_LIFETIME_SECONDS
    ):
        raise TrustedContextError("trusted context is expired or outside its lifetime")
    return actor


def account_center_context(
    settings: ProviderSettings,
    actor: Mapping[str, str | None],
    payload: Mapping[str, Any],
    *,
    method: str = "redeem_credential_lease",
    now: int | None = None,
) -> dict[str, Any]:
    """Sign a redemption for the configured Account Center binding and actor."""

    issued_at = int(time.time()) if now is None else now
    context = sign_context(
        method=method,
        payload=dict(payload),
        caller_id=settings.provider_caller_id,
        issuer=str(actor["issuer"]),
        subject=str(actor["subject"]),
        organization_id=str(actor["organization_id"]),
        workspace_id=actor["workspace_id"],
        binding_id=settings.account_center_binding_id,
        request_id=str(uuid.uuid4()),
        operation_id=method,
        key=settings.provider_signing_key,
        issued_at=issued_at,
        expires_at=issued_at + 60,
    )
    return context.as_dict()
