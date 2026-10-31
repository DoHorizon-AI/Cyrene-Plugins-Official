"""
┌────────────────────────────────────────────────────────────────────┐
│  Closed Microsoft Graph activation settings                          │
│  只读取宿主配置引用，不接收 OAuth 令牌                                │
└────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

PROVIDER_CALLER_ID = "microsoft-graph-provider"
_REQUIRED_HOST_REFS = {
    "microsoft-graph/exchange-verification-key",
    "microsoft-graph/account-center-provider-signing-key",
}
_OPTIONAL_MTLS_REFS = {
    "microsoft-graph/account-center-root-certificate",
    "microsoft-graph/account-center-client-certificate",
    "microsoft-graph/account-center-client-key",
}
_OFFICE_V2_FLAGS = {
    "planner_enabled",
    "todo_enabled",
    "calendar_enabled",
    "mail_metadata_enabled",
    "mail_drafts_enabled",
    "mail_send_enabled",
    "sharepoint_drive_metadata_enabled",
}
_SITE_ID = re.compile(r"[A-Za-z0-9.-]{1,255},[0-9a-fA-F-]{36},[0-9a-fA-F-]{36}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


class ConfigurationError(ValueError):
    """The provider binding lacks a required host-resolved setting."""


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Host-resolved Account Center, caller trust, and bounded Graph settings."""

    binding_id: str
    exchange_caller_id: str
    exchange_verification_key: bytes
    account_center_binding_id: str
    account_center_connection_ref: str
    account_center_transport: str
    provider_caller_id: str
    provider_signing_key: bytes
    account_center_root_cert_ref: str | None
    account_center_client_cert_ref: str | None
    account_center_client_key_ref: str | None
    host_secret_names: Mapping[str, str]
    read_only: bool
    timeout_seconds: float
    max_page_size: int
    sharepoint_site_id: str | None = None
    sharepoint_list_ids: tuple[str, ...] = ()
    office_v2_flags: Mapping[str, bool] | None = None

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> ProviderSettings:
        """Load a closed configuration object and resolve only stable secret refs."""

        source = os.environ if environ is None else environ
        binding_id = (source.get("CYRENE_CAPABILITY_BINDING_ID") or "").strip()
        raw = source.get("CYRENE_CAPABILITY_CONFIGURATION_JSON") or "{}"
        try:
            config = json.loads(raw, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError):
            raise ConfigurationError("provider configuration must be valid JSON") from None
        if not isinstance(config, Mapping) or not binding_id:
            raise ConfigurationError("provider binding configuration is required")
        allowed = {
            "exchange_caller_id",
            "exchange_verification_key_ref",
            "account_center",
            "read_only",
            "timeout_seconds",
            "max_page_size",
            "sharepointTargets",
            "officeV2",
            "hostSecretRefs",
        }
        if set(config) - allowed:
            raise ConfigurationError("provider binding contains unsupported fields")

        host_names = config.get("hostSecretRefs")
        if not isinstance(host_names, Mapping) or not host_names:
            raise ConfigurationError("hostSecretRefs is required")
        names: dict[str, str] = {}
        for reference, environment_name in host_names.items():
            if (
                not isinstance(reference, str)
                or not re.fullmatch(r"[a-z][a-z0-9._/-]{0,127}", reference)
                or not isinstance(environment_name, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", environment_name)
            ):
                raise ConfigurationError("hostSecretRefs contains an invalid entry")
            names[reference] = environment_name

        exchange_caller_id = _required_text(config, "exchange_caller_id")
        exchange_key_ref = _required_text(config, "exchange_verification_key_ref")
        account_center = config.get("account_center")
        if not isinstance(account_center, Mapping):
            raise ConfigurationError("resolved Account Center connection is required")
        allowed_account_center = {
            "binding_id",
            "connection_ref",
            "transport",
            "caller_id",
            "signing_key_ref",
            "mtls",
        }
        if set(account_center) - allowed_account_center:
            raise ConfigurationError("Account Center configuration has unsupported fields")
        account_center_binding = _required_text(account_center, "binding_id")
        connection_ref = _required_text(account_center, "connection_ref")
        transport = account_center.get("transport", "local")
        caller_id = _required_text(account_center, "caller_id")
        signing_key_ref = _required_text(account_center, "signing_key_ref")
        if caller_id != PROVIDER_CALLER_ID:
            raise ConfigurationError("Account Center provider caller ID does not match")
        if transport not in {"local", "mtls"}:
            raise ConfigurationError("Account Center transport must be local or mtls")
        if transport == "local" and not connection_ref.startswith("grpc://"):
            raise ConfigurationError("local Account Center ref must use grpc://")
        if transport == "mtls" and not connection_ref.startswith("grpcs://"):
            raise ConfigurationError("remote Account Center ref must use grpcs://")

        mtls = account_center.get("mtls", {})
        if not isinstance(mtls, Mapping):
            raise ConfigurationError("Account Center mTLS settings must be an object")
        if set(mtls) - {"root_cert_ref", "client_cert_ref", "client_key_ref"}:
            raise ConfigurationError("Account Center mTLS settings have unsupported fields")
        if transport == "local" and mtls:
            raise ConfigurationError("local Account Center transport cannot configure mTLS")
        root_ref: str | None = None
        client_cert_ref: str | None = None
        client_key_ref: str | None = None
        if transport == "mtls":
            root_ref = _required_text(mtls, "root_cert_ref")
            client_cert_ref = _required_text(mtls, "client_cert_ref")
            client_key_ref = _required_text(mtls, "client_key_ref")
            expected_refs = _REQUIRED_HOST_REFS | {
                exchange_key_ref,
                signing_key_ref,
                root_ref,
                client_cert_ref,
                client_key_ref,
            }
        else:
            expected_refs = _REQUIRED_HOST_REFS | {exchange_key_ref, signing_key_ref}
        if set(names) != expected_refs:
            raise ConfigurationError("hostSecretRefs do not match configured secret refs")

        read_only = config.get("read_only", False)
        if not isinstance(read_only, bool):
            raise ConfigurationError("read_only must be boolean")
        timeout = _positive_timeout(config.get("timeout_seconds", 15))
        page_size = config.get("max_page_size", 50)
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
        ):
            raise ConfigurationError("max_page_size must be between 1 and 100")
        sharepoint_site_id, sharepoint_list_ids = _sharepoint_targets(
            config.get("sharepointTargets")
        )
        office_v2_flags = _office_v2_flags(config.get("officeV2"))

        def secret(reference: str) -> str:
            environment_name = names.get(reference)
            if environment_name is None:
                raise ConfigurationError("required host secret reference is missing")
            value = source.get(environment_name)
            if not value:
                raise ConfigurationError("required host secret is unavailable")
            return value

        return cls(
            binding_id=binding_id,
            exchange_caller_id=exchange_caller_id,
            exchange_verification_key=_hmac_secret(secret(exchange_key_ref)),
            account_center_binding_id=account_center_binding,
            account_center_connection_ref=connection_ref,
            account_center_transport=str(transport),
            provider_caller_id=caller_id,
            provider_signing_key=_hmac_secret(secret(signing_key_ref)),
            account_center_root_cert_ref=root_ref,
            account_center_client_cert_ref=client_cert_ref,
            account_center_client_key_ref=client_key_ref,
            host_secret_names=dict(names),
            read_only=read_only,
            timeout_seconds=timeout,
            max_page_size=page_size,
            sharepoint_site_id=sharepoint_site_id,
            sharepoint_list_ids=sharepoint_list_ids,
            office_v2_flags=office_v2_flags,
        )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate object fields while decoding activation JSON."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _required_text(value: Mapping[str, Any], field: str) -> str:
    """Return bounded non-empty configuration text."""

    text = value.get(field)
    if not isinstance(text, str) or not text.strip() or len(text) > 512:
        raise ConfigurationError(f"{field} must be non-empty bounded text")
    return text.strip()


def _sharepoint_targets(value: Any) -> tuple[str | None, tuple[str, ...]]:
    """Validate the optional binding-owned SharePoint target allowlist."""

    if value is None:
        return None, ()
    if not isinstance(value, Mapping) or set(value) != {"siteId", "listIds"}:
        raise ConfigurationError("sharepointTargets must contain one exact site and list set")
    site_id = value.get("siteId")
    list_ids = value.get("listIds")
    if (
        not isinstance(site_id, str)
        or _SITE_ID.fullmatch(site_id) is None
        or not isinstance(list_ids, list)
        or not 1 <= len(list_ids) <= 100
        or any(not isinstance(item, str) or _UUID.fullmatch(item) is None for item in list_ids)
        or len(list_ids) != len(set(list_ids))
    ):
        raise ConfigurationError("sharepointTargets contains an invalid site or list set")
    if any(str(UUID(item)) != item for item in list_ids):
        raise ConfigurationError("sharepointTargets list IDs must be canonical lowercase UUIDs")
    return site_id, tuple(list_ids)


def _office_v2_flags(value: Any) -> dict[str, bool]:
    """Validate the host-owned Office v2 feature switches.

    Account Center profiles further restrict each actor and exact target. This
    activation object only controls which tools this provider may advertise.
    """

    if value is None:
        return {key: False for key in sorted(_OFFICE_V2_FLAGS)}
    if not isinstance(value, Mapping) or set(value) != _OFFICE_V2_FLAGS:
        raise ConfigurationError("officeV2 must contain the exact feature flag set")
    if any(not isinstance(flag, bool) for flag in value.values()):
        raise ConfigurationError("officeV2 feature flags must be boolean")
    return {key: value[key] for key in sorted(_OFFICE_V2_FLAGS)}


def _positive_timeout(value: Any) -> float:
    """Validate a bounded request timeout."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 60:
        raise ConfigurationError("timeout_seconds must be greater than 0 and at most 60")
    return float(value)


def _hmac_secret(value: str) -> bytes:
    """Decode exactly 256 bits from a host-injected hex or Base64 key."""

    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError:
            raise ConfigurationError("service HMAC key is invalid") from None
    if len(decoded) != 32:
        raise ConfigurationError("service HMAC key must contain 32 bytes")
    return decoded
