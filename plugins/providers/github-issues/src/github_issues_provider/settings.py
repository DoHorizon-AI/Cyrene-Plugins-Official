"""Activation settings for one fixed GitHub Issues provider binding.

中文:一个固定 GitHub Issues 提供方绑定的激活设置。
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
    """The provider binding cannot be used safely.

    中文:提供方绑定缺少安全运行所需的配置。
    """


@dataclass(frozen=True, slots=True)
class Repository:
    """One operator-configured repository identity.

    The numeric GitHub repository ID and owner/name slug must agree with the
    API response before the provider returns a resource.
    """

    repository_id: str
    owner: str
    name: str

    @property
    def slug(self) -> str:
        """Return the fixed API path segment for this repository."""

        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Secrets and repository identities injected by the operator."""

    binding_id: str
    account_center_binding_id: str
    account_center_connection_ref: str
    account_center_transport: str
    account_center_root_cert_ref: str | None
    account_center_client_cert_ref: str | None
    account_center_client_key_ref: str | None
    host_secret_names: Mapping[str, str]
    repositories: tuple[Repository, ...]
    provider_caller_id: str
    exchange_caller_id: str
    exchange_verification_key: bytes
    provider_signing_key: bytes
    github_webhook_secret: bytes
    allowed_installation_ids: frozenset[str]
    allowed_events: frozenset[str]
    allowed_actions: frozenset[str]
    timeout_seconds: float = 15.0

    def repository(self, repository_id: str, slug: str) -> Repository:
        """Resolve both configured identity components or reject the request."""

        for repository in self.repositories:
            if repository.repository_id == repository_id and repository.slug == slug:
                return repository
        raise ValueError("repository is not configured for this binding")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ProviderSettings:
        """Read a bounded JSON activation object and separately injected keys."""

        source = os.environ if environ is None else environ
        binding_id = (source.get("CYRENE_CAPABILITY_BINDING_ID") or "").strip()
        config_raw = source.get("CYRENE_CAPABILITY_CONFIGURATION_JSON") or "{}"
        try:
            config = json.loads(config_raw, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as error:
            raise ConfigurationError("provider configuration must be valid JSON") from error
        if not isinstance(config, Mapping):
            raise ConfigurationError("provider configuration must be an object")
        allowed_config = {
            "exchange_caller_id",
            "exchange_verification_key_ref",
            "github_webhook_secret_ref",
            "account_center",
            "repositories",
            "allowed_installation_ids",
            "allowed_events",
            "allowed_actions",
            "timeout_seconds",
            "hostSecretRefs",
        }
        if set(config) - allowed_config:
            raise ConfigurationError("provider configuration contains unsupported fields")
        if not binding_id:
            raise ConfigurationError("CYRENE_CAPABILITY_BINDING_ID is required")

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

        repositories_raw = config.get("repositories")
        if not isinstance(repositories_raw, list) or not repositories_raw:
            raise ConfigurationError("at least one configured repository is required")
        repositories: list[Repository] = []
        seen_ids: set[str] = set()
        for item in repositories_raw:
            if not isinstance(item, Mapping):
                raise ConfigurationError("repository entries must be objects")
            repository_id = _required_text(item.get("repository_id"), "repository_id")
            owner = _required_text(item.get("owner"), "owner")
            name = _required_text(item.get("name"), "name")
            if not repository_id.isdecimal() or repository_id in seen_ids:
                raise ConfigurationError("repository IDs must be unique numeric IDs")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(
                r"[A-Za-z0-9_.-]+", name
            ):
                raise ConfigurationError("repository owner/name is invalid")
            seen_ids.add(repository_id)
            repositories.append(Repository(repository_id, owner, name))

        account_center = config.get("account_center")
        if not isinstance(account_center, Mapping):
            raise ConfigurationError("resolved account_center configuration is required")
        account_center_binding_id = _required_text(
            account_center.get("binding_id"), "account_center.binding_id"
        )
        connection_ref = _required_text(
            account_center.get("connection_ref"), "account_center.connection_ref"
        )
        transport = _required_text(
            account_center.get("transport", "local"), "account_center.transport"
        )
        if transport not in {"local", "mtls"}:
            raise ConfigurationError("account_center.transport must be local or mtls")
        mtls = account_center.get("mtls", {})
        if not isinstance(mtls, Mapping):
            raise ConfigurationError("account_center.mtls must be an object")
        allowed_account_center = {
            "binding_id",
            "connection_ref",
            "transport",
            "caller_id",
            "signing_key_ref",
            "mtls",
        }
        if set(account_center) - allowed_account_center:
            raise ConfigurationError("account_center contains unsupported fields")
        if transport == "local" and mtls:
            raise ConfigurationError("local Account Center transport cannot configure mTLS refs")
        if transport == "local" and connection_ref.startswith("grpcs://"):
            raise ConfigurationError("local Account Center ref must use grpc://")
        if transport == "mtls" and not connection_ref.startswith("grpcs://"):
            raise ConfigurationError("remote Account Center refs must use grpcs://")

        allowed_installations = _text_set(config.get("allowed_installation_ids"))
        allowed_events = _text_set(config.get("allowed_events"))
        allowed_actions = _text_set(config.get("allowed_actions"))
        if not allowed_installations or not allowed_events or not allowed_actions:
            raise ConfigurationError(
                "webhook installation, event, and action allowlists are required"
            )
        secret_references = {
            _required_text(
                config.get("exchange_verification_key_ref"),
                "exchange_verification_key_ref",
            ),
            _required_text(config.get("github_webhook_secret_ref"), "github_webhook_secret_ref"),
            _required_text(account_center.get("signing_key_ref"), "account_center.signing_key_ref"),
        }
        if transport == "mtls":
            secret_references.update(
                {
                    _required_text(
                        mtls.get("root_cert_ref"), "account_center.mtls.root_cert_ref"
                    ),
                    _required_text(
                        mtls.get("client_cert_ref"), "account_center.mtls.client_cert_ref"
                    ),
                    _required_text(
                        mtls.get("client_key_ref"), "account_center.mtls.client_key_ref"
                    ),
                }
            )
        if set(host_secret_names) != secret_references:
            raise ConfigurationError(
                "hostSecretRefs must contain exactly the configured secret refs"
            )
        return cls(
            binding_id=binding_id,
            account_center_binding_id=account_center_binding_id,
            account_center_connection_ref=connection_ref,
            account_center_transport=transport,
            account_center_root_cert_ref=(
                _required_text(mtls.get("root_cert_ref"), "account_center.mtls.root_cert_ref")
                if transport == "mtls"
                else None
            ),
            account_center_client_cert_ref=(
                _required_text(
                    mtls.get("client_cert_ref"), "account_center.mtls.client_cert_ref"
                )
                if transport == "mtls"
                else None
            ),
            account_center_client_key_ref=(
                _required_text(mtls.get("client_key_ref"), "account_center.mtls.client_key_ref")
                if transport == "mtls"
                else None
            ),
            host_secret_names=dict(host_secret_names),
            repositories=tuple(repositories),
            provider_caller_id=_required_text(
                account_center.get("caller_id"), "account_center.caller_id"
            ),
            exchange_caller_id=_required_text(
                config.get("exchange_caller_id"), "exchange_caller_id"
            ),
            exchange_verification_key=_hmac_secret(
                source, config, "exchange_verification_key_ref", host_secret_names
            ),
            provider_signing_key=_hmac_secret(
                source,
                {"provider_signing_key_ref": account_center.get("signing_key_ref")},
                "provider_signing_key_ref",
                host_secret_names,
            ),
            github_webhook_secret=_secret(
                source, config, "github_webhook_secret_ref", host_secret_names
            ),
            allowed_installation_ids=allowed_installations,
            allowed_events=allowed_events,
            allowed_actions=allowed_actions,
            timeout_seconds=_positive_timeout(config.get("timeout_seconds", 15)),
        )


def _required_text(value: Any, field: str) -> str:
    """Return bounded non-empty configuration text."""

    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ConfigurationError(f"{field} must be non-empty bounded text")
    return value.strip()


def _text_set(value: Any) -> frozenset[str]:
    """Validate a non-empty set of bounded allowlist strings."""

    if not isinstance(value, list) or not value:
        return frozenset()
    if any(not isinstance(item, str) or not item or len(item) > 128 for item in value):
        raise ConfigurationError("allowlist entries must be bounded strings")
    return frozenset(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys before activation settings are trusted."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result


def _secret(
    source: Mapping[str, str],
    config: Mapping[str, Any],
    field: str,
    host_secret_names: Mapping[str, str],
) -> bytes:
    """Resolve one stable secret reference through host-provided environment names."""

    reference = _required_text(config.get(field), field)
    environment_name = host_secret_names.get(reference)
    if not isinstance(environment_name, str):
        raise ConfigurationError(f"{field} is missing from hostSecretRefs")
    value = source.get(environment_name, "")
    if not value:
        raise ConfigurationError(f"host secret reference {field} is unavailable")
    secret = value.encode("utf-8")
    if len(secret) < 32:
        raise ConfigurationError(f"host secret reference {field} must contain 32 bytes")
    return secret


def _hmac_secret(
    source: Mapping[str, str],
    config: Mapping[str, Any],
    field: str,
    host_secret_names: Mapping[str, str],
) -> bytes:
    """Decode one 256-bit HMAC key as hex or strict base64, like Account Center."""

    reference = _required_text(config.get(field), field)
    environment_name = host_secret_names.get(reference)
    if not isinstance(environment_name, str):
        raise ConfigurationError(f"{field} is missing from hostSecretRefs")
    encoded = source.get(environment_name, "")
    try:
        key = (
            bytes.fromhex(encoded)
            if len(encoded) == 64
            else base64.b64decode(encoded, validate=True)
        )
    except (ValueError, TypeError):
        raise ConfigurationError(f"configured secret reference {field} is invalid") from None
    if len(key) != 32:
        raise ConfigurationError(f"configured secret reference {field} must decode to 32 bytes")
    return key


def _positive_timeout(value: Any) -> float:
    """Validate the configured request timeout."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 60:
        raise ConfigurationError("timeout_seconds must be greater than zero and at most 60")
    return float(value)
