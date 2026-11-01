"""Closed activation settings for signed, registry-bound knowledge retrieval.

中文：解析宿主 caller-context 信任、身份映射和固定知识制品注册表。
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

from .bundle import BundleError, resolve_bundle_path

CONTEXT_KEY_REF = "knowledge-retrieval/caller-context-verification-key"
_MAX_CONFIGURATION_BYTES = 64 * 1024
_MAX_BUNDLES = 256
_MAX_PRINCIPAL_MAPPINGS = 10_000
_SECRET_REF = re.compile(r"[a-z][a-z0-9._/-]{0,127}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_SELECTOR = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ConfigurationError(ValueError):
    """The plugin activation configuration is absent or invalid."""


@dataclass(frozen=True, slots=True)
class TrustedCaller:
    """One configured host workload allowed to sign knowledge search calls."""

    caller_workload: str
    audience: str
    issuer: str
    organization_id: str
    workspace_id: str
    verification_key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class BundleArtifact:
    """One immutable ArtifactRef pinned to a path beneath artifact_root."""

    library_selector: str
    bundle_locator: str
    uri: str
    digest: str
    size_bytes: int
    kind: str
    manifest_digest: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalSettings:
    """Validated trust boundaries and resource bounds for one provider binding."""

    artifact_root: Path
    binding_id: str
    trusted_caller: TrustedCaller
    principal_mappings: Mapping[tuple[str, str, str, str], tuple[str, ...]] = field(repr=False)
    bundle_registry: Mapping[str, BundleArtifact]
    max_bundle_bytes: int = 256 * 1024 * 1024
    max_member_bytes: int = 192 * 1024 * 1024
    max_search_limit: int = 20
    max_jsonl_records: int = 500_000
    max_query_chars: int = 4_096

    def principals_for(
        self, *, issuer: str, subject: str, organization_id: str, workspace_id: str
    ) -> tuple[str, ...]:
        """Resolve only the configured principal IDs for one verified identity tuple."""

        key = (issuer, subject, organization_id, workspace_id)
        principal_refs = self.principal_mappings.get(key)
        if principal_refs is None:
            raise ConfigurationError("verified caller has no trusted principal mapping")
        return principal_refs

    def bundle(self, library_selector: str) -> BundleArtifact:
        """Resolve a model-visible selector to one fixed server-configured artifact."""

        bundle = self.bundle_registry.get(library_selector)
        if bundle is None:
            raise ConfigurationError("librarySelector is not configured for this binding")
        return bundle

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        binding_id: str,
        environ: Mapping[str, str] | None = None,
    ) -> RetrievalSettings:
        """Validate a closed activation mapping and resolve its host-only HMAC key."""

        allowed = {
            "artifact_root",
            "trusted_caller",
            "trusted_principals",
            "bundle_registry",
            "max_bundle_bytes",
            "max_member_bytes",
            "max_search_limit",
            "max_jsonl_records",
            "max_query_chars",
            "hostSecretRefs",
        }
        required = {
            "artifact_root",
            "trusted_caller",
            "trusted_principals",
            "bundle_registry",
            "hostSecretRefs",
        }
        if not required.issubset(value) or set(value) - allowed:
            raise ConfigurationError("configuration must contain the required retrieval fields")
        if not isinstance(binding_id, str) or not binding_id.strip() or len(binding_id) > 256:
            raise ConfigurationError("plugin binding ID must be non-empty bounded text")

        raw_root = _required_text(value.get("artifact_root"), "artifact_root", 1_024)
        root = Path(raw_root)
        if not root.is_absolute():
            raise ConfigurationError("artifact_root must be absolute")
        try:
            root = root.resolve(strict=True)
        except OSError as error:
            raise ConfigurationError("artifact_root must exist and be readable") from error
        if not root.is_dir():
            raise ConfigurationError("artifact_root must be a directory")

        bounds = {
            "max_bundle_bytes": (256 * 1024 * 1024, 1, 256 * 1024 * 1024),
            "max_member_bytes": (192 * 1024 * 1024, 1, 192 * 1024 * 1024),
            "max_search_limit": (20, 1, 100),
            "max_jsonl_records": (500_000, 1, 2_000_000),
            "max_query_chars": (4_096, 1, 16_384),
        }
        limits: dict[str, int] = {}
        for name, (default, minimum, maximum) in bounds.items():
            item = value.get(name, default)
            if type(item) is not int or not minimum <= item <= maximum:
                raise ConfigurationError(f"{name} must be an integer from {minimum} to {maximum}")
            limits[name] = item
        if limits["max_member_bytes"] > limits["max_bundle_bytes"]:
            raise ConfigurationError("max_member_bytes cannot exceed max_bundle_bytes")

        secret_names = _parse_host_secret_refs(value.get("hostSecretRefs"))
        source = os.environ if environ is None else environ
        environment_name = secret_names[CONTEXT_KEY_REF]
        verification_key = _parse_verification_key(source.get(environment_name))
        trusted_caller = _parse_trusted_caller(value.get("trusted_caller"), verification_key)
        principal_mappings = _parse_principal_mappings(
            value.get("trusted_principals"), trusted_caller
        )
        bundle_registry = _parse_bundle_registry(
            value.get("bundle_registry"), root, limits["max_bundle_bytes"]
        )
        for artifact in bundle_registry.values():
            try:
                path = resolve_bundle_path(root, artifact.bundle_locator)
                if path.stat().st_size != artifact.size_bytes:
                    raise ConfigurationError(
                        "bundle registry ArtifactRef size does not match its configured artifact"
                    )
            except BundleError as error:
                raise ConfigurationError(
                    "bundle registry locator is missing or escapes artifact_root"
                ) from error
            except OSError as error:
                raise ConfigurationError("bundle registry artifact cannot be read") from error
        return cls(
            artifact_root=root,
            binding_id=binding_id,
            trusted_caller=trusted_caller,
            principal_mappings=principal_mappings,
            bundle_registry=bundle_registry,
            **limits,
        )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> RetrievalSettings:
        """Read bounded standard activation JSON and its separately injected host key."""

        source = os.environ if environ is None else environ
        binding_id = source.get("CYRENE_CAPABILITY_BINDING_ID")
        encoded = source.get("CYRENE_CAPABILITY_CONFIGURATION_JSON")
        if not binding_id or encoded is None:
            raise ConfigurationError("plugin binding ID and activation configuration are required")
        if len(encoded.encode("utf-8")) > _MAX_CONFIGURATION_BYTES:
            raise ConfigurationError("plugin activation configuration exceeds its size limit")
        try:
            decoded = json.loads(encoded, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as error:
            raise ConfigurationError("plugin configuration must be valid JSON") from error
        if not isinstance(decoded, Mapping):
            raise ConfigurationError("plugin configuration must be an object")
        return cls.from_mapping(decoded, binding_id=binding_id, environ=source)


def _parse_trusted_caller(value: Any, verification_key: bytes) -> TrustedCaller:
    required = {
        "caller_workload",
        "audience",
        "issuer",
        "organization_id",
        "workspace_id",
        "verification_key_ref",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ConfigurationError("trusted_caller has an invalid shape")
    if value.get("verification_key_ref") != CONTEXT_KEY_REF:
        raise ConfigurationError("trusted_caller must use the fixed verification key ref")
    workload = _required_text(value.get("caller_workload"), "caller_workload", 256)
    audience = _required_text(value.get("audience"), "audience", 256)
    issuer = _required_text(value.get("issuer"), "issuer", 512)
    organization_id = _required_text(value.get("organization_id"), "organization_id", 256)
    workspace_id = _required_text(value.get("workspace_id"), "workspace_id", 256)
    for label, item in (("caller_workload", workload), ("audience", audience)):
        if re.fullmatch(r"[A-Za-z0-9._:/-]{1,256}", item) is None:
            raise ConfigurationError(f"{label} contains unsupported characters")
    return TrustedCaller(
        caller_workload=workload,
        audience=audience,
        issuer=issuer,
        organization_id=organization_id,
        workspace_id=workspace_id,
        verification_key=verification_key,
    )


def _parse_principal_mappings(
    value: Any, caller: TrustedCaller
) -> Mapping[tuple[str, str, str, str], tuple[str, ...]]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_PRINCIPAL_MAPPINGS:
        raise ConfigurationError("trusted_principals must contain bounded identity mappings")
    result: dict[tuple[str, str, str, str], tuple[str, ...]] = {}
    for item in value:
        required = {
            "issuer",
            "subject",
            "organization_id",
            "workspace_id",
            "principal_refs",
        }
        if not isinstance(item, Mapping) or set(item) != required:
            raise ConfigurationError("trusted principal mapping has an invalid shape")
        issuer = _required_text(item.get("issuer"), "principal issuer", 512)
        subject = _required_text(item.get("subject"), "principal subject", 512)
        organization_id = _required_text(item.get("organization_id"), "principal organization", 256)
        workspace_id = _required_text(item.get("workspace_id"), "principal workspace", 256)
        if (
            issuer != caller.issuer
            or organization_id != caller.organization_id
            or workspace_id != caller.workspace_id
        ):
            raise ConfigurationError("trusted principal mapping escapes the caller scope")
        raw_principals = item.get("principal_refs")
        if (
            not isinstance(raw_principals, list)
            or not 1 <= len(raw_principals) <= 256
            or any(
                not isinstance(principal, str) or not principal.strip() or len(principal) > 512
                for principal in raw_principals
            )
            or len(raw_principals) != len(set(raw_principals))
        ):
            raise ConfigurationError("principal_refs must be a bounded unique string array")
        identity = (issuer, subject, organization_id, workspace_id)
        if identity in result:
            raise ConfigurationError("trusted principal identity is duplicated")
        result[identity] = tuple(raw_principals)
    return result


def _parse_bundle_registry(
    value: Any, artifact_root: Path, max_bundle_bytes: int
) -> Mapping[str, BundleArtifact]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_BUNDLES:
        raise ConfigurationError("bundle_registry must contain between one and 256 entries")
    result: dict[str, BundleArtifact] = {}
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "library_selector",
            "bundle_locator",
            "artifact_ref",
        }:
            raise ConfigurationError("bundle registry entry has an invalid shape")
        selector = _required_text(item.get("library_selector"), "library_selector", 128)
        if _SELECTOR.fullmatch(selector) is None or selector in result:
            raise ConfigurationError("library_selector is invalid or duplicated")
        locator = _required_text(item.get("bundle_locator"), "bundle_locator", 2_048)
        artifact = item.get("artifact_ref")
        if not isinstance(artifact, Mapping) or set(artifact) - {
            "uri",
            "digest",
            "size_bytes",
            "kind",
            "manifest_digest",
        }:
            raise ConfigurationError("bundle registry ArtifactRef has an invalid shape")
        if not {"uri", "digest", "size_bytes", "kind"}.issubset(artifact):
            raise ConfigurationError("bundle registry ArtifactRef is incomplete")
        digest = artifact.get("digest")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ConfigurationError("ArtifactRef.digest must be a SHA-256 digest")
        uri = _required_text(artifact.get("uri"), "ArtifactRef.uri", 256)
        if uri != f"artifact://sha256/{digest.removeprefix('sha256:')}":
            raise ConfigurationError("ArtifactRef.uri does not identify its digest")
        size_bytes = artifact.get("size_bytes")
        if type(size_bytes) is not int or not 1 <= size_bytes <= max_bundle_bytes:
            raise ConfigurationError("ArtifactRef.size_bytes exceeds the configured limit")
        kind = _required_text(artifact.get("kind"), "ArtifactRef.kind", 128)
        manifest_digest = artifact.get("manifest_digest")
        if manifest_digest is not None and (
            not isinstance(manifest_digest, str) or _DIGEST.fullmatch(manifest_digest) is None
        ):
            raise ConfigurationError("ArtifactRef.manifest_digest is invalid")
        result[selector] = BundleArtifact(
            library_selector=selector,
            bundle_locator=locator,
            uri=uri,
            digest=digest,
            size_bytes=size_bytes,
            kind=kind,
            manifest_digest=manifest_digest,
        )
        if size_bytes > max_bundle_bytes:
            raise ConfigurationError("ArtifactRef.size_bytes exceeds max_bundle_bytes")
    return result


def _parse_host_secret_refs(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {CONTEXT_KEY_REF}:
        raise ConfigurationError("hostSecretRefs must contain only the caller-context key ref")
    environment_name = value[CONTEXT_KEY_REF]
    if (
        not isinstance(environment_name, str)
        or _ENVIRONMENT_NAME.fullmatch(environment_name) is None
    ):
        raise ConfigurationError(
            "caller-context key ref must name an injected environment variable"
        )
    return {CONTEXT_KEY_REF: environment_name}


def _parse_verification_key(value: str | None) -> bytes:
    if not isinstance(value, str) or not value:
        raise ConfigurationError("caller-context verification key is unavailable")
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return bytes.fromhex(value)
    try:
        key = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ConfigurationError("caller-context key must be 32-byte hex or base64") from error
    if len(key) != 32:
        raise ConfigurationError("caller-context key must contain exactly 32 bytes")
    return key


def _required_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > maximum:
        raise ConfigurationError(f"{label} must be non-empty bounded text")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError(f"duplicate configuration field {key!r}")
        result[key] = item
    return result
