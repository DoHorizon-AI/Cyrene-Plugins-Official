"""Expose signed, bounded Bilibili tools through DirectPluginRuntime."""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any

from cyrene_account_center_contracts import AuthMaterial, payload_sha256
from cyrene_integration_contracts import TOOL_OPERATIONS
from cyrene_plugin_runtime.configuration import read_environment_settings
from cyrene_tool_provider_contracts import (
    CAPABILITY_ID,
    METHOD_CALL_TOOL,
    METHOD_LIST_TOOLS,
    REQUEST_TYPE_URLS,
    TypedPayload,
    decode_request,
    encode_response,
)
from cyrene_tool_provider_contracts import tool_provider_pb2 as wire
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .account_center import AccountCenterDirectClient, AccountCenterUnavailable
from .api import BilibiliApi, BilibiliFailure, _parse_cookie_jar
from .receipts import ReceiptStore, ReceiptStoreError
from .settings import ConfigurationError, ProviderSettings
from .tool_catalog import CANONICAL_TOOLS, PLUGIN_ID, PLUGIN_VERSION, TOOL_BY_ID, operation_for
from .trust import (
    QR_OPERATIONS,
    TrustedContextError,
    expected_target,
    verify_exchange_context,
)

MAX_REQUEST_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 512 * 1024
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class BilibiliProvider:
    """Serve the Bilibili operation catalog and verify actor-bound credentials."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: ProviderSettings | None = None,
        account_center: Any = None,
        api: BilibiliApi | None = None,
        store: ReceiptStore | None = None,
    ) -> None:
        loaded_from_environment = False
        if settings is None:
            if read_environment_settings() is None:
                raise ConfigurationError("Bilibili plugin activation is required")
            settings = ProviderSettings.from_environment()
            loaded_from_environment = True
        self._settings = settings
        self._account_center = account_center
        self._owns_account_center = False
        if self._account_center is None and loaded_from_environment:
            try:
                self._account_center = AccountCenterDirectClient(settings)
                self._owns_account_center = True
            except AccountCenterUnavailable:
                self._account_center = None
        self._api = api or BilibiliApi(timeout_seconds=settings.timeout_seconds)
        self._owns_api = api is None
        self._store = store or ReceiptStore(settings.state_dir, settings.provider_signing_key)

    def close(self) -> None:
        """Close only the network clients created by this provider."""

        if self._owns_api:
            self._api.close()
        if self._owns_account_center and self._account_center is not None:
            self._account_center.close()
        self._store.close()

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        cancellation: Any = None,
        cancellation_event: Any = None,
        **_: Any,
    ) -> tuple[bool, Any]:
        """Dispatch exactly one canonical tool request and propagate cancellation."""

        if cancellation_event is None:
            cancellation_event = cancellation
        if capability != CAPABILITY_ID:
            return False, "INVALID_REQUEST: unsupported capability"
        if len(payload) > MAX_REQUEST_BYTES:
            return False, "INVALID_REQUEST: request exceeds its byte limit"
        if action == METHOD_LIST_TOOLS:
            if request_type_url not in (None, REQUEST_TYPE_URLS[METHOD_LIST_TOOLS]):
                return False, "INVALID_REQUEST: catalog payload type mismatch"
            return self._list_tools(payload)
        if action == METHOD_CALL_TOOL:
            if request_type_url not in (None, REQUEST_TYPE_URLS[METHOD_CALL_TOOL]):
                return False, "INVALID_REQUEST: tool payload type mismatch"
            return self._call_tool(payload, cancellation_event)
        return False, "METHOD_NOT_FOUND: unsupported provider method"

    def _list_tools(self, payload: bytes) -> tuple[bool, Any]:
        try:
            request = decode_request(
                METHOD_LIST_TOOLS,
                TypedPayload(REQUEST_TYPE_URLS[METHOD_LIST_TOOLS], payload),
            )
        except (ValueError, TypeError):
            return False, "INVALID_REQUEST: catalog request is malformed"
        if request.HasField("binding_id") and request.binding_id != self._settings.binding_id:
            return False, "INVALID_REQUEST: binding ID does not match"
        response = wire.ListToolsResponse()
        response.catalog.catalog_version = PLUGIN_VERSION
        # QR operations are called only by the authenticated account-link REST
        # route; never advertise them as model-callable tools.
        for definition in CANONICAL_TOOLS:
            if str(definition["provider_tool_id"]) in QR_OPERATIONS:
                continue
            item = response.catalog.tools.add()
            item.binding_id = self._settings.binding_id
            item.provider_tool_id = str(definition["provider_tool_id"])
            item.display_name = str(definition["display_name"])
            item.description = str(definition["description"])
            item.input_schema_json = json.dumps(
                definition["input_schema"], ensure_ascii=False, separators=(",", ":")
            )
        return True, encode_response(METHOD_LIST_TOOLS, response)

    def _call_tool(self, payload: bytes, cancellation_event: Any) -> tuple[bool, Any]:
        try:
            request = decode_request(
                METHOD_CALL_TOOL,
                TypedPayload(REQUEST_TYPE_URLS[METHOD_CALL_TOOL], payload),
            )
            envelope = json.loads(request.arguments_json, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError, TypeError):
            return False, "INVALID_REQUEST: tool request is malformed"
        if request.binding_id != self._settings.binding_id:
            return False, "INVALID_REQUEST: binding ID does not match"
        if request.HasField("catalog_version") and request.catalog_version != PLUGIN_VERSION:
            return False, "INVALID_REQUEST: tool catalog version does not match"
        tool_id = request.provider_tool_id
        if tool_id not in TOOL_BY_ID:
            return True, _tool_error("TOOL_NOT_FOUND")
        if not isinstance(envelope, Mapping):
            return True, _tool_error("AUTHENTICATION_DENIED")
        operation_id = operation_for(tool_id)
        if TOOL_OPERATIONS.get(tool_id) != operation_id:
            return True, _tool_error("NOT_CONFIGURED", message="operation registry is unavailable")
        try:
            actor, arguments, profile_ref, lease_ref, target, idempotency_key = self._authorize(
                tool_id, envelope
            )
            Draft202012Validator(TOOL_BY_ID[tool_id]["input_schema"]).validate(arguments)
        except ValidationError:
            return True, _tool_error("INVALID_ARGUMENTS")
        except TrustedContextError:
            return True, _tool_error("AUTHENTICATION_DENIED")
        except (KeyError, TypeError, ValueError):
            return True, _tool_error("AUTHENTICATION_DENIED")
        try:
            _check_cancelled(cancellation_event)
            if tool_id in QR_OPERATIONS:
                result = self._execute_qr(tool_id, arguments, actor)
            else:
                result = self._execute_account_operation(
                    tool_id,
                    arguments,
                    actor,
                    profile_ref=profile_ref,
                    lease_ref=lease_ref,
                    target=target,
                    idempotency_key=idempotency_key,
                    cancellation_event=cancellation_event,
                )
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        except InterruptedError:
            raise
        except AccountCenterUnavailable:
            return True, _tool_error("AUTHORIZATION_UNAVAILABLE", retryable=True)
        except BilibiliFailure as error:
            if error.code == "NOT_CONFIGURED":
                return True, _tool_error(
                    "NOT_CONFIGURED", message="Bilibili account is not configured"
                )
            return True, _tool_error(error.code, retryable=error.retryable)
        except ReceiptStoreError:
            return True, _tool_error("STATE_UNAVAILABLE")
        except Exception:
            return True, _tool_error("PROVIDER_FAILURE", retryable=True)
        if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
            return True, _tool_error("OUTPUT_LIMIT_EXCEEDED")
        return True, _tool_response(encoded)

    def _authorize(
        self,
        tool_id: str,
        envelope: Mapping[str, Any],
    ) -> tuple[
        dict[str, str | None],
        Mapping[str, Any],
        str | None,
        str | None,
        Mapping[str, Any] | None,
        str | None,
    ]:
        """Verify the shared ToolEnvelope before opening private state."""

        if set(envelope) != {"caller_context", "arguments"}:
            raise TrustedContextError("shared tool envelope is invalid")
        arguments = envelope.get("arguments")
        caller_context = envelope.get("caller_context")
        if not isinstance(arguments, Mapping):
            raise TrustedContextError("tool arguments are invalid")
        if tool_id in QR_OPERATIONS:
            actor = verify_exchange_context(
                self._settings,
                arguments,
                caller_context,
                expected_operation=tool_id,
            )
            return actor, arguments, None, None, None, None

        is_write = tool_id in {
            "bilibili.comment.create",
            "bilibili.dynamic.publish",
            "bilibili.dm.send",
        }
        expected_fields = {
            "profile_ref",
            "lease_ref",
            "target",
            "arguments_sha256",
            "payload",
        }
        if is_write:
            expected_fields.add("idempotency_key")
        if set(arguments) != expected_fields:
            raise TrustedContextError("signed provider lease envelope is invalid")
        tool_arguments = arguments.get("payload")
        profile_ref = arguments.get("profile_ref")
        lease_ref = arguments.get("lease_ref")
        target = arguments.get("target")
        digest = arguments.get("arguments_sha256")
        idempotency_key = arguments.get("idempotency_key")
        if (
            not isinstance(tool_arguments, Mapping)
            or not _UUID.fullmatch(str(profile_ref))
            or not _UUID.fullmatch(str(lease_ref))
            or not isinstance(target, Mapping)
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or (
                is_write
                and (
                    not isinstance(idempotency_key, str)
                    or re.fullmatch(r"[\x21-\x7e]{8,128}", idempotency_key) is None
                )
            )
            or (not is_write and idempotency_key is not None)
        ):
            raise TrustedContextError("provider lease values are invalid")
        actor = verify_exchange_context(
            self._settings,
            arguments,
            caller_context,
            expected_operation=tool_id,
        )
        if digest != payload_sha256(dict(tool_arguments)):
            raise TrustedContextError("tool arguments digest does not match")
        if dict(target) != expected_target(tool_id, tool_arguments):
            raise TrustedContextError("lease target does not match tool arguments")
        return (
            actor,
            tool_arguments,
            str(profile_ref),
            str(lease_ref),
            target,
            idempotency_key if isinstance(idempotency_key, str) else None,
        )

    def _execute_qr(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        actor: Mapping[str, str | None],
    ) -> dict[str, Any]:
        if self._account_center is None:
            raise BilibiliFailure("NOT_CONFIGURED")
        scope_key = self._store.scope_key(actor)
        if tool_id == "bilibili.auth.qr.begin":
            profile_ref = str(arguments["profile_ref"])
            link = self._account_center.begin_bilibili_link(
                profile_ref,
                str(arguments["idempotency_key"]),
                actor,
            )
            ticket = self._api.generate_qr()
            expires_at = min(int(link["expires_at"]), ticket.expires_at)
            login_flow_id = uuid.uuid4().hex + uuid.uuid4().hex
            flow = self._store.begin_flow(
                scope_key=scope_key,
                profile_ref=str(link["profile_ref"]),
                link_session_ref=str(link["link_session_ref"]),
                idempotency_key=str(arguments["idempotency_key"]),
                qrcode_key=ticket.qrcode_key,
                qr_png_base64=base64.b64encode(ticket.qr_png).decode("ascii"),
                qr_png_sha256=ticket.qr_png_sha256,
                qr_cookies_json=ticket.cookies_json,
                expires_at=expires_at,
                login_flow_id=login_flow_id,
            )
            if flow.status != "PENDING" or flow.expires_at <= int(time.time()):
                return {
                    "status": "EXPIRED",
                    "login_flow_id": flow.login_flow_id,
                    "expires_at": _iso8601(flow.expires_at),
                }
            return {
                "status": "PENDING",
                "login_flow_id": flow.login_flow_id,
                "qr_png_base64": flow.qr_png_base64,
                "qr_png_sha256": flow.qr_png_sha256,
                "expires_at": _iso8601(flow.expires_at),
            }

        flow_id = str(arguments["login_flow_id"])
        flow = self._store.get_flow(scope_key, flow_id)
        if flow is None:
            return {"status": "NOT_FOUND"}
        if flow.status == "LINKED":
            return {
                "status": "LINKED",
                "profile": self._store.get_linked_profile(scope_key, flow_id),
            }
        if flow.status in {"EXPIRED", "FAILED", "UNKNOWN_RESULT"}:
            return {"status": flow.status}
        if flow.expires_at <= int(time.time()):
            self._store.mark_flow(scope_key, flow_id, "EXPIRED")
            return {"status": "EXPIRED"}
        poll = self._api.poll_qr(flow.qrcode_key, flow.qr_cookies_json)
        if poll.status == "PENDING":
            return {"status": "PENDING"}
        if poll.status == "SCANNED":
            return {"status": "SCANNED"}
        if poll.status == "EXPIRED":
            self._store.mark_flow(scope_key, flow_id, "EXPIRED")
            return {"status": "EXPIRED"}
        if poll.status != "LINKED" or poll.external_subject is None:
            self._store.mark_flow(scope_key, flow_id, "FAILED")
            return {"status": "FAILED"}
        cookies = _parse_cookie_jar(poll.cookies_json)
        account_id = cookies.get("DedeUserID", {}).get("value")
        if account_id is not None and account_id != poll.external_subject:
            self._store.mark_flow(scope_key, flow_id, "FAILED")
            return {"status": "FAILED"}
        expiry_values = [
            item.get("expires_at")
            for item in cookies.values()
            if isinstance(item.get("expires_at"), int)
        ]
        credential_expires_at = min(expiry_values) if expiry_values else None
        try:
            profile = self._account_center.complete_bilibili_link(
                {
                    "profile_ref": flow.profile_ref,
                    "link_session_ref": flow.link_session_ref,
                    "external_subject": poll.external_subject,
                    "credential_json": poll.cookies_json,
                    "credential_expires_at": credential_expires_at,
                    "idempotency_key": f"bili-complete-{flow.login_flow_id}",
                },
                actor,
            )
        except AccountCenterUnavailable:
            # Cookie material is deliberately not persisted for replay. The
            # caller receives an explicit unknown outcome and must reconcile
            # via Account Center before starting a fresh login.
            self._store.mark_flow(scope_key, flow_id, "UNKNOWN_RESULT")
            return {"status": "UNKNOWN_RESULT", "relogin_required": True}
        safe_profile = _safe_linked_profile(profile)
        self._store.complete_flow(scope_key, flow_id, safe_profile)
        return {"status": "LINKED", "profile": safe_profile}

    def _execute_account_operation(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        actor: Mapping[str, str | None],
        *,
        profile_ref: str | None,
        lease_ref: str | None,
        target: Mapping[str, Any] | None,
        idempotency_key: str | None,
        cancellation_event: Any,
    ) -> dict[str, Any]:
        if (
            self._account_center is None
            or profile_ref is None
            or lease_ref is None
            or target is None
        ):
            raise BilibiliFailure("NOT_CONFIGURED")
        scope_key = self._store.scope_key(actor)
        # Exchange selects this connection's fixed profile_ref. Account Center's
        # owner/binding-scoped one-use lease is the authorization proof; do not
        # guess among actor profiles or impose a provider-wide active profile.
        is_write = tool_id in {
            "bilibili.comment.create",
            "bilibili.dynamic.publish",
            "bilibili.dm.send",
        }
        arguments_digest = payload_sha256(dict(arguments))
        lease_payload = {
            "lease_ref": lease_ref,
            "profile_ref": profile_ref,
            "operation_id": tool_id,
            "target": dict(target),
            "arguments_sha256": arguments_digest,
        }
        receipt_id = str(uuid.uuid4())
        if is_write:
            if idempotency_key is None:
                raise BilibiliFailure("INVALID_ARGUMENTS")
            state, previous = self._store.start_write(
                scope_key=scope_key,
                idempotency_key=idempotency_key,
                profile_ref=profile_ref,
                tool_id=tool_id,
                arguments_sha256=arguments_digest,
                receipt_id=receipt_id,
            )
            if state != "NEW":
                replay = dict(previous or {"status": state, "receipt_id": receipt_id})
                replay["operation_ref"] = idempotency_key
                if replay.get("status") in {"UNKNOWN_RESULT", "REJECTED"}:
                    replay.setdefault("error_code", str(replay["status"]))
                return replay
        try:
            credential = self._account_center.redeem_provider_credential_lease(lease_payload, actor)
            if (
                not isinstance(credential, AuthMaterial)
                or credential.token_type != "BilibiliCookieJar"
                or not credential.access_token
                or not credential.external_subject
                or (credential.expires_at is not None and credential.expires_at <= int(time.time()))
            ):
                raise AccountCenterUnavailable(
                    "Account Center returned unusable Bilibili credentials"
                )
            cookies = _parse_cookie_jar(credential.access_token)
            account_id = cookies.get("DedeUserID", {}).get("value")
            if account_id != credential.external_subject:
                raise BilibiliFailure("PROFILE_IDENTITY_MISMATCH")
            _check_cancelled(cancellation_event)
        except Exception:
            if is_write:
                self._store.cancel_write_claim(
                    scope_key=scope_key,
                    idempotency_key=idempotency_key,
                    receipt_id=receipt_id,
                )
            raise
        if not is_write:
            return self._execute_read(tool_id, arguments, credential)
        try:
            if tool_id == "bilibili.comment.create":
                outcome = self._api.comment_create(
                    video_id=str(arguments["video_id"]),
                    root_comment_id=str(arguments["root_comment_id"]),
                    parent_comment_id=str(arguments["parent_comment_id"]),
                    content=str(arguments["content"]),
                    cookies_json=credential.access_token,
                )
            elif tool_id == "bilibili.dynamic.publish":
                outcome = self._api.publish_dynamic(
                    str(arguments["user_id"]),
                    str(arguments["content"]),
                    credential.access_token,
                    credential.external_subject,
                )
            else:
                outcome = self._api.send_dm(
                    str(arguments["peer_user_id"]),
                    str(arguments["content"]),
                    credential.access_token,
                    credential.external_subject,
                )
            result = {
                "status": "SUCCEEDED",
                "operation_ref": idempotency_key,
                "receipt_id": receipt_id,
                **outcome,
            }
            self._store.finish_write(
                scope_key=scope_key,
                idempotency_key=idempotency_key,
                receipt_id=receipt_id,
                status="SUCCEEDED",
                result=result,
            )
            return result
        except BilibiliFailure as error:
            status = "UNKNOWN_RESULT" if error.unknown_write else "REJECTED"
            result = {
                "status": status,
                "operation_ref": idempotency_key,
                "receipt_id": receipt_id,
                "error_code": error.code,
            }
            if error.external_id is not None:
                result["external_id"] = error.external_id
            self._store.finish_write(
                scope_key=scope_key,
                idempotency_key=idempotency_key,
                receipt_id=receipt_id,
                status=status,
                result=result,
            )
            return result
        except InterruptedError:
            self._store.finish_write(
                scope_key=scope_key,
                idempotency_key=idempotency_key,
                receipt_id=receipt_id,
                status="UNKNOWN_RESULT",
                result={
                    "status": "UNKNOWN_RESULT",
                    "operation_ref": idempotency_key,
                    "receipt_id": receipt_id,
                    "error_code": "CANCELLED_DURING_WRITE",
                },
            )
            raise
        except Exception:
            result = {
                "status": "UNKNOWN_RESULT",
                "operation_ref": idempotency_key,
                "receipt_id": receipt_id,
                "error_code": "PROVIDER_FAILURE",
            }
            self._store.finish_write(
                scope_key=scope_key,
                idempotency_key=idempotency_key,
                receipt_id=receipt_id,
                status="UNKNOWN_RESULT",
                result=result,
            )
            return result

    def _execute_read(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        credential: AuthMaterial,
    ) -> dict[str, Any]:
        cookies = credential.access_token
        subject = credential.external_subject
        if tool_id == "bilibili.search":
            return self._api.search(
                str(arguments["keyword"]),
                str(arguments["search_type"]),
                int(arguments.get("page", 1)),
                cookies,
            )
        if tool_id == "bilibili.user.find":
            return {"item": self._api.user_find(str(arguments["user_id"]), cookies)}
        if tool_id == "bilibili.video.read":
            return self._api.video_read(str(arguments["video_id"]), cookies)
        if tool_id == "bilibili.comments.list":
            return self._api.comments_list(
                str(arguments["video_id"]),
                arguments.get("root_comment_id"),
                int(arguments.get("page", 1)),
                int(arguments.get("limit", self._settings.max_page_size)),
                cookies,
            )
        if tool_id == "bilibili.dynamics.list":
            return self._api.dynamics_list(
                str(arguments["user_id"]),
                arguments.get("cursor"),
                int(arguments.get("limit", self._settings.max_page_size)),
                cookies,
            )
        if tool_id == "bilibili.dynamic.read":
            return self._api.dynamic_read(str(arguments["dynamic_id"]), cookies)
        if tool_id == "bilibili.dm.list":
            return self._api.dm_list(
                str(arguments["peer_user_id"]),
                arguments.get("cursor"),
                int(arguments.get("limit", self._settings.max_page_size)),
                cookies,
                subject,
            )
        if tool_id == "bilibili.dm.read":
            return self._api.dm_read(
                str(arguments["peer_user_id"]),
                str(arguments["message_id"]),
                cookies,
                subject,
            )
        raise BilibiliFailure("TOOL_NOT_FOUND")


def _tool_response(value: str) -> TypedPayload:
    response = wire.CallToolResponse()
    response.outcome.is_error = False
    response.outcome.content.add().json.json = value
    return encode_response(METHOD_CALL_TOOL, response)


def _tool_error(
    code: str,
    *,
    retryable: bool = False,
    message: str | None = None,
) -> TypedPayload:
    response = wire.CallToolResponse()
    response.outcome.is_error = True
    error: dict[str, Any] = {"code": code, "retryable": retryable}
    if message is not None:
        error["message"] = message
    response.outcome.content.add().json.json = json.dumps(
        {"error": error}, ensure_ascii=False, separators=(",", ":")
    )
    return encode_response(METHOD_CALL_TOOL, response)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _check_cancelled(cancellation: Any) -> None:
    is_set = getattr(cancellation, "is_set", None)
    if (callable(is_set) and is_set()) or getattr(cancellation, "cancelled", False):
        raise InterruptedError("Bilibili operation cancelled")


def _iso8601(epoch: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def _safe_linked_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    safe = {
        key: value[key]
        for key in (
            "profile_ref",
            "provider",
            "auth_status",
            "external_subject",
            "expires_at",
            "auth_mode",
            "access_mode",
            "workspace_id",
            "credential_revision",
            "reauth_required",
        )
        if key in value
    }
    # Defensive output denylist: a malformed upstream can never leak a jar.
    sensitive_fields = {
        "credential",
        "credential_json",
        "credential_blob",
        "access_token",
        "refresh_token",
        "token",
        "cookies",
        "cookie_jar",
        "link_session_ref",
    }
    if any(key.lower() in sensitive_fields or "cookie" in key.lower() for key in safe):
        raise AccountCenterUnavailable("Account Center profile contained sensitive fields")
    return safe


__all__ = ["BilibiliProvider"]
