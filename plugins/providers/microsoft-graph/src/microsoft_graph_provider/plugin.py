"""
┌────────────────────────────────────────────────────────────────────┐
│  Fixed Microsoft Graph tool provider                                 │
│  固定 Planner 与选定 SharePoint 工具及严格用户配置租约                │
└────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from cyrene_account_center_contracts import ProviderProfile, payload_sha256
from cyrene_plugin_runtime.configuration import read_environment_settings

from ._generated import tool_provider_pb2 as wire
from .account_center import (
    AccountCenterClient,
    AccountCenterDirectClient,
    AccountCenterUnavailable,
)
from .microsoft_graph_api import (
    GraphApiFailure,
    MicrosoftGraphApi,
    MicrosoftGraphSession,
)
from .settings import ConfigurationError, ProviderSettings
from .trust import (
    LEASE_METHOD,
    TrustedContextError,
    account_center_context,
    verify_exchange_context,
)

CAPABILITY_ID = "tool.provider.v1"
PLUGIN_ID = "cyrene.providers.microsoft-graph"
PLUGIN_VERSION = "0.1.0"
LIST_TOOLS_METHOD = "list_tools"
CALL_TOOL_METHOD = "call_tool"
LIST_TOOLS_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest"
LIST_TOOLS_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse"
CALL_TOOL_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest"
CALL_TOOL_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse"
_PROFILE_REF = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_PLANNER_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_UUID = _PROFILE_REF
_SITE_ID = re.compile(r"[A-Za-z0-9.-]{1,255},[0-9a-fA-F-]{36},[0-9a-fA-F-]{36}\Z")

_PLAN_ARGUMENT = {"plan_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}$"}}
_TASK_ARGUMENTS = {
    "plan_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}$"},
    "task_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}$"},
}
_PAGE_ARGUMENTS = {
    "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
    "page_cursor": {"type": "string", "minLength": 1, "maxLength": 12000},
}
_SITE_ARGUMENT = {
    "site_id": {
        "type": "string",
        "pattern": "^[A-Za-z0-9.-]{1,255},[0-9a-fA-F-]{36},[0-9a-fA-F-]{36}$",
    }
}
_LIST_ARGUMENTS = {
    **_SITE_ARGUMENT,
    "list_id": {
        "type": "string",
        "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    },
}
_UPDATE_FIELDS = {
    "title": {"type": "string", "minLength": 1, "maxLength": 255},
    "due_date_time": {"type": "string", "minLength": 1, "maxLength": 64},
    "start_date_time": {"type": "string", "minLength": 1, "maxLength": 64},
    "percent_complete": {"type": "integer", "minimum": 0, "maximum": 100},
    "priority": {"type": "integer", "minimum": 0, "maximum": 10},
}

CANONICAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "provider_tool_id": "microsoft_graph.planner.plan.get",
        "display_name": "Get Planner Plan Metadata",
        "description": "Read metadata for one plan selected in this Microsoft Graph profile.",
        "input_schema": {
            "type": "object",
            "properties": dict(_PLAN_ARGUMENT),
            "required": ["plan_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.planner.tasks.list",
        "display_name": "List Planner Tasks",
        "description": "Read one bounded page of tasks from one selected Planner plan.",
        "input_schema": {
            "type": "object",
            "properties": {**_PLAN_ARGUMENT, **_PAGE_ARGUMENTS},
            "required": ["plan_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.planner.task.get",
        "display_name": "Get Planner Task",
        "description": "Read one task in the exact selected plan, including its ETag.",
        "input_schema": {
            "type": "object",
            "properties": dict(_TASK_ARGUMENTS),
            "required": ["plan_id", "task_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.planner.task.create",
        "display_name": "Create Planner Task",
        "description": (
            "Create one task in a selected plan and bucket. Ambiguous outcomes are read back "
            "without replay."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_PLAN_ARGUMENT,
                "bucket_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}$"},
                "title": {"type": "string", "minLength": 1, "maxLength": 255},
                "due_date_time": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "required": ["plan_id", "bucket_id", "title"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.planner.task.update",
        "display_name": "Update Planner Task",
        "description": (
            "Update a narrow set of task fields only when the supplied current ETag matches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_TASK_ARGUMENTS,
                "if_match": {"type": "string", "minLength": 1, "maxLength": 1024},
                "changes": {
                    "type": "object",
                    "properties": dict(_UPDATE_FIELDS),
                    "minProperties": 1,
                    "additionalProperties": False,
                },
            },
            "required": ["plan_id", "task_id", "if_match", "changes"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.sharepoint.site.get",
        "display_name": "Get Selected SharePoint Site Metadata",
        "description": "Read minimal metadata for one exact selected SharePoint site.",
        "input_schema": {
            "type": "object",
            "properties": dict(_SITE_ARGUMENT),
            "required": ["site_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.sharepoint.list.get",
        "display_name": "Get Selected SharePoint List Metadata",
        "description": "Read metadata for one list in an exact selected SharePoint site.",
        "input_schema": {
            "type": "object",
            "properties": dict(_LIST_ARGUMENTS),
            "required": ["site_id", "list_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.sharepoint.list.items.delta",
        "display_name": "Read Selected SharePoint List Metadata Changes",
        "description": (
            "Read one bounded delta page of metadata only for a selected SharePoint list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {**_LIST_ARGUMENTS, **_PAGE_ARGUMENTS},
            "required": ["site_id", "list_id"],
            "additionalProperties": False,
        },
    },
)
_TOOLS_BY_ID = {item["provider_tool_id"]: item for item in CANONICAL_TOOLS}
_WRITE_TOOLS = frozenset(
    {
        "microsoft_graph.planner.task.create",
        "microsoft_graph.planner.task.update",
    }
)
_LEASE_TARGET_ARGUMENTS: Mapping[str, frozenset[str]] = {
    "microsoft_graph.planner.plan.get": frozenset({"plan_id"}),
    "microsoft_graph.planner.tasks.list": frozenset({"plan_id"}),
    "microsoft_graph.planner.task.get": frozenset({"plan_id", "task_id"}),
    "microsoft_graph.planner.task.create": frozenset({"plan_id", "bucket_id"}),
    "microsoft_graph.planner.task.update": frozenset({"plan_id", "task_id"}),
    "microsoft_graph.sharepoint.site.get": frozenset({"site_id"}),
    "microsoft_graph.sharepoint.list.get": frozenset({"site_id", "list_id"}),
    "microsoft_graph.sharepoint.list.items.delta": frozenset({"site_id", "list_id"}),
}


class ProfileScopeMismatch(ValueError):
    """The profile digest or identity differs from the Account Center projection."""


class LeaseBindingMismatch(ValueError):
    """The signed Exchange target or projected argument digest is inconsistent."""


class ProfileAuthorizationDenied(ValueError):
    """The exact profile, resource, permission, or user-owned scope is not authorized."""


class TrustedActorMappingMissing(ValueError):
    """The verified Exchange caller has no canonical Microsoft Entra mapping."""


class TrustedActorProfileMismatch(ValueError):
    """The actor's original Entra identity differs from the selected profile."""


class MicrosoftGraphProvider:
    """Serve fixed Graph tools through actor-owned Account Center profile leases."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: ProviderSettings | None = None,
        account_center: AccountCenterClient | None = None,
        graph_api: MicrosoftGraphApi | None = None,
    ) -> None:
        self._settings = settings
        loaded_from_environment = False
        if settings is None:
            if read_environment_settings() is None:
                raise ConfigurationError("provider activation configuration is required")
            self._settings = ProviderSettings.from_environment()
            loaded_from_environment = True
        self._account_center = account_center
        self._graph_api = graph_api
        if self._account_center is None and loaded_from_environment:
            try:
                self._account_center = AccountCenterDirectClient(self._require_settings())
            except AccountCenterUnavailable:
                raise ConfigurationError(
                    "resolved Account Center connection is unavailable"
                ) from None

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        cancellation: Any = None,
        **_: Any,
    ) -> tuple[bool, Any]:
        """Dispatch the fixed catalog or one signed Graph provider tool."""

        del cancellation
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
        """Return the fixed tool schemas, hiding task writes for read-only bindings."""

        request = wire.ListToolsRequest()  # type: ignore[attr-defined]
        try:
            request.ParseFromString(payload)
        except Exception:
            return False, "INVALID_REQUEST: catalog request is malformed"
        settings = self._require_settings()
        if request.HasField("binding_id") and request.binding_id != settings.binding_id:
            return False, "INVALID_REQUEST: binding ID does not match"
        response = wire.ListToolsResponse()  # type: ignore[attr-defined]
        response.catalog.catalog_version = PLUGIN_VERSION
        for definition in CANONICAL_TOOLS:
            tool_id = str(definition["provider_tool_id"])
            if settings.read_only and tool_id in _WRITE_TOOLS:
                continue
            descriptor = response.catalog.tools.add()
            descriptor.binding_id = settings.binding_id
            descriptor.provider_tool_id = tool_id
            descriptor.display_name = str(definition["display_name"])
            descriptor.description = str(definition["description"])
            descriptor.input_schema_json = json.dumps(
                definition["input_schema"], ensure_ascii=False, separators=(",", ":")
            )
        return True, _TypedPayload(response.SerializeToString(), LIST_TOOLS_RESPONSE_TYPE_URL)

    def _call_tool(self, payload: bytes) -> tuple[bool, Any]:
        """Validate scope, redeem one lease, verify `/me`, then call a fixed route."""

        request = wire.CallToolRequest()  # type: ignore[attr-defined]
        try:
            request.ParseFromString(payload)
            envelope = _parse_json(request.arguments_json)
        except (ValueError, TypeError):
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
            return False, "INVALID_REQUEST: signed user profile lease is required"
        arguments = envelope.get("payload")
        profile_ref = envelope.get("profile_ref")
        lease_ref = envelope.get("lease_ref")
        supplied_target = envelope.get("target")
        supplied_arguments_digest = envelope.get("arguments_sha256")
        supplied_scope_digest = envelope.get("profile_scope_sha256")
        trusted_context = envelope.get("trusted_context")
        if (
            not isinstance(arguments, Mapping)
            or not isinstance(profile_ref, str)
            or not _PROFILE_REF.fullmatch(profile_ref)
            or not isinstance(lease_ref, str)
            or not _PROFILE_REF.fullmatch(lease_ref)
            or not isinstance(supplied_target, Mapping)
            or not isinstance(supplied_arguments_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", supplied_arguments_digest)
            or not isinstance(supplied_scope_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", supplied_scope_digest)
            or not isinstance(trusted_context, Mapping)
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
            actor = verify_exchange_context(settings, signed_payload, trusted_context)
            if trusted_context.get("operation_id") != tool_id:
                raise TrustedContextError("trusted operation does not match provider tool")
            trusted_tenant_id, trusted_object_id = _trusted_entra_identity(actor)
            trusted_external_subject = f"{trusted_tenant_id}:{trusted_object_id}"
            if settings.read_only and tool_id in _WRITE_TOOLS:
                return True, _tool_error("READ_ONLY_PROFILE")
            if not _valid_arguments(tool_id, arguments, settings):
                return False, "INVALID_ARGUMENTS: tool arguments do not match the schema"
            account_center = self._require_account_center()
            profile = _find_actor_profile(account_center.list_provider_profiles(actor), profile_ref)
            scope = _profile_scope(profile)
            _authorize_profile(
                scope,
                tool_id,
                arguments,
                profile,
                settings=settings,
                expected_external_subject=trusted_external_subject,
            )
            target = _target(scope, tool_id, arguments)
            operation_digest = payload_sha256(_arguments_for_lease(tool_id, arguments))
            scope_digest = payload_sha256(scope)
            if dict(supplied_target) != target or not hmac.compare_digest(
                supplied_arguments_digest, operation_digest
            ):
                raise LeaseBindingMismatch("signed Graph target or argument digest differs")
            if not hmac.compare_digest(supplied_scope_digest, scope_digest):
                raise ProfileScopeMismatch(
                    "Exchange Graph scope digest differs from Account Center"
                )
            lease_payload = {
                "lease_ref": lease_ref,
                "profile_ref": profile_ref,
                "operation_id": tool_id,
                "target": dict(supplied_target),
                "arguments_sha256": supplied_arguments_digest,
            }
            credential = account_center.redeem_provider_credential_lease(
                lease_payload,
                account_center_context(
                    settings,
                    actor,
                    lease_payload,
                    method=LEASE_METHOD,
                ),
            )
            _verify_credential(
                profile,
                scope,
                credential,
                expected_external_subject=trusted_external_subject,
            )
            session = self._require_graph_api().open_session(
                credential.access_token,
                credential.external_subject,
                binding_id=settings.binding_id,
                profile_ref=profile_ref,
            )
            result = _run_tool(session, tool_id, arguments, settings)
            return True, _tool_response(result)
        except TrustedContextError:
            return True, _tool_error("AUTHENTICATION_DENIED")
        except TrustedActorMappingMissing:
            return True, _tool_error("TRUSTED_ACTOR_MAPPING_REQUIRED")
        except TrustedActorProfileMismatch:
            return True, _tool_error("TRUSTED_ACTOR_PROFILE_MISMATCH")
        except LeaseBindingMismatch:
            return True, _tool_error("LEASE_BINDING_MISMATCH")
        except AccountCenterUnavailable:
            return True, _tool_error("AUTHORIZATION_UNAVAILABLE", retryable=True)
        except ProfileScopeMismatch:
            return True, _tool_error("PROFILE_SCOPE_MISMATCH")
        except ProfileAuthorizationDenied:
            return True, _tool_error("AUTHORIZATION_DENIED")
        except GraphApiFailure as error:
            return True, _tool_error(
                error.code,
                retryable=error.retryable,
                retry_after=error.retry_after,
                uncertain=error.uncertain,
            )
        except (ValueError, TypeError):
            return True, _tool_error("INVALID_ARGUMENTS")
        except Exception:
            return True, _tool_error("PROVIDER_FAILURE", uncertain=tool_id in _WRITE_TOOLS)

    def _require_settings(self) -> ProviderSettings:
        """Return host activation settings or load the standard environment object."""

        if self._settings is None:
            self._settings = ProviderSettings.from_environment()
        return self._settings

    def _require_account_center(self) -> AccountCenterClient:
        """Construct the fixed private Account Center client on first use."""

        if self._account_center is None:
            self._account_center = AccountCenterDirectClient(self._require_settings())
        return self._account_center

    def _require_graph_api(self) -> MicrosoftGraphApi:
        """Construct the fixed-host Graph API client with a host signing key for cursors."""

        if self._graph_api is None:
            settings = self._require_settings()
            self._graph_api = MicrosoftGraphApi(
                cursor_key=hmac.new(
                    settings.provider_signing_key,
                    b"cyrene.microsoft-graph.cursor.v1",
                    hashlib.sha256,
                ).digest(),
                timeout_seconds=settings.timeout_seconds,
            )
        return self._graph_api

    def on_shutdown(self, timeout_ms: int) -> None:
        """Close the private Account Center channel during runtime shutdown."""

        del timeout_ms
        close = getattr(self._account_center, "close", None)
        if callable(close):
            close()


def _valid_arguments(
    tool_id: str,
    arguments: Mapping[str, Any],
    settings: ProviderSettings,
) -> bool:
    """Enforce the closed tool schemas and local bounds before lease redemption."""

    schema = _TOOLS_BY_ID[tool_id]["input_schema"]
    fields = set(schema["properties"])
    required = set(schema.get("required", []))
    if not required.issubset(arguments) or set(arguments) - fields:
        return False
    if tool_id in {
        "microsoft_graph.planner.plan.get",
        "microsoft_graph.planner.tasks.list",
    } and not _valid_planner_id(arguments.get("plan_id")):
        return False
    if tool_id in {
        "microsoft_graph.planner.task.get",
        "microsoft_graph.planner.task.update",
    } and not all(_valid_planner_id(arguments.get(name)) for name in ("plan_id", "task_id")):
        return False
    if tool_id == "microsoft_graph.planner.task.create":
        title = arguments.get("title")
        if (
            not _valid_planner_id(arguments.get("plan_id"))
            or not _valid_planner_id(arguments.get("bucket_id"))
            or not isinstance(title, str)
            or not 1 <= len(title) <= 255
            or not title.strip()
            or any(ord(char) < 32 for char in title)
        ):
            return False
    if tool_id == "microsoft_graph.planner.task.update" and (
        not _valid_etag(arguments.get("if_match")) or not _valid_changes(arguments.get("changes"))
    ):
        return False
    if tool_id.startswith("microsoft_graph.sharepoint.") and not _valid_site_id(
        arguments.get("site_id")
    ):
        return False
    if tool_id in {
        "microsoft_graph.sharepoint.list.get",
        "microsoft_graph.sharepoint.list.items.delta",
    } and not _valid_uuid(arguments.get("list_id")):
        return False
    if (tool_id.endswith(".tasks.list") or tool_id.endswith(".list.items.delta")) and not (
        _valid_page_arguments(arguments, settings.max_page_size)
    ):
        return False
    return not (
        tool_id == "microsoft_graph.planner.task.create"
        and "due_date_time" in arguments
        and not _valid_datetime_text(arguments["due_date_time"])
    )


def _find_actor_profile(
    profiles: tuple[ProviderProfile, ...],
    profile_ref: str,
) -> ProviderProfile:
    """Require exactly one actor-owned, healthy Graph profile for the signed reference."""

    matches = [
        profile
        for profile in profiles
        if profile.profile_ref == profile_ref and profile.provider == "microsoft_graph"
    ]
    if len(matches) != 1:
        raise ProfileAuthorizationDenied("Graph profile is unavailable to this actor")
    profile = matches[0]
    if (
        profile.auth_mode != "delegated_oauth"
        or profile.auth_status != "healthy"
        or profile.access_mode != "actor_owned"
        or not isinstance(profile.external_subject, str)
        or profile.identity_ref != profile.external_subject
    ):
        raise ProfileAuthorizationDenied("Graph profile is not healthy and actor-owned")
    return profile


def _profile_scope(profile: ProviderProfile) -> dict[str, Any]:
    """Validate the exact Account Center Graph scope without expanding it locally."""

    scope = profile.target_scope
    if not isinstance(scope, Mapping) or set(scope) != {"tenant_id", "planner", "sharepoint"}:
        raise ProfileScopeMismatch("profile Graph scope shape differs")
    tenant_id = scope.get("tenant_id")
    planner = scope.get("planner")
    sharepoint = scope.get("sharepoint")
    if (
        not _valid_uuid(tenant_id)
        or not isinstance(planner, Mapping)
        or not isinstance(sharepoint, Mapping)
    ):
        raise ProfileScopeMismatch("profile Graph scope is invalid")
    if set(planner) != {"plan_ids", "tasks_read", "tasks_write"}:
        raise ProfileScopeMismatch("Planner scope shape differs")
    plan_ids = planner.get("plan_ids")
    tasks_read = planner.get("tasks_read")
    tasks_write = planner.get("tasks_write")
    if (
        not isinstance(plan_ids, list)
        or not 1 <= len(plan_ids) <= 100
        or any(not _valid_planner_id(item) for item in plan_ids)
        or len(plan_ids) != len(set(plan_ids))
        or not isinstance(tasks_read, bool)
        or not isinstance(tasks_write, bool)
        or (tasks_write and not tasks_read)
    ):
        raise ProfileScopeMismatch("Planner scope values are invalid")
    if set(sharepoint) != {"enabled", "site_id", "list_ids"}:
        raise ProfileScopeMismatch("SharePoint scope shape differs")
    enabled = sharepoint.get("enabled")
    site_id = sharepoint.get("site_id")
    list_ids = sharepoint.get("list_ids")
    if (
        not isinstance(enabled, bool)
        or not isinstance(list_ids, list)
        or len(list_ids) > 100
        or any(not _valid_uuid(item) for item in list_ids)
        or len(list_ids) != len(set(list_ids))
        or (enabled and (not _valid_site_id(site_id) or not list_ids))
        or (not enabled and (site_id is not None or list_ids))
    ):
        raise ProfileScopeMismatch("SharePoint scope values are invalid")
    return dict(scope)


def _authorize_profile(
    scope: Mapping[str, Any],
    tool_id: str,
    arguments: Mapping[str, Any],
    profile: ProviderProfile,
    *,
    settings: ProviderSettings,
    expected_external_subject: str,
) -> None:
    """Require exact per-user target membership and the selected operation scope."""

    external_subject = profile.external_subject
    if not isinstance(external_subject, str):
        raise ProfileAuthorizationDenied("Graph profile identity is invalid")
    if external_subject != expected_external_subject:
        raise TrustedActorProfileMismatch("Graph profile differs from the signed Entra actor")
    try:
        tenant_id, subject_object_id = external_subject.split(":", 1)
    except ValueError:
        raise ProfileAuthorizationDenied("Graph profile identity is invalid") from None
    if not _valid_uuid(tenant_id) or not _valid_uuid(subject_object_id):
        raise ProfileAuthorizationDenied("Graph profile identity is invalid")
    if (
        f"{tenant_id}:{subject_object_id}" != expected_external_subject
        or scope.get("tenant_id") != tenant_id
    ):
        raise TrustedActorProfileMismatch("Graph profile differs from the signed Entra actor")
    oauth_scopes = profile.oauth_scopes
    if (
        not isinstance(oauth_scopes, list)
        or any(not isinstance(item, str) for item in oauth_scopes)
        or len(oauth_scopes) != len(set(oauth_scopes))
    ):
        raise ProfileScopeMismatch("Graph delegated scopes are invalid")
    expected_scopes = {"User.Read", "Tasks.Read"}
    if scope["planner"]["tasks_write"]:
        expected_scopes.add("Tasks.ReadWrite")
    if scope["sharepoint"]["enabled"]:
        expected_scopes.add("Sites.Selected")
    if set(oauth_scopes) != expected_scopes:
        raise ProfileScopeMismatch("Graph delegated scopes exceed the selected profile")
    sharepoint = scope["sharepoint"]
    if sharepoint["enabled"] and (
        settings.sharepoint_site_id is None
        or sharepoint["site_id"] != settings.sharepoint_site_id
        or tuple(sharepoint["list_ids"]) != settings.sharepoint_list_ids
    ):
        raise ProfileScopeMismatch(
            "Account Center SharePoint scope differs from the host binding allowlist"
        )
    if tool_id.startswith("microsoft_graph.planner."):
        planner = scope["planner"]
        if not planner["tasks_read"]:
            raise ProfileAuthorizationDenied("Planner read permission is not granted")
        plan_id = arguments.get("plan_id")
        if plan_id not in planner["plan_ids"]:
            raise ProfileAuthorizationDenied("Planner plan is not selected")
        if tool_id in _WRITE_TOOLS and not planner["tasks_write"]:
            raise ProfileAuthorizationDenied("Planner write permission is not granted")
        return
    if not sharepoint["enabled"] or sharepoint["site_id"] != arguments.get("site_id"):
        raise ProfileAuthorizationDenied("SharePoint site is not selected")
    if tool_id != "microsoft_graph.sharepoint.site.get" and (
        arguments.get("list_id") not in sharepoint["list_ids"]
    ):
        raise ProfileAuthorizationDenied("SharePoint list is not selected")


def _target(
    scope: Mapping[str, Any],
    tool_id: str,
    arguments: Mapping[str, Any],
) -> dict[str, str]:
    """Build the exact Account Center resource target for the selected operation."""

    target = {"tenant_id": str(scope["tenant_id"])}
    if tool_id in {
        "microsoft_graph.planner.plan.get",
        "microsoft_graph.planner.tasks.list",
    }:
        return {**target, "plan_id": str(arguments["plan_id"])}
    if tool_id in {
        "microsoft_graph.planner.task.get",
        "microsoft_graph.planner.task.update",
    }:
        return {
            **target,
            "plan_id": str(arguments["plan_id"]),
            "task_id": str(arguments["task_id"]),
        }
    if tool_id == "microsoft_graph.planner.task.create":
        return {
            **target,
            "plan_id": str(arguments["plan_id"]),
            "bucket_id": str(arguments["bucket_id"]),
        }
    if tool_id == "microsoft_graph.sharepoint.site.get":
        return {**target, "site_id": str(arguments["site_id"])}
    return {
        **target,
        "site_id": str(arguments["site_id"]),
        "list_id": str(arguments["list_id"]),
    }


def _arguments_for_lease(
    tool_id: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Exclude only resource selectors already bound by the signed lease target."""

    target_fields = _LEASE_TARGET_ARGUMENTS.get(tool_id)
    if target_fields is None:
        raise GraphApiFailure("INVALID_OPERATION")
    return {key: value for key, value in arguments.items() if key not in target_fields}


def _verify_credential(
    profile: ProviderProfile,
    scope: Mapping[str, Any],
    credential: Any,
    *,
    expected_external_subject: str,
) -> None:
    """Bind the one-use token to the exact external identity and profile scope."""

    if (
        getattr(credential, "token_type", None) != "Bearer"
        or not isinstance(getattr(credential, "access_token", None), str)
        or not credential.access_token
        or not isinstance(profile.external_subject, str)
        or getattr(credential, "external_subject", None) != profile.external_subject
        or getattr(credential, "external_subject", None) != expected_external_subject
    ):
        raise TrustedActorProfileMismatch("Redeemed Graph identity differs from the signed actor")
    expires_at = getattr(credential, "expires_at", None)
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at <= int(time.time()) + 5
    ):
        raise AccountCenterUnavailable("Account Center token is expired or expiring")
    digest = getattr(credential, "profile_scope_sha256", None)
    try:
        expected = payload_sha256(dict(scope))
    except (TypeError, ValueError):
        raise ProfileScopeMismatch("profile Graph scope cannot be hashed") from None
    if not isinstance(digest, str) or not hmac.compare_digest(digest, expected):
        raise ProfileScopeMismatch("Account Center profile scope digest differs")


def _run_tool(
    session: MicrosoftGraphSession,
    tool_id: str,
    arguments: Mapping[str, Any],
    settings: ProviderSettings,
) -> dict[str, Any]:
    """Dispatch one allowlisted operation to one exact Graph v1.0 endpoint."""

    if tool_id == "microsoft_graph.planner.plan.get":
        return session.get_plan(str(arguments["plan_id"]))
    if tool_id == "microsoft_graph.planner.tasks.list":
        return session.list_tasks(
            str(arguments["plan_id"]),
            page_size=int(arguments.get("page_size", settings.max_page_size)),
            page_cursor=str(arguments["page_cursor"]) if "page_cursor" in arguments else None,
        )
    if tool_id == "microsoft_graph.planner.task.get":
        return session.get_task(str(arguments["plan_id"]), str(arguments["task_id"]))
    if tool_id == "microsoft_graph.planner.task.create":
        return session.create_task(
            str(arguments["plan_id"]),
            str(arguments["bucket_id"]),
            str(arguments["title"]),
            due_date_time=(
                str(arguments["due_date_time"]) if "due_date_time" in arguments else None
            ),
        )
    if tool_id == "microsoft_graph.planner.task.update":
        changes = arguments["changes"]
        if not isinstance(changes, Mapping):
            raise GraphApiFailure("INVALID_ARGUMENTS")
        return session.update_task(
            str(arguments["plan_id"]),
            str(arguments["task_id"]),
            str(arguments["if_match"]),
            changes,
        )
    if tool_id == "microsoft_graph.sharepoint.site.get":
        return session.get_site(str(arguments["site_id"]))
    if tool_id == "microsoft_graph.sharepoint.list.get":
        return session.get_list(str(arguments["site_id"]), str(arguments["list_id"]))
    return session.list_items_delta(
        str(arguments["site_id"]),
        str(arguments["list_id"]),
        page_size=int(arguments.get("page_size", settings.max_page_size)),
        page_cursor=str(arguments["page_cursor"]) if "page_cursor" in arguments else None,
    )


def _valid_page_arguments(arguments: Mapping[str, Any], maximum: int) -> bool:
    page_size = arguments.get("page_size", maximum)
    page_cursor = arguments.get("page_cursor")
    return (
        not isinstance(page_size, bool)
        and isinstance(page_size, int)
        and 1 <= page_size <= maximum
        and (
            "page_cursor" not in arguments
            or isinstance(page_cursor, str)
            and 1 <= len(page_cursor) <= 12_000
        )
    )


def _valid_changes(value: Any) -> bool:
    if not isinstance(value, Mapping) or not value or set(value) - set(_UPDATE_FIELDS):
        return False
    if "title" in value:
        title = value["title"]
        if (
            not isinstance(title, str)
            or not 1 <= len(title) <= 255
            or not title.strip()
            or any(ord(char) < 32 for char in title)
        ):
            return False
    for field in ("due_date_time", "start_date_time"):
        if field in value:
            item = value[field]
            if not _valid_datetime_text(item):
                return False
    for field, minimum, maximum in (("percent_complete", 0, 100), ("priority", 0, 10)):
        if field in value:
            item = value[field]
            if (
                isinstance(item, bool)
                or not isinstance(item, int)
                or not minimum <= item <= maximum
            ):
                return False
    return True


def _valid_datetime_text(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)


def _valid_etag(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 1024
        and value.endswith('"')
        and (value.startswith('W/"') or value.startswith('"'))
        and not any(char in value for char in "\r\n\x00")
    )


def _valid_planner_id(value: Any) -> bool:
    return isinstance(value, str) and _PLANNER_ID.fullmatch(value) is not None


def _valid_site_id(value: Any) -> bool:
    return isinstance(value, str) and _SITE_ID.fullmatch(value) is not None


def _valid_uuid(value: Any) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _trusted_entra_identity(actor: Mapping[str, str | None]) -> tuple[str, str]:
    """Require the signed caller anchor itself to be the original canonical Entra user."""

    issuer = actor.get("issuer")
    subject = actor.get("subject")
    if not isinstance(issuer, str) or not isinstance(subject, str):
        raise TrustedActorMappingMissing("signed caller has no Entra identity mapping")
    match = re.fullmatch(
        r"https://login\.microsoftonline\.com/([0-9a-f]{8}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/v2\.0",
        issuer,
    )
    if match is None or not _valid_uuid(subject):
        raise TrustedActorMappingMissing("signed caller has no canonical Entra identity mapping")
    return match.group(1), subject


def _parse_json(payload: bytes | str) -> Any:
    """Parse JSON while rejecting duplicate keys before trust decisions."""

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(payload, object_pairs_hook=unique)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise ValueError("invalid JSON") from None


def _tool_response(result: Mapping[str, Any]) -> Any:
    """Encode one JSON result with no credential or private profile material."""

    response = wire.CallToolResponse()  # type: ignore[attr-defined]
    response.outcome.is_error = False
    response.outcome.content.add().json.json = json.dumps(
        result, ensure_ascii=False, separators=(",", ":")
    )
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


def _tool_error(
    code: str,
    *,
    retryable: bool = False,
    retry_after: int | None = None,
    uncertain: bool = False,
) -> Any:
    """Return a structured, sanitized provider error."""

    error: dict[str, Any] = {"code": code, "retryable": retryable}
    if retry_after is not None:
        error["retry_after_seconds"] = retry_after
    if uncertain:
        error["outcome"] = "UNKNOWN"
    response = wire.CallToolResponse()  # type: ignore[attr-defined]
    response.outcome.is_error = True
    response.outcome.content.add().json.json = json.dumps({"error": error}, separators=(",", ":"))
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


class _TypedPayload:
    """Byte response wrapper consumed by DirectPluginRuntime."""

    def __init__(self, value: bytes, type_url: str) -> None:
        self.value = value
        self.type_url = type_url
