"""
┌────────────────────────────────────────────────────────────────────┐
│  Exchange actor verification and Account Center request signing      │
│  校验 Exchange 用户上下文并签署账号中心请求                           │
└────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from typing import Any

from cyrene_account_center_contracts import sign_context, verify_context

from .settings import ProviderSettings

MAX_CONTEXT_LIFETIME_SECONDS = 300
LEASE_METHOD = "redeem_provider_credential_lease"
PROFILE_LIST_METHOD = "list_provider_profiles"
OPERATIONS = frozenset(
    {
        "microsoft_graph.planner.plan.get",
        "microsoft_graph.planner.plans.list",
        "microsoft_graph.planner.tasks.list",
        "microsoft_graph.planner.task.get",
        "microsoft_graph.planner.task.create",
        "microsoft_graph.planner.task.update",
        "microsoft_graph.sharepoint.site.get",
        "microsoft_graph.sharepoint.list.get",
        "microsoft_graph.sharepoint.list.items.delta",
        "microsoft_graph.todo.lists.list",
        "microsoft_graph.todo.tasks.list",
        "microsoft_graph.todo.task.get",
        "microsoft_graph.todo.task.create",
        "microsoft_graph.todo.task.update",
        "microsoft_graph.todo.task.delete",
        "microsoft_graph.calendar.calendars.list",
        "microsoft_graph.calendar.events.list",
        "microsoft_graph.calendar.event.get",
        "microsoft_graph.calendar.event.create",
        "microsoft_graph.calendar.event.update",
        "microsoft_graph.calendar.event.delete",
        "microsoft_graph.calendar.freebusy.get",
        "microsoft_graph.sharepoint.drives.list",
        "microsoft_graph.sharepoint.drive.items.list",
    }
)


class TrustedContextError(ValueError):
    """The invocation does not carry valid Exchange identity proof."""


def verify_exchange_context(
    settings: ProviderSettings,
    payload: Mapping[str, Any],
    context: Any,
    *,
    now: int | None = None,
) -> dict[str, str | None]:
    """Verify payload digest, signed caller, binding, operation, and freshness."""

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
    if context.get("operation_id") not in OPERATIONS:
        raise TrustedContextError("trusted operation is invalid")
    if context.get("binding_id") != settings.binding_id:
        raise TrustedContextError("trusted binding does not match")
    actor: dict[str, str | None] = {}
    for field in ("issuer", "subject", "organization_id", "binding_id"):
        value = context.get(field)
        if not isinstance(value, str) or not value:
            raise TrustedContextError("trusted actor anchor is incomplete")
        actor[field] = value
    workspace = context.get("workspace_id")
    if workspace is not None and (not isinstance(workspace, str) or not workspace):
        raise TrustedContextError("trusted workspace scope is invalid")
    actor["workspace_id"] = workspace
    issued = context.get("issued_at")
    expires = context.get("expires_at")
    current = int(time.time()) if now is None else now
    if (
        isinstance(issued, bool)
        or not isinstance(issued, int)
        or isinstance(expires, bool)
        or not isinstance(expires, int)
        or issued > current + 30
        or expires <= current
        or expires <= issued
        or expires - issued > MAX_CONTEXT_LIFETIME_SECONDS
    ):
        raise TrustedContextError("trusted context is expired")
    return actor


def account_center_context(
    settings: ProviderSettings,
    actor: Mapping[str, str | None],
    payload: Mapping[str, Any],
    *,
    method: str,
    now: int | None = None,
) -> dict[str, Any]:
    """Sign a one-use Account Center request for the verified Cyrene actor."""

    if method not in {LEASE_METHOD, PROFILE_LIST_METHOD}:
        raise TrustedContextError("Account Center method is invalid")
    issued = int(time.time()) if now is None else now
    signed = sign_context(
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
        issued_at=issued,
        expires_at=issued + 60,
    )
    return signed.as_dict()
