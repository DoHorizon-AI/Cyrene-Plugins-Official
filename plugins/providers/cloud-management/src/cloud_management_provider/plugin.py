"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud_management_provider.plugin                           │
│  Role: Verify signed cloud tool calls and expose the fixed catalog. │
│                                                                      │
│  模块职责：校验云工具签名调用并公开固定工具目录。                     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cyrene_integration_contracts import (
    parse_trusted_caller_context,
    serialize_trusted_caller_context,
    verify_context,
)
from cyrene_plugin_runtime.configuration import read_environment_settings
from google.protobuf.message import DecodeError
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ._generated import tool_provider_pb2 as wire
from .providers import CloudManagementExecutor, ProviderFailure
from .settings import ConfigurationError, CredentialMissingError, ProviderSettings
from .tool_catalog import (
    CALL_TOOL_METHOD,
    CALL_TOOL_REQUEST_TYPE_URL,
    CALL_TOOL_RESPONSE_TYPE_URL,
    CANONICAL_TOOLS,
    CAPABILITY_ID,
    LIST_TOOLS_METHOD,
    LIST_TOOLS_REQUEST_TYPE_URL,
    LIST_TOOLS_RESPONSE_TYPE_URL,
    MAX_TOOL_JSON_BYTES,
    MAX_TOOL_REQUEST_BYTES,
    PLUGIN_ID,
    PLUGIN_VERSION,
    TOOL_BY_ID,
    TOOL_OPERATIONS,
)


class CloudManagementProvider:
    """Serve fixed signed Azure, Google Cloud, and Runpod Pod management tools."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: ProviderSettings | None = None,
        executor: CloudManagementExecutor | None = None,
    ) -> None:
        self._settings = settings
        if self._settings is None:
            if read_environment_settings() is None:
                raise ConfigurationError("cloud management activation configuration is required")
            self._settings = ProviderSettings.from_environment()
        self._executor = executor or CloudManagementExecutor(self._settings)

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        cancellation_event: Any = None,
        **_: Any,
    ) -> tuple[bool, Any]:
        """Dispatch the catalog methods and return typed Tool Provider payloads."""

        if capability != CAPABILITY_ID:
            return False, "INVALID_REQUEST: unsupported capability"
        if len(payload) > MAX_TOOL_REQUEST_BYTES:
            return False, "INVALID_REQUEST: tool request exceeds its byte limit"
        try:
            if action == LIST_TOOLS_METHOD:
                if request_type_url not in (None, LIST_TOOLS_REQUEST_TYPE_URL):
                    return False, "INVALID_REQUEST: catalog payload type mismatch"
                return self._list_tools(payload)
            if action == CALL_TOOL_METHOD:
                if request_type_url not in (None, CALL_TOOL_REQUEST_TYPE_URL):
                    return False, "INVALID_REQUEST: tool payload type mismatch"
                return self._call_tool(payload, cancellation_event=cancellation_event)
            return False, "METHOD_NOT_FOUND: unsupported provider method"
        except ConfigurationError:
            return False, "INVALID_CONFIGURATION: cloud management binding is unavailable"

    def close(self) -> None:
        """Close locally constructed API clients during runtime shutdown."""

        self._executor.close()

    def _list_tools(self, payload: bytes) -> tuple[bool, Any]:
        """Return the static provider catalog and no target or credential data."""

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

    def _call_tool(self, payload: bytes, *, cancellation_event: Any = None) -> tuple[bool, Any]:
        """Verify exact actor, workspace, profile, operation, and argument digest."""

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
        definition = TOOL_BY_ID.get(tool_id)
        if definition is None:
            return False, "METHOD_NOT_FOUND: provider tool is not available"
        if not isinstance(envelope, Mapping) or set(envelope) != {"caller_context", "arguments"}:
            return False, "INVALID_REQUEST: signed caller context is required"
        arguments = envelope.get("arguments")
        if not isinstance(arguments, Mapping):
            return True, _tool_error("INVALID_ARGUMENTS")
        try:
            Draft202012Validator(definition["input_schema"]).validate(arguments)
            parsed = parse_trusted_caller_context(envelope.get("caller_context"))
            context = serialize_trusted_caller_context(parsed)
            self._verify_caller(tool_id, arguments, context)
        except ValidationError:
            return True, _tool_error("INVALID_ARGUMENTS")
        except (KeyError, TypeError, ValueError):
            return True, _tool_error("AUTHENTICATION_DENIED")
        try:
            result = self._executor.call(
                tool_id,
                arguments,
                request_id=parsed.request_id,
                cancel_event=(
                    cancellation_event if hasattr(cancellation_event, "is_set") else None
                ),
            )
        except ProviderFailure as error:
            return True, _tool_error(error.code, retryable=error.retryable)
        except CredentialMissingError:
            return True, _tool_error("CREDENTIAL_MISSING")
        except ConfigurationError:
            return True, _tool_error("INVALID_CONFIGURATION")
        except Exception:
            return True, _tool_error("PROVIDER_UNAVAILABLE", retryable=True)
        if isinstance(result, Mapping) and isinstance(result.get("error"), Mapping):
            failure = result["error"]
            return True, _tool_error(
                str(failure.get("code", "PROVIDER_ERROR")),
                retryable=failure.get("retryable") is True,
            )
        try:
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return True, _tool_error("INVALID_PROVIDER_RESPONSE")
        if len(encoded.encode("utf-8")) > MAX_TOOL_JSON_BYTES:
            return True, _tool_error("OUTPUT_LIMIT_EXCEEDED")
        return True, _tool_response(encoded)

    def _verify_caller(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> None:
        """Verify Exchange HMAC and immutable caller/workspace binding pins."""

        settings = self._require_settings()
        caller_workload = context.get("caller_workload")
        if not isinstance(caller_workload, str):
            raise ValueError("caller workload is missing")
        caller = settings.trusted_callers.get(caller_workload)
        if caller is None:
            raise ValueError("caller workload is not trusted")
        valid = verify_context(
            method=tool_id,
            operation_id=TOOL_OPERATIONS[tool_id],
            payload=dict(arguments),
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
            raise ValueError("signed caller context is invalid")

    def _require_settings(self) -> ProviderSettings:
        """Return the validated activation settings or fail closed."""

        if self._settings is None:
            raise ConfigurationError("cloud management activation configuration is unavailable")
        return self._settings


@dataclass(frozen=True, slots=True)
class _TypedPayload:
    """DirectPluginRuntime response body and public type URL."""

    value: bytes
    type_url: str


def _tool_response(value: str) -> _TypedPayload:
    """Wrap one bounded JSON result in the Tool Provider response contract."""

    response = wire.CallToolResponse()
    response.outcome.is_error = False
    response.outcome.content.add().json.json = value
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


def _tool_error(code: str, *, retryable: bool = False) -> _TypedPayload:
    """Return a stable typed error without provider response bodies or secrets."""

    response = wire.CallToolResponse()
    response.outcome.is_error = True
    response.outcome.content.add().json.json = json.dumps(
        {"error": {"code": code, "retryable": retryable}}, separators=(",", ":")
    )
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


def _parse_json(raw: str) -> Any:
    """Parse bounded JSON while rejecting duplicate properties."""

    if len(raw.encode("utf-8")) > MAX_TOOL_JSON_BYTES:
        raise ValueError("tool JSON exceeds its limit")
    return json.loads(raw, object_pairs_hook=_unique_object)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys before validating signatures."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("tool JSON contains a duplicate field")
        result[key] = value
    return result
