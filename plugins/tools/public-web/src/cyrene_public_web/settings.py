"""Closed caller, search, artifact, and resource settings for public web tools."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cyrene_plugin_runtime.configuration import read_environment_settings

PLUGIN_ID = "cyrene.tools.public-web"
_REQUIRED_SECRET_REFS = frozenset(
    {
        "public-web/caller-verification-key",
        "public-web/cursor-hmac-key",
        "public-web/state-dir",
        "public-web/artifact-bridge-token",
    }
)
_OPTIONAL_SECRET_REFS = frozenset({"public-web/search-bearer-token"})
_ENV_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_SECRET_REF_RE = re.compile(r"[a-z][a-z0-9._/-]{0,127}\Z")


class ConfigurationError(ValueError):
    """Public web binding settings are missing or outside the fixed policy."""


@dataclass(frozen=True, slots=True)
class TrustedCaller:
    """One signed workload and Workspace scope allowed to invoke this binding."""

    caller_workload: str
    audience: str
    organization_id: str
    workspace_id: str
    issuer: str
    verification_key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class PublicWebSettings:
    """Operator-resolved endpoints and keys; task arguments cannot alter them."""

    binding_id: str
    trusted_callers: tuple[TrustedCaller, ...]
    state_dir: Path
    search_endpoint: str | None
    search_bearer_token: str | None = field(repr=False)
    artifact_bridge_origin: str
    artifact_bridge_token: str = field(repr=False)
    cursor_key: bytes = field(repr=False)
    loopback_test_origins: frozenset[str] = frozenset()
    timeout_seconds: float = 8
    max_page_size: int = 10

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> PublicWebSettings:
        """Load standard activation JSON and resolve only host-owned secrets."""

        source = os.environ if environ is None else environ
        try:
            generic = read_environment_settings() if environ is None else _generic_settings(source)
        except (TypeError, ValueError) as error:
            raise ConfigurationError("public web activation is malformed") from error
        if generic is None:
            raise ConfigurationError("public web activation is required")
        binding_id = generic["binding_id"].strip()
        if not binding_id or len(binding_id) > 256:
            raise ConfigurationError("public web binding ID is invalid")
        try:
            config = json.loads(generic["config"], object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as error:
            raise ConfigurationError("public web configuration is malformed") from error
        if not isinstance(config, Mapping):
            raise ConfigurationError("public web configuration must be an object")
        allowed = {
            "trusted_callers",
            "search_endpoint",
            "search_bearer_token_ref",
            "artifact_bridge_origin",
            "loopback_test_origins",
            "timeout_seconds",
            "max_page_size",
            "hostSecretRefs",
        }
        if set(config) - allowed:
            raise ConfigurationError("public web configuration has unsupported fields")

        names = _parse_secret_names(config.get("hostSecretRefs"))
        search_ref = config.get("search_bearer_token_ref")
        if search_ref is not None and search_ref != "public-web/search-bearer-token":
            raise ConfigurationError("search bearer token reference is invalid")
        expected_refs = set(_REQUIRED_SECRET_REFS)
        if search_ref is not None:
            expected_refs.add(str(search_ref))
        if set(names) != expected_refs:
            raise ConfigurationError("hostSecretRefs do not match configured secrets")

        caller_key = _decode_hmac_key(_secret(source, names, "public-web/caller-verification-key"))
        cursor_key = _decode_hmac_key(_secret(source, names, "public-web/cursor-hmac-key"))
        bridge_token = _secret(source, names, "public-web/artifact-bridge-token")
        if re.fullmatch(r"[0-9a-f]{64}", bridge_token) is None:
            raise ConfigurationError("artifact bridge token must be 32-byte lowercase hex")
        state_dir = _private_state_dir(_secret(source, names, "public-web/state-dir"))

        loopback_origins = _parse_loopback_origins(config.get("loopback_test_origins", []))
        bridge_origin = config.get("artifact_bridge_origin")
        if not isinstance(bridge_origin, str) or not _valid_loopback_origin(bridge_origin):
            raise ConfigurationError("artifact bridge must be a fixed loopback origin")
        search_endpoint = config.get("search_endpoint")
        if search_endpoint is not None and (
            not isinstance(search_endpoint, str)
            or not _valid_endpoint(search_endpoint, loopback_origins)
        ):
            raise ConfigurationError("search endpoint is invalid or uses a private host")
        search_bearer = None
        if search_ref is not None:
            search_bearer = _secret(source, names, str(search_ref))
            if (
                not search_bearer
                or len(search_bearer) > 4096
                or "\r" in search_bearer
                or "\n" in search_bearer
            ):
                raise ConfigurationError("search bearer token is invalid")

        timeout = _bounded_number(config.get("timeout_seconds", 8), 1, 20)
        page_size = config.get("max_page_size", 10)
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 20
        ):
            raise ConfigurationError("max_page_size must be between 1 and 20")
        return cls(
            binding_id=binding_id,
            trusted_callers=_parse_trusted_callers(config.get("trusted_callers"), caller_key),
            state_dir=state_dir,
            search_endpoint=search_endpoint,
            search_bearer_token=search_bearer,
            artifact_bridge_origin=bridge_origin.rstrip("/"),
            artifact_bridge_token=bridge_token,
            cursor_key=cursor_key,
            loopback_test_origins=frozenset(loopback_origins),
            timeout_seconds=timeout,
            max_page_size=page_size,
        )


def _generic_settings(source: Mapping[str, str]) -> dict[str, str] | None:
    binding = source.get("CYRENE_CAPABILITY_BINDING_ID")
    raw = source.get("CYRENE_CAPABILITY_CONFIGURATION_JSON")
    if not binding and raw is None:
        return None
    if not binding:
        raise ConfigurationError("CYRENE_CAPABILITY_BINDING_ID is required")
    raw = raw or "{}"
    if len(raw.encode("utf-8")) > 64 * 1024:
        raise ConfigurationError("public web configuration exceeds its limit")
    return {"binding_id": binding, "config": raw}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _parse_secret_names(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ConfigurationError("hostSecretRefs is required")
    result: dict[str, str] = {}
    for reference, env_name in value.items():
        if (
            not isinstance(reference, str)
            or _SECRET_REF_RE.fullmatch(reference) is None
            or not isinstance(env_name, str)
            or _ENV_RE.fullmatch(env_name) is None
        ):
            raise ConfigurationError("hostSecretRefs contains an invalid entry")
        result[reference] = env_name
    return result


def _parse_trusted_callers(value: object, key: bytes) -> tuple[TrustedCaller, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ConfigurationError("trusted_callers must contain one to 100 scopes")
    required = {
        "caller_workload",
        "audience",
        "organization_id",
        "workspace_id",
        "issuer",
        "verification_key_ref",
    }
    callers: list[TrustedCaller] = []
    identities: set[tuple[str, ...]] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != required:
            raise ConfigurationError("trusted caller scope has unsupported fields")
        if item.get("verification_key_ref") != "public-web/caller-verification-key":
            raise ConfigurationError("trusted caller key reference is invalid")
        values: dict[str, str] = {}
        for name in required - {"verification_key_ref"}:
            entry = item.get(name)
            if not isinstance(entry, str) or not entry or len(entry) > 512:
                raise ConfigurationError("trusted caller identity is invalid")
            values[name] = entry
        identity = tuple(values[name] for name in sorted(values))
        if identity in identities:
            raise ConfigurationError("trusted caller scope is duplicated")
        identities.add(identity)
        callers.append(TrustedCaller(**values, verification_key=key))
    return tuple(callers)


def _secret(source: Mapping[str, str], names: Mapping[str, str], reference: str) -> str:
    value = source.get(names[reference])
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


def _parse_loopback_origins(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 10:
        raise ConfigurationError("loopback_test_origins must be an array of at most 10 origins")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _valid_loopback_origin(item):
            raise ConfigurationError("loopback test origins must be exact HTTP loopback origins")
        origin = item.rstrip("/")
        if origin in result:
            raise ConfigurationError("loopback test origin is duplicated")
        result.append(origin)
    return tuple(result)


def _valid_loopback_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname
    return (
        parsed.scheme == "http"
        and hostname in {"127.0.0.1", "localhost", "::1"}
        and port is not None
        and 1 <= port <= 65535
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _valid_endpoint(value: str, loopback_origins: tuple[str, ...]) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or (port is not None and not 1 <= port <= 65535)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        return False
    origin = f"{parsed.scheme}://{_host_for_origin(parsed.hostname)}"
    if port is not None:
        origin += f":{port}"
    if parsed.scheme == "http":
        return origin in loopback_origins
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return True
    return literal.is_global


def _host_for_origin(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _bounded_number(value: object, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError("timeout must be numeric")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ConfigurationError("timeout is outside its supported range")
    return result
