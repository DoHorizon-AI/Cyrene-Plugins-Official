"""Host activation and source-scope validation for Azure DevOps observations.

The organization, projects, and profile references are resolved by the host.
中文：工具参数只能选择 activation 中明确配置的只读来源。
"""

from __future__ import annotations

import base64
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class ConfigurationError(ValueError):
    """The provider activation configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class AuthorizedSource:
    """One fixed Azure DevOps project source granted to this provider binding."""

    source_id: str
    kind: str
    project_id: str
    project_name: str
    profile_ref: str
    pipeline_id: int | None = None
    definition_id: int | None = None
    definition_environment_id: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Resolved Account Center connection, verification keys, and source grants."""

    binding_id: str
    account_center_binding_id: str
    account_center_connection_ref: str
    account_center_transport: str
    account_center_root_cert_ref: str | None
    account_center_client_cert_ref: str | None
    account_center_client_key_ref: str | None
    host_secret_names: Mapping[str, str]
    organization: str
    tenant_id: str
    sources: tuple[AuthorizedSource, ...]
    provider_caller_id: str
    exchange_caller_id: str
    exchange_verification_key: bytes
    provider_signing_key: bytes
    timeout_seconds: float = 15.0

    def source(self, source_id: str, kind: str) -> AuthorizedSource:
        """Resolve a configured source and require its kind to match exactly."""

        for item in self.sources:
            if item.source_id == source_id and item.kind == kind:
                return item
        raise ValueError("source is not configured for this binding")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ProviderSettings:
        """Load a closed activation object and resolve host-injected secret refs."""

        source = os.environ if environ is None else environ
        binding_id = (source.get("CYRENE_CAPABILITY_BINDING_ID") or "").strip()
        config_raw = source.get("CYRENE_CAPABILITY_CONFIGURATION_JSON") or "{}"
        try:
            config = json.loads(config_raw, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as error:
            raise ConfigurationError("provider configuration must be valid JSON") from error
        if not isinstance(config, Mapping):
            raise ConfigurationError("provider configuration must be an object")
        allowed = {
            "exchange_caller_id",
            "exchange_verification_key_ref",
            "account_center",
            "organization",
            "tenant_id",
            "authorized_sources",
            "timeout_seconds",
            "hostSecretRefs",
        }
        if set(config) - allowed or not binding_id:
            raise ConfigurationError("provider binding contains unsupported fields")

        host_secret_names = config.get("hostSecretRefs")
        if not isinstance(host_secret_names, Mapping) or not host_secret_names:
            raise ConfigurationError("hostSecretRefs must map refs to environment variables")
        for reference, environment_name in host_secret_names.items():
            if (
                not isinstance(reference, str)
                or not re.fullmatch(r"[a-z][a-z0-9._/-]{0,127}", reference)
                or not isinstance(environment_name, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", environment_name)
            ):
                raise ConfigurationError("hostSecretRefs contains an invalid reference")

        def secret(reference: Any) -> str:
            if not isinstance(reference, str) or reference not in host_secret_names:
                raise ConfigurationError("required host secret reference is missing")
            value = source.get(str(host_secret_names[reference]))
            if not value:
                raise ConfigurationError("required host secret is unavailable")
            return value

        exchange_caller_id = _required_text(config, "exchange_caller_id")
        exchange_key_ref = _required_text(config, "exchange_verification_key_ref")
        organization = _required_text(config, "organization")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", organization):
            raise ConfigurationError("Azure DevOps organization slug is invalid")
        tenant_id = _required_text(config, "tenant_id")
        try:
            if str(uuid.UUID(tenant_id)) != tenant_id.lower():
                raise ValueError
            tenant_id = tenant_id.lower()
        except ValueError as error:
            raise ConfigurationError("tenant_id must be a UUID") from error

        account_center = config.get("account_center")
        if not isinstance(account_center, Mapping):
            raise ConfigurationError("resolved Account Center connection is required")
        if set(account_center) - {
            "binding_id",
            "connection_ref",
            "transport",
            "caller_id",
            "signing_key_ref",
            "mtls",
        }:
            raise ConfigurationError("Account Center configuration contains unsupported fields")
        account_center_binding = _required_text(account_center, "binding_id")
        connection_ref = _required_text(account_center, "connection_ref")
        transport = str(account_center.get("transport", "local"))
        if transport not in {"local", "mtls"}:
            raise ConfigurationError("Account Center transport is invalid")
        if transport == "local" and not connection_ref.startswith("grpc://"):
            raise ConfigurationError("local Account Center refs must use grpc://")
        if transport == "mtls" and not connection_ref.startswith("grpcs://"):
            raise ConfigurationError("remote Account Center refs must use grpcs://")
        provider_caller_id = _required_text(account_center, "caller_id")
        provider_key_ref = _required_text(account_center, "signing_key_ref")
        mtls = account_center.get("mtls", {})
        if not isinstance(mtls, Mapping):
            raise ConfigurationError("Account Center mTLS configuration is invalid")
        if transport == "local" and mtls:
            raise ConfigurationError("local Account Center refs cannot configure mTLS")
        required_refs = {exchange_key_ref, provider_key_ref}
        if transport == "mtls":
            required_refs.update(
                {
                    _required_text(mtls, "root_cert_ref"),
                    _required_text(mtls, "client_cert_ref"),
                    _required_text(mtls, "client_key_ref"),
                }
            )
        if set(host_secret_names) != required_refs:
            raise ConfigurationError("hostSecretRefs must match configured secret refs")

        timeout = config.get("timeout_seconds", 15)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (float, int))
            or not 0 < timeout <= 60
        ):
            raise ConfigurationError("timeout_seconds must be between 0 and 60")

        return cls(
            binding_id=binding_id,
            account_center_binding_id=account_center_binding,
            account_center_connection_ref=connection_ref,
            account_center_transport=transport,
            account_center_root_cert_ref=(
                str(mtls["root_cert_ref"]) if transport == "mtls" else None
            ),
            account_center_client_cert_ref=(
                str(mtls["client_cert_ref"]) if transport == "mtls" else None
            ),
            account_center_client_key_ref=(
                str(mtls["client_key_ref"]) if transport == "mtls" else None
            ),
            host_secret_names=dict(host_secret_names),
            organization=organization,
            tenant_id=tenant_id,
            sources=_authorized_sources(config.get("authorized_sources")),
            provider_caller_id=provider_caller_id,
            exchange_caller_id=exchange_caller_id,
            exchange_verification_key=_hmac_secret(secret(exchange_key_ref)),
            provider_signing_key=_hmac_secret(secret(provider_key_ref)),
            timeout_seconds=float(timeout),
        )


def _authorized_sources(value: Any) -> tuple[AuthorizedSource, ...]:
    """Validate and normalize project and optional resource identifiers."""

    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ConfigurationError("authorized_sources must contain 1 to 100 sources")
    result: list[AuthorizedSource] = []
    seen: set[str] = set()
    seen_profiles: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ConfigurationError("authorized source must be an object")
        kind = item.get("kind")
        if not isinstance(kind, str):
            raise ConfigurationError("authorized source kind is invalid")
        expected = {
            "project_pipelines": {"source_id", "kind", "project_id", "project_name", "profile_ref"},
            "pipeline_runs": {
                "source_id",
                "kind",
                "project_id",
                "project_name",
                "profile_ref",
                "pipeline_id",
            },
            "release_deployments": {
                "source_id",
                "kind",
                "project_id",
                "project_name",
                "profile_ref",
                "definition_id",
                "definition_environment_id",
            },
        }.get(kind)
        if expected is None or set(item) - expected:
            raise ConfigurationError("authorized source contains unsupported fields")
        if kind in {"project_pipelines", "pipeline_runs"} and not expected.issubset(set(item)):
            raise ConfigurationError("pipeline source is missing a required field")
        if kind == "release_deployments" and not {
            "source_id",
            "kind",
            "project_id",
            "project_name",
            "profile_ref",
        }.issubset(set(item)):
            raise ConfigurationError("deployment source is missing a required field")
        source_id = _required_item_text(item, "source_id", 128)
        project_id = _required_item_text(item, "project_id", 36)
        project_name = _required_item_text(item, "project_name", 128)
        profile_ref = _required_item_text(item, "profile_ref", 36)
        try:
            if str(uuid.UUID(project_id)) != project_id.lower():
                raise ValueError
            project_id = project_id.lower()
            if str(uuid.UUID(profile_ref)) != profile_ref.lower():
                raise ValueError
            profile_ref = profile_ref.lower()
        except ValueError as error:
            raise ConfigurationError("project_id and profile_ref must be UUIDs") from error
        if any(ord(char) < 32 for char in project_name):
            raise ConfigurationError("project_name is invalid")
        if source_id in seen:
            raise ConfigurationError("authorized source IDs must be unique")
        seen.add(source_id)
        if profile_ref in seen_profiles:
            raise ConfigurationError("each authorized source requires its own profile_ref")
        seen_profiles.add(profile_ref)

        pipeline_id = _positive_id(item.get("pipeline_id")) if kind == "pipeline_runs" else None
        definition_id = None
        environment_id = None
        if kind == "release_deployments":
            definition_id = _optional_positive_id(item.get("definition_id"))
            environment_id = _optional_positive_id(item.get("definition_environment_id"))
            if environment_id is not None and definition_id is None:
                raise ConfigurationError("definition_environment_id requires definition_id")
        result.append(
            AuthorizedSource(
                source_id=source_id,
                kind=str(kind),
                project_id=project_id,
                project_name=project_name,
                profile_ref=profile_ref,
                pipeline_id=pipeline_id,
                definition_id=definition_id,
                definition_environment_id=environment_id,
            )
        )
    return tuple(result)


def _required_text(value: Mapping[str, Any], key: str) -> str:
    """Return a nonempty configuration string."""

    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ConfigurationError(f"{key} is required")
    return item.strip()


def _required_item_text(value: Mapping[str, Any], key: str, maximum: int) -> str:
    """Return bounded nonempty text from a source record."""

    item = value.get(key)
    if not isinstance(item, str) or not 1 <= len(item) <= maximum or item != item.strip():
        raise ConfigurationError(f"authorized source {key} is invalid")
    return item


def _positive_id(value: Any) -> int:
    """Parse a positive bounded Azure DevOps resource identifier."""

    result = _optional_positive_id(value)
    if result is None:
        raise ConfigurationError("resource identifier must be a positive integer")
    return result


def _optional_positive_id(value: Any) -> int | None:
    """Parse an optional positive integer without accepting booleans."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_147_483_647:
        raise ConfigurationError("resource identifier must be a positive 32-bit integer")
    return value


def _hmac_secret(value: str) -> bytes:
    """Decode a 32-byte HMAC key supplied through hostSecretRefs."""

    try:
        if re.fullmatch(r"[0-9a-fA-F]{64}", value):
            decoded = bytes.fromhex(value)
        else:
            decoded = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise ConfigurationError("service HMAC key must be 32 bytes") from error
    if len(decoded) != 32:
        raise ConfigurationError("service HMAC key must be 32 bytes")
    return decoded


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON members before validating activation settings."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration field")
        result[key] = value
    return result
