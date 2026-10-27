"""Canonical tool.provider.v1 dispatch for signed Azure DevOps observations.

Calls are read-only, project-scoped, lease-backed, and emitted as bounded
structured results. 中文：没有账号授权时明确返回认证或授权错误。
"""

from __future__ import annotations

import hmac
import json
import re
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from cyrene_account_center_contracts import payload_sha256
from cyrene_plugin_runtime.configuration import read_environment_settings
from google.protobuf.message import DecodeError

from . import account_center, cursors
from . import azure_devops_api as ado
from ._generated import tool_provider_pb2 as wire
from .settings import ConfigurationError, ProviderSettings
from .trust import TrustedContextError, account_center_context, verify_exchange_context

CAPABILITY_ID = "tool.provider.v1"
PLUGIN_ID = "cyrene.providers.azure-devops-observation"
PLUGIN_VERSION = "0.1.0"
LIST_TOOLS_METHOD = "list_tools"
CALL_TOOL_METHOD = "call_tool"
LIST_TOOLS_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest"
LIST_TOOLS_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse"
CALL_TOOL_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest"
CALL_TOOL_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse"
MAX_TOOL_JSON_BYTES = 32_768
MAX_PAGE_SIZE = ado.MAX_PAGE_SIZE

_TARGET_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": 128},
        "kind": {
            "type": "string",
            "enum": ["project_pipelines", "pipeline_runs", "release_deployments"],
        },
        "bindingId": {"type": "string", "minLength": 1, "maxLength": 256},
    },
    "required": ["id", "kind", "bindingId"],
    "additionalProperties": False,
}
_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "target": _TARGET_SCHEMA,
        "pageSize": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE},
        "pageCursor": {"type": ["string", "null"], "maxLength": 16_384},
        "windowStart": {"type": ["string", "null"], "maxLength": 64},
        "windowEnd": {"type": "string", "maxLength": 64},
    },
    "required": ["target", "pageSize", "pageCursor", "windowStart", "windowEnd"],
    "additionalProperties": False,
}
_LIST_PIPELINES_SCHEMA = {
    "type": "object",
    "properties": {
        "target": _TARGET_SCHEMA,
        "pageSize": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE},
        "pageCursor": {"type": ["string", "null"], "maxLength": 16_384},
    },
    "required": ["target", "pageSize", "pageCursor"],
    "additionalProperties": False,
}
_GET_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "target": _TARGET_SCHEMA,
        "runId": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647},
    },
    "required": ["target", "runId"],
    "additionalProperties": False,
}
_GET_ENVIRONMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "target": _TARGET_SCHEMA,
        "releaseId": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647},
        "environmentId": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647},
    },
    "required": ["target", "releaseId", "environmentId"],
    "additionalProperties": False,
}

CANONICAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "provider_tool_id": "azure_devops.observations.poll",
        "display_name": "Poll Azure DevOps Observations",
        "description": "Read one configured pipeline-run or release-deployment event page.",
        "input_schema": _PAGE_SCHEMA,
    },
    {
        "provider_tool_id": "azure_devops.pipelines.list",
        "display_name": "List Azure DevOps Pipelines",
        "description": "List pipelines from one configured project with bounded continuation.",
        "input_schema": _LIST_PIPELINES_SCHEMA,
    },
    {
        "provider_tool_id": "azure_devops.pipeline_runs.list",
        "display_name": "List Azure DevOps Pipeline Runs",
        "description": "Read status events from one configured pipeline and time window.",
        "input_schema": _PAGE_SCHEMA,
    },
    {
        "provider_tool_id": "azure_devops.pipeline_run.get",
        "display_name": "Get Azure DevOps Pipeline Run",
        "description": "Read status for one run within a configured pipeline.",
        "input_schema": _GET_RUN_SCHEMA,
    },
    {
        "provider_tool_id": "azure_devops.deployments.list",
        "display_name": "List Azure DevOps Deployments",
        "description": "Read release deployment status within one configured project.",
        "input_schema": _PAGE_SCHEMA,
    },
    {
        "provider_tool_id": "azure_devops.release_environment.get",
        "display_name": "Get Azure DevOps Release Environment",
        "description": "Read status for one environment within one configured release project.",
        "input_schema": _GET_ENVIRONMENT_SCHEMA,
    },
)
_TOOLS_BY_ID = {item["provider_tool_id"]: item for item in CANONICAL_TOOLS}


class SourceScopeDenied(ValueError):
    """The requested target is outside the exact activation allowlist."""


class AzureDevOpsObservationProvider:
    """Serve fixed read-only Azure DevOps tools with signed actor and lease checks."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: ProviderSettings | None = None,
        account_center_client: Any = None,
        api: ado.AzureDevOpsApi | None = None,
    ) -> None:
        self._settings = settings
        loaded_from_environment = False
        if settings is None:
            if read_environment_settings() is None:
                raise ConfigurationError("provider activation configuration is required")
            self._settings = ProviderSettings.from_environment()
            loaded_from_environment = True
        self._account_center = account_center_client
        self._api = api
        self._owns_account_center = False
        if self._account_center is None and loaded_from_environment:
            try:
                self._account_center = account_center.AccountCenterDirectLeaseClient(
                    self._require_settings()
                )
                self._owns_account_center = True
            except account_center.AccountCenterUnavailable:
                raise ConfigurationError(
                    "resolved Account Center connection is unavailable"
                ) from None

    def close(self) -> None:
        """Release provider-owned HTTP and Account Center channels."""

        if self._api is not None:
            self._api.close()
        if self._owns_account_center and self._account_center is not None:
            self._account_center.close()

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        **_: Any,
    ) -> tuple[bool, Any]:
        """Dispatch the canonical provider catalog or one signed tool call."""

        if capability != CAPABILITY_ID:
            return False, "INVALID_REQUEST: unsupported capability"
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
        """Return the fixed tool catalog with closed argument schemas."""

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
            descriptor.provider_tool_id = definition["provider_tool_id"]
            descriptor.display_name = definition["display_name"]
            descriptor.description = definition["description"]
            descriptor.input_schema_json = json.dumps(
                definition["input_schema"], ensure_ascii=False, separators=(",", ":")
            )
        return True, _TypedPayload(response.SerializeToString(), LIST_TOOLS_RESPONSE_TYPE_URL)

    def _call_tool(self, payload: bytes) -> tuple[bool, Any]:
        """Validate signed context and scope before redeeming one provider lease."""

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
        if tool_id not in _TOOLS_BY_ID:
            return False, "METHOD_NOT_FOUND: provider tool is not available"
        if not isinstance(envelope, Mapping) or set(envelope) != {
            "payload",
            "profile_ref",
            "lease_ref",
            "target",
            "arguments_sha256",
            "profile_scope_sha256",
            "trusted_context",
        }:
            return False, "INVALID_REQUEST: signed profile lease is required"
        arguments = envelope.get("payload")
        profile_ref = envelope.get("profile_ref")
        lease_ref = envelope.get("lease_ref")
        supplied_target = envelope.get("target")
        supplied_arguments_digest = envelope.get("arguments_sha256")
        supplied_scope_digest = envelope.get("profile_scope_sha256")
        if (
            not isinstance(arguments, Mapping)
            or not _uuid_text(profile_ref)
            or not _uuid_text(lease_ref)
            or not isinstance(supplied_target, Mapping)
            or not isinstance(supplied_arguments_digest, str)
            or not _sha256_text(supplied_arguments_digest)
            or not isinstance(supplied_scope_digest, str)
            or not _sha256_text(supplied_scope_digest)
        ):
            return False, "INVALID_ARGUMENTS: tool arguments do not match the schema"
        signed_payload = {
            "payload": dict(arguments),
            "profile_ref": profile_ref,
            "lease_ref": lease_ref,
            "target": dict(supplied_target),
            "arguments_sha256": supplied_arguments_digest,
            "profile_scope_sha256": supplied_scope_digest,
        }
        try:
            actor = verify_exchange_context(
                settings,
                signed_payload,
                envelope.get("trusted_context"),
                expected_operation=tool_id,
            )
        except TrustedContextError:
            return True, _tool_error("AUTHENTICATION_DENIED")
        expected_external_subject = _expected_external_subject(settings, actor)
        if expected_external_subject is None:
            return True, _tool_error("TRUSTED_ACTOR_MAPPING_REQUIRED")

        try:
            source, normalized = _validate_arguments(tool_id, arguments, settings, actor)
        except cursors.CursorError:
            return True, _tool_error("INVALID_CURSOR")
        except SourceScopeDenied:
            return True, _tool_error("SOURCE_SCOPE_DENIED")
        except ValueError:
            return True, _tool_error("INVALID_ARGUMENTS")
        if profile_ref != source.profile_ref:
            return True, _tool_error("PROFILE_SCOPE_MISMATCH")

        expected_target = _lease_target(settings, source, tool_id, normalized)
        if dict(supplied_target) != expected_target:
            return True, _tool_error("LEASE_BINDING_MISMATCH")
        try:
            expected_arguments_digest = payload_sha256(dict(arguments))
            expected_profile_scope_digest = payload_sha256(
                _profile_target_scope(settings, source)
            )
        except (TypeError, ValueError):
            return True, _tool_error("INVALID_ARGUMENTS")
        if not hmac.compare_digest(supplied_arguments_digest, expected_arguments_digest):
            return True, _tool_error("LEASE_BINDING_MISMATCH")
        if not hmac.compare_digest(supplied_scope_digest, expected_profile_scope_digest):
            return True, _tool_error("PROFILE_SCOPE_MISMATCH")

        lease_payload = {
            "lease_ref": lease_ref,
            "profile_ref": profile_ref,
            "operation_id": tool_id,
            "target": expected_target,
            "arguments_sha256": expected_arguments_digest,
        }
        if self._account_center is None:
            return True, _tool_error("AUTHORIZATION_UNAVAILABLE", retryable=True)
        try:
            credential = self._account_center.redeem_provider_credential_lease(
                lease_payload,
                account_center_context(settings, actor, lease_payload),
            )
        except account_center.AccountCenterUnavailable:
            return True, _tool_error("AUTHORIZATION_UNAVAILABLE", retryable=True)
        except Exception:
            return True, _tool_error("AUTHORIZATION_UNAVAILABLE", retryable=True)
        access_token = getattr(credential, "access_token", None)
        token_type = getattr(credential, "token_type", None)
        if not isinstance(access_token, str) or not access_token or token_type != "Bearer":
            return True, _tool_error("AUTHORIZATION_UNAVAILABLE", retryable=True)
        if getattr(credential, "external_subject", None) != expected_external_subject:
            return True, _tool_error("TRUSTED_ACTOR_PROFILE_MISMATCH")
        expires_at = getattr(credential, "expires_at", None)
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or expires_at <= int(time.time())
        ):
            return True, _tool_error("AUTH_EXPIRED")
        profile_scope_digest = getattr(credential, "profile_scope_sha256", None)
        if not isinstance(profile_scope_digest, str) or not hmac.compare_digest(
            profile_scope_digest, supplied_scope_digest
        ):
            return True, _tool_error("PROFILE_SCOPE_MISMATCH")

        try:
            result = self._execute(tool_id, settings, source, normalized, access_token)
            return True, _tool_response(result)
        except ado.AzureDevOpsApiFailure as error:
            return True, _tool_error(
                error.code,
                retryable=error.retryable,
                retry_after_seconds=error.retry_after_seconds,
            )
        except (cursors.CursorError, ValueError):
            return True, _tool_error("INVALID_ARGUMENTS")
        except Exception:
            return True, _tool_error("PROVIDER_FAILURE", retryable=True)

    def _execute(
        self,
        tool_id: str,
        settings: ProviderSettings,
        source: Any,
        arguments: Mapping[str, Any],
        token: str,
    ) -> dict[str, Any]:
        """Invoke exactly one fixed read and return its sanitized projection."""

        api = self._require_api()
        if tool_id == "azure_devops.pipelines.list":
            return _list_pipeline_page(api, settings, source, arguments, token)
        if tool_id == "azure_devops.pipeline_runs.list":
            return _list_run_page(api, settings, source, arguments, token)
        if tool_id == "azure_devops.deployments.list":
            return _list_deployment_page(api, settings, source, arguments, token)
        if tool_id == "azure_devops.pipeline_run.get":
            return {"item": api.get_pipeline_run(source, arguments["runId"], token)}
        if tool_id == "azure_devops.release_environment.get":
            return {
                "item": api.get_release_environment(
                    source,
                    arguments["releaseId"],
                    arguments["environmentId"],
                    token,
                )
            }
        if tool_id == "azure_devops.observations.poll":
            if source.kind == "pipeline_runs":
                return _list_run_page(api, settings, source, arguments, token)
            if source.kind == "release_deployments":
                return _list_deployment_page(api, settings, source, arguments, token)
        raise ValueError("tool is not available for the configured source")

    def _require_settings(self) -> ProviderSettings:
        """Return loaded activation settings or load the DirectPluginRuntime env."""

        if self._settings is None:
            self._settings = ProviderSettings.from_environment()
        return self._settings

    def _require_api(self) -> ado.AzureDevOpsApi:
        """Create the fixed-host API client on first read."""

        if self._api is None:
            self._api = ado.AzureDevOpsApi(self._require_settings())
        return self._api


def _validate_arguments(
    tool_id: str,
    arguments: Mapping[str, Any],
    settings: ProviderSettings,
    actor: Mapping[str, str],
) -> tuple[Any, dict[str, Any]]:
    """Validate a closed payload and resolve its exact activation source."""

    if tool_id not in _TOOLS_BY_ID:
        raise ValueError("unknown tool")
    schema = _TOOLS_BY_ID[tool_id]["input_schema"]
    _validate_json_shape(arguments, schema)
    target = arguments.get("target")
    if not isinstance(target, Mapping) or target.get("bindingId") != settings.binding_id:
        raise SourceScopeDenied("binding scope mismatch")
    source_id = target.get("id")
    kind = target.get("kind")
    if not isinstance(source_id, str) or not isinstance(kind, str):
        raise ValueError("target source is invalid")
    try:
        source = settings.source(source_id, kind)
    except ValueError as error:
        raise SourceScopeDenied("source is not allowlisted") from error
    expected_kind = {
        "azure_devops.pipelines.list": "project_pipelines",
        "azure_devops.pipeline_runs.list": "pipeline_runs",
        "azure_devops.pipeline_run.get": "pipeline_runs",
        "azure_devops.deployments.list": "release_deployments",
        "azure_devops.release_environment.get": "release_deployments",
    }.get(tool_id)
    if expected_kind is not None and source.kind != expected_kind:
        raise SourceScopeDenied("tool source kind mismatch")
    if tool_id == "azure_devops.observations.poll" and source.kind == "project_pipelines":
        raise SourceScopeDenied("project inventory is not an event source")

    normalized = dict(arguments)
    normalized["_actor_scope_sha256"] = payload_sha256(
        {key: actor[key] for key in ("issuer", "subject", "organization_id", "workspace_id")}
    )
    if tool_id in {
        "azure_devops.observations.poll",
        "azure_devops.pipeline_runs.list",
        "azure_devops.deployments.list",
    }:
        page_size = arguments.get("pageSize")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_SIZE
        ):
            raise ValueError("page size is invalid")
        window_start = _parse_input_time(arguments.get("windowStart"), optional=True)
        window_end = _parse_input_time(arguments.get("windowEnd"), optional=False)
        if window_start is not None and window_end is not None and window_end <= window_start:
            raise ValueError("poll window is invalid")
        normalized["_window_start"] = window_start
        normalized["_window_end"] = window_end
    if tool_id == "azure_devops.observations.poll" and source.kind not in {
        "pipeline_runs",
        "release_deployments",
    }:
        raise ValueError("source kind cannot be polled")
    if tool_id == "azure_devops.pipelines.list":
        if source.kind != "project_pipelines":
            raise SourceScopeDenied("source kind cannot list pipelines")
        _validate_page_cursor(arguments.get("pageCursor"))
        _decode_scoped_cursor(
            arguments.get("pageCursor"), settings, source, "pipelines", normalized
        )
    elif tool_id in {
        "azure_devops.pipeline_runs.list",
        "azure_devops.deployments.list",
        "azure_devops.observations.poll",
    }:
        _validate_page_cursor(arguments.get("pageCursor"))
        mode = "runs" if source.kind == "pipeline_runs" else "deployments"
        _decode_scoped_cursor(arguments.get("pageCursor"), settings, source, mode, normalized)
    if tool_id in {"azure_devops.pipeline_run.get", "azure_devops.release_environment.get"}:
        for key in ("runId", "releaseId", "environmentId"):
            value = arguments.get(key)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 2_147_483_647
            ):
                raise ValueError("resource identifier is invalid")
    return source, normalized


def _list_pipeline_page(
    api: ado.AzureDevOpsApi,
    settings: ProviderSettings,
    source: Any,
    arguments: Mapping[str, Any],
    token: str,
) -> dict[str, Any]:
    """Fetch and authenticate one pipeline inventory continuation page."""

    cursor = _decode_scoped_cursor(
        arguments.get("pageCursor"), settings, source, "pipelines", arguments
    )
    result = api.list_pipelines(
        source,
        token,
        page_size=arguments["pageSize"],
        continuation_token=cursor.get("vendor_token") if cursor else None,
    )
    next_token = result["next_token"]
    prior_ids = set(cursor.get("seen_ids", [])) if cursor else set()
    items = [item for item in result["items"] if str(item["id"]) not in prior_ids]
    next_cursor = None
    if next_token is not None:
        if cursor is not None and next_token == cursor.get("vendor_token"):
            raise ado.AzureDevOpsApiFailure("UPSTREAM_PAGINATION_STALLED")
        seen_ids = list(prior_ids | {str(item["id"]) for item in items})
        next_cursor = _encode_scoped_cursor(
            settings,
            source,
            "pipelines",
            arguments,
            vendor_token=next_token,
            seen_ids=seen_ids[-2 * arguments["pageSize"] :],
        )
    return _page_response(
        arguments.get("pageCursor"), items, next_cursor, result["complete"], result["truncated"]
    )


def _list_run_page(
    api: ado.AzureDevOpsApi,
    settings: ProviderSettings,
    source: Any,
    arguments: Mapping[str, Any],
    token: str,
) -> dict[str, Any]:
    """Page locally over the v7.1 top-10,000 run result and preserve its bound."""

    cursor = _decode_scoped_cursor(arguments.get("pageCursor"), settings, source, "runs", arguments)
    items, coverage_complete, current_min_id = api.list_pipeline_runs(
        source,
        token,
        window_start=arguments["_window_start"],
        window_end=arguments["_window_end"],
    )
    after_id = 0 if cursor is None else _cursor_integer(cursor, "after_id", minimum=0)
    snapshot_min_id = (
        current_min_id if cursor is None else _cursor_optional_integer(cursor, "snapshot_min_id")
    )
    if (
        cursor is not None
        and snapshot_min_id is not None
        and current_min_id is not None
        and current_min_id > snapshot_min_id
    ):
        coverage_complete = False
    matching = [item for item in items if _event_numeric_id(item["sourceEventId"]) > after_id]
    page = matching[: arguments["pageSize"]]
    if not coverage_complete:
        return _page_response(arguments.get("pageCursor"), page, None, False, True)
    has_more = len(matching) > len(page)
    next_cursor = None
    if has_more and page:
        last_id = _event_numeric_id(page[-1]["sourceEventId"])
        next_cursor = _encode_scoped_cursor(
            settings,
            source,
            "runs",
            arguments,
            after_id=last_id,
            snapshot_min_id=snapshot_min_id,
        )
    complete = not has_more and coverage_complete
    truncated = not has_more and not coverage_complete
    return _page_response(arguments.get("pageCursor"), page, next_cursor, complete, truncated)


def _list_deployment_page(
    api: ado.AzureDevOpsApi,
    settings: ProviderSettings,
    source: Any,
    arguments: Mapping[str, Any],
    token: str,
) -> dict[str, Any]:
    """Fetch one deployment page and skip duplicate IDs from the preceding page."""

    cursor = _decode_scoped_cursor(
        arguments.get("pageCursor"), settings, source, "deployments", arguments
    )
    result = api.list_deployments(
        source,
        token,
        page_size=arguments["pageSize"],
        continuation_token=cursor.get("vendor_token") if cursor else None,
        window_start=arguments["_window_start"],
        window_end=arguments["_window_end"],
    )
    previous_ids = set(cursor.get("seen_ids", [])) if cursor else set()
    items = [item for item in result["items"] if item["sourceEventId"] not in previous_ids]
    next_token = result["next_token"]
    next_cursor = None
    if next_token is not None:
        if cursor is not None and next_token == cursor.get("vendor_token"):
            raise ado.AzureDevOpsApiFailure("UPSTREAM_PAGINATION_STALLED")
        seen_ids = list(previous_ids | {item["sourceEventId"] for item in items})
        next_cursor = _encode_scoped_cursor(
            settings,
            source,
            "deployments",
            arguments,
            vendor_token=next_token,
            seen_ids=seen_ids[-2 * arguments["pageSize"] :],
        )
    return _page_response(
        arguments.get("pageCursor"), items, next_cursor, result["complete"], result["truncated"]
    )


def _encode_scoped_cursor(
    settings: ProviderSettings,
    source: Any,
    mode: str,
    arguments: Mapping[str, Any],
    **extra: Any,
) -> str:
    """Authenticate continuation state to the source, window, and page size."""

    payload = {
        "mode": mode,
        "binding_id": settings.binding_id,
        "tenant_id": settings.tenant_id,
        "source_id": source.source_id,
        "source_kind": source.kind,
        "scope_sha256": _cursor_scope_sha256(settings, source),
        "actor_scope_sha256": arguments["_actor_scope_sha256"],
        "page_size": arguments["pageSize"],
        "window_start": _canonical_input_time(arguments.get("_window_start")),
        "window_end": _canonical_input_time(arguments.get("_window_end")),
        **extra,
    }
    return cursors.encode_cursor(payload, settings.provider_signing_key)


def _decode_scoped_cursor(
    value: Any,
    settings: ProviderSettings,
    source: Any,
    mode: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Verify a signed continuation and reject reuse across scope or windows."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise cursors.CursorError("cursor is invalid")
    cursor = cursors.decode_cursor(value, settings.provider_signing_key)
    expected = {
        "mode": mode,
        "binding_id": settings.binding_id,
        "tenant_id": settings.tenant_id,
        "source_id": source.source_id,
        "source_kind": source.kind,
        "scope_sha256": _cursor_scope_sha256(settings, source),
        "actor_scope_sha256": arguments["_actor_scope_sha256"],
        "page_size": arguments["pageSize"],
        "window_start": _canonical_input_time(arguments.get("_window_start")),
        "window_end": _canonical_input_time(arguments.get("_window_end")),
    }
    if any(cursor.get(key) != expected_value for key, expected_value in expected.items()):
        raise cursors.CursorError("cursor belongs to another scope")
    if mode in {"pipelines", "deployments"}:
        vendor_token = cursor.get("vendor_token")
        seen_ids = cursor.get("seen_ids")
        if (
            not isinstance(vendor_token, str)
            or not vendor_token
            or len(vendor_token) > 128
            or not isinstance(seen_ids, list)
            or len(seen_ids) > 2 * arguments["pageSize"]
            or any(not isinstance(item, str) or len(item) > 128 for item in seen_ids)
        ):
            raise cursors.CursorError("cursor pagination state is invalid")
        if mode == "deployments" and (
            not vendor_token.isdigit()
            or len(vendor_token) > 10
            or int(vendor_token) > 2_147_483_647
        ):
            raise cursors.CursorError("deployment continuation token is invalid")
    elif mode == "runs":
        _cursor_integer(cursor, "after_id", minimum=0)
        _cursor_optional_integer(cursor, "snapshot_min_id")
    return cursor


def _cursor_scope_sha256(settings: ProviderSettings, source: Any) -> str:
    """Bind cursors to the full configured project/profile/resource tuple."""

    return payload_sha256(
        {
            "tenant_id": settings.tenant_id,
            "organization": settings.organization,
            "source_id": source.source_id,
            "source_kind": source.kind,
            "project_id": source.project_id,
            "profile_ref": source.profile_ref,
            "pipeline_id": source.pipeline_id,
            "definition_id": source.definition_id,
            "definition_environment_id": source.definition_environment_id,
        }
    )


def _profile_target_scope(settings: ProviderSettings, source: Any) -> dict[str, Any]:
    """Return the exact source tuple shared with the Account Center profile scope."""

    scope: dict[str, Any] = {
        "tenant_id": settings.tenant_id,
        "organization": settings.organization,
        "project_id": source.project_id,
        "source_id": source.source_id,
        "source_kind": source.kind,
    }
    if source.pipeline_id is not None:
        scope["pipeline_id"] = source.pipeline_id
    if source.definition_id is not None:
        scope["definition_id"] = source.definition_id
    if source.definition_environment_id is not None:
        scope["definition_environment_id"] = source.definition_environment_id
    return scope


def _page_response(
    page_cursor: Any,
    items: list[dict[str, Any]],
    next_cursor: str | None,
    complete: bool,
    truncated: bool,
) -> dict[str, Any]:
    """Return the shared event-page envelope consumed by WorkflowRuntime."""

    return {
        "items": items,
        "pageCursor": page_cursor,
        "nextCursor": next_cursor,
        "complete": bool(complete),
        "truncated": bool(truncated),
    }


def _validate_json_shape(value: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    """Validate closed tool argument shape without an additional runtime dependency."""

    properties = schema["properties"]
    required = set(schema["required"])
    if set(value) - set(properties) or not required.issubset(value):
        raise ValueError("arguments do not match the closed schema")
    for key, item in value.items():
        rule = properties[key]
        if key == "target":
            if not isinstance(item, Mapping):
                raise ValueError("target is invalid")
            _validate_json_shape(item, rule)
            continue
        types = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
        if item is None and "null" in types:
            continue
        if "integer" in types:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError("integer argument is invalid")
            if item < rule.get("minimum", item) or item > rule.get("maximum", item):
                raise ValueError("integer argument is outside its allowed bounds")
        elif "string" in types and not isinstance(item, str):
            raise ValueError("string argument is invalid")
        if isinstance(item, str) and "enum" in rule and item not in rule["enum"]:
            raise ValueError("string argument is outside its allowed values")
        if isinstance(item, str) and len(item) > rule.get("maxLength", 1_000_000):
            raise ValueError("string argument is too long")


def _parse_input_time(value: Any, *, optional: bool) -> datetime | None:
    """Parse an input UTC RFC3339 time while rejecting naive or malformed values."""

    if value is None and optional:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value
    ):
        raise ValueError("time window is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("time window is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("time window must be UTC")
    return parsed.astimezone(UTC)


def _canonical_input_time(value: datetime | None) -> str | None:
    """Normalize optional cursor window times for exact scope binding."""

    return value.isoformat(timespec="microseconds").replace("+00:00", "Z") if value else None


def _validate_page_cursor(value: Any) -> None:
    """Reject oversized cursors before signature verification or lease redemption."""

    if value is not None and (not isinstance(value, str) or len(value) > 16_384):
        raise ValueError("page cursor is invalid")


def _cursor_integer(cursor: Mapping[str, Any], key: str, *, minimum: int) -> int:
    """Read a cursor integer without accepting booleans or out-of-range values."""

    value = cursor.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise cursors.CursorError("cursor integer is invalid")
    return value


def _cursor_optional_integer(cursor: Mapping[str, Any], key: str) -> int | None:
    """Read an optional cursor identifier."""

    value = cursor.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise cursors.CursorError("cursor integer is invalid")
    return value


def _event_numeric_id(source_event_id: str) -> int:
    """Extract the final numeric run identifier from the canonical event key."""

    try:
        return int(source_event_id.rsplit(":", 1)[1])
    except (IndexError, ValueError) as error:
        raise ado.AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE") from error


def _lease_target(
    settings: ProviderSettings,
    source: Any,
    tool_id: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a lease to the activation source and any exact get target."""

    target: dict[str, Any] = {
        "tenant_id": settings.tenant_id,
        "organization": settings.organization,
        "project_id": source.project_id,
        "source_id": source.source_id,
        "source_kind": source.kind,
    }
    if source.pipeline_id is not None:
        target["pipeline_id"] = source.pipeline_id
    if source.definition_id is not None:
        target["definition_id"] = source.definition_id
    if source.definition_environment_id is not None:
        target["definition_environment_id"] = source.definition_environment_id
    if tool_id == "azure_devops.pipeline_run.get":
        target["run_id"] = arguments["runId"]
    if tool_id == "azure_devops.release_environment.get":
        target["release_id"] = arguments["releaseId"]
        target["environment_id"] = arguments["environmentId"]
    return target


def _uuid_text(value: Any) -> bool:
    """Require a canonical UUID string for a profile and one-use lease."""

    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def _sha256_text(value: Any) -> bool:
    """Require one canonical lower-case SHA-256 claim."""

    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _expected_external_subject(
    settings: ProviderSettings,
    actor: Mapping[str, str],
) -> str | None:
    """Map the trusted Entra actor to the immutable Account Center subject."""

    if actor.get("issuer") != f"https://login.microsoftonline.com/{settings.tenant_id}/v2.0":
        return None
    subject = actor.get("subject")
    if not isinstance(subject, str):
        return None
    try:
        canonical_subject = str(uuid.UUID(subject))
    except ValueError:
        return None
    if subject != canonical_subject:
        return None
    return f"{settings.tenant_id}:{canonical_subject}"


def _parse_json(raw: str) -> Any:
    """Decode small invocation JSON while rejecting duplicate keys."""

    if len(raw.encode("utf-8")) > MAX_TOOL_JSON_BYTES:
        raise ValueError("tool JSON exceeds its byte limit")
    return json.loads(raw, object_pairs_hook=_unique_object)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate invocation JSON fields before signature validation."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _tool_response(result: Mapping[str, Any]) -> _TypedPayload:
    """Encode a sanitized JSON result as the canonical provider response."""

    response = wire.CallToolResponse()
    response.outcome.is_error = False
    response.outcome.content.add().json.json = json.dumps(
        result, ensure_ascii=False, separators=(",", ":")
    )
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


def _tool_error(
    code: str,
    *,
    retryable: bool = False,
    retry_after_seconds: int | None = None,
) -> _TypedPayload:
    """Return a stable error code without forwarding upstream response bodies."""

    error: dict[str, Any] = {"code": code, "retryable": retryable}
    if retry_after_seconds is not None:
        error["retry_after_seconds"] = retry_after_seconds
    response = wire.CallToolResponse()
    response.outcome.is_error = True
    response.outcome.content.add().json.json = json.dumps({"error": error}, separators=(",", ":"))
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


class _TypedPayload:
    """Byte payload wrapper consumed by DirectPluginRuntime."""

    def __init__(self, value: bytes, type_url: str) -> None:
        self.value = value
        self.type_url = type_url
