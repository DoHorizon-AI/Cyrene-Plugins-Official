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
from datetime import UTC, datetime, timedelta
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
PLUGIN_VERSION = "0.2.0"
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
_OPAQUE_ID = {"type": "string", "pattern": "^[A-Za-z0-9._!~+=-]{1,512}$"}
_OFFSET_DATE_TIME = {"type": "string", "minLength": 20, "maxLength": 64}
_ETAG = {"type": "string", "minLength": 1, "maxLength": 1024}
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
        "provider_tool_id": "microsoft_graph.planner.plans.list",
        "display_name": "List Planner Plans",
        "description": (
            "Discover plan metadata visible to the signed-in user for explicit selection."
        ),
        "input_schema": {
            "type": "object",
            "properties": dict(_PAGE_ARGUMENTS),
            "additionalProperties": False,
        },
    },
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
    {
        "provider_tool_id": "microsoft_graph.todo.lists.list",
        "display_name": "List Microsoft To Do Lists",
        "description": (
            "Read one complete server-sized metadata page for the signed-in user's To Do lists; "
            "the Graph service controls page length and returns a signed continuation cursor. "
            "page_size is validated and bound to the cursor but does not limit the upstream page."
        ),
        "input_schema": {
            "type": "object",
            "properties": dict(_PAGE_ARGUMENTS),
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.todo.tasks.list",
        "display_name": "List Microsoft To Do Tasks",
        "description": "Read bounded task metadata from one selected To Do list.",
        "input_schema": {
            "type": "object",
            "properties": {"task_list_id": dict(_OPAQUE_ID), **_PAGE_ARGUMENTS},
            "required": ["task_list_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.todo.task.get",
        "display_name": "Get Microsoft To Do Task",
        "description": "Read one task's metadata and ETag from a selected list.",
        "input_schema": {
            "type": "object",
            "properties": {"task_list_id": dict(_OPAQUE_ID), "task_id": dict(_OPAQUE_ID)},
            "required": ["task_list_id", "task_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.todo.task.create",
        "display_name": "Create Microsoft To Do Task",
        "description": "Create one task in a selected list; ambiguous outcomes are never replayed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_list_id": dict(_OPAQUE_ID),
                "title": {"type": "string", "minLength": 1, "maxLength": 255},
                "due_date_time": _OFFSET_DATE_TIME,
                "importance": {"enum": ["low", "normal", "high"]},
            },
            "required": ["task_list_id", "title"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.todo.task.update",
        "display_name": "Update Microsoft To Do Task",
        "description": "Update a narrow set of fields only when the supplied ETag matches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_list_id": dict(_OPAQUE_ID),
                "task_id": dict(_OPAQUE_ID),
                "if_match": dict(_ETAG),
                "changes": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 255},
                        "due_date_time": _OFFSET_DATE_TIME,
                        "status": {
                            "enum": [
                                "notStarted",
                                "inProgress",
                                "completed",
                                "waitingOnOthers",
                                "deferred",
                            ]
                        },
                        "importance": {"enum": ["low", "normal", "high"]},
                    },
                    "minProperties": 1,
                    "additionalProperties": False,
                },
            },
            "required": ["task_list_id", "task_id", "if_match", "changes"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.todo.task.delete",
        "display_name": "Delete Microsoft To Do Task",
        "description": "Delete one selected task only when its current ETag matches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_list_id": dict(_OPAQUE_ID),
                "task_id": dict(_OPAQUE_ID),
                "if_match": dict(_ETAG),
            },
            "required": ["task_list_id", "task_id", "if_match"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.calendar.calendars.list",
        "display_name": "List Microsoft Calendars",
        "description": "Read bounded calendar metadata for the signed-in user.",
        "input_schema": {
            "type": "object",
            "properties": dict(_PAGE_ARGUMENTS),
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.calendar.events.list",
        "display_name": "List Microsoft Calendar Events",
        "description": "Read event metadata in an exact UTC-normalized calendar range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "calendar_id": dict(_OPAQUE_ID),
                "start_date_time": _OFFSET_DATE_TIME,
                "end_date_time": _OFFSET_DATE_TIME,
                **_PAGE_ARGUMENTS,
            },
            "required": ["calendar_id", "start_date_time", "end_date_time"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.calendar.event.get",
        "display_name": "Get Microsoft Calendar Event",
        "description": "Read one event projection without body or attendee identities.",
        "input_schema": {
            "type": "object",
            "properties": {"calendar_id": dict(_OPAQUE_ID), "event_id": dict(_OPAQUE_ID)},
            "required": ["calendar_id", "event_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.calendar.freebusy.get",
        "display_name": "Get Microsoft Calendar Availability",
        "description": "Read availability slots for explicitly selected schedules only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "schedules": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 3, "maxLength": 320},
                },
                "start_date_time": _OFFSET_DATE_TIME,
                "end_date_time": _OFFSET_DATE_TIME,
                "interval_minutes": {"enum": [5, 10, 15, 30, 60]},
            },
            "required": ["schedules", "start_date_time", "end_date_time", "interval_minutes"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.calendar.event.create",
        "display_name": "Create Microsoft Calendar Event",
        "description": "Create one event with no attendees and a caller-provided transaction ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "calendar_id": dict(_OPAQUE_ID),
                "subject": {"type": "string", "minLength": 1, "maxLength": 255},
                "start_date_time": _OFFSET_DATE_TIME,
                "end_date_time": _OFFSET_DATE_TIME,
                "transaction_id": {"type": "string", "format": "uuid"},
                "location": {"type": "string", "minLength": 1, "maxLength": 255},
            },
            "required": [
                "calendar_id",
                "subject",
                "start_date_time",
                "end_date_time",
                "transaction_id",
            ],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.calendar.event.update",
        "display_name": "Update Microsoft Calendar Event",
        "description": "Update one event without attendee changes and with ETag protection.",
        "input_schema": {
            "type": "object",
            "properties": {
                "calendar_id": dict(_OPAQUE_ID),
                "event_id": dict(_OPAQUE_ID),
                "if_match": dict(_ETAG),
                "changes": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string", "minLength": 1, "maxLength": 255},
                        "start_date_time": _OFFSET_DATE_TIME,
                        "end_date_time": _OFFSET_DATE_TIME,
                        "location": {"type": "string", "minLength": 1, "maxLength": 255},
                    },
                    "minProperties": 1,
                    "additionalProperties": False,
                },
            },
            "required": ["calendar_id", "event_id", "if_match", "changes"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.calendar.event.delete",
        "display_name": "Delete Microsoft Calendar Event",
        "description": "Delete an event only if it has no attendees and its ETag matches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "calendar_id": dict(_OPAQUE_ID),
                "event_id": dict(_OPAQUE_ID),
                "if_match": dict(_ETAG),
            },
            "required": ["calendar_id", "event_id", "if_match"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.sharepoint.drives.list",
        "display_name": "List Selected SharePoint Document Libraries",
        "description": "Read document-library metadata for one selected site.",
        "input_schema": {
            "type": "object",
            "properties": {**_SITE_ARGUMENT, **_PAGE_ARGUMENTS},
            "required": ["site_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "microsoft_graph.sharepoint.drive.items.list",
        "display_name": "List Selected SharePoint Library Items",
        "description": "Read item metadata and original links only; file content is never fetched.",
        "input_schema": {
            "type": "object",
            "properties": {
                **_SITE_ARGUMENT,
                "drive_id": dict(_OPAQUE_ID),
                "parent_item_id": dict(_OPAQUE_ID),
                **_PAGE_ARGUMENTS,
            },
            "required": ["site_id", "drive_id"],
            "additionalProperties": False,
        },
    },
)
_TOOLS_BY_ID = {item["provider_tool_id"]: item for item in CANONICAL_TOOLS}
_V2_TOOLS = frozenset(
    item["provider_tool_id"]
    for item in CANONICAL_TOOLS
    if item["provider_tool_id"].startswith(
        ("microsoft_graph.todo.", "microsoft_graph.calendar.", "microsoft_graph.sharepoint.drive")
    )
) | {"microsoft_graph.planner.plans.list"}
_V2_WRITE_TOOLS = frozenset(
    {
        "microsoft_graph.todo.task.create",
        "microsoft_graph.todo.task.update",
        "microsoft_graph.todo.task.delete",
        "microsoft_graph.calendar.event.create",
        "microsoft_graph.calendar.event.update",
        "microsoft_graph.calendar.event.delete",
    }
)
_WRITE_TOOLS = frozenset(
    {
        "microsoft_graph.planner.task.create",
        "microsoft_graph.planner.task.update",
    }
    | set(_V2_WRITE_TOOLS)
)
_TOOL_FEATURES = {
    "microsoft_graph.planner.plans.list": "planner_enabled",
    **{
        tool_id: "todo_enabled"
        for tool_id in _V2_TOOLS
        if tool_id.startswith("microsoft_graph.todo.")
    },
    **{
        tool_id: "calendar_enabled"
        for tool_id in _V2_TOOLS
        if tool_id.startswith("microsoft_graph.calendar.")
    },
    **{
        tool_id: "sharepoint_drive_metadata_enabled"
        for tool_id in _V2_TOOLS
        if tool_id.startswith("microsoft_graph.sharepoint.drive")
    },
}
_LEASE_TARGET_ARGUMENTS: Mapping[str, frozenset[str]] = {
    "microsoft_graph.planner.plan.get": frozenset({"plan_id"}),
    "microsoft_graph.planner.plans.list": frozenset(),
    "microsoft_graph.planner.tasks.list": frozenset({"plan_id"}),
    "microsoft_graph.planner.task.get": frozenset({"plan_id", "task_id"}),
    "microsoft_graph.planner.task.create": frozenset({"plan_id", "bucket_id"}),
    "microsoft_graph.planner.task.update": frozenset({"plan_id", "task_id"}),
    "microsoft_graph.sharepoint.site.get": frozenset({"site_id"}),
    "microsoft_graph.sharepoint.list.get": frozenset({"site_id", "list_id"}),
    "microsoft_graph.sharepoint.list.items.delta": frozenset({"site_id", "list_id"}),
    "microsoft_graph.todo.lists.list": frozenset(),
    "microsoft_graph.todo.tasks.list": frozenset({"task_list_id"}),
    "microsoft_graph.todo.task.get": frozenset({"task_list_id", "task_id"}),
    "microsoft_graph.todo.task.create": frozenset({"task_list_id"}),
    "microsoft_graph.todo.task.update": frozenset({"task_list_id", "task_id"}),
    "microsoft_graph.todo.task.delete": frozenset({"task_list_id", "task_id"}),
    "microsoft_graph.calendar.calendars.list": frozenset(),
    "microsoft_graph.calendar.events.list": frozenset({"calendar_id"}),
    "microsoft_graph.calendar.event.get": frozenset({"calendar_id", "event_id"}),
    "microsoft_graph.calendar.event.create": frozenset({"calendar_id", "transaction_id"}),
    "microsoft_graph.calendar.event.update": frozenset({"calendar_id", "event_id"}),
    "microsoft_graph.calendar.event.delete": frozenset({"calendar_id", "event_id"}),
    "microsoft_graph.calendar.freebusy.get": frozenset({"schedules"}),
    "microsoft_graph.sharepoint.drives.list": frozenset({"site_id"}),
    "microsoft_graph.sharepoint.drive.items.list": frozenset({"site_id", "drive_id"}),
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
            feature = _TOOL_FEATURES.get(tool_id)
            if feature is not None and not (
                settings.office_v2_flags and settings.office_v2_flags.get(feature, False)
            ):
                continue
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
        feature = _TOOL_FEATURES.get(tool_id)
        if feature is not None and not (
            settings.office_v2_flags and settings.office_v2_flags.get(feature, False)
        ):
            return True, _tool_error("FEATURE_DISABLED")
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
                expected_workspace_id=actor.get("workspace_id"),
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
    if ("page_size" in arguments or "page_cursor" in arguments) and not _valid_page_arguments(
        arguments, settings.max_page_size
    ):
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
    if (
        tool_id
        in {
            "microsoft_graph.planner.task.create",
            "microsoft_graph.todo.task.create",
        }
        and "due_date_time" in arguments
        and not _valid_offset_datetime_text(arguments["due_date_time"])
    ):
        return False
    if tool_id.startswith("microsoft_graph.todo."):
        if "task_list_id" in arguments and not _valid_opaque_id(arguments["task_list_id"]):
            return False
        if "task_id" in arguments and not _valid_opaque_id(arguments["task_id"]):
            return False
        if tool_id.endswith((".create", ".update")):
            changes = arguments.get("changes") if tool_id.endswith(".update") else None
            if tool_id.endswith(".update") and not _valid_todo_changes(changes):
                return False
            title = arguments.get("title") if tool_id.endswith(".create") else None
            if title is not None and not _valid_plain_text(title, 255):
                return False
            if "if_match" in arguments and not _valid_etag(arguments.get("if_match")):
                return False
    if tool_id.startswith("microsoft_graph.calendar."):
        for field in ("calendar_id", "event_id"):
            if field in arguments and not _valid_opaque_id(arguments[field]):
                return False
        if "if_match" in arguments and not _valid_etag(arguments.get("if_match")):
            return False
        for field in ("start_date_time", "end_date_time"):
            if field in arguments and not _valid_offset_datetime_text(arguments[field]):
                return False
        start = _parse_aware_datetime(arguments.get("start_date_time"))
        end = _parse_aware_datetime(arguments.get("end_date_time"))
        if ("start_date_time" in arguments or "end_date_time" in arguments) and (
            start is None or end is None or end <= start
        ):
            return False
        if tool_id.endswith(".create"):
            if not _valid_plain_text(arguments.get("subject"), 255):
                return False
            if not _valid_uuid(arguments.get("transaction_id")):
                return False
        if tool_id.endswith(".update") and not _valid_event_changes(arguments.get("changes")):
            return False
        if "location" in arguments and not _valid_plain_text(arguments["location"], 255):
            return False
        if tool_id.endswith(".freebusy.get"):
            schedules = arguments.get("schedules")
            if (
                not isinstance(schedules, list)
                or not 1 <= len(schedules) <= 20
                or any(not _valid_schedule_id(item) for item in schedules)
                or len({item.casefold() for item in schedules}) != len(schedules)
                or start is None
                or end is None
                or end - start > timedelta(days=31)
                or arguments.get("interval_minutes") not in {5, 10, 15, 30, 60}
            ):
                return False
    if tool_id.startswith("microsoft_graph.sharepoint.drive"):
        if "drive_id" in arguments and not _valid_opaque_id(arguments["drive_id"]):
            return False
        if "parent_item_id" in arguments and not _valid_opaque_id(arguments["parent_item_id"]):
            return False
    if tool_id == "microsoft_graph.planner.task.create" and "due_date_time" in arguments:
        return _valid_datetime_text(arguments["due_date_time"])
    return True


def _valid_offset_datetime_text(value: Any) -> bool:
    """Accept an ISO timestamp only when it carries an explicit UTC offset."""

    return _parse_aware_datetime(value) is not None


def _parse_aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _valid_opaque_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._!~+=-]{1,512}", value) is not None


def _valid_plain_text(value: Any, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and bool(value.strip())
        and not any(ord(char) < 32 for char in value)
    )


def _valid_schedule_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 3 <= len(value) <= 320
        and re.fullmatch(r"[^\s@]+@[^\s@]+", value) is not None
    )


def _valid_todo_changes(value: Any) -> bool:
    if (
        not isinstance(value, Mapping)
        or not value
        or set(value) - {"title", "due_date_time", "status", "importance"}
    ):
        return False
    if "title" in value and not _valid_plain_text(value["title"], 255):
        return False
    if "due_date_time" in value and not _valid_offset_datetime_text(value["due_date_time"]):
        return False
    if "status" in value and value["status"] not in {
        "notStarted",
        "inProgress",
        "completed",
        "waitingOnOthers",
        "deferred",
    }:
        return False
    return not ("importance" in value and value["importance"] not in {"low", "normal", "high"})


def _valid_event_changes(value: Any) -> bool:
    if (
        not isinstance(value, Mapping)
        or not value
        or set(value) - {"subject", "start_date_time", "end_date_time", "location"}
    ):
        return False
    for field in ("subject", "location"):
        if field in value and not _valid_plain_text(value[field], 255):
            return False
    return all(
        _valid_offset_datetime_text(value[field])
        for field in ("start_date_time", "end_date_time")
        if field in value
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
    if isinstance(scope, Mapping) and scope.get("schema_version") == 2:
        return _office_v2_profile_scope(scope)
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


def _office_v2_profile_scope(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate v2 feature grants and target lists without widening v1 rules."""

    expected_keys = {
        "schema_version",
        "tenant_id",
        "workspace_id",
        "planner",
        "sharepoint",
        "todo",
        "calendar",
        "mail",
    }
    if set(value) != expected_keys or value.get("schema_version") != 2:
        raise ProfileScopeMismatch("Office v2 profile scope shape differs")
    if not _valid_uuid(value.get("tenant_id")) or not _valid_uuid(value.get("workspace_id")):
        raise ProfileScopeMismatch("Office v2 tenant or workspace is invalid")

    planner = value.get("planner")
    if not isinstance(planner, Mapping) or set(planner) != {
        "plan_ids",
        "tasks_read",
        "tasks_write",
    }:
        raise ProfileScopeMismatch("Office v2 Planner scope shape differs")
    plan_ids = planner.get("plan_ids")
    if (
        not isinstance(plan_ids, list)
        or len(plan_ids) > 100
        or any(not _valid_planner_id(item) for item in plan_ids)
        or len(set(plan_ids)) != len(plan_ids)
        or not isinstance(planner.get("tasks_read"), bool)
        or not isinstance(planner.get("tasks_write"), bool)
        or (planner["tasks_write"] and not planner["tasks_read"])
        or (not planner["tasks_read"] and (plan_ids or planner["tasks_write"]))
    ):
        raise ProfileScopeMismatch("Office v2 Planner scope values are invalid")

    sharepoint = value.get("sharepoint")
    if not isinstance(sharepoint, Mapping) or set(sharepoint) != {
        "enabled",
        "site_id",
        "list_ids",
        "drive_ids",
        "metadata_read",
        "content_read",
    }:
        raise ProfileScopeMismatch("Office v2 SharePoint scope shape differs")
    sp_enabled = sharepoint.get("enabled")
    list_ids = sharepoint.get("list_ids")
    drive_ids = sharepoint.get("drive_ids")
    if (
        not isinstance(sp_enabled, bool)
        or not isinstance(sharepoint.get("metadata_read"), bool)
        or sharepoint.get("content_read") is not False
        or not isinstance(list_ids, list)
        or len(list_ids) > 100
        or any(not _valid_uuid(item) for item in list_ids)
        or len(set(list_ids)) != len(list_ids)
        or not isinstance(drive_ids, list)
        or len(drive_ids) > 100
        or any(not _valid_opaque_id(item) for item in drive_ids)
        or len(set(drive_ids)) != len(drive_ids)
    ):
        raise ProfileScopeMismatch("Office v2 SharePoint scope values are invalid")
    if sp_enabled:
        if not _valid_site_id(sharepoint.get("site_id")) or not sharepoint["metadata_read"]:
            raise ProfileScopeMismatch("Office v2 SharePoint site scope is invalid")
    elif (
        sharepoint.get("site_id") is not None
        or list_ids
        or drive_ids
        or sharepoint["metadata_read"]
    ):
        raise ProfileScopeMismatch("disabled SharePoint scope contains targets")

    todo = value.get("todo")
    if not isinstance(todo, Mapping) or set(todo) != {
        "enabled",
        "task_list_ids",
        "tasks_read",
        "tasks_write",
    }:
        raise ProfileScopeMismatch("Office v2 To Do scope shape differs")
    task_list_ids = todo.get("task_list_ids")
    if (
        not isinstance(todo.get("enabled"), bool)
        or not isinstance(todo.get("tasks_read"), bool)
        or not isinstance(todo.get("tasks_write"), bool)
        or not isinstance(task_list_ids, list)
        or len(task_list_ids) > 100
        or any(not _valid_opaque_id(item) for item in task_list_ids)
        or len(set(task_list_ids)) != len(task_list_ids)
        or (todo["tasks_write"] and not todo["tasks_read"])
        or (todo["enabled"] and not todo["tasks_read"])
        or (not todo["enabled"] and (todo["tasks_read"] or todo["tasks_write"] or task_list_ids))
    ):
        raise ProfileScopeMismatch("Office v2 To Do scope values are invalid")

    calendar = value.get("calendar")
    calendar_keys = {
        "enabled",
        "calendar_ids",
        "events_read",
        "events_write",
        "freebusy_read",
        "freebusy_subjects",
        "attendees_write",
    }
    if not isinstance(calendar, Mapping) or set(calendar) != calendar_keys:
        raise ProfileScopeMismatch("Office v2 Calendar scope shape differs")
    calendar_ids = calendar.get("calendar_ids")
    freebusy = calendar.get("freebusy_subjects")
    calendar_flags = (
        calendar.get("enabled"),
        calendar.get("events_read"),
        calendar.get("events_write"),
        calendar.get("freebusy_read"),
        calendar.get("attendees_write"),
    )
    if (
        any(not isinstance(flag, bool) for flag in calendar_flags)
        or not isinstance(calendar_ids, list)
        or len(calendar_ids) > 100
        or any(not _valid_opaque_id(item) for item in calendar_ids)
        or len(set(calendar_ids)) != len(calendar_ids)
        or not isinstance(freebusy, list)
        or len(freebusy) > 100
        or any(not _valid_schedule_id(item) for item in freebusy)
        or len({item.casefold() for item in freebusy}) != len(freebusy)
        or calendar["attendees_write"]
        or (calendar["events_write"] and not calendar["events_read"])
        or (calendar["enabled"] and not (calendar["events_read"] or calendar["freebusy_read"]))
        or (
            not calendar["enabled"]
            and (
                calendar_ids
                or calendar["events_read"]
                or calendar["events_write"]
                or calendar["freebusy_read"]
                or freebusy
            )
        )
        or (not calendar["events_read"] and calendar_ids)
        or (not calendar["freebusy_read"] and freebusy)
    ):
        raise ProfileScopeMismatch("Office v2 Calendar scope values are invalid")

    mail = value.get("mail")
    if (
        not isinstance(mail, Mapping)
        or set(mail)
        != {"enabled", "folder_ids", "metadata_read", "body_read", "drafts_write", "send_enabled"}
        or any(
            mail.get(key) is not expected
            for key, expected in {
                "enabled": False,
                "metadata_read": False,
                "body_read": False,
                "drafts_write": False,
                "send_enabled": False,
            }.items()
        )
        or mail.get("folder_ids") != []
    ):
        raise ProfileScopeMismatch("Microsoft Graph mail features are not enabled in this release")
    return dict(value)


def _authorize_profile(
    scope: Mapping[str, Any],
    tool_id: str,
    arguments: Mapping[str, Any],
    profile: ProviderProfile,
    *,
    settings: ProviderSettings,
    expected_external_subject: str,
    expected_workspace_id: str | None,
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
    if scope.get("schema_version") == 2:
        if (
            not isinstance(expected_workspace_id, str)
            or scope.get("workspace_id") != expected_workspace_id
        ):
            raise TrustedActorProfileMismatch(
                "Graph profile differs from the signed Workspace actor"
            )
        expected_scopes = {"User.Read"}
        planner = scope["planner"]
        todo = scope["todo"]
        sharepoint = scope["sharepoint"]
        calendar = scope["calendar"]
        if planner["tasks_write"] or todo["tasks_write"]:
            expected_scopes.add("Tasks.ReadWrite")
        elif planner["tasks_read"] or todo["tasks_read"]:
            expected_scopes.add("Tasks.Read")
        if sharepoint["enabled"]:
            expected_scopes.add("Sites.Selected")
        if calendar["events_write"]:
            expected_scopes.add("Calendars.ReadWrite")
        elif calendar["events_read"] or calendar["freebusy_read"]:
            expected_scopes.add("Calendars.ReadBasic")
        if set(oauth_scopes) != expected_scopes:
            raise ProfileScopeMismatch("Office v2 delegated scopes exceed the selected profile")
        if tool_id.startswith("microsoft_graph.planner."):
            if not planner["tasks_read"]:
                raise ProfileAuthorizationDenied("Planner read permission is not granted")
            if tool_id == "microsoft_graph.planner.plans.list":
                return
            if arguments.get("plan_id") not in planner["plan_ids"]:
                raise ProfileAuthorizationDenied("Planner plan is not selected")
            if tool_id in _WRITE_TOOLS and not planner["tasks_write"]:
                raise ProfileAuthorizationDenied("Planner write permission is not granted")
            return
        if tool_id.startswith("microsoft_graph.todo."):
            if not todo["enabled"] or not todo["tasks_read"]:
                raise ProfileAuthorizationDenied("To Do access is not enabled")
            if tool_id == "microsoft_graph.todo.lists.list":
                return
            if arguments.get("task_list_id") not in todo["task_list_ids"]:
                raise ProfileAuthorizationDenied("To Do task list is not selected")
            if tool_id in _V2_WRITE_TOOLS and not todo["tasks_write"]:
                raise ProfileAuthorizationDenied("To Do write permission is not granted")
            return
        if tool_id.startswith("microsoft_graph.calendar."):
            if not calendar["enabled"]:
                raise ProfileAuthorizationDenied("Calendar access is not enabled")
            if tool_id == "microsoft_graph.calendar.calendars.list":
                return
            if tool_id == "microsoft_graph.calendar.freebusy.get":
                if not calendar["freebusy_read"]:
                    raise ProfileAuthorizationDenied("Free/busy permission is not granted")
                if not set(item.casefold() for item in arguments["schedules"]) <= set(
                    calendar["freebusy_subjects"]
                ):
                    raise ProfileAuthorizationDenied("Free/busy target is not selected")
                return
            if arguments.get("calendar_id") not in calendar["calendar_ids"]:
                raise ProfileAuthorizationDenied("Calendar is not selected")
            if tool_id in _V2_WRITE_TOOLS:
                if not calendar["events_write"]:
                    raise ProfileAuthorizationDenied("Calendar write permission is not granted")
            elif not calendar["events_read"]:
                raise ProfileAuthorizationDenied("Calendar read permission is not granted")
            return
        if tool_id.startswith("microsoft_graph.sharepoint."):
            if (
                not sharepoint["enabled"]
                or not sharepoint["metadata_read"]
                or arguments.get("site_id") != sharepoint["site_id"]
            ):
                raise ProfileAuthorizationDenied("SharePoint site is not selected")
            if tool_id == "microsoft_graph.sharepoint.site.get":
                return
            if tool_id in {
                "microsoft_graph.sharepoint.drives.list",
                "microsoft_graph.sharepoint.drive.items.list",
            }:
                if (
                    tool_id.endswith("drive.items.list")
                    and arguments.get("drive_id") not in sharepoint["drive_ids"]
                ):
                    raise ProfileAuthorizationDenied("SharePoint drive is not selected")
                return
            if arguments.get("list_id") not in sharepoint["list_ids"]:
                raise ProfileAuthorizationDenied("SharePoint list is not selected")
            return
        raise ProfileAuthorizationDenied("Microsoft Graph operation is not enabled")
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
) -> dict[str, Any]:
    """Build the exact Account Center resource target for the selected operation."""

    target = {"tenant_id": str(scope["tenant_id"])}
    if scope.get("schema_version") == 2:
        if tool_id in {
            "microsoft_graph.planner.plans.list",
            "microsoft_graph.todo.lists.list",
            "microsoft_graph.calendar.calendars.list",
        }:
            return target
        if tool_id.startswith("microsoft_graph.todo."):
            result = {**target, "task_list_id": str(arguments["task_list_id"])}
            if "task_id" in arguments:
                result["task_id"] = str(arguments["task_id"])
            return result
        if tool_id.startswith("microsoft_graph.calendar."):
            if tool_id == "microsoft_graph.calendar.freebusy.get":
                return {
                    **target,
                    "schedule_ids": sorted(item.casefold() for item in arguments["schedules"]),
                }
            result = {**target, "calendar_id": str(arguments["calendar_id"])}
            if "event_id" in arguments:
                result["event_id"] = str(arguments["event_id"])
            if "transaction_id" in arguments:
                result["transaction_id"] = str(arguments["transaction_id"])
            return result
        if tool_id.startswith("microsoft_graph.sharepoint."):
            result = {**target, "site_id": str(arguments["site_id"])}
            if "list_id" in arguments:
                result["list_id"] = str(arguments["list_id"])
            if "drive_id" in arguments:
                result["drive_id"] = str(arguments["drive_id"])
            return result
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
    if tool_id == "microsoft_graph.planner.plans.list":
        return session.list_plans(
            page_size=int(arguments.get("page_size", settings.max_page_size)),
            page_cursor=str(arguments["page_cursor"]) if "page_cursor" in arguments else None,
        )
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
    if tool_id == "microsoft_graph.sharepoint.list.items.delta":
        return session.list_items_delta(
            str(arguments["site_id"]),
            str(arguments["list_id"]),
            page_size=int(arguments.get("page_size", settings.max_page_size)),
            page_cursor=str(arguments["page_cursor"]) if "page_cursor" in arguments else None,
        )
    if tool_id == "microsoft_graph.todo.lists.list":
        return session.list_todo_lists(
            page_size=int(arguments.get("page_size", settings.max_page_size)),
            page_cursor=str(arguments["page_cursor"]) if "page_cursor" in arguments else None,
        )
    if tool_id == "microsoft_graph.todo.tasks.list":
        return session.list_todo_tasks(
            str(arguments["task_list_id"]),
            page_size=int(arguments.get("page_size", settings.max_page_size)),
            page_cursor=str(arguments["page_cursor"]) if "page_cursor" in arguments else None,
        )
    if tool_id == "microsoft_graph.todo.task.get":
        return session.get_todo_task(str(arguments["task_list_id"]), str(arguments["task_id"]))
    if tool_id == "microsoft_graph.todo.task.create":
        return session.create_todo_task(
            str(arguments["task_list_id"]),
            str(arguments["title"]),
            due_date_time=str(arguments["due_date_time"]) if "due_date_time" in arguments else None,
            importance=str(arguments["importance"]) if "importance" in arguments else None,
        )
    if tool_id == "microsoft_graph.todo.task.update":
        changes = arguments["changes"]
        if not isinstance(changes, Mapping):
            raise GraphApiFailure("INVALID_ARGUMENTS")
        return session.update_todo_task(
            str(arguments["task_list_id"]),
            str(arguments["task_id"]),
            str(arguments["if_match"]),
            changes,
        )
    if tool_id == "microsoft_graph.todo.task.delete":
        return session.delete_todo_task(
            str(arguments["task_list_id"]),
            str(arguments["task_id"]),
            str(arguments["if_match"]),
        )
    if tool_id == "microsoft_graph.calendar.calendars.list":
        return session.list_calendars(
            page_size=int(arguments.get("page_size", settings.max_page_size)),
            page_cursor=str(arguments["page_cursor"]) if "page_cursor" in arguments else None,
        )
    if tool_id == "microsoft_graph.calendar.events.list":
        return session.list_calendar_events(
            str(arguments["calendar_id"]),
            str(arguments["start_date_time"]),
            str(arguments["end_date_time"]),
            page_size=int(arguments.get("page_size", settings.max_page_size)),
            page_cursor=str(arguments["page_cursor"]) if "page_cursor" in arguments else None,
        )
    if tool_id == "microsoft_graph.calendar.event.get":
        return session.get_calendar_event(str(arguments["calendar_id"]), str(arguments["event_id"]))
    if tool_id == "microsoft_graph.calendar.freebusy.get":
        return session.get_calendar_freebusy(
            list(arguments["schedules"]),
            str(arguments["start_date_time"]),
            str(arguments["end_date_time"]),
            int(arguments["interval_minutes"]),
        )
    if tool_id == "microsoft_graph.calendar.event.create":
        return session.create_calendar_event(
            str(arguments["calendar_id"]),
            str(arguments["subject"]),
            str(arguments["start_date_time"]),
            str(arguments["end_date_time"]),
            str(arguments["transaction_id"]),
            location=str(arguments["location"]) if "location" in arguments else None,
        )
    if tool_id == "microsoft_graph.calendar.event.update":
        changes = arguments["changes"]
        if not isinstance(changes, Mapping):
            raise GraphApiFailure("INVALID_ARGUMENTS")
        return session.update_calendar_event(
            str(arguments["calendar_id"]),
            str(arguments["event_id"]),
            str(arguments["if_match"]),
            changes,
        )
    if tool_id == "microsoft_graph.calendar.event.delete":
        return session.delete_calendar_event(
            str(arguments["calendar_id"]),
            str(arguments["event_id"]),
            str(arguments["if_match"]),
        )
    if tool_id == "microsoft_graph.sharepoint.drives.list":
        return session.list_sharepoint_drives(
            str(arguments["site_id"]),
            page_size=int(arguments.get("page_size", settings.max_page_size)),
            page_cursor=str(arguments["page_cursor"]) if "page_cursor" in arguments else None,
        )
    if tool_id == "microsoft_graph.sharepoint.drive.items.list":
        return session.list_sharepoint_drive_items(
            str(arguments["site_id"]),
            str(arguments["drive_id"]),
            parent_item_id=str(arguments["parent_item_id"])
            if "parent_item_id" in arguments
            else None,
            page_size=int(arguments.get("page_size", settings.max_page_size)),
            page_cursor=str(arguments["page_cursor"]) if "page_cursor" in arguments else None,
        )
    raise GraphApiFailure("INVALID_OPERATION")


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
