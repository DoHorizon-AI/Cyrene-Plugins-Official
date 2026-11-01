"""Private Account Center calls for Bilibili link sessions and leases."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

import grpc
from cyrene_account_center_contracts import AuthMaterial, canonical_json, sign_context
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient

from .settings import ProviderSettings

CAPABILITY_ID = "account.center.v1"
INTERFACE_VERSION = "1"
_TYPE_PREFIX = "type.googleapis.com/cyrene.account.center.v1."
_BEGIN_METHOD = "begin_bilibili_link"
_COMPLETE_METHOD = "complete_bilibili_link"
_REDEEM_METHOD = "redeem_provider_credential_lease"
_BEGIN_REQUEST = _TYPE_PREFIX + "BeginBilibiliLinkRequest"
_BEGIN_RESPONSE = _TYPE_PREFIX + "BilibiliLinkSession"
_COMPLETE_REQUEST = _TYPE_PREFIX + "CompleteBilibiliLinkRequest"
_COMPLETE_RESPONSE = _TYPE_PREFIX + "LinkedProviderProfileResponse"
_REDEEM_REQUEST = _TYPE_PREFIX + "RedeemProviderCredentialLeaseRequest"
_REDEEM_RESPONSE = _TYPE_PREFIX + "RedeemProviderCredentialLeaseResponse"
_MAX_RESPONSE_BYTES = 128 * 1024


class AccountCenterUnavailable(RuntimeError):
    """The private Account Center operation or response is unavailable."""


class AccountCenterClient(Protocol):
    """The narrow Account Center calls needed by one Bilibili operation."""

    def begin_bilibili_link(
        self,
        profile_ref: str,
        idempotency_key: str,
        actor: Mapping[str, str | None],
    ) -> Mapping[str, Any]: ...

    def complete_bilibili_link(
        self,
        payload: Mapping[str, Any],
        actor: Mapping[str, str | None],
    ) -> Mapping[str, Any]: ...

    def redeem_provider_credential_lease(
        self,
        payload: Mapping[str, Any],
        actor: Mapping[str, str | None],
    ) -> AuthMaterial: ...

    def close(self) -> None: ...


class AccountCenterDirectClient:
    """Call one host-resolved Account Center binding over DirectPluginRuntime."""

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        client: DirectPluginClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or _direct_client(settings)
        self._owns_client = client is None

    def close(self) -> None:
        """Close only a client created by this wrapper."""

        if self._owns_client:
            self._client.close()

    def begin_bilibili_link(
        self,
        profile_ref: str,
        idempotency_key: str,
        actor: Mapping[str, str | None],
    ) -> Mapping[str, Any]:
        """Create an owner-scoped one-use link session without returning it to UI."""

        payload = {
            "profile_ref": _uuid(profile_ref),
            "idempotency_key": _bounded_text(idempotency_key, 128),
        }
        value = self._invoke(_BEGIN_METHOD, _BEGIN_REQUEST, _BEGIN_RESPONSE, payload, actor)
        if set(value) != {"profile_ref", "link_session_ref", "expires_at"}:
            raise AccountCenterUnavailable("Account Center begin response is invalid")
        if value["profile_ref"] != payload["profile_ref"]:
            raise AccountCenterUnavailable("Account Center profile did not match")
        result = {
            "profile_ref": _uuid(value["profile_ref"]),
            "link_session_ref": _uuid(value["link_session_ref"]),
            "expires_at": _epoch(value["expires_at"]),
        }
        return result

    def complete_bilibili_link(
        self,
        payload: Mapping[str, Any],
        actor: Mapping[str, str | None],
    ) -> Mapping[str, Any]:
        """Encrypt and link one validated cookie jar inside Account Center."""

        required = {
            "profile_ref",
            "link_session_ref",
            "external_subject",
            "credential_json",
            "idempotency_key",
        }
        if set(payload) != required:
            raise AccountCenterUnavailable("Account Center completion request is invalid")
        request = {
            "profile_ref": _uuid(payload["profile_ref"]),
            "link_session_ref": _uuid(payload["link_session_ref"]),
            "external_subject": _numeric_id(payload["external_subject"]),
            "credential_json": _bounded_text(payload["credential_json"], 16 * 1024),
            "idempotency_key": _bounded_text(payload["idempotency_key"], 128),
        }
        expires = payload.get("credential_expires_at")
        if expires is not None:
            request["credential_expires_at"] = _epoch(expires)
        value = self._invoke(
            _COMPLETE_METHOD,
            _COMPLETE_REQUEST,
            _COMPLETE_RESPONSE,
            request,
            actor,
        )
        allowed = {
            "profile_ref",
            "provider",
            "auth_status",
            "external_subject",
            "expires_at",
            "auth_mode",
            "access_mode",
            "refresh_expires_at",
            "workspace_id",
            "scope_schema_version",
            "oauth_scopes",
            "credential_revision",
            "reauth_required",
        }
        if set(value) - allowed or not {
            "profile_ref",
            "provider",
            "auth_status",
            "external_subject",
        } <= set(value):
            raise AccountCenterUnavailable("Account Center link response is invalid")
        if value["profile_ref"] != request["profile_ref"] or value["provider"] != "bilibili":
            raise AccountCenterUnavailable("Account Center linked another profile")
        if value["external_subject"] != request["external_subject"]:
            raise AccountCenterUnavailable("Account Center linked another identity")
        return _safe_profile(value)

    def redeem_provider_credential_lease(
        self,
        payload: Mapping[str, Any],
        actor: Mapping[str, str | None],
    ) -> AuthMaterial:
        """Consume one lease bound to the exact Bilibili operation and arguments."""

        required = {"lease_ref", "profile_ref", "operation_id", "target", "arguments_sha256"}
        if set(payload) != required or not isinstance(payload.get("target"), Mapping):
            raise AccountCenterUnavailable("provider lease request is invalid")
        request = {
            "lease_ref": _uuid(payload["lease_ref"]),
            "profile_ref": _uuid(payload["profile_ref"]),
            "operation_id": _bounded_text(payload["operation_id"], 128),
            "target": dict(payload["target"]),
            "arguments_sha256": _digest(payload["arguments_sha256"]),
        }
        if request["operation_id"] not in _BILIBILI_LEASE_OPERATIONS:
            raise AccountCenterUnavailable("provider lease operation is invalid")
        value = self._invoke(_REDEEM_METHOD, _REDEEM_REQUEST, _REDEEM_RESPONSE, request, actor)
        required_response = {"access_token", "token_type", "external_subject", "expires_at"}
        if not required_response <= set(value) or set(value) - (
            required_response | {"profile_scope_sha256"}
        ):
            raise AccountCenterUnavailable("Account Center credential response is invalid")
        token = value["access_token"]
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 16 * 1024
            or value["token_type"] != "BilibiliCookieJar"
        ):
            raise AccountCenterUnavailable("Account Center credential material is invalid")
        subject = _numeric_id(value["external_subject"])
        expires_at = _epoch(value["expires_at"]) if value["expires_at"] is not None else None
        scope = value.get("profile_scope_sha256")
        if scope is not None:
            scope = _digest(scope)
        return AuthMaterial(
            access_token=token,
            token_type="BilibiliCookieJar",
            external_subject=subject,
            expires_at=expires_at,
            profile_scope_sha256=scope,
        )

    def _invoke(
        self,
        method: str,
        request_type: str,
        response_type: str,
        payload: Mapping[str, Any],
        actor: Mapping[str, str | None],
    ) -> dict[str, Any]:
        """Sign and invoke exactly one fixed Account Center request."""

        issued_at = int(time.time())
        signed = sign_context(
            method=method,
            payload=dict(payload),
            caller_id=self._settings.provider_caller_id,
            issuer=_actor_text(actor, "issuer"),
            subject=_actor_text(actor, "subject"),
            organization_id=_actor_text(actor, "organization_id"),
            workspace_id=actor.get("workspace_id"),
            binding_id=self._settings.account_center_binding_id,
            request_id=str(uuid.uuid4()),
            operation_id=method,
            key=self._settings.provider_signing_key,
            issued_at=issued_at,
            expires_at=issued_at + 15,
        )
        envelope = {"payload": dict(payload), "trusted_context": signed.as_dict()}
        try:
            response = self._client.invoke(
                capability=CAPABILITY_ID,
                interface_version=INTERFACE_VERSION,
                method=method,
                request=DirectPayload(type_url=request_type, value=canonical_json(envelope)),
                deadline_seconds=self._settings.timeout_seconds,
            )
        except Exception:
            raise AccountCenterUnavailable("Account Center request failed") from None
        if response.type_url != response_type or len(response.value) > _MAX_RESPONSE_BYTES:
            raise AccountCenterUnavailable("Account Center response type is invalid")
        try:
            value = json.loads(response.value, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise AccountCenterUnavailable("Account Center response JSON is invalid") from None
        if not isinstance(value, dict):
            raise AccountCenterUnavailable("Account Center response must be an object")
        return value


_BILIBILI_LEASE_OPERATIONS = frozenset(
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


def _direct_client(settings: ProviderSettings) -> DirectPluginClient:
    if settings.account_center_transport == "local":
        try:
            return DirectPluginClient.for_local_connection_ref(
                settings.account_center_connection_ref
            )
        except ValueError as error:
            raise AccountCenterUnavailable("Account Center ref is not loopback") from error
    if settings.account_center_transport != "mtls":
        raise AccountCenterUnavailable("Account Center transport is unsupported")
    reference = settings.account_center_connection_ref
    if not reference.startswith("grpcs://"):
        raise AccountCenterUnavailable("remote Account Center ref must use grpcs")
    target = reference.removeprefix("grpcs://")
    if (
        any(char in target for char in "/?#@")
        or re.fullmatch(r"(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):[0-9]{1,5}", target) is None
    ):
        raise AccountCenterUnavailable("remote Account Center authority is invalid")
    root = _read_certificate(settings, settings.account_center_root_cert_ref)
    cert = _read_certificate(settings, settings.account_center_client_cert_ref)
    key = _read_certificate(settings, settings.account_center_client_key_ref)
    credentials = grpc.ssl_channel_credentials(
        root_certificates=root, private_key=key, certificate_chain=cert
    )
    return DirectPluginClient(grpc.secure_channel(target, credentials), owns_channel=True)


def _read_certificate(settings: ProviderSettings, reference: str | None) -> bytes:
    if reference is None:
        raise AccountCenterUnavailable("Account Center mTLS certificate is missing")
    environment = settings.host_secret_names.get(reference)
    data = None if environment is None else os.environ.get(environment)
    if not data:
        raise AccountCenterUnavailable("Account Center mTLS certificate is unavailable")
    try:
        return (
            data.encode("ascii") if "-----BEGIN" in data else base64.b64decode(data, validate=True)
        )
    except (ValueError, UnicodeEncodeError):
        raise AccountCenterUnavailable("Account Center mTLS certificate is invalid") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


def _safe_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project only non-secret link metadata into the tool response."""

    return {
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


def _uuid(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value)
        is None
    ):
        raise AccountCenterUnavailable("Account Center UUID is invalid")
    return value


def _numeric_id(value: object) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise AccountCenterUnavailable("Bilibili identity is invalid")
    text = str(value)
    if re.fullmatch(r"[0-9]{1,20}", text) is None:
        raise AccountCenterUnavailable("Bilibili identity is invalid")
    return text


def _bounded_text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or value != value.strip():
        raise AccountCenterUnavailable("Account Center request text is invalid")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise AccountCenterUnavailable("Account Center digest is invalid")
    return value


def _epoch(value: object) -> int:
    if isinstance(value, bool):
        raise AccountCenterUnavailable("Account Center expiry is invalid")
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    raise AccountCenterUnavailable("Account Center expiry is invalid")


def _actor_text(actor: Mapping[str, str | None], field: str) -> str:
    value = actor.get(field)
    if not isinstance(value, str) or not value:
        raise AccountCenterUnavailable("verified actor context is incomplete")
    return value
