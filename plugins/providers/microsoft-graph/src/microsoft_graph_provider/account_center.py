"""
┌────────────────────────────────────────────────────────────────────┐
│  Private Account Center profile and one-use lease client             │
│  只在内存中读取用户配置元数据与委托式短期令牌                         │
└────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any, Protocol

import grpc
from cyrene_account_center_contracts import (
    AuthMaterial,
    ProviderProfile,
    RedeemProviderCredentialLeaseRequest,
    canonical_json,
)
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient

from .settings import ProviderSettings
from .trust import account_center_context

CAPABILITY_ID = "account.center.v1"
INTERFACE_VERSION = "1"
LIST_METHOD = "list_provider_profiles"
REDEEM_METHOD = "redeem_provider_credential_lease"
LIST_REQUEST_TYPE_URL = "type.googleapis.com/cyrene.account.center.v1.ListProviderProfilesRequest"
LIST_RESPONSE_TYPE_URL = "type.googleapis.com/cyrene.account.center.v1.ListProviderProfilesResponse"
REDEEM_REQUEST_TYPE_URL = (
    "type.googleapis.com/cyrene.account.center.v1.RedeemProviderCredentialLeaseRequest"
)
REDEEM_RESPONSE_TYPE_URL = (
    "type.googleapis.com/cyrene.account.center.v1.RedeemProviderCredentialLeaseResponse"
)
MAX_RPC_RESPONSE_BYTES = 64 * 1024


class AccountCenterUnavailable(RuntimeError):
    """The private Account Center channel or response is unavailable."""


class AccountCenterClient(Protocol):
    """Private metadata and lease operations used for one Graph call."""

    def list_provider_profiles(
        self,
        trusted_context: Mapping[str, str | None],
    ) -> tuple[ProviderProfile, ...]:
        """Return profiles visible to the signed Cyrene actor without secrets."""

    def redeem_provider_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | None],
    ) -> AuthMaterial:
        """Return the exact one-use Graph credential into provider memory."""

    def close(self) -> None:
        """Release a locally constructed Account Center channel."""


class AccountCenterDirectClient:
    """Use one host-resolved Account Center connection with signed fixed methods."""

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
        """Close the direct-runtime channel when this instance owns it."""

        if self._owns_client:
            self._client.close()

    def list_provider_profiles(
        self,
        trusted_context: Mapping[str, str | None],
    ) -> tuple[ProviderProfile, ...]:
        """Read the verified actor's profile scope and auth state without credentials."""

        payload: dict[str, Any] = {}
        context = account_center_context(
            self._settings,
            trusted_context,
            payload,
            method=LIST_METHOD,
        )
        result = self._invoke(
            method=LIST_METHOD,
            request_type_url=LIST_REQUEST_TYPE_URL,
            response_type_url=LIST_RESPONSE_TYPE_URL,
            payload=payload,
            trusted_context=context,
        )
        return _profiles(_parse_object(result))

    def redeem_provider_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | None],
    ) -> AuthMaterial:
        """Redeem a one-use lease bound to exact profile, operation, target, and digest."""

        request = _request(payload)
        request_payload = _request_dict(request)
        context = account_center_context(
            self._settings,
            trusted_context,
            request_payload,
            method=REDEEM_METHOD,
        )
        result = self._invoke(
            method=REDEEM_METHOD,
            request_type_url=REDEEM_REQUEST_TYPE_URL,
            response_type_url=REDEEM_RESPONSE_TYPE_URL,
            payload=request_payload,
            trusted_context=context,
        )
        return _auth_material(_parse_object(result))

    def _invoke(
        self,
        *,
        method: str,
        request_type_url: str,
        response_type_url: str,
        payload: Mapping[str, Any],
        trusted_context: Mapping[str, Any],
    ) -> bytes:
        """Invoke one fixed Account Center RPC and validate its response envelope."""

        envelope = {"payload": dict(payload), "trusted_context": dict(trusted_context)}
        try:
            result = self._client.invoke(
                capability=CAPABILITY_ID,
                interface_version=INTERFACE_VERSION,
                method=method,
                request=DirectPayload(
                    type_url=request_type_url,
                    value=canonical_json(envelope),
                ),
                deadline_seconds=self._settings.timeout_seconds,
            )
        except Exception:
            raise AccountCenterUnavailable("Account Center request failed") from None
        if result.type_url != response_type_url or len(result.value) > MAX_RPC_RESPONSE_BYTES:
            raise AccountCenterUnavailable("Account Center response contract is invalid")
        return bytes(result.value)


def _request(payload: Mapping[str, object]) -> RedeemProviderCredentialLeaseRequest:
    """Parse the exact one-use provider lease redemption shape."""

    required = {"lease_ref", "profile_ref", "operation_id", "target", "arguments_sha256"}
    if set(payload) != required:
        raise AccountCenterUnavailable("profile lease request shape is invalid")
    target = payload.get("target")
    if not isinstance(target, Mapping):
        raise AccountCenterUnavailable("profile lease target is invalid")
    try:
        return RedeemProviderCredentialLeaseRequest(
            lease_ref=_uuid(payload["lease_ref"]),
            profile_ref=_uuid(payload["profile_ref"]),
            operation_id=_text(payload["operation_id"], "operation_id"),
            target=dict(target),
            arguments_sha256=_digest(payload["arguments_sha256"]),
        )
    except (KeyError, TypeError, ValueError):
        raise AccountCenterUnavailable("profile lease request is invalid") from None


def _request_dict(request: RedeemProviderCredentialLeaseRequest) -> dict[str, Any]:
    """Serialize the signed lease redemption DTO exactly as Account Center expects."""

    return {
        "lease_ref": request.lease_ref,
        "profile_ref": request.profile_ref,
        "operation_id": request.operation_id,
        "target": request.target,
        "arguments_sha256": request.arguments_sha256,
    }


def _profiles(data: Mapping[str, Any]) -> tuple[ProviderProfile, ...]:
    """Validate a bounded owner-scoped profile list and retain known metadata only."""

    if set(data) != {"profiles"}:
        raise AccountCenterUnavailable("Account Center profile response shape is invalid")
    values = data.get("profiles")
    if not isinstance(values, list) or len(values) > 100:
        raise AccountCenterUnavailable("Account Center profile list is invalid")
    result: list[ProviderProfile] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise AccountCenterUnavailable("Account Center profile entry is invalid")
        allowed = {
            "profile_ref",
            "provider",
            "identity_ref",
            "target_scope",
            "auth_status",
            "auth_mode",
            "oauth_scopes",
            "access_mode",
            "external_subject",
            "expires_at",
            "credential_revision",
        }
        if set(value) - allowed or set(value) & {
            "access_token",
            "refresh_token",
            "token",
            "credential_blob",
            "client_secret",
        }:
            raise AccountCenterUnavailable("Account Center profile contains unsupported fields")
        try:
            profile_ref = _uuid(value["profile_ref"])
            provider = _text(value["provider"], "provider")
            identity_ref = _text(value["identity_ref"], "identity_ref")
            target_scope = value["target_scope"]
            auth_status = _text(value["auth_status"], "auth_status")
            if not isinstance(target_scope, Mapping):
                raise TypeError("target_scope must be an object")
            oauth_scopes = value.get("oauth_scopes")
            if oauth_scopes is not None and (
                not isinstance(oauth_scopes, list)
                or len(oauth_scopes) > 32
                or any(not isinstance(item, str) or len(item) > 256 for item in oauth_scopes)
            ):
                raise TypeError("oauth_scopes is invalid")
            external_subject = value.get("external_subject")
            if external_subject is not None and (
                not isinstance(external_subject, str) or len(external_subject) > 256
            ):
                raise TypeError("external_subject is invalid")
            expires_at = value.get("expires_at")
            if expires_at is not None and (
                isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= 0
            ):
                raise TypeError("expires_at is invalid")
            result.append(
                ProviderProfile(
                    profile_ref=profile_ref,
                    provider=provider,
                    identity_ref=identity_ref,
                    target_scope=dict(target_scope),
                    auth_status=auth_status,
                    auth_mode=_optional_text(value.get("auth_mode"), 100),
                    oauth_scopes=list(oauth_scopes) if oauth_scopes is not None else None,
                    access_mode=_optional_text(value.get("access_mode"), 64),
                    external_subject=external_subject,
                    expires_at=expires_at,
                )
            )
        except (KeyError, TypeError, ValueError):
            raise AccountCenterUnavailable("Account Center profile response is invalid") from None
    return tuple(result)


def _auth_material(data: Mapping[str, Any]) -> AuthMaterial:
    """Validate memory-only Graph token material and its profile scope digest."""

    required = {"access_token", "token_type", "external_subject", "expires_at"}
    if not required.issubset(data) or set(data) - required - {"profile_scope_sha256"}:
        raise AccountCenterUnavailable("Account Center auth response shape is invalid")
    token = data.get("access_token")
    token_type = data.get("token_type")
    external_subject = data.get("external_subject")
    expires_at = data.get("expires_at")
    scope_digest = data.get("profile_scope_sha256")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 16_384
        or any(char in token for char in "\r\n\x00")
        or token_type != "Bearer"
        or not isinstance(external_subject, str)
        or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:"
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            external_subject,
        )
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at <= 0
        or not isinstance(scope_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", scope_digest)
    ):
        raise AccountCenterUnavailable("Account Center auth response is invalid")
    return AuthMaterial(token, "Bearer", external_subject, expires_at, scope_digest)


def _parse_object(data: bytes) -> dict[str, Any]:
    """Parse one bounded JSON object and reject duplicate keys."""

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate response key")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise AccountCenterUnavailable("Account Center response JSON is invalid") from None
    if not isinstance(value, dict):
        raise AccountCenterUnavailable("Account Center response must be an object")
    return value


def _uuid(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value
    ):
        raise ValueError("reference is invalid")
    return value


def _text(value: object, field: str) -> str:
    del field
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("text is invalid")
    return value


def _optional_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise AccountCenterUnavailable("Account Center profile metadata is invalid")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("digest is invalid")
    return value


def _direct_client(settings: ProviderSettings) -> DirectPluginClient:
    """Create only a loopback gRPC or host-approved mTLS Account Center channel."""

    reference = settings.account_center_connection_ref
    if settings.account_center_transport == "local":
        if not reference.startswith("grpc://"):
            raise AccountCenterUnavailable("local Account Center ref must use grpc")
        target = reference.removeprefix("grpc://")
        match = re.fullmatch(r"(?:127\.0\.0\.1|localhost):([0-9]{1,5})", target)
        if match is None or not 1 <= int(match.group(1)) <= 65_535:
            raise AccountCenterUnavailable("local Account Center ref must use loopback")
        return DirectPluginClient(grpc.insecure_channel(target), owns_channel=True)
    if settings.account_center_transport != "mtls" or not reference.startswith("grpcs://"):
        raise AccountCenterUnavailable("remote Account Center configuration is invalid")
    target = reference.removeprefix("grpcs://")
    if any(char in target for char in "/?#@") or not re.fullmatch(
        r"(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):([0-9]{1,5})", target
    ):
        raise AccountCenterUnavailable("remote Account Center authority is invalid")
    match = re.search(r":([0-9]{1,5})\Z", target)
    if match is None or not 1 <= int(match.group(1)) <= 65_535:
        raise AccountCenterUnavailable("remote Account Center port is invalid")
    root_cert = _secret(settings, settings.account_center_root_cert_ref)
    client_cert = _secret(settings, settings.account_center_client_cert_ref)
    client_key = _secret(settings, settings.account_center_client_key_ref)
    credentials = grpc.ssl_channel_credentials(
        root_certificates=root_cert,
        private_key=client_key,
        certificate_chain=client_cert,
    )
    return DirectPluginClient(grpc.secure_channel(target, credentials), owns_channel=True)


def _secret(settings: ProviderSettings, reference: str | None) -> bytes:
    """Read mTLS material through a stable host-injected secret reference."""

    if not reference:
        raise AccountCenterUnavailable("Account Center mTLS refs are incomplete")
    environment_name = settings.host_secret_names.get(reference)
    if not isinstance(environment_name, str):
        raise AccountCenterUnavailable("Account Center mTLS refs are incomplete")
    value = os.environ.get(environment_name, "")
    if not value:
        raise AccountCenterUnavailable("Account Center mTLS secret is unavailable")
    return value.encode("utf-8")
