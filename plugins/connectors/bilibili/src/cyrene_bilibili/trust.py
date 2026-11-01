"""Verify signed Exchange calls and bind Account Center signatures to actors."""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from typing import Any

from cyrene_account_center_contracts import sign_context
from cyrene_integration_contracts import verify_context

from .settings import ProviderSettings

LEASE_OPERATIONS = frozenset(
    {
        "bilibili.search",
        "bilibili.user.find",
        "bilibili.video.read",
        "bilibili.comments.list",
        "bilibili.dynamics.list",
        "bilibili.dynamic.read",
        "bilibili.dm.list",
        "bilibili.dm.read",
        "bilibili.comment.create",
        "bilibili.dynamic.publish",
        "bilibili.dm.send",
    }
)
QR_OPERATIONS = frozenset({"bilibili.auth.qr.begin", "bilibili.auth.qr.status"})


class TrustedContextError(ValueError):
    """The operation does not have valid Exchange-authenticated identity."""


def verify_exchange_context(
    settings: ProviderSettings,
    signed_payload: Mapping[str, Any],
    context: Any,
    *,
    expected_operation: str,
) -> dict[str, str | None]:
    """Verify HMAC, exact payload digest, caller, operation, and activation binding."""

    if not isinstance(context, Mapping):
        raise TrustedContextError("caller context is missing")
    valid = False
    for caller in settings.trusted_callers:
        if (
            context.get("caller_workload") != caller.caller_workload
            or context.get("audience") != caller.audience
            or context.get("organization_id") != caller.organization_id
            or context.get("workspace_id") != caller.workspace_id
            or context.get("issuer") != caller.issuer
        ):
            continue
        try:
            valid = verify_context(
                method=expected_operation,
                operation_id=expected_operation,
                payload=dict(signed_payload),
                context=dict(context),
                key=caller.verification_key,
                expected_caller_workload=caller.caller_workload,
                expected_audience=caller.audience,
                expected_binding_id=settings.binding_id,
                expected_organization_id=caller.organization_id,
                expected_workspace_id=caller.workspace_id,
                expected_issuer=caller.issuer,
            )
        except (TypeError, ValueError):
            valid = False
        if valid:
            break
    if not valid or context.get("operation_id") != expected_operation:
        raise TrustedContextError("caller context signature or operation is invalid")
    if context.get("binding_id") != settings.binding_id:
        raise TrustedContextError("caller context binding does not match")
    actor: dict[str, str | None] = {}
    for field in ("issuer", "subject", "organization_id", "binding_id"):
        value = context.get(field)
        if not isinstance(value, str) or not value:
            raise TrustedContextError("authenticated actor anchor is incomplete")
        actor[field] = value
    workspace = context.get("workspace_id")
    if workspace is not None and (not isinstance(workspace, str) or not workspace):
        raise TrustedContextError("authenticated workspace is invalid")
    actor["workspace_id"] = workspace
    return actor


def account_center_context(
    settings: ProviderSettings,
    actor: Mapping[str, str | None],
    payload: Mapping[str, Any],
    *,
    method: str,
    now: int | None = None,
) -> dict[str, Any]:
    """Sign a fixed Account Center method using the current verified Exchange actor."""

    if method not in {
        "begin_bilibili_link",
        "complete_bilibili_link",
        "redeem_provider_credential_lease",
    }:
        raise TrustedContextError("Account Center method is invalid")
    issued = int(time.time()) if now is None else now
    signed = sign_context(
        method=method,
        payload=dict(payload),
        caller_id=settings.provider_caller_id,
        issuer=_required_actor(actor, "issuer"),
        subject=_required_actor(actor, "subject"),
        organization_id=_required_actor(actor, "organization_id"),
        workspace_id=actor.get("workspace_id"),
        binding_id=settings.account_center_binding_id,
        request_id=str(uuid.uuid4()),
        operation_id=method,
        key=settings.provider_signing_key,
        issued_at=issued,
        expires_at=issued + 15,
    )
    return signed.as_dict()


def expected_target(tool_id: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Build the exact URL-free target tuple Account Center leases authorize."""

    if tool_id == "bilibili.search":
        return {"search_type": arguments["search_type"]}
    if tool_id == "bilibili.user.find":
        return {"user_id": arguments["user_id"]}
    if tool_id == "bilibili.video.read":
        return {"video_id": arguments["video_id"]}
    if tool_id == "bilibili.comments.list":
        target = {"video_id": arguments["video_id"]}
        if "root_comment_id" in arguments:
            target["root_comment_id"] = arguments["root_comment_id"]
        return target
    if tool_id == "bilibili.dynamics.list":
        return {"user_id": arguments["user_id"]}
    if tool_id == "bilibili.dynamic.read":
        return {"dynamic_id": arguments["dynamic_id"]}
    if tool_id in {"bilibili.dm.list", "bilibili.dm.send"}:
        return {"peer_user_id": arguments["peer_user_id"]}
    if tool_id == "bilibili.dm.read":
        return {
            "peer_user_id": arguments["peer_user_id"],
            "message_id": arguments["message_id"],
        }
    if tool_id == "bilibili.comment.create":
        return {
            "video_id": arguments["video_id"],
            "root_comment_id": arguments["root_comment_id"],
            "parent_comment_id": arguments["parent_comment_id"],
        }
    if tool_id == "bilibili.dynamic.publish":
        return {"user_id": arguments["user_id"]}
    raise TrustedContextError("Bilibili operation has no lease target")


def _required_actor(actor: Mapping[str, str | None], field: str) -> str:
    value = actor.get(field)
    if not isinstance(value, str) or not value:
        raise TrustedContextError("authenticated actor anchor is incomplete")
    return value
