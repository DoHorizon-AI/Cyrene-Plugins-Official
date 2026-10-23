"""Activation settings for one allowlisted Hugging Face Hub provider.

The provider receives credentials only through Account Center leases. 中文：令牌不进入插件配置。
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class ConfigurationError(ValueError):
    """The binding lacks a required host-resolved setting."""


@dataclass(frozen=True, slots=True)
class RepositoryScope:
    """One Hub repository and the operations allowed for its type and ID."""

    repo_type: str
    repo_id: str
    permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Verified connection refs, service keys, and repository allowlists."""

    binding_id: str
    account_center_binding_id: str
    account_center_connection_ref: str
    account_center_transport: str
    account_center_root_cert_ref: str | None
    account_center_client_cert_ref: str | None
    account_center_client_key_ref: str | None
    host_secret_names: Mapping[str, str]
    repositories: tuple[RepositoryScope, ...]
    provider_caller_id: str
    exchange_caller_id: str
    exchange_verification_key: bytes
    provider_signing_key: bytes
    timeout_seconds: float = 15.0
    max_artifact_bytes: int = 1_048_576

    def repository(self, repo_type: str, repo_id: str, permission: str) -> RepositoryScope:
        """Resolve an exact configured Hub repository and permission."""

        for item in self.repositories:
            if (
                item.repo_type == repo_type
                and item.repo_id == repo_id
                and permission in item.permissions
            ):
                return item
        raise ValueError("repository is not configured for this binding")

    def repositories_for_type(self, repo_type: str) -> tuple[RepositoryScope, ...]:
        """Return configured targets for one Hub repository type."""

        return tuple(item for item in self.repositories if item.repo_type == repo_type)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ProviderSettings:
        """Load a closed activation object and resolve stable host secret refs."""

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
            "repositories",
            "timeout_seconds",
            "max_artifact_bytes",
            "hostSecretRefs",
        }
        if set(config) - allowed or not binding_id:
            raise ConfigurationError("provider binding contains unsupported fields")
        host_secret_names = config.get("hostSecretRefs")
        if not isinstance(host_secret_names, Mapping) or not host_secret_names:
            raise ConfigurationError("hostSecretRefs must map stable refs to environment names")
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
            raise ConfigurationError("hostSecretRefs must match the configured secret refs")
        repositories = _repository_scopes(config.get("repositories"))
        timeout = _positive_timeout(config.get("timeout_seconds", 15))
        maximum = config.get("max_artifact_bytes", 1_048_576)
        if (
            isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 1 <= maximum <= 1_048_576
        ):
            raise ConfigurationError("max_artifact_bytes must be between 1 and 1048576")
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
            repositories=repositories,
            provider_caller_id=provider_caller_id,
            exchange_caller_id=exchange_caller_id,
            exchange_verification_key=_hmac_secret(secret(exchange_key_ref)),
            provider_signing_key=_hmac_secret(secret(provider_key_ref)),
            timeout_seconds=timeout,
            max_artifact_bytes=maximum,
        )


def _repository_scopes(value: Any) -> tuple[RepositoryScope, ...]:
    """Validate a bounded static list of Hub resource scopes."""

    if not isinstance(value, list) or not value or len(value) > 100:
        raise ConfigurationError("at least one configured Hub repository is required")
    result: list[RepositoryScope] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"repo_type", "repo_id", "permissions"}:
            raise ConfigurationError("Hub repository scope shape is invalid")
        repo_type = item["repo_type"]
        repo_id = item["repo_id"]
        permissions = item["permissions"]
        if (
            repo_type not in {"model", "dataset", "space"}
            or not isinstance(repo_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo_id)
            or not isinstance(permissions, list)
            or not permissions
            or set(permissions) - {"read", "write"}
            or len(permissions) != len(set(permissions))
            or (repo_type, repo_id) in seen
        ):
            raise ConfigurationError("Hub repository scope is invalid")
        seen.add((repo_type, repo_id))
        result.append(RepositoryScope(repo_type, repo_id, frozenset(permissions)))
    return tuple(result)


def _required_text(value: Mapping[str, Any], field: str) -> str:
    """Return bounded non-empty configuration text."""

    text = value.get(field)
    if not isinstance(text, str) or not text.strip() or len(text) > 512:
        raise ConfigurationError(f"{field} must be non-empty bounded text")
    return text.strip()


def _hmac_secret(value: str) -> bytes:
    """Decode exactly 256 bits from host-injected hex or Base64."""

    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError as error:
            raise ConfigurationError("service HMAC key is invalid") from error
    if len(decoded) != 32:
        raise ConfigurationError("service HMAC key must be exactly 32 bytes")
    return decoded


def _positive_timeout(value: Any) -> float:
    """Validate the bounded upstream timeout."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 60:
        raise ConfigurationError("timeout_seconds must be greater than 0 and at most 60")
    return float(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys in activation settings."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result
