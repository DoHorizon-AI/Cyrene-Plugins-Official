"""Closed activation settings for one Bilibili connector binding."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cyrene_plugin_runtime.configuration import read_environment_settings

PLUGIN_CALLER_ID = "cyrene.connectors.bilibili"
_SECRET_RE = re.compile(r"[a-z][a-z0-9._/-]{0,127}\Z")
_ENV_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_REQUIRED_SECRET_REFS = frozenset(
    {
        "bilibili/exchange-verification-key",
        "bilibili/account-center-provider-signing-key",
        "bilibili/state-dir",
    }
)
_MTLS_SECRET_REFS = frozenset(
    {
        "bilibili/account-center-root-certificate",
        "bilibili/account-center-client-certificate",
        "bilibili/account-center-client-key",
    }
)


class ConfigurationError(ValueError):
    """The connector activation is incomplete or violates its trust boundary."""


@dataclass(frozen=True, slots=True)
class TrustedCaller:
    """One HMAC caller scope pinned to a workload and workspace."""

    caller_workload: str
    audience: str
    organization_id: str
    workspace_id: str
    issuer: str
    verification_key: bytes


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Operator-owned connector and Account Center settings."""

    binding_id: str
    trusted_callers: tuple[TrustedCaller, ...]
    account_center_binding_id: str
    account_center_connection_ref: str
    account_center_transport: str
    provider_caller_id: str
    provider_signing_key: bytes
    account_center_root_cert_ref: str | None
    account_center_client_cert_ref: str | None
    account_center_client_key_ref: str | None
    state_dir: Path
    host_secret_names: Mapping[str, str]
    timeout_seconds: float = 12.0
    max_page_size: int = 20

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> ProviderSettings:
        """Load standard Plugin activation JSON and resolve host-owned secrets."""

        source = os.environ if environ is None else environ
        try:
            generic = read_environment_settings() if environ is None else _generic_settings(source)
        except (TypeError, ValueError) as error:
            raise ConfigurationError("connector activation is malformed") from error
        if generic is None:
            raise ConfigurationError("connector activation is required")
        binding_id = generic["binding_id"].strip()
        if not binding_id or len(binding_id) > 256:
            raise ConfigurationError("connector binding ID is invalid")
        try:
            config = json.loads(generic["config"], object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as error:
            raise ConfigurationError("connector configuration is malformed") from error
        if not isinstance(config, Mapping):
            raise ConfigurationError("connector configuration must be an object")
        allowed = {
            "trusted_callers",
            "account_center",
            "timeout_seconds",
            "max_page_size",
            "hostSecretRefs",
        }
        if set(config) - allowed:
            raise ConfigurationError("connector configuration has unsupported fields")

        account = config.get("account_center")
        if not isinstance(account, Mapping):
            raise ConfigurationError("Account Center configuration is required")
        if set(account) - {
            "binding_id",
            "connection_ref",
            "transport",
            "caller_id",
            "signing_key_ref",
            "mtls",
        }:
            raise ConfigurationError("Account Center configuration has unsupported fields")
        account_binding = _required_text(account, "binding_id")
        connection_ref = _required_text(account, "connection_ref")
        transport = account.get("transport", "local")
        if transport not in {"local", "mtls"}:
            raise ConfigurationError("Account Center transport is invalid")
        if transport == "local" and not connection_ref.startswith("grpc://"):
            raise ConfigurationError("local Account Center must use a resolved grpc ref")
        if transport == "mtls" and not connection_ref.startswith("grpcs://"):
            raise ConfigurationError("remote Account Center must use a resolved grpcs ref")
        caller_id = _required_text(account, "caller_id")
        if caller_id != PLUGIN_CALLER_ID:
            raise ConfigurationError("Account Center caller ID does not match")
        signing_ref = _required_text(account, "signing_key_ref")
        if signing_ref != "bilibili/account-center-provider-signing-key":
            raise ConfigurationError("Account Center signing key reference is invalid")
        mtls = account.get("mtls", {})
        if not isinstance(mtls, Mapping) or set(mtls) - {
            "root_cert_ref",
            "client_cert_ref",
            "client_key_ref",
        }:
            raise ConfigurationError("Account Center mTLS configuration is invalid")
        if transport == "local" and mtls:
            raise ConfigurationError("local Account Center cannot use mTLS settings")
        root_ref = client_cert_ref = client_key_ref = None
        if transport == "mtls":
            root_ref = _required_text(mtls, "root_cert_ref")
            client_cert_ref = _required_text(mtls, "client_cert_ref")
            client_key_ref = _required_text(mtls, "client_key_ref")
            if {root_ref, client_cert_ref, client_key_ref} != _MTLS_SECRET_REFS:
                raise ConfigurationError("Account Center certificate refs are invalid")

        host_refs = config.get("hostSecretRefs")
        if not isinstance(host_refs, Mapping):
            raise ConfigurationError("hostSecretRefs must be configured")
        names: dict[str, str] = {}
        for reference, env_name in host_refs.items():
            if (
                not isinstance(reference, str)
                or not _SECRET_RE.fullmatch(reference)
                or not isinstance(env_name, str)
                or not _ENV_RE.fullmatch(env_name)
            ):
                raise ConfigurationError("hostSecretRefs has an invalid entry")
            names[reference] = env_name
        expected_refs = set(_REQUIRED_SECRET_REFS)
        if transport == "mtls":
            expected_refs.update(_MTLS_SECRET_REFS)
        if set(names) != expected_refs:
            raise ConfigurationError("hostSecretRefs do not match configured secrets")

        callers = _parse_trusted_callers(config.get("trusted_callers"))
        # Stable secret references resolve to operator-owned environment names.
        exchange_key = _resolve_secret(source, names["bilibili/exchange-verification-key"])
        callers = tuple(
            TrustedCaller(
                caller_workload=caller["caller_workload"],
                audience=caller["audience"],
                organization_id=caller["organization_id"],
                workspace_id=caller["workspace_id"],
                issuer=caller["issuer"],
                verification_key=_decode_hmac_key(exchange_key),
            )
            for caller in callers
        )
        signing_key = _resolve_secret(source, names[signing_ref])
        state_value = _resolve_secret(source, names["bilibili/state-dir"])
        state_dir = _private_state_dir(state_value)
        timeout = _bounded_number(config.get("timeout_seconds", 12), 1, 30)
        page_size = config.get("max_page_size", 20)
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 50
        ):
            raise ConfigurationError("max_page_size must be between 1 and 50")
        return cls(
            binding_id=binding_id,
            trusted_callers=callers,
            account_center_binding_id=account_binding,
            account_center_connection_ref=connection_ref,
            account_center_transport=str(transport),
            provider_caller_id=caller_id,
            provider_signing_key=_decode_hmac_key(signing_key),
            account_center_root_cert_ref=root_ref,
            account_center_client_cert_ref=client_cert_ref,
            account_center_client_key_ref=client_key_ref,
            state_dir=state_dir,
            host_secret_names=names,
            timeout_seconds=timeout,
            max_page_size=page_size,
        )


def _generic_settings(source: Mapping[str, str]) -> dict[str, str] | None:
    """Parse standard activation variables for explicit test environments."""

    binding = source.get("CYRENE_CAPABILITY_BINDING_ID")
    raw = source.get("CYRENE_CAPABILITY_CONFIGURATION_JSON")
    if not binding and raw is None:
        return None
    if not binding:
        raise ConfigurationError("CYRENE_CAPABILITY_BINDING_ID is required")
    raw = raw or "{}"
    if len(raw.encode("utf-8")) > 64 * 1024:
        raise ConfigurationError("connector configuration exceeds its limit")
    return {"binding_id": binding, "config": raw}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _required_text(value: Mapping[str, Any], key: str) -> str:
    text = value.get(key)
    if not isinstance(text, str) or not text.strip() or len(text) > 512 or text != text.strip():
        raise ConfigurationError(f"{key} is invalid")
    return text


def _parse_trusted_callers(value: object) -> tuple[dict[str, str], ...]:
    """Parse closed workload and workspace pins for signed Exchange calls."""

    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ConfigurationError("trusted_callers must contain one to 100 scopes")
    fields = {
        "caller_workload",
        "audience",
        "organization_id",
        "workspace_id",
        "issuer",
        "verification_key_ref",
    }
    parsed: list[dict[str, str]] = []
    identities: set[tuple[str, ...]] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != fields:
            raise ConfigurationError("trusted caller scope has unsupported fields")
        values: dict[str, str] = {}
        for field in fields - {"verification_key_ref"}:
            text = item.get(field)
            if not isinstance(text, str) or not text or len(text) > 512:
                raise ConfigurationError("trusted caller identity is invalid")
            values[field] = text
        if item.get("verification_key_ref") != "bilibili/exchange-verification-key":
            raise ConfigurationError("trusted caller key reference is invalid")
        identity = tuple(values[field] for field in sorted(values))
        if identity in identities:
            raise ConfigurationError("trusted caller scope is duplicated")
        identities.add(identity)
        parsed.append(values)
    return tuple(parsed)


def _resolve_secret(source: Mapping[str, str], env_name: str) -> str:
    value = source.get(env_name)
    if not isinstance(value, str) or not value:
        raise ConfigurationError("a required host secret is unavailable")
    return value


def _decode_hmac_key(value: str) -> bytes:
    try:
        decoded = (
            bytes.fromhex(value)
            if re.fullmatch(r"[0-9a-fA-F]{64}", value)
            else base64.b64decode(value, validate=True)
        )
    except (ValueError, base64.binascii.Error) as error:
        raise ConfigurationError("HMAC key must contain 32 bytes") from error
    if len(decoded) != 32:
        raise ConfigurationError("HMAC key must contain 32 bytes")
    return decoded


def _private_state_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigurationError("state directory must be absolute")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = path.lstat().st_mode
    except OSError as error:
        raise ConfigurationError("state directory is unavailable") from error
    if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) & 0o077:
        raise ConfigurationError("state directory must be private and non-symlinked")
    return path


def _bounded_number(value: Any, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError("timeout must be numeric")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ConfigurationError("timeout is outside its supported range")
    return result
