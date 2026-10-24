"""Resolved DirectPluginRuntime client for one-use lease redemption.

中文:通过 DirectPluginRuntime 兑换一次性租约的 Account Center 客户端。
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
    RedeemCredentialLeaseRequest,
    RedeemProviderCredentialLeaseRequest,
    Resource,
    canonical_json,
    sign_context,
)
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient

from .settings import ProviderSettings

ACCOUNT_CENTER_CAPABILITY = "account.center.v1"
ACCOUNT_CENTER_INTERFACE_VERSION = "1"
ACCOUNT_CENTER_METHOD = "redeem_credential_lease"
ACCOUNT_CENTER_REQUEST_TYPE_URL = (
    "type.googleapis.com/cyrene.account.center.v1.RedeemCredentialLeaseRequest"
)
ACCOUNT_CENTER_RESPONSE_TYPE_URL = (
    "type.googleapis.com/cyrene.account.center.v1.RedeemCredentialLeaseResponse"
)
PROVIDER_LEASE_METHOD = "redeem_provider_credential_lease"
PROVIDER_LEASE_REQUEST_TYPE_URL = (
    "type.googleapis.com/cyrene.account.center.v1.RedeemProviderCredentialLeaseRequest"
)
PROVIDER_LEASE_RESPONSE_TYPE_URL = ACCOUNT_CENTER_RESPONSE_TYPE_URL
MAX_RPC_RESPONSE_BYTES = 64 * 1024


class AccountCenterUnavailable(RuntimeError):
    """Account Center is unavailable; the provider must fail closed."""


class AccountCenterLeaseClient(Protocol):
    """Call one resolved ``account.center.v1`` provider-role endpoint."""

    def redeem_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | int | None],
    ) -> AuthMaterial:
        """Redeem a lease once and return private in-memory auth material."""

    def redeem_provider_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | int | None],
    ) -> AuthMaterial:
        """Redeem one profile-scoped lease and return private in-memory auth."""


class AccountCenterDirectLeaseClient:
    """Call one host-resolved Account Center endpoint over DirectPluginRuntime."""

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

    def redeem_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | int | None],
    ) -> AuthMaterial:
        """Sign and invoke exactly one provider-role Account Center redemption."""

        request = _redeem_request(payload)
        request_payload = _request_dict(request)
        actor = _actor_context(trusted_context)
        signed = sign_context(
            method=ACCOUNT_CENTER_METHOD,
            payload=request_payload,
            caller_id=self._settings.provider_caller_id,
            issuer=str(actor["issuer"]),
            subject=str(actor["subject"]),
            organization_id=str(actor["organization_id"]),
            workspace_id=actor["workspace_id"],
            binding_id=str(actor["binding_id"]),
            request_id=str(uuid.uuid4()),
            operation_id=ACCOUNT_CENTER_METHOD,
            key=self._settings.provider_signing_key,
            issued_at=int(time.time()),
            expires_at=int(time.time()) + 60,
        )
        envelope = {
            "payload": request_payload,
            "trusted_context": signed.as_dict(),
        }
        try:
            result = self._client.invoke(
                capability=ACCOUNT_CENTER_CAPABILITY,
                interface_version=ACCOUNT_CENTER_INTERFACE_VERSION,
                method=ACCOUNT_CENTER_METHOD,
                request=DirectPayload(
                    type_url=ACCOUNT_CENTER_REQUEST_TYPE_URL,
                    value=canonical_json(envelope),
                ),
                deadline_seconds=self._settings.timeout_seconds,
            )
        except Exception as error:
            raise AccountCenterUnavailable(
                "Account Center lease redemption failed"
            ) from error
        if (
            result.type_url != ACCOUNT_CENTER_RESPONSE_TYPE_URL
            or len(result.value) > MAX_RPC_RESPONSE_BYTES
        ):
            raise AccountCenterUnavailable("Account Center response contract is invalid")
        return _auth_material(_parse_response(result.value))

    def redeem_provider_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | int | None],
    ) -> AuthMaterial:
        """Redeem the exact generic profile/operation/target lease privately."""

        request = _provider_lease_request(payload)
        request_payload = _provider_lease_request_dict(request)
        actor = _actor_context(trusted_context)
        issued_at = int(time.time())
        signed = sign_context(
            method=PROVIDER_LEASE_METHOD,
            payload=request_payload,
            caller_id=self._settings.provider_caller_id,
            issuer=str(actor["issuer"]),
            subject=str(actor["subject"]),
            organization_id=str(actor["organization_id"]),
            workspace_id=actor["workspace_id"],
            binding_id=str(actor["binding_id"]),
            request_id=str(uuid.uuid4()),
            operation_id=PROVIDER_LEASE_METHOD,
            key=self._settings.provider_signing_key,
            issued_at=issued_at,
            expires_at=issued_at + 60,
        )
        envelope = {"payload": request_payload, "trusted_context": signed.as_dict()}
        try:
            result = self._client.invoke(
                capability=ACCOUNT_CENTER_CAPABILITY,
                interface_version=ACCOUNT_CENTER_INTERFACE_VERSION,
                method=PROVIDER_LEASE_METHOD,
                request=DirectPayload(
                    type_url=PROVIDER_LEASE_REQUEST_TYPE_URL,
                    value=canonical_json(envelope),
                ),
                deadline_seconds=self._settings.timeout_seconds,
            )
        except Exception as error:
            raise AccountCenterUnavailable(
                "Account Center profile lease redemption failed"
            ) from error
        if (
            result.type_url != PROVIDER_LEASE_RESPONSE_TYPE_URL
            or len(result.value) > MAX_RPC_RESPONSE_BYTES
        ):
            raise AccountCenterUnavailable("Account Center profile response contract is invalid")
        return _auth_material(_parse_response(result.value))


def require_credential(value: object) -> AuthMaterial:
    """Validate the memory-only SDK response and reject incomplete credentials."""

    if isinstance(value, AuthMaterial):
        credential = value
    elif isinstance(value, Mapping):
        credential = _auth_material(value)
    else:
        raise AccountCenterUnavailable("Account Center returned an invalid credential result")
    if credential.token_type != "Bearer" or not credential.access_token:
        raise AccountCenterUnavailable("Account Center returned an unusable user credential")
    return credential


def _direct_client(settings: ProviderSettings) -> DirectPluginClient:
    """Build a loopback client or an explicitly configured remote mTLS client."""

    if settings.account_center_transport == "local":
        try:
            return DirectPluginClient.for_local_connection_ref(
                settings.account_center_connection_ref
            )
        except ValueError as error:
            raise AccountCenterUnavailable("resolved Account Center ref is not loopback") from error
    if settings.account_center_transport != "mtls":
        raise AccountCenterUnavailable("Account Center transport mode is unsupported")
    connection_ref = settings.account_center_connection_ref
    if not connection_ref.startswith("grpcs://"):
        raise AccountCenterUnavailable("remote Account Center ref must use grpcs")
    target = connection_ref.removeprefix("grpcs://")
    if not _valid_remote_target(target):
        raise AccountCenterUnavailable("remote Account Center ref is invalid")
    root_cert = _secret_bytes(settings, settings.account_center_root_cert_ref)
    client_cert = _secret_bytes(settings, settings.account_center_client_cert_ref)
    client_key = _secret_bytes(settings, settings.account_center_client_key_ref)
    credentials = grpc.ssl_channel_credentials(
        root_certificates=root_cert,
        private_key=client_key,
        certificate_chain=client_cert,
    )
    return DirectPluginClient(grpc.secure_channel(target, credentials), owns_channel=True)


def _valid_remote_target(target: str) -> bool:
    """Reject URI syntax beyond a fixed host:port authority."""

    if any(char in target for char in "/?#@"):
        return False
    match = re.fullmatch(
        r"(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):([0-9]{1,5})", target
    )
    return match is not None and 1 <= int(match.group(1)) <= 65_535


def _secret_bytes(settings: ProviderSettings, reference: str | None) -> bytes:
    """Read certificate material through a stable host-resolved secret reference."""

    if not reference:
        raise AccountCenterUnavailable("remote Account Center mTLS refs are incomplete")
    environment_name = settings.host_secret_names.get(reference)
    if not isinstance(environment_name, str):
        raise AccountCenterUnavailable("remote Account Center mTLS refs are incomplete")
    value = os.environ.get(environment_name, "")
    if not value:
        raise AccountCenterUnavailable("remote Account Center mTLS secret is unavailable")
    return value.encode("utf-8")


def _redeem_request(payload: Mapping[str, object]) -> RedeemCredentialLeaseRequest:
    """Convert one fixed JSON method payload into the public typed DTO."""

    if set(payload) != {"lease_ref", "operation_id", "resource", "arguments_sha256"}:
        raise AccountCenterUnavailable("lease redemption request shape is invalid")
    resource = payload.get("resource")
    if not isinstance(resource, Mapping) or set(resource) != {
        "repository_id",
        "repository",
        "issue_number",
    }:
        raise AccountCenterUnavailable("lease resource is invalid")
    try:
        issue_number = resource["issue_number"]
        if isinstance(issue_number, bool) or not isinstance(issue_number, int):
            raise TypeError("issue_number must be an integer")
        return RedeemCredentialLeaseRequest(
            lease_ref=str(payload["lease_ref"]),
            operation_id=str(payload["operation_id"]),
            resource=Resource(
                repository_id=str(resource["repository_id"]),
                repository=str(resource["repository"]),
                issue_number=issue_number,
            ),
            arguments_sha256=str(payload["arguments_sha256"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AccountCenterUnavailable("lease redemption request is invalid") from error


def _request_dict(request: RedeemCredentialLeaseRequest) -> dict[str, Any]:
    """Serialize public DTO fields into the exact capability JSON shape."""

    return {
        "lease_ref": request.lease_ref,
        "operation_id": request.operation_id,
        "resource": {
            "repository_id": request.resource.repository_id,
            "repository": request.resource.repository,
            "issue_number": request.resource.issue_number,
        },
        "arguments_sha256": request.arguments_sha256,
    }


def _provider_lease_request(
    payload: Mapping[str, object],
) -> RedeemProviderCredentialLeaseRequest:
    """Validate generic lease fields without coercing targets or digests."""

    required = {
        "lease_ref",
        "profile_ref",
        "operation_id",
        "target",
        "arguments_sha256",
    }
    if set(payload) != required:
        raise AccountCenterUnavailable("profile lease redemption request shape is invalid")
    target = payload.get("target")
    if not isinstance(target, Mapping):
        raise AccountCenterUnavailable("profile lease target is invalid")
    try:
        return RedeemProviderCredentialLeaseRequest(
            lease_ref=str(payload["lease_ref"]),
            profile_ref=str(payload["profile_ref"]),
            operation_id=str(payload["operation_id"]),
            target=dict(target),
            arguments_sha256=str(payload["arguments_sha256"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AccountCenterUnavailable("profile lease redemption request is invalid") from error


def _provider_lease_request_dict(
    request: RedeemProviderCredentialLeaseRequest,
) -> dict[str, Any]:
    """Serialize the generic DTO exactly as signed by the Account Center SDK."""

    return {
        "lease_ref": request.lease_ref,
        "profile_ref": request.profile_ref,
        "operation_id": request.operation_id,
        "target": request.target,
        "arguments_sha256": request.arguments_sha256,
    }


def _actor_context(
    context: Mapping[str, str | int | None],
) -> dict[str, str | None]:
    """Retain the verified actor and original provider binding claims."""

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


def _parse_response(data: bytes) -> dict[str, Any]:
    """Parse bounded JSON and reject duplicate response keys."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate response key")
            result[key] = value
        return result

    try:
        decoded = json.loads(data, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AccountCenterUnavailable("Account Center response JSON is invalid") from error
    if not isinstance(decoded, dict):
        raise AccountCenterUnavailable("Account Center response must be an object")
    return decoded


def _auth_material(value: Mapping[str, Any]) -> AuthMaterial:
    """Build the private response DTO and validate its actor/token fields."""

    if set(value) != {"access_token", "token_type", "external_subject", "expires_at"}:
        raise AccountCenterUnavailable("Account Center auth material shape is invalid")
    try:
        token = value["access_token"]
        token_type = value["token_type"]
        subject = value["external_subject"]
        expires_at = value["expires_at"]
    except KeyError as error:
        raise AccountCenterUnavailable("Account Center auth material is incomplete") from error
    if (
        not isinstance(token, str)
        or not token
        or token_type != "Bearer"
        or not isinstance(subject, str)
        or not subject
        or (
            expires_at is not None
            and (isinstance(expires_at, bool) or not isinstance(expires_at, int))
        )
    ):
        raise AccountCenterUnavailable("Account Center auth material is invalid")
    return AuthMaterial(token, token_type, subject, expires_at)
