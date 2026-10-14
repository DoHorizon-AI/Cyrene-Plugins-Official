"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud_resources_provider.settings                          │
│  Role: Validate fixed cloud targets, callers, and host secret refs.  │
│                                                                      │
│  模块职责：校验固定云目标、可信调用方和主机密钥引用。                 │
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
from typing import TypeAlias

_CONFIG_LIMIT = 64 * 1024
_MAX_TARGETS = 500
_EXCHANGE_KEY_REF = "cloud-resources/exchange-verification-key"
_GCP_CREDENTIAL_REF = "cloud-resources/gcp-service-account-json"
_HF_TOKEN_REF = "cloud-resources/hf-read-token"
_TAILSCALE_CLIENT_SECRET_REF = "cloud-resources/tailscale-client-secret"
_RUNPOD_API_KEY_REF = "cloud-resources/runpod-api-key"
_SECRET_REF = re.compile(r"[a-z][a-z0-9._/-]{0,127}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_TARGET_REF = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


class ConfigurationError(ValueError):
    """The binding configuration is not safe to activate."""


class CredentialMissingError(ValueError):
    """A configured host credential reference has no injected value."""


@dataclass(frozen=True, slots=True)
class AzureVmTarget:
    """One fixed Azure Resource Manager VM instance-view target."""

    target_ref: str
    display_name: str
    subscription_id: str
    resource_group: str
    vm_name: str
    provider: str = "azure"
    kind: str = "azure.vm"


@dataclass(frozen=True, slots=True)
class GcpInstanceTarget:
    """One fixed Google Compute Engine instance target."""

    target_ref: str
    display_name: str
    project_id: str
    zone: str
    instance_name: str
    provider: str = "gcp"
    kind: str = "gcp.instance"


@dataclass(frozen=True, slots=True)
class HuggingFaceTarget:
    """One fixed Hub model, dataset, or Space metadata target."""

    target_ref: str
    display_name: str
    kind: str
    repo_id: str
    provider: str = "hf"


@dataclass(frozen=True, slots=True)
class TailscaleDeviceTarget:
    """One fixed device read within a configured Tailscale tailnet."""

    target_ref: str
    display_name: str
    tailnet: str
    device_id: str
    provider: str = "tailscale"
    kind: str = "tailscale.device"


@dataclass(frozen=True, slots=True)
class RunpodPodTarget:
    """One fixed Runpod Pod REST resource."""

    target_ref: str
    display_name: str
    pod_id: str
    provider: str = "runpod"
    kind: str = "runpod.pod"


CloudTarget: TypeAlias = (
    AzureVmTarget | GcpInstanceTarget | HuggingFaceTarget | TailscaleDeviceTarget | RunpodPodTarget
)


@dataclass(frozen=True, slots=True)
class TrustedCaller:
    """One workload and audience accepted by this Plugin binding."""

    caller_workload: str
    audience: str
    verification_key: bytes = field(repr=False)
    organization_id: str
    workspace_id: str
    issuer: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Validated activation settings without copying provider credentials."""

    binding_id: str
    authorization_generation: int
    trusted_callers: Mapping[str, TrustedCaller]
    azure_client_id: str | None
    gcp_auth_mode: str
    gcp_credential_environment_name: str | None
    hf_token_environment_name: str | None
    tailscale_client_id: str | None
    tailscale_client_secret_environment_name: str | None
    tailscale_tailnet: str | None
    runpod_api_key_environment_name: str | None
    host_secret_names: Mapping[str, str]
    targets: tuple[CloudTarget, ...]
    timeout_seconds: float
    environ: Mapping[str, str] = field(repr=False, compare=False)

    def target(self, target_ref: str) -> CloudTarget:
        """Resolve a caller's opaque selector to one configured target."""

        for target in self.targets:
            if target.target_ref == target_ref:
                return target
        raise ValueError("target_ref is not configured for this binding")

    def secret_value(self, reference: str) -> str | None:
        """Return the host-injected value for a configured stable secret ref."""

        environment_name = self.host_secret_names.get(reference)
        if environment_name is None:
            return None
        value = self.environ.get(environment_name)
        return value if isinstance(value, str) and value else None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ProviderSettings:
        """Parse bounded activation JSON and separately injected secret bytes."""

        source = os.environ if environ is None else environ
        binding_id = _required_text(source.get("CYRENE_CAPABILITY_BINDING_ID"), "binding ID", 256)
        config_raw = source.get("CYRENE_CAPABILITY_CONFIGURATION_JSON") or "{}"
        if len(config_raw.encode("utf-8")) > _CONFIG_LIMIT:
            raise ConfigurationError("provider activation configuration exceeds its size limit")
        try:
            config = json.loads(config_raw, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as error:
            raise ConfigurationError(
                "provider activation configuration must be valid JSON"
            ) from error
        if not isinstance(config, Mapping):
            raise ConfigurationError("provider activation configuration must be an object")
        allowed = {
            "authorization_generation",
            "trusted_callers",
            "azure_auth",
            "gcp_auth",
            "hf_token_ref",
            "tailscale_auth",
            "runpod_auth",
            "targets",
            "timeout_seconds",
            "hostSecretRefs",
        }
        if set(config) - allowed:
            raise ConfigurationError("provider activation contains unsupported fields")

        generation = config.get("authorization_generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ConfigurationError("authorization_generation must be a nonnegative integer")
        caller_configs, used_secret_refs = _parse_callers(config.get("trusted_callers"))
        azure_client_id = _parse_azure_auth(config.get("azure_auth"))
        gcp_auth_mode, gcp_credential_ref = _parse_gcp_auth(config.get("gcp_auth"))
        hf_token_ref = config.get("hf_token_ref")
        if hf_token_ref is not None:
            if hf_token_ref != _HF_TOKEN_REF:
                raise ConfigurationError("hf_token_ref must use the manifest's stable token ref")
            used_secret_refs.add(_HF_TOKEN_REF)
        if gcp_credential_ref is not None:
            used_secret_refs.add(gcp_credential_ref)

        targets = _parse_targets(config.get("targets"))
        tailscale_client_id, tailscale_secret_ref, tailscale_tailnet = _parse_tailscale_auth(
            config.get("tailscale_auth"),
            requires_auth=any(isinstance(target, TailscaleDeviceTarget) for target in targets),
        )
        if tailscale_tailnet is not None and any(
            target.tailnet.casefold() != tailscale_tailnet.casefold()
            for target in targets
            if isinstance(target, TailscaleDeviceTarget)
        ):
            raise ConfigurationError(
                "Tailscale device tailnet must match the configured OAuth binding"
            )
        if tailscale_secret_ref is not None:
            used_secret_refs.add(tailscale_secret_ref)
        runpod_api_key_ref = _parse_runpod_auth(
            config.get("runpod_auth"),
            requires_auth=any(isinstance(target, RunpodPodTarget) for target in targets),
        )
        if runpod_api_key_ref is not None:
            used_secret_refs.add(runpod_api_key_ref)
        timeout_seconds = _bounded_timeout(config.get("timeout_seconds", 8))
        host_secret_names = _parse_host_secret_names(config.get("hostSecretRefs"), used_secret_refs)
        trusted_callers = {
            workload: TrustedCaller(
                caller_workload=caller.caller_workload,
                audience=caller.audience,
                verification_key=_parse_verification_key(
                    source.get(host_secret_names["cloud-resources/exchange-verification-key"])
                ),
                organization_id=caller.organization_id,
                workspace_id=caller.workspace_id,
                issuer=caller.issuer,
            )
            for workload, caller in caller_configs.items()
        }
        return cls(
            binding_id=binding_id,
            authorization_generation=generation,
            trusted_callers=trusted_callers,
            azure_client_id=azure_client_id,
            gcp_auth_mode=gcp_auth_mode,
            gcp_credential_environment_name=host_secret_names.get(gcp_credential_ref)
            if gcp_credential_ref is not None
            else None,
            hf_token_environment_name=host_secret_names.get(hf_token_ref)
            if isinstance(hf_token_ref, str)
            else None,
            tailscale_client_id=tailscale_client_id,
            tailscale_client_secret_environment_name=(
                host_secret_names.get(tailscale_secret_ref)
                if tailscale_secret_ref is not None
                else None
            ),
            tailscale_tailnet=tailscale_tailnet,
            runpod_api_key_environment_name=host_secret_names.get(runpod_api_key_ref)
            if runpod_api_key_ref is not None
            else None,
            host_secret_names=host_secret_names,
            targets=targets,
            timeout_seconds=timeout_seconds,
            environ=source,
        )


def _parse_callers(raw: object) -> tuple[dict[str, TrustedCaller], set[str]]:
    """Load one fixed trusted caller and its configured verification key."""

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
    organization_id = _required_text(item.get("organization_id"), "organization_id", 256)
    workspace_id = _required_text(item.get("workspace_id"), "workspace_id", 256)
    reference = _required_text(item.get("verification_key_ref"), "verification_key_ref", 128)
    if reference != _EXCHANGE_KEY_REF:
        raise ConfigurationError("trusted caller must use the manifest's Exchange verification ref")
    issuer_value = item.get("issuer")
    issuer = _required_text(issuer_value, "issuer", 512) if issuer_value is not None else None
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,256}", workload):
        raise ConfigurationError("caller_workload contains unsupported characters")
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,256}", audience):
        raise ConfigurationError("audience contains unsupported characters")
    return (
        {workload: TrustedCaller(workload, audience, b"", organization_id, workspace_id, issuer)},
        {reference},
    )


def _parse_tailscale_auth(
    raw: object, *, requires_auth: bool
) -> tuple[str | None, str | None, str | None]:
    """Require scoped OAuth client credentials for configured Tailscale devices."""

    if raw is None and not requires_auth:
        return None, None, None
    if not requires_auth:
        raise ConfigurationError("tailscale_auth requires a configured Tailscale device target")
    if not isinstance(raw, Mapping) or set(raw) != {
        "mode",
        "client_id",
        "client_secret_ref",
        "tailnet",
    }:
        raise ConfigurationError("tailscale_auth must configure one OAuth client")
    if raw.get("mode") != "oauth_client":
        raise ConfigurationError("tailscale_auth.mode must be oauth_client")
    client_id = _required_text(raw.get("client_id"), "tailscale_auth.client_id", 256)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", client_id):
        raise ConfigurationError("Tailscale OAuth client_id contains unsupported characters")
    reference = raw.get("client_secret_ref")
    if reference != _TAILSCALE_CLIENT_SECRET_REF:
        raise ConfigurationError("Tailscale OAuth client secret must use its stable manifest ref")
    tailnet = _required_text(raw.get("tailnet"), "tailscale_auth.tailnet", 200)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", tailnet) or tailnet == "-":
        raise ConfigurationError("Tailscale OAuth tailnet must be a fixed non-shorthand selector")
    return client_id, _TAILSCALE_CLIENT_SECRET_REF, tailnet


def _parse_runpod_auth(raw: object, *, requires_auth: bool) -> str | None:
    """Require the stable secret ref for a Runpod Read Only API key."""

    if raw is None and not requires_auth:
        return None
    if not requires_auth:
        raise ConfigurationError("runpod_auth requires a configured Runpod Pod target")
    if not isinstance(raw, Mapping) or set(raw) != {"mode", "api_key_ref"}:
        raise ConfigurationError("runpod_auth must configure one Read Only API key")
    if raw.get("mode") != "read_only_api_key" or raw.get("api_key_ref") != _RUNPOD_API_KEY_REF:
        raise ConfigurationError("runpod_auth must use the manifest's Read Only API-key ref")
    return _RUNPOD_API_KEY_REF


def _parse_azure_auth(raw: object) -> str | None:
    """Validate explicit system- or user-assigned managed-identity selection."""

    if not isinstance(raw, Mapping) or set(raw) - {"mode", "client_id"}:
        raise ConfigurationError("azure_auth must select a supported authentication mode")
    if raw.get("mode") != "managed_identity":
        raise ConfigurationError("azure_auth.mode must be managed_identity")
    client_id = raw.get("client_id")
    if client_id is None:
        return None
    if not isinstance(client_id, str) or not _UUID.fullmatch(client_id):
        raise ConfigurationError("azure_auth.client_id must be a UUID")
    return client_id


def _parse_gcp_auth(raw: object) -> tuple[str, str | None]:
    """Accept metadata-server or host-injected service-account credentials."""

    if not isinstance(raw, Mapping) or set(raw) - {"mode", "credential_ref"}:
        raise ConfigurationError("gcp_auth must select a supported authentication mode")
    mode = raw.get("mode")
    reference = raw.get("credential_ref")
    if mode == "metadata" and reference is None:
        return "metadata", None
    if mode == "service_account_json" and reference == _GCP_CREDENTIAL_REF:
        return "service_account_json", _GCP_CREDENTIAL_REF
    raise ConfigurationError("gcp_auth credential mode and stable ref do not match")


def _parse_targets(raw: object) -> tuple[CloudTarget, ...]:
    """Parse the fixed provider-specific target allowlist."""

    if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_TARGETS:
        raise ConfigurationError("targets must contain between one and 500 entries")
    targets: list[CloudTarget] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ConfigurationError("target entries must be objects")
        target_ref = _required_text(item.get("target_ref"), "target_ref", 128)
        display_name = _required_text(item.get("display_name"), "display_name", 256)
        if not _TARGET_REF.fullmatch(target_ref) or target_ref in seen:
            raise ConfigurationError("target_ref values must be unique opaque selectors")
        seen.add(target_ref)
        provider = item.get("provider")
        kind = item.get("kind")
        if provider == "azure" and kind == "azure.vm":
            _allow_exact_fields(
                item,
                {
                    "target_ref",
                    "display_name",
                    "provider",
                    "kind",
                    "subscription_id",
                    "resource_group",
                    "vm_name",
                },
            )
            subscription_id = _required_text(item.get("subscription_id"), "subscription_id", 36)
            resource_group = _required_text(item.get("resource_group"), "resource_group", 90)
            vm_name = _required_text(item.get("vm_name"), "vm_name", 64)
            if not _UUID.fullmatch(subscription_id):
                raise ConfigurationError("Azure subscription_id must be a UUID")
            if not re.fullmatch(r"[A-Za-z0-9_.() -]{1,90}", resource_group):
                raise ConfigurationError("Azure resource_group contains unsupported characters")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", vm_name):
                raise ConfigurationError("Azure vm_name contains unsupported characters")
            targets.append(
                AzureVmTarget(target_ref, display_name, subscription_id, resource_group, vm_name)
            )
        elif provider == "gcp" and kind == "gcp.instance":
            _allow_exact_fields(
                item,
                {
                    "target_ref",
                    "display_name",
                    "provider",
                    "kind",
                    "project_id",
                    "zone",
                    "instance_name",
                },
            )
            project_id = _required_text(item.get("project_id"), "project_id", 30)
            zone = _required_text(item.get("zone"), "zone", 32)
            instance_name = _required_text(item.get("instance_name"), "instance_name", 63)
            if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id):
                raise ConfigurationError("GCP project_id is invalid")
            if not re.fullmatch(r"[a-z]+-[a-z0-9]+-[a-z]", zone):
                raise ConfigurationError("GCP zone is invalid")
            if not re.fullmatch(r"[a-z]([-a-z0-9]{0,61}[a-z0-9])?", instance_name):
                raise ConfigurationError("GCP instance_name is invalid")
            targets.append(
                GcpInstanceTarget(target_ref, display_name, project_id, zone, instance_name)
            )
        elif provider == "hf" and kind in {"hf.model", "hf.dataset", "hf.space"}:
            _allow_exact_fields(item, {"target_ref", "display_name", "provider", "kind", "repo_id"})
            repo_id = _required_text(item.get("repo_id"), "repo_id", 256)
            repo_parts = repo_id.split("/")
            if (
                len(repo_parts) != 2
                or any(part in {".", ".."} for part in repo_parts)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_id)
            ):
                raise ConfigurationError("Hugging Face repo_id is invalid")
            targets.append(HuggingFaceTarget(target_ref, display_name, kind, repo_id))
        elif provider == "tailscale" and kind == "tailscale.device":
            _allow_exact_fields(
                item,
                {"target_ref", "display_name", "provider", "kind", "tailnet", "device_id"},
            )
            tailnet = _required_text(item.get("tailnet"), "tailnet", 200)
            device_id = _required_text(item.get("device_id"), "device_id", 128)
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", tailnet) or tailnet == "-":
                raise ConfigurationError("Tailscale tailnet must be a fixed non-shorthand selector")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", device_id):
                raise ConfigurationError("Tailscale device_id contains unsupported characters")
            targets.append(TailscaleDeviceTarget(target_ref, display_name, tailnet, device_id))
        elif provider == "runpod" and kind == "runpod.pod":
            _allow_exact_fields(item, {"target_ref", "display_name", "provider", "kind", "pod_id"})
            pod_id = _required_text(item.get("pod_id"), "pod_id", 128)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", pod_id):
                raise ConfigurationError("Runpod pod_id contains unsupported characters")
            targets.append(RunpodPodTarget(target_ref, display_name, pod_id))
        else:
            raise ConfigurationError("target provider and kind are not supported")
    return tuple(targets)


def _parse_host_secret_names(raw: object, required_refs: set[str]) -> dict[str, str]:
    """Require exact stable-ref to environment-name mappings."""

    if not isinstance(raw, Mapping):
        raise ConfigurationError("hostSecretRefs must be an object")
    if set(raw) != required_refs:
        raise ConfigurationError("hostSecretRefs must contain exactly the configured secret refs")
    result: dict[str, str] = {}
    for reference, environment_name in raw.items():
        if not isinstance(reference, str) or not _SECRET_REF.fullmatch(reference):
            raise ConfigurationError("hostSecretRefs contains an invalid secret ref")
        result[reference] = _required_environment_name(environment_name, reference)
    if len(set(result.values())) != len(result):
        raise ConfigurationError(
            "hostSecretRefs must not alias secret refs to one environment name"
        )
    return result


def _bounded_timeout(raw: object) -> float:
    """Validate a single request timeout within the provider's hard bound."""

    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 < raw <= 15:
        raise ConfigurationError("timeout_seconds must be greater than zero and at most 15")
    return float(raw)


def _parse_verification_key(value: str | None) -> bytes:
    """Decode exactly 32 bytes from a hex or base64 injected key."""

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


def _required_environment_name(value: object, field_name: str) -> str:
    """Validate a host-provided environment-variable name."""

    if not isinstance(value, str) or not _ENVIRONMENT_NAME.fullmatch(value):
        raise ConfigurationError(f"{field_name} must name an injected environment variable")
    return value


def _required_text(value: object, field_name: str, maximum: int) -> str:
    """Accept bounded nonempty text without accepting booleans as strings."""

    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ConfigurationError(
            f"{field_name} must be nonempty text of at most {maximum} characters"
        )
    return value


def _allow_exact_fields(value: Mapping[str, object], allowed: set[str]) -> None:
    """Reject unsupported target configuration so no field is silently trusted."""

    if set(value) != allowed:
        raise ConfigurationError("target contains unsupported or missing fields")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys before configuration values become authoritative."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("configuration contains a duplicate object key")
        result[key] = value
    return result
