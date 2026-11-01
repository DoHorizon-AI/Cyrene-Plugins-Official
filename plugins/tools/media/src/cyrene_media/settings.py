"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 settings.py                                                      │
│  Module: cyrene_media.settings                                      │
│  Role: Validate one fixed actor-scoped media binding.                │
│                                                                      │
│  模块职责：校验一个固定 actor 范围的媒体 binding。                   │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cyrene_plugin_runtime.configuration import read_environment_settings

from .artifact_resolver import WorkArtifactResolver

_MAX_CONFIGURATION_BYTES = 64 * 1024
_BINDING_SECRET_REFS = {
    "media-library/caller-verification-key",
    "media-library/state-dir",
    "media-library/artifact-bridge-token",
}
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")


class ConfigurationError(ValueError):
    """The media binding is incomplete or violates fixed trust boundaries."""


@dataclass(frozen=True, slots=True)
class TrustedCaller:
    """One workload and immutable organization/workspace scope for a binding."""

    caller_workload: str
    audience: str
    organization_id: str
    workspace_id: str
    issuer: str
    verification_key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class MediaSettings:
    """Validated binding settings; Work tokens stay inside the Navigator host."""

    binding_id: str
    state_dir: Path
    timeout_seconds: float
    trusted_callers: tuple[TrustedCaller, ...]
    artifact_resolver: WorkArtifactResolver = field(repr=False)

    @classmethod
    def from_environment(
        cls,
    ) -> MediaSettings:
        """Read the standard plugin activation payload and fixed secret refs."""

        source = os.environ
        generic = read_environment_settings()
        if generic is None:
            raise ConfigurationError("media plugin binding is not configured")
        binding_id = generic["binding_id"]
        encoded = generic["config"]
        if len(encoded.encode("utf-8")) > _MAX_CONFIGURATION_BYTES:
            raise ConfigurationError(
                "media plugin configuration exceeds its size limit"
            )
        try:
            config = json.loads(encoded, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as error:
            raise ConfigurationError(
                "media plugin configuration is malformed"
            ) from error
        if not isinstance(config, Mapping):
            raise ConfigurationError("media plugin configuration must be an object")
        allowed = {
            "trusted_callers",
            "artifact_bridge_origin",
            "timeout_seconds",
            "hostSecretRefs",
        }
        if set(config) - allowed:
            raise ConfigurationError(
                "media plugin configuration has unsupported fields"
            )

        callers = _parse_callers(config.get("trusted_callers"))
        secret_names = _parse_secret_refs(config.get("hostSecretRefs"))
        key = _decode_key(
            source.get(secret_names["media-library/caller-verification-key"])
        )
        callers = tuple(
            TrustedCaller(
                caller_workload=caller.caller_workload,
                audience=caller.audience,
                organization_id=caller.organization_id,
                workspace_id=caller.workspace_id,
                issuer=caller.issuer,
                verification_key=key,
            )
            for caller in callers
        )

        state_value = source.get(secret_names["media-library/state-dir"])
        if not isinstance(state_value, str) or not state_value:
            raise ConfigurationError("media state directory is unavailable")
        state_dir = Path(state_value).expanduser()
        if not state_dir.is_absolute():
            raise ConfigurationError("media state directory must be absolute")
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not state_dir.is_dir() or state_dir.stat().st_mode & 0o022:
            raise ConfigurationError("media state directory must be private")

        endpoint = config.get("artifact_bridge_origin")
        if not isinstance(endpoint, str):
            raise ConfigurationError("artifact bridge origin is required")
        token = source.get(secret_names["media-library/artifact-bridge-token"])
        if not isinstance(token, str) or not token:
            raise ConfigurationError("artifact bridge token is unavailable")
        timeout = _bounded_number(config.get("timeout_seconds", 8), 1, 30)
        try:
            resolver = WorkArtifactResolver(
                endpoint=endpoint,
                bridge_token=token,
                timeout_seconds=timeout,
            )
        except ValueError as error:
            raise ConfigurationError("artifact bridge binding is invalid") from error
        return cls(
            binding_id=binding_id,
            state_dir=state_dir,
            timeout_seconds=timeout,
            trusted_callers=callers,
            artifact_resolver=resolver,
        )

    def actor_scope_key(
        self,
        *,
        issuer: str,
        subject: str,
        organization_id: str,
        workspace_id: str,
    ) -> str:
        """Return a private storage namespace for one verified actor scope."""

        material = "\0".join((issuer, subject, organization_id, workspace_id)).encode(
            "utf-8"
        )
        return hashlib.sha256(material).hexdigest()[:32]


def _parse_callers(raw: object) -> tuple[TrustedCaller, ...]:
    """Accept one workload and immutable organization/workspace claims."""

    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise ConfigurationError("one to 100 trusted caller scopes are required")
    parsed: list[TrustedCaller] = []
    identities: set[tuple[str, str, str, str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ConfigurationError("trusted caller must be an object")
        caller = _parse_caller(item)
        identity = (
            caller.caller_workload,
            caller.audience,
            caller.organization_id,
            caller.workspace_id,
            caller.issuer,
        )
        if identity in identities:
            raise ConfigurationError("trusted caller scope is duplicated")
        identities.add(identity)
        parsed.append(caller)
    return tuple(parsed)


def _parse_caller(item: Mapping[str, Any]) -> TrustedCaller:
    """Parse one workload pin for a single organization/workspace."""

    required = {
        "caller_workload",
        "audience",
        "organization_id",
        "workspace_id",
        "issuer",
        "verification_key_ref",
    }
    if set(item) != required:
        raise ConfigurationError("trusted caller fields are invalid")
    values = {key: item.get(key) for key in required - {"verification_key_ref"}}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ConfigurationError("trusted caller identity fields are required")
    reference = item.get("verification_key_ref")
    if reference != "media-library/caller-verification-key":
        raise ConfigurationError(
            "trusted caller must use the fixed verification key ref"
        )
    return TrustedCaller(
        caller_workload=str(item["caller_workload"]),
        audience=str(item["audience"]),
        organization_id=str(item["organization_id"]),
        workspace_id=str(item["workspace_id"]),
        issuer=str(item["issuer"]),
        verification_key=b"",
    )


def _parse_secret_refs(raw: object) -> dict[str, str]:
    """Map the three fixed secret refs to unique host environment variables."""

    if not isinstance(raw, Mapping) or set(raw) != _BINDING_SECRET_REFS:
        raise ConfigurationError(
            "media hostSecretRefs must contain exact required refs"
        )
    result: dict[str, str] = {}
    for reference, environment_name in raw.items():
        if not isinstance(environment_name, str) or not _ENV_NAME.fullmatch(
            environment_name
        ):
            raise ConfigurationError("media host secret env name is invalid")
        result[str(reference)] = environment_name
    if len(set(result.values())) != len(result):
        raise ConfigurationError("media host secret environment names must be unique")
    return result


def _decode_key(value: str | None) -> bytes:
    """Decode the fixed 32-byte verification key from hex or base64."""

    if not isinstance(value, str) or not value:
        raise ConfigurationError("caller verification key is missing")
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ConfigurationError("caller key must be hex or base64") from error
    if len(decoded) != 32:
        raise ConfigurationError(
            "caller verification key must contain exactly 32 bytes"
        )
    return decoded


def _bounded_number(value: object, minimum: float, maximum: float) -> float:
    """Validate a finite timeout before configuring HTTP calls."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= float(value) <= maximum
    ):
        raise ConfigurationError("media timeout is outside the supported range")
    return float(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate keys in untrusted activation JSON."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result
