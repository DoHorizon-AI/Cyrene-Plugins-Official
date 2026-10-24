"""Private Account Center client for one-use Hugging Face profile leases.

Credentials are returned only to this provider's memory after exact lease checks.
中文：Exchange 仅传 lease_ref，不接触 HF token。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

import grpc
from cyrene_account_center_contracts import (
    AuthMaterial,
    RedeemProviderCredentialLeaseRequest,
    canonical_json,
    sign_context,
)
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient

from .settings import ProviderSettings

CAPABILITY_ID = "account.center.v1"
INTERFACE_VERSION = "1"
METHOD = "redeem_provider_credential_lease"
REQUEST_TYPE_URL = (
    "type.googleapis.com/cyrene.account.center.v1.RedeemProviderCredentialLeaseRequest"
)
RESPONSE_TYPE_URL = "type.googleapis.com/cyrene.account.center.v1.RedeemCredentialLeaseResponse"
MAX_RPC_RESPONSE_BYTES = 64 * 1024


class AccountCenterUnavailable(RuntimeError):
    """The private Account Center lease channel is unavailable or invalid."""


class AccountCenterLeaseClient(Protocol):
    """Redeem a one-use lease over a host-resolved private channel."""

    def redeem_provider_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | int | None],
    ) -> AuthMaterial:
        """Return private in-memory credential material after exact lease checks."""


class AccountCenterDirectLeaseClient:
    """Call one Account Center binding through DirectPluginRuntime."""

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
        """Close a locally constructed direct runtime channel."""

        if self._owns_client:
            self._client.close()

    def redeem_provider_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | int | None],
    ) -> AuthMaterial:
        """Sign and redeem exact profile, operation, target, and argument claims."""

        request = _request(payload)
        request_payload = _request_dict(request)
        actor = _actor(trusted_context)
        issued_at = int(time.time())
        signed = sign_context(
            method=METHOD,
            payload=request_payload,
            caller_id=self._settings.provider_caller_id,
            issuer=str(actor["issuer"]),
            subject=str(actor["subject"]),
            organization_id=str(actor["organization_id"]),
            workspace_id=actor["workspace_id"],
            binding_id=self._settings.account_center_binding_id,
            request_id=str(uuid.uuid4()),
            operation_id=METHOD,
            key=self._settings.provider_signing_key,
            issued_at=issued_at,
            expires_at=issued_at + 60,
        )
        envelope = {"payload": request_payload, "trusted_context": signed.as_dict()}
        try:
            result = self._client.invoke(
                capability=CAPABILITY_ID,
                interface_version=INTERFACE_VERSION,
                method=METHOD,
                request=DirectPayload(type_url=REQUEST_TYPE_URL, value=canonical_json(envelope)),
                deadline_seconds=self._settings.timeout_seconds,
            )
        except Exception as error:
            raise AccountCenterUnavailable(
                "Account Center profile lease redemption failed"
            ) from error
        if result.type_url != RESPONSE_TYPE_URL or len(result.value) > MAX_RPC_RESPONSE_BYTES:
            raise AccountCenterUnavailable("Account Center response contract is invalid")
        return _auth_material(_parse_object(result.value))


def _request(payload: Mapping[str, object]) -> RedeemProviderCredentialLeaseRequest:
    """Convert a fixed JSON request into the shared Account Center DTO."""

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
    except (KeyError, TypeError, ValueError) as error:
        raise AccountCenterUnavailable("profile lease request is invalid") from error


def _request_dict(request: RedeemProviderCredentialLeaseRequest) -> dict[str, Any]:
    """Serialize all lease fields in the shared contract's signed wire shape."""

    return {
        "lease_ref": request.lease_ref,
        "profile_ref": request.profile_ref,
        "operation_id": request.operation_id,
        "target": request.target,
        "arguments_sha256": request.arguments_sha256,
    }


def _actor(context: Mapping[str, str | int | None]) -> dict[str, str | None]:
    """Retain only the verified caller identity and binding claims."""

    result: dict[str, str | None] = {}
    for field in ("issuer", "subject", "organization_id", "binding_id"):
        value = context.get(field)
        if not isinstance(value, str) or not value:
            raise AccountCenterUnavailable("trusted actor anchor is incomplete")
        result[field] = value
    workspace = context.get("workspace_id")
    if workspace is not None and (not isinstance(workspace, str) or not workspace):
        raise AccountCenterUnavailable("trusted workspace scope is invalid")
    result["workspace_id"] = workspace
    return result


def _parse_object(data: bytes) -> dict[str, Any]:
    """Parse a bounded JSON object while rejecting duplicate keys."""

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate response key")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AccountCenterUnavailable("Account Center response JSON is invalid") from error
    if not isinstance(value, dict):
        raise AccountCenterUnavailable("Account Center response must be an object")
    return value


def _auth_material(value: Mapping[str, Any]) -> AuthMaterial:
    """Validate the memory-only bearer token response before returning it."""

    required = {"access_token", "token_type", "external_subject", "expires_at"}
    if not required.issubset(value) or set(value) - required - {"profile_scope_sha256"}:
        raise AccountCenterUnavailable("Account Center auth response shape is invalid")
    token = value["access_token"]
    token_type = value["token_type"]
    subject = value["external_subject"]
    expires_at = value["expires_at"]
    profile_scope_sha256 = value.get("profile_scope_sha256")
    if (
        not isinstance(token, str)
        or not token
        or "\r" in token
        or "\n" in token
        or token_type != "Bearer"
        or not isinstance(subject, str)
        or not subject
        or (
            profile_scope_sha256 is not None
            and (
                not isinstance(profile_scope_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", profile_scope_sha256) is None
            )
        )
        or (
            expires_at is not None
            and (isinstance(expires_at, bool) or not isinstance(expires_at, int))
        )
    ):
        raise AccountCenterUnavailable("Account Center auth response is invalid")
    return AuthMaterial(
        token,
        token_type,
        subject,
        expires_at,
        profile_scope_sha256=profile_scope_sha256,
    )


def _uuid(value: object) -> str:
    """Require a canonical UUID reference."""

    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value
    ):
        raise ValueError("reference is invalid")
    return value


def _text(value: object, field: str) -> str:
    """Return bounded non-empty request text."""

    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError(f"{field} is invalid")
    return value


def _digest(value: object) -> str:
    """Require a lowercase SHA-256 payload digest."""

    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("arguments digest is invalid")
    return value


def _direct_client(settings: ProviderSettings) -> DirectPluginClient:
    """Create a resolved local or mutually authenticated Account Center client."""

    reference = settings.account_center_connection_ref
    if settings.account_center_transport == "local":
        if not reference.startswith("grpc://"):
            raise AccountCenterUnavailable("local Account Center ref must use grpc")
        target = reference.removeprefix("grpc://")
        if not re.fullmatch(r"(?:127\.0\.0\.1|localhost):[0-9]{1,5}", target):
            raise AccountCenterUnavailable("local Account Center ref must use loopback")
        return DirectPluginClient(grpc.insecure_channel(target), owns_channel=True)
    if settings.account_center_transport != "mtls" or not reference.startswith("grpcs://"):
        raise AccountCenterUnavailable("remote Account Center configuration is invalid")
    target = reference.removeprefix("grpcs://")
    if any(char in target for char in "/?#@") or not re.fullmatch(
        r"(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):[0-9]{1,5}", target
    ):
        raise AccountCenterUnavailable("remote Account Center authority is invalid")
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
    """Resolve one mTLS certificate secret from host-injected environment."""

    if not reference:
        raise AccountCenterUnavailable("Account Center mTLS refs are incomplete")
    environment_name = settings.host_secret_names.get(reference)
    if not isinstance(environment_name, str):
        raise AccountCenterUnavailable("Account Center mTLS refs are incomplete")
    value = os.environ.get(environment_name, "")
    if not value:
        raise AccountCenterUnavailable("Account Center mTLS secret is unavailable")
    return value.encode("utf-8")
