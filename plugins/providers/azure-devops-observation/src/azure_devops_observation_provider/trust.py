"""Verify signed Exchange calls and bind one-use Azure DevOps leases.

Actor, workspace, provider binding, operation, target, and argument hash come
from verified host context. 中文：调用方不能自报身份或扩大项目范围。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from typing import Any

from cyrene_account_center_contracts import sign_context, verify_context

from .settings import ProviderSettings

METHOD = "redeem_provider_credential_lease"
MAX_CONTEXT_LIFETIME_SECONDS = 300
OPERATIONS = frozenset(
    {
        "azure_devops.observations.poll",
        "azure_devops.pipelines.list",
        "azure_devops.pipeline_runs.list",
        "azure_devops.pipeline_run.get",
        "azure_devops.deployments.list",
        "azure_devops.release_environment.get",
    }
)


class TrustedContextError(ValueError):
    """The invocation does not carry valid Exchange-signed identity proof."""


def verify_exchange_context(
    settings: ProviderSettings,
    payload: Mapping[str, Any],
    context: Any,
    *,
    expected_operation: str,
    now: int | None = None,
) -> dict[str, str]:
    """Verify exact arguments, actor/workspace, binding, operation, and freshness."""

    if not isinstance(context, Mapping):
        raise TrustedContextError("trusted context is required")
    try:
        valid = verify_context(
            method="call_tool",
            payload=dict(payload),
            context=dict(context),
            keys={settings.exchange_caller_id: settings.exchange_verification_key},
            now=now,
            expected_caller_id=settings.exchange_caller_id,
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise TrustedContextError("trusted context is invalid")
    if context.get("operation_id") != expected_operation or expected_operation not in OPERATIONS:
        raise TrustedContextError("trusted operation does not match the selected tool")
    if context.get("binding_id") != settings.binding_id:
        raise TrustedContextError("trusted binding does not match")

    actor: dict[str, str] = {}
    for field in ("issuer", "subject", "organization_id", "workspace_id"):
        value = context.get(field)
        if not isinstance(value, str) or not value:
            raise TrustedContextError("trusted actor or workspace anchor is incomplete")
        actor[field] = value

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
    actor: Mapping[str, str],
    payload: Mapping[str, Any],
    *,
    now: int | None = None,
) -> dict[str, Any]:
    """Sign a one-use provider lease redemption with the verified actor anchor."""

    issued_at = int(time.time()) if now is None else now
    signed = sign_context(
        method=METHOD,
        payload=dict(payload),
        caller_id=settings.provider_caller_id,
        issuer=actor["issuer"],
        subject=actor["subject"],
        organization_id=actor["organization_id"],
        workspace_id=actor["workspace_id"],
        binding_id=settings.account_center_binding_id,
        request_id=str(uuid.uuid4()),
        operation_id=METHOD,
        key=settings.provider_signing_key,
        issued_at=issued_at,
        expires_at=issued_at + 60,
    )
    return signed.as_dict()
