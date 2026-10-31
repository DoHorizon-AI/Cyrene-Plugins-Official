"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud_management_provider.settings                         │
│  Role: Validate profiles, scopes, callers, and host credential refs.│
│                                                                      │
│  模块职责：校验 profile、资源范围、调用方和主机凭据引用。             │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CONFIG_LIMIT = 96 * 1024
_MAX_PROFILES = 100
_EXCHANGE_KEY_REF = "cloud-management/exchange-verification-key"
_STATE_DIR_REF = "cloud-management/state-dir"
_GCP_WIF_REF = "cloud-management/gcp-wif-credential-config"
_RUNPOD_KEY_REF = "cloud-management/runpod-api-key"
_SECRET_REF = re.compile(r"[a-z][a-z0-9._/-]{0,127}\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_PROFILE_REF = re.compile(r"[A-Za-z0-9._/-]{1,192}\Z")
_RESOURCE_GROUP = re.compile(r"[A-Za-z0-9_.() -]{1,90}\Z")
_API_VERSION = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}(?:-preview)?\Z")
_GCP_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")
_GCP_ZONE = re.compile(r"[a-z]+-[a-z0-9]+-[a-z]\Z")
_GCP_NAME_PREFIX = re.compile(r"[a-z]([-a-z0-9]{0,60})?\Z")
_GCP_NAME_PREFIX_WITH_DASH = re.compile(r"[a-z][-a-z0-9]{0,61}\Z")
_GCP_SOURCE_IMAGE = re.compile(
    r"projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/global/images/"
    r"(?:family/[a-z][a-z0-9-]{0,62}|[a-z][a-z0-9-]{0,62})\Z"
)
_RUNPOD_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_RUNPOD_NAME_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
_RUNPOD_SCOPE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:-]{0,127}\Z")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


class ConfigurationError(ValueError):
    """The provider activation configuration is not safe to use."""


class CredentialMissingError(ValueError):
    """A host-injected stable credential reference has no value."""


class ProfileNotFoundError(ValueError):
    """A profile reference is not present in this provider binding."""


@dataclass(frozen=True, slots=True)
class TrustedCaller:
    """One Exchange workload and workspace accepted by this binding."""

    caller_workload: str
    audience: str
    organization_id: str
    workspace_id: str | None
    issuer: str | None
    verification_key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class CloudProfile:
    """One immutable provider identity and target scope resolved by profile_ref."""

    profile_ref: str
    provider: str
    identity_ref: str
    scope: Mapping[str, Any]
    auth_mode: str
    client_id: str | None
    credential_config_ref: str | None
    api_key_ref: str | None
    cli_executable: str | None
    cli_timeout_seconds: float
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Validated activation data; provider secrets remain in host environment."""

    binding_id: str
    authorization_generation: int
    trusted_callers: Mapping[str, TrustedCaller]
    profiles: Mapping[str, CloudProfile]
    host_secret_names: Mapping[str, str]
    state_dir: Path
    timeout_seconds: float
    ledger_max_rows: int
    ledger_max_bytes: int
    ledger_terminal_retention_seconds: int
    environ: Mapping[str, str] = field(repr=False, compare=False)

    def profile(self, profile_ref: str) -> CloudProfile:
        """Resolve one opaque profile ref from this binding's allowlist."""

        profile = self.profiles.get(profile_ref)
        if profile is None:
            raise ProfileNotFoundError("profile_ref is not configured for this binding")
        return profile

    def secret_value(self, reference: str) -> str | None:
        """Return one host-injected value by stable reference without logging it."""

        name = self.host_secret_names.get(reference)
        if name is None:
            return None
        value = self.environ.get(name)
        return value if isinstance(value, str) and value else None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ProviderSettings:
        """Parse and validate bounded activation JSON plus host-only secret values."""

        source = os.environ if environ is None else environ
        binding_id = _required_text(source.get("CYRENE_CAPABILITY_BINDING_ID"), "binding ID", 256)
        config_raw = source.get("CYRENE_CAPABILITY_CONFIGURATION_JSON") or "{}"
        if len(config_raw.encode("utf-8")) > _CONFIG_LIMIT:
            raise ConfigurationError("activation configuration exceeds its size limit")
        try:
            config = json.loads(config_raw, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as error:
            raise ConfigurationError("activation configuration must be valid JSON") from error
        if not isinstance(config, Mapping):
            raise ConfigurationError("activation configuration must be an object")
        allowed = {
            "authorization_generation",
            "trusted_callers",
            "profiles",
            "timeout_seconds",
            "hostSecretRefs",
        }
        if set(config) - allowed:
            raise ConfigurationError("activation contains unsupported fields")

        generation = config.get("authorization_generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ConfigurationError("authorization_generation must be a nonnegative integer")
        callers, used_refs = _parse_callers(config.get("trusted_callers"))
        profiles, profile_refs = _parse_profiles(config.get("profiles"))
        used_refs.update(profile_refs)
        used_refs.add(_STATE_DIR_REF)
        secret_names = _parse_host_secret_names(config.get("hostSecretRefs"), used_refs)
        _resolve_caller_keys(callers, secret_names, source)

        state_value = source.get(secret_names[_STATE_DIR_REF])
        if not isinstance(state_value, str) or not state_value:
            raise CredentialMissingError("cloud management state directory is unavailable")
        state_dir = Path(state_value)
        if not state_dir.is_absolute():
            raise ConfigurationError("cloud management state directory must be absolute")
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not state_dir.is_dir():
            raise ConfigurationError("cloud management state directory is not a directory")
        if state_dir.stat().st_mode & 0o022:
            raise ConfigurationError(
                "cloud management state directory must not be group/world writable"
            )

        timeout = _bounded_number(config.get("timeout_seconds", 12), "timeout_seconds", 1, 30)
        ledger_max_rows = _bounded_env_int(
            source,
            "CYRENE_CLOUD_MANAGEMENT_LEDGER_MAX_ROWS",
            default=8192,
            minimum=1,
            maximum=100_000,
        )
        ledger_max_bytes = _bounded_env_int(
            source,
            "CYRENE_CLOUD_MANAGEMENT_LEDGER_MAX_BYTES",
            default=128 * 1024 * 1024,
            minimum=1024 * 1024,
            maximum=512 * 1024 * 1024,
        )
        terminal_retention_days = _bounded_env_int(
            source,
            "CYRENE_CLOUD_MANAGEMENT_LEDGER_TERMINAL_RETENTION_DAYS",
            default=90,
            minimum=31,
            maximum=3650,
        )
        return cls(
            binding_id=binding_id,
            authorization_generation=generation,
            trusted_callers=callers,
            profiles=profiles,
            host_secret_names=secret_names,
            state_dir=state_dir,
            timeout_seconds=timeout,
            ledger_max_rows=ledger_max_rows,
            ledger_max_bytes=ledger_max_bytes,
            ledger_terminal_retention_seconds=terminal_retention_days * 24 * 60 * 60,
            environ=source,
        )


def _parse_callers(raw: object) -> tuple[dict[str, TrustedCaller], set[str]]:
    """Load one fixed Exchange workload identity and its key reference."""

    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], Mapping):
        raise ConfigurationError("exactly one trusted caller must be configured")
    item = raw[0]
    allowed = {
        "caller_workload",
        "audience",
        "organization_id",
        "workspace_id",
        "issuer",
        "verification_key_ref",
    }
    if set(item) - allowed:
        raise ConfigurationError("trusted caller contains unsupported fields")
    workload = _required_text(item.get("caller_workload"), "caller_workload", 256)
    audience = _required_text(item.get("audience"), "audience", 256)
    if audience != "cloud-management":
        raise ConfigurationError("trusted caller audience must be cloud-management")
    organization = _required_text(item.get("organization_id"), "organization_id", 256)
    workspace = _required_text(item.get("workspace_id"), "workspace_id", 256)
    issuer = _required_text(item.get("issuer"), "issuer", 512)
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,256}", workload):
        raise ConfigurationError("caller workload contains unsupported characters")
    reference = _required_text(item.get("verification_key_ref"), "verification_key_ref", 128)
    if reference != _EXCHANGE_KEY_REF:
        raise ConfigurationError("caller must use the fixed Exchange verification ref")
    caller = TrustedCaller(workload, audience, organization, workspace, issuer, b"")
    return {workload: caller}, {reference}


def _parse_profiles(raw: object) -> tuple[dict[str, CloudProfile], set[str]]:
    """Parse provider-specific bounded identity and resource scopes."""

    if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_PROFILES:
        raise ConfigurationError("profiles must contain between 1 and 100 entries")
    profiles: dict[str, CloudProfile] = {}
    refs: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) - {
            "profile_ref",
            "provider",
            "identity_ref",
            "scope",
            "auth",
            "cli",
            "read_only",
        }:
            raise ConfigurationError("profile has an invalid shape")
        profile_ref = _required_text(item.get("profile_ref"), "profile_ref", 192)
        if not _PROFILE_REF.fullmatch(profile_ref) or profile_ref in profiles:
            raise ConfigurationError("profile_ref is invalid or duplicated")
        provider = item.get("provider")
        if provider not in {"azure", "gcp", "runpod"}:
            raise ConfigurationError("profile provider must be azure, gcp, or runpod")
        identity_ref = _required_text(item.get("identity_ref"), "identity_ref", 256)
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,256}", identity_ref):
            raise ConfigurationError("identity_ref contains unsupported characters")
        read_only = item.get("read_only", False)
        if not isinstance(read_only, bool):
            raise ConfigurationError("profile read_only must be boolean")
        scope = _parse_scope(provider, item.get("scope"))
        auth_mode, client_id, credential_ref, api_key_ref = _parse_auth(provider, item.get("auth"))
        if credential_ref is not None:
            refs.add(credential_ref)
        if api_key_ref is not None:
            refs.add(api_key_ref)
        cli_executable, cli_timeout = _parse_cli(provider, item.get("cli"))
        profiles[profile_ref] = CloudProfile(
            profile_ref=profile_ref,
            provider=provider,
            identity_ref=identity_ref,
            scope=scope,
            auth_mode=auth_mode,
            read_only=read_only,
            client_id=client_id,
            credential_config_ref=credential_ref,
            api_key_ref=api_key_ref,
            cli_executable=cli_executable,
            cli_timeout_seconds=cli_timeout,
        )
    return profiles, refs


def _parse_scope(provider: str, raw: object) -> Mapping[str, Any]:
    """Validate the exact identifiers that every provider request may address."""

    if not isinstance(raw, Mapping):
        raise ConfigurationError("profile scope must be an object")
    if provider == "azure":
        allowed = {
            "subscription_id",
            "resource_groups",
            "resource_api_versions",
            "vm_api_version",
            "deployment_api_version",
        }
        if set(raw) - allowed:
            raise ConfigurationError("Azure scope has unsupported fields")
        subscription_id = _required_text(raw.get("subscription_id"), "subscription_id", 36)
        if not _UUID.fullmatch(subscription_id):
            raise ConfigurationError("Azure subscription_id must be a UUID")
        groups = _text_list(raw.get("resource_groups"), "resource_groups", 1, 100)
        if any(not _RESOURCE_GROUP.fullmatch(value) for value in groups):
            raise ConfigurationError("Azure resource group name is invalid")
        versions_raw = raw.get("resource_api_versions", {})
        if not isinstance(versions_raw, Mapping) or len(versions_raw) > 100:
            raise ConfigurationError("Azure resource_api_versions must be an object")
        versions: dict[str, str] = {}
        for resource_type, version in versions_raw.items():
            if not isinstance(resource_type, str) or not re.fullmatch(
                r"[A-Za-z0-9.]+/[A-Za-z0-9./]+", resource_type
            ):
                raise ConfigurationError("Azure resource type key is invalid")
            if not isinstance(version, str) or not _API_VERSION.fullmatch(version):
                raise ConfigurationError("Azure resource API version is invalid")
            versions[resource_type.casefold()] = version
        vm_version = _optional_api_version(
            raw.get("vm_api_version", "2024-07-01"), "vm_api_version"
        )
        deployment_version = _optional_api_version(
            raw.get("deployment_api_version", "2025-04-01"), "deployment_api_version"
        )
        return {
            "subscription_id": subscription_id,
            "resource_groups": tuple(groups),
            "resource_api_versions": versions,
            "vm_api_version": vm_version,
            "deployment_api_version": deployment_version,
        }
    if provider == "gcp":
        allowed = {
            "project_id",
            "zones",
            "instance_name_prefixes",
            "disk_name_prefixes",
            "disk_size_gb_range",
            "disk_source_images",
        }
        if set(raw) - allowed:
            raise ConfigurationError("GCP scope has unsupported fields")
        project_id = _required_text(raw.get("project_id"), "project_id", 30)
        if not _GCP_PROJECT.fullmatch(project_id):
            raise ConfigurationError("GCP project_id is invalid")
        zones = _text_list(raw.get("zones"), "zones", 1, 100)
        prefixes = _text_list(raw.get("instance_name_prefixes"), "instance_name_prefixes", 1, 100)
        disk_prefixes = _text_list(raw.get("disk_name_prefixes", []), "disk_name_prefixes", 0, 100)
        if any(not _GCP_ZONE.fullmatch(zone) for zone in zones):
            raise ConfigurationError("GCP zone is invalid")
        if any(
            not (_GCP_NAME_PREFIX.fullmatch(prefix) or _GCP_NAME_PREFIX_WITH_DASH.fullmatch(prefix))
            for prefix in prefixes
        ):
            raise ConfigurationError("GCP instance name prefix is invalid")
        if any(
            not (_GCP_NAME_PREFIX.fullmatch(prefix) or _GCP_NAME_PREFIX_WITH_DASH.fullmatch(prefix))
            for prefix in disk_prefixes
        ):
            raise ConfigurationError("GCP disk name prefix is invalid")
        size_range_raw = raw.get("disk_size_gb_range")
        size_range: Mapping[str, int] | None = None
        if size_range_raw is not None:
            if not isinstance(size_range_raw, Mapping) or set(size_range_raw) != {"min", "max"}:
                raise ConfigurationError("GCP disk_size_gb_range must contain min and max")
            minimum = _bounded_int(size_range_raw.get("min"), "disk_size_gb_range.min", 10, 1024)
            maximum = _bounded_int(size_range_raw.get("max"), "disk_size_gb_range.max", 10, 1024)
            if minimum > maximum:
                raise ConfigurationError("GCP disk size range minimum exceeds maximum")
            size_range = {"min": minimum, "max": maximum}
        source_images = _text_list(raw.get("disk_source_images", []), "disk_source_images", 0, 100)
        if any(not _GCP_SOURCE_IMAGE.fullmatch(image) for image in source_images):
            raise ConfigurationError("GCP disk source image must be an exact project image path")
        return {
            "project_id": project_id,
            "zones": tuple(zones),
            "instance_name_prefixes": tuple(prefixes),
            "disk_name_prefixes": tuple(disk_prefixes),
            "disk_size_gb_range": size_range,
            "disk_source_images": tuple(source_images),
        }
    allowed = {
        "pod_ids",
        "pod_id_prefixes",
        "pod_name_prefixes",
        "gpu_type_ids",
        "data_center_ids",
        "max_gpu_count",
        "max_container_disk_gb",
        "max_volume_disk_gb",
    }
    if set(raw) - allowed:
        raise ConfigurationError("Runpod scope has unsupported fields")
    pod_ids = _text_list(raw.get("pod_ids", []), "pod_ids", 0, 500)
    id_prefixes = _text_list(raw.get("pod_id_prefixes", []), "pod_id_prefixes", 0, 50)
    name_prefixes = _text_list(raw.get("pod_name_prefixes"), "pod_name_prefixes", 1, 50)
    gpu_ids = _text_list(raw.get("gpu_type_ids"), "gpu_type_ids", 1, 100)
    data_centers = _text_list(raw.get("data_center_ids"), "data_center_ids", 1, 100)
    if not pod_ids and not id_prefixes:
        raise ConfigurationError("Runpod scope needs pod_ids or pod_id_prefixes")
    if any(not _RUNPOD_ID.fullmatch(value) for value in (*pod_ids, *id_prefixes)):
        raise ConfigurationError("Runpod Pod ID scope is invalid")
    if any(not _RUNPOD_NAME_PREFIX.fullmatch(value) for value in name_prefixes):
        raise ConfigurationError("Runpod Pod name prefix is invalid")
    if any(not _RUNPOD_SCOPE_VALUE.fullmatch(value) for value in gpu_ids):
        raise ConfigurationError("Runpod GPU type scope is invalid")
    if any(not _RUNPOD_NAME_PREFIX.fullmatch(value) for value in data_centers):
        raise ConfigurationError("Runpod GPU or data center scope is invalid")
    max_gpu = _bounded_int(raw.get("max_gpu_count", 1), "max_gpu_count", 1, 8)
    max_container = _bounded_int(
        raw.get("max_container_disk_gb", 100), "max_container_disk_gb", 1, 2048
    )
    max_volume = _bounded_int(raw.get("max_volume_disk_gb", 100), "max_volume_disk_gb", 0, 4096)
    return {
        "pod_ids": tuple(pod_ids),
        "pod_id_prefixes": tuple(id_prefixes),
        "pod_name_prefixes": tuple(name_prefixes),
        "gpu_type_ids": tuple(gpu_ids),
        "data_center_ids": tuple(data_centers),
        "max_gpu_count": max_gpu,
        "max_container_disk_gb": max_container,
        "max_volume_disk_gb": max_volume,
    }


def _parse_auth(provider: str, raw: object) -> tuple[str, str | None, str | None, str | None]:
    """Accept only managed identity, WIF credential config, or host API key modes."""

    if not isinstance(raw, Mapping):
        raise ConfigurationError("profile auth must be an object")
    if provider == "azure":
        if set(raw) - {"mode", "client_id"} or raw.get("mode") != "managed_identity":
            raise ConfigurationError("Azure profile must use managed_identity")
        client_id = raw.get("client_id")
        if client_id is not None and (
            not isinstance(client_id, str) or not _UUID.fullmatch(client_id)
        ):
            raise ConfigurationError("Azure managed identity client_id must be a UUID")
        return "managed_identity", client_id, None, None
    if provider == "gcp":
        if set(raw) != {"mode", "credential_config_ref"}:
            raise ConfigurationError("GCP profile must configure its WIF credential ref")
        if raw.get("mode") != "workload_identity_federation":
            raise ConfigurationError("GCP profile must use Workload Identity Federation")
        if raw.get("credential_config_ref") != _GCP_WIF_REF:
            raise ConfigurationError("GCP profile must use the stable WIF credential ref")
        return "workload_identity_federation", None, _GCP_WIF_REF, None
    if set(raw) != {"mode", "api_key_ref"}:
        raise ConfigurationError("Runpod profile must configure its host API key ref")
    if raw.get("mode") != "api_key" or raw.get("api_key_ref") != _RUNPOD_KEY_REF:
        raise ConfigurationError("Runpod profile must use the stable host API key ref")
    return "api_key", None, None, _RUNPOD_KEY_REF


def _parse_cli(provider: str, raw: object) -> tuple[str | None, float]:
    """Pin the optional native CLI executable to a known vendor command."""

    if raw is None:
        return None, 30.0
    if not isinstance(raw, Mapping) or set(raw) - {"enabled", "executable", "timeout_seconds"}:
        raise ConfigurationError("CLI profile has an invalid shape")
    if raw.get("enabled") is not True:
        raise ConfigurationError("configured CLI profile must be enabled")
    executable = _required_text(raw.get("executable"), "cli.executable", 512)
    path = Path(executable)
    expected = {"azure": "az", "gcp": "gcloud", "runpod": "runpodctl"}[provider]
    if not path.is_absolute() or path.name != expected:
        raise ConfigurationError(
            "CLI executable must be an absolute path to the fixed vendor binary"
        )
    timeout = _bounded_number(raw.get("timeout_seconds", 30), "cli.timeout_seconds", 1, 120)
    return str(path), timeout


def _parse_host_secret_names(raw: object, used_refs: set[str]) -> dict[str, str]:
    """Map only used stable references to host-injected environment names."""

    if not isinstance(raw, Mapping):
        raise ConfigurationError("hostSecretRefs must be an object")
    if set(raw) != used_refs:
        raise ConfigurationError("hostSecretRefs must exactly match configured references")
    result: dict[str, str] = {}
    for reference, environment_name in raw.items():
        if not isinstance(reference, str) or not _SECRET_REF.fullmatch(reference):
            raise ConfigurationError("host secret reference is invalid")
        if not isinstance(environment_name, str) or not _ENV_NAME.fullmatch(environment_name):
            raise ConfigurationError("host secret ref must map to an environment name")
        if environment_name in result.values():
            raise ConfigurationError("host secret environment names must be unique")
        result[reference] = environment_name
    for reference in (_EXCHANGE_KEY_REF, _STATE_DIR_REF):
        if reference not in result:
            raise ConfigurationError("Exchange key and state directory refs are required")
    return result


def _resolve_caller_keys(
    callers: dict[str, TrustedCaller], names: Mapping[str, str], source: Mapping[str, str]
) -> None:
    """Load a fixed 32-byte verification key into the immutable caller binding."""

    encoded = source.get(names[_EXCHANGE_KEY_REF])
    key = _parse_verification_key(encoded)
    for workload, caller in tuple(callers.items()):
        callers[workload] = TrustedCaller(
            caller_workload=caller.caller_workload,
            audience=caller.audience,
            organization_id=caller.organization_id,
            workspace_id=caller.workspace_id,
            issuer=caller.issuer,
            verification_key=key,
        )


def _parse_verification_key(value: str | None) -> bytes:
    """Decode exactly 32 bytes from a host-injected hex or base64 key."""

    if not isinstance(value, str) or not value:
        raise CredentialMissingError("Exchange verification key is missing")
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ConfigurationError("Exchange verification key must be hex or base64") from error
    if len(decoded) != 32:
        raise ConfigurationError("Exchange verification key must contain exactly 32 bytes")
    return decoded


def _optional_api_version(value: object, field_name: str) -> str:
    """Validate one explicit Azure Resource Manager API version."""

    if not isinstance(value, str) or not _API_VERSION.fullmatch(value):
        raise ConfigurationError(f"{field_name} is not a supported API version string")
    return value


def _text_list(value: object, field_name: str, minimum: int, maximum: int) -> list[str]:
    """Require a bounded list of unique nonempty text values."""

    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ConfigurationError(f"{field_name} must contain {minimum} to {maximum} values")
    result = [_required_text(item, field_name, 256) for item in value]
    if len(set(result)) != len(result):
        raise ConfigurationError(f"{field_name} values must be unique")
    return result


def _required_text(value: object, field_name: str, maximum: int) -> str:
    """Validate a bounded, nonempty text configuration value."""

    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ConfigurationError(f"{field_name} must be nonempty bounded text")
    return value


def _bounded_int(value: object, field_name: str, minimum: int, maximum: int) -> int:
    """Validate integer bounds without accepting booleans."""

    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigurationError(f"{field_name} is outside its allowed range")
    return value


def _bounded_env_int(
    environ: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Parse a host-only integer setting within a fixed safe range."""

    raw = environ.get(name)
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
        raise ConfigurationError(f"{name} must be an unsigned integer")
    return _bounded_int(int(raw), name, minimum, maximum)


def _bounded_number(value: object, field_name: str, minimum: float, maximum: float) -> float:
    """Validate finite numeric bounds without accepting booleans."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{field_name} must be numeric")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ConfigurationError(f"{field_name} is outside its allowed range")
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys before parsing trust or profile state."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result
