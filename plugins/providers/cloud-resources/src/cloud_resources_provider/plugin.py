"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud_resources_provider.plugin                            │
│  Role: Verify signed calls and expose fixed read-only cloud tools.   │
│                                                                      │
│  模块职责：验签并提供固定的只读云资源工具。                           │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cyrene_integration_contracts import (
    TOOL_OPERATIONS,
    CloudObservation,
    CloudQueryResult,
    parse_authorization_health,
    parse_cloud_authorization_health_arguments,
    parse_cloud_observation,
    parse_cloud_resources_query_arguments,
    parse_trusted_caller_context,
    serialize_authorization_health,
    serialize_cloud_query_result,
    serialize_trusted_caller_context,
    verify_context,
)
from cyrene_plugin_runtime.configuration import read_environment_settings
from google.protobuf.message import DecodeError
from jsonschema.exceptions import ValidationError as SchemaValidationError

from ._generated import tool_provider_pb2 as wire
from .cloud_api import (
    CloudObservationData,
    CloudProviderFailure,
    CloudResourceReader,
)
from .settings import ConfigurationError, CredentialMissingError, ProviderSettings

CAPABILITY_ID = "tool.provider.v1"
INTERFACE_VERSION = "1"
PLUGIN_ID = "cyrene.providers.cloud-resources"
PLUGIN_VERSION = "0.1.0"
LIST_TOOLS_METHOD = "list_tools"
CALL_TOOL_METHOD = "call_tool"
LIST_TOOLS_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest"
LIST_TOOLS_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse"
CALL_TOOL_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest"
CALL_TOOL_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse"
_MAX_TOOL_REQUEST_BYTES = 32_768
_MAX_TOOL_JSON_BYTES = 16_384

_RESOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "target_ref": {
            "type": "string",
            "pattern": "^[A-Za-z0-9._-]{1,128}$",
            "description": "Opaque selector for one activation-configured cloud resource.",
        }
    },
    "required": ["target_ref"],
    "additionalProperties": False,
}
_HEALTH_SCHEMA = {
    "type": "object",
    "properties": {
        "target_ref": {
            "type": "string",
            "pattern": "^[A-Za-z0-9._-]{1,128}$",
            "description": "Opaque selector for one activation-configured cloud scope.",
        }
    },
    "required": ["target_ref"],
    "additionalProperties": False,
}
CANONICAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "provider_tool_id": "cloud_resources_query",
        "display_name": "Query Cloud Resource Observation",
        "description": (
            "Read one Azure VM, GCP instance, or Hugging Face repository metadata target "
            "from this binding."
        ),
        "input_schema": _RESOURCE_SCHEMA,
    },
    {
        "provider_tool_id": "cloud_authorization_health",
        "display_name": "Check Cloud Authorization Health",
        "description": (
            "Read one configured target to verify the current authorization scope and report "
            "its typed health."
        ),
        "input_schema": _HEALTH_SCHEMA,
    },
)
_TOOL_BY_ID = {str(item["provider_tool_id"]): item for item in CANONICAL_TOOLS}


class TrustedContextError(ValueError):
    """The invocation did not carry a valid signed caller context."""


class CloudResourcesProvider:
    """Serve signed cloud observation tools through DirectPluginRuntime."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: ProviderSettings | None = None,
        reader: CloudResourceReader | None = None,
    ) -> None:
        self._settings = settings
        if self._settings is None:
            if read_environment_settings() is None:
                raise ConfigurationError("provider activation configuration is required")
            self._settings = ProviderSettings.from_environment()
        self._reader = reader or CloudResourceReader(self._settings)

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        **_: Any,
    ) -> tuple[bool, Any]:
        """Dispatch the fixed tool catalog and signed tool-call methods."""

        if capability != CAPABILITY_ID:
            return False, "INVALID_REQUEST: unsupported capability"
        if len(payload) > _MAX_TOOL_REQUEST_BYTES:
            return False, "INVALID_REQUEST: tool request exceeds its byte limit"
        try:
            if action == LIST_TOOLS_METHOD:
                if request_type_url not in (None, LIST_TOOLS_REQUEST_TYPE_URL):
                    return False, "INVALID_REQUEST: catalog payload type mismatch"
                return self._list_tools(payload)
            if action == CALL_TOOL_METHOD:
                if request_type_url not in (None, CALL_TOOL_REQUEST_TYPE_URL):
                    return False, "INVALID_REQUEST: tool payload type mismatch"
                return self._call_tool(payload)
            return False, "METHOD_NOT_FOUND: unsupported provider method"
        except ConfigurationError:
            return False, "INVALID_CONFIGURATION: provider binding is unavailable"

    def _list_tools(self, payload: bytes) -> tuple[bool, Any]:
        """Return only the two fixed schemas for this binding."""

        request = wire.ListToolsRequest()
        try:
            request.ParseFromString(payload)
        except DecodeError:
            return False, "INVALID_REQUEST: catalog request is malformed"
        settings = self._require_settings()
        if request.HasField("binding_id") and request.binding_id != settings.binding_id:
            return False, "INVALID_REQUEST: binding ID does not match"
        response = wire.ListToolsResponse()
        response.catalog.catalog_version = PLUGIN_VERSION
        for definition in CANONICAL_TOOLS:
            descriptor = response.catalog.tools.add()
            descriptor.binding_id = settings.binding_id
            descriptor.provider_tool_id = str(definition["provider_tool_id"])
            descriptor.display_name = str(definition["display_name"])
            descriptor.description = str(definition["description"])
            descriptor.input_schema_json = json.dumps(
                definition["input_schema"], ensure_ascii=False, separators=(",", ":")
            )
        return True, _TypedPayload(response.SerializeToString(), LIST_TOOLS_RESPONSE_TYPE_URL)

    def _call_tool(self, payload: bytes) -> tuple[bool, Any]:
        """Verify caller, binding, operation, and inner arguments before reading cloud state."""

        request = wire.CallToolRequest()
        try:
            request.ParseFromString(payload)
            envelope = _parse_json(request.arguments_json)
        except (DecodeError, ValueError, TypeError):
            return False, "INVALID_REQUEST: tool request is malformed"
        settings = self._require_settings()
        if request.binding_id != settings.binding_id:
            return False, "INVALID_REQUEST: binding ID does not match"
        tool_id = request.provider_tool_id
        if tool_id not in _TOOL_BY_ID:
            return False, "METHOD_NOT_FOUND: provider tool is not available"
        if not isinstance(envelope, Mapping) or set(envelope) != {"caller_context", "arguments"}:
            return False, "INVALID_REQUEST: signed caller context is required"
        arguments_raw = envelope.get("arguments")
        if not isinstance(arguments_raw, Mapping):
            return False, "INVALID_ARGUMENTS: arguments must be an object"
        try:
            if tool_id == "cloud_resources_query":
                arguments = parse_cloud_resources_query_arguments(arguments_raw)
            else:
                arguments = parse_cloud_authorization_health_arguments(arguments_raw)
        except (KeyError, SchemaValidationError, TypeError, ValueError):
            return True, _tool_error("INVALID_ARGUMENTS")
        try:
            context = parse_trusted_caller_context(envelope.get("caller_context"))
            context_value = serialize_trusted_caller_context(context)
            self._verify_caller(tool_id, arguments_raw, context, context_value)
        except (KeyError, SchemaValidationError, TypeError, ValueError):
            return True, _tool_error("AUTHENTICATION_DENIED")
        target_ref = arguments.target_ref
        try:
            if tool_id == "cloud_resources_query":
                return True, self._query_resources(target_ref)
            return True, self._authorization_health(target_ref)
        except CredentialMissingError:
            return True, _tool_error("CLOUD_CREDENTIAL_MISSING")
        except CloudProviderFailure as error:
            code = "CLOUD_" + error.code
            return True, _tool_error(code, retryable=error.retryable)
        except (ConfigurationError, ValueError):
            return True, _tool_error("INVALID_ARGUMENTS")
        except Exception:
            # Internal SDK and transport messages may contain credential or tenant details.
            return True, _tool_error("CLOUD_PROVIDER_FAILURE", retryable=True)

    def _verify_caller(
        self,
        tool_id: str,
        payload: Mapping[str, Any],
        context: Any,
        context_value: Mapping[str, Any],
    ) -> None:
        """Verify a signed workload context against its configured binding key."""

        settings = self._require_settings()
        caller_workload = context_value.get("caller_workload")
        if not isinstance(caller_workload, str):
            raise TrustedContextError("caller workload is missing")
        caller = settings.trusted_callers.get(caller_workload)
        if caller is None:
            raise TrustedContextError("caller workload is not configured")
        expected_operation = TOOL_OPERATIONS[tool_id]
        valid = verify_context(
            method=tool_id,
            operation_id=expected_operation,
            payload=dict(payload),
            context=context,
            key=caller.verification_key,
            expected_caller_workload=caller.caller_workload,
            expected_audience=caller.audience,
            expected_binding_id=settings.binding_id,
            expected_organization_id=caller.organization_id,
            expected_workspace_id=caller.workspace_id,
            expected_issuer=caller.issuer,
        )
        if not valid:
            raise TrustedContextError("signed caller context is invalid")

    def _query_resources(self, target_ref: str) -> _TypedPayload:
        """Return one typed observation without claiming Platform execution readiness."""

        settings = self._require_settings()
        target = settings.target(target_ref)
        observation_data, _ = self._reader.observe(target_ref)
        observation = _cloud_observation(observation_data)
        result = CloudQueryResult(
            provider=target.provider,
            checked_at=observation_data.collected_at,
            resources=(observation,),
        )
        return _tool_response(serialize_cloud_query_result(result))

    def _authorization_health(self, target_ref: str) -> _TypedPayload:
        """Read the same configured target and report only source-proven auth scope."""

        settings = self._require_settings()
        target = settings.target(target_ref)
        checked_at = datetime.now(UTC)
        last_verified_at: datetime | None = None
        login_state = "UNKNOWN"
        authorization_state = "UNKNOWN"
        reason_code: str | None = None
        expires_at: datetime | None = None
        try:
            observation_data, credential = self._reader.observe(target_ref)
            checked_at = observation_data.collected_at
            expires_at = credential.expires_at
            if target.provider == "hf" and credential.token is None:
                login_state = "MISSING"
                is_private = observation_data.attributes.get("private")
                is_gated = observation_data.attributes.get("gated")
                if is_private is False and is_gated is False:
                    authorization_state = "VALID"
                    last_verified_at = checked_at
                else:
                    authorization_state = "UNKNOWN"
                    reason_code = "PUBLIC_SCOPE_NOT_PROVEN"
            else:
                login_state = "PRESENT"
                authorization_state = "VALID"
                last_verified_at = checked_at
        except CredentialMissingError:
            login_state = "MISSING"
            authorization_state = "UNKNOWN"
            reason_code = "CLOUD_CREDENTIAL_MISSING"
        except CloudProviderFailure as error:
            checked_at = datetime.now(UTC)
            reason_code = "CLOUD_" + error.code
            expires_at = error.expires_at
            if error.credential_present is True:
                login_state = "PRESENT"
            elif error.credential_present is False:
                login_state = "MISSING"
            if error.code == "AUTHENTICATION_REQUIRED":
                authorization_state = (
                    "REAUTHORIZATION_REQUIRED" if error.credential_present is True else "UNKNOWN"
                )
            elif error.code == "PERMISSION_DENIED":
                authorization_state = "PERMISSION_DENIED"
            elif error.code == "CREDENTIAL_PROVIDER_UNAVAILABLE":
                login_state = "UNAVAILABLE"
                authorization_state = "UNAVAILABLE"
            else:
                if error.credential_present is None:
                    login_state = "UNKNOWN"
                authorization_state = "UNAVAILABLE" if error.retryable else "UNKNOWN"
        health_value = {
            "schema_version": 1,
            "authorization_ref": settings.binding_id,
            "login_state": login_state,
            "authorization_state": authorization_state,
            "checked_at": _format_time(checked_at),
            "expires_at": _format_time(expires_at) if expires_at is not None else None,
            "generation": settings.authorization_generation,
            "capabilities": [
                {
                    "capability_id": "cloud.resources.query",
                    "state": authorization_state,
                    "last_verified_at": (
                        _format_time(last_verified_at) if last_verified_at is not None else None
                    ),
                    "expires_at": _format_time(expires_at) if expires_at is not None else None,
                    "reason_code": reason_code,
                }
            ],
        }
        typed = parse_authorization_health(health_value)
        return _tool_response(serialize_authorization_health(typed))

    def _require_settings(self) -> ProviderSettings:
        """Return activation settings after constructor validation."""

        if self._settings is None:
            raise ConfigurationError("provider activation configuration is required")
        return self._settings


def _cloud_observation(data: CloudObservationData) -> CloudObservation:
    """Convert normalized vendor state into the shared typed observation DTO."""

    value = {
        "external_ref": data.external_ref,
        "kind": data.kind,
        "display_name": data.display_name,
        "status": data.status,
        "provider_status": data.provider_status,
        "collected_at": _format_time(data.collected_at),
        "observed_at": _format_time(data.observed_at) if data.observed_at is not None else None,
        "attributes": dict(data.attributes),
        "platform_resource_ref": data.platform_resource_ref,
        "platform_generation": data.platform_generation,
    }
    return parse_cloud_observation(value)


def _tool_response(value: Mapping[str, Any]) -> _TypedPayload:
    """Encode a bounded result as ToolJsonContent."""

    response = wire.CallToolResponse()
    response.outcome.is_error = False
    response.outcome.content.add().json.json = json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    )
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


def _tool_error(code: str, *, retryable: bool = False) -> _TypedPayload:
    """Return a stable machine-readable failure without provider response text."""

    response = wire.CallToolResponse()
    response.outcome.is_error = True
    response.outcome.content.add().json.json = json.dumps(
        {"error": {"code": code, "retryable": retryable}}, separators=(",", ":")
    )
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


def _parse_json(raw: str) -> Any:
    """Decode small JSON payloads while rejecting duplicate keys."""

    if len(raw.encode("utf-8")) > _MAX_TOOL_JSON_BYTES:
        raise ValueError("tool JSON exceeds its byte limit")
    return json.loads(raw, object_pairs_hook=_unique_object)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys before signature or schema validation."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("tool JSON contains a duplicate field")
        result[key] = value
    return result


def _format_time(value: datetime | None) -> str:
    """Format one aware timestamp as RFC3339 UTC with explicit zone data."""

    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class _TypedPayload:
    """Byte payload wrapper consumed by DirectPluginRuntime."""

    value: bytes
    type_url: str
