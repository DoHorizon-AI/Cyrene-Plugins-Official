"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud_management_provider.tool_catalog                     │
│  Role: Define fixed tool and operation IDs for cloud management.    │
│                                                                      │
│  模块职责：定义云管理 provider 的固定工具和操作 ID。                  │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from typing import Any

from cyrene_integration_contracts import TOOL_OPERATIONS as CONTRACT_TOOL_OPERATIONS

CAPABILITY_ID = "tool.provider.v1"
PLUGIN_ID = "cyrene.providers.cloud-management"
PLUGIN_VERSION = "0.1.0"
LIST_TOOLS_METHOD = "list_tools"
CALL_TOOL_METHOD = "call_tool"
LIST_TOOLS_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest"
LIST_TOOLS_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse"
CALL_TOOL_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest"
CALL_TOOL_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse"
MAX_TOOL_REQUEST_BYTES = 65_536
MAX_TOOL_JSON_BYTES = 48_000

_PROFILE = {
    "type": "string",
    "pattern": "^[A-Za-z0-9._/-]{1,192}$",
    "description": "Opaque profile_ref resolved only by this provider binding.",
}
_PROFILE_ARGUMENTS = {
    "type": "object",
    "properties": {"profile_ref": _PROFILE},
    "required": ["profile_ref"],
    "additionalProperties": False,
}
_TARGET = {
    "type": "object",
    "minProperties": 1,
    "maxProperties": 10,
    "properties": {
        "scope_ref": {"type": "string", "maxLength": 192},
        "resource_id": {"type": "string", "maxLength": 2048},
        "resource_group": {"type": "string", "maxLength": 90},
        "deployment_name": {"type": "string", "maxLength": 64},
        "instance_name": {"type": "string", "maxLength": 63},
        "disk_name": {"type": "string", "pattern": "^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$"},
        "kind": {"type": "string", "enum": ["instance", "disk"]},
        "zone": {"type": "string", "maxLength": 128},
        "pod_id": {"type": "string", "maxLength": 128},
        "pod_name": {"type": "string", "maxLength": 63},
    },
    "additionalProperties": False,
}
_BASE_TARGET_ARGUMENTS = {
    "type": "object",
    "properties": {"profile_ref": _PROFILE, "target": _TARGET},
    "required": ["profile_ref", "target"],
    "additionalProperties": False,
}
_PARAMETER_ARGUMENTS = {
    "type": "object",
    "properties": {
        "profile_ref": _PROFILE,
        "target": _TARGET,
        "parameters": {"type": "object", "maxProperties": 128},
    },
    "required": ["profile_ref", "target", "parameters"],
    "additionalProperties": False,
}
_OPERATION_ARGUMENTS = {
    "type": "object",
    "properties": {
        "profile_ref": _PROFILE,
        "operation_ref": {"type": "string", "maxLength": 8192},
    },
    "required": ["profile_ref", "operation_ref"],
    "additionalProperties": False,
}

CANONICAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "provider_tool_id": "cloud_provider_health",
        "display_name": "Check Cloud Provider Profile",
        "description": "Check authentication and scope health for one bound cloud profile.",
        "operation_id": "cloud.provider.health.read",
        "input_schema": _PROFILE_ARGUMENTS,
    },
    {
        "provider_tool_id": "cloud_resources_list",
        "display_name": "List Cloud Resources",
        "description": "List resources visible within one bound provider profile and target scope.",
        "operation_id": "cloud.resources.list",
        "input_schema": {
            **_BASE_TARGET_ARGUMENTS,
            "properties": {
                **_BASE_TARGET_ARGUMENTS["properties"],
                "cursor": {"type": "string", "maxLength": 4096},
            },
            "required": ["profile_ref", "target"],
        },
    },
    {
        "provider_tool_id": "cloud_resource_get",
        "display_name": "Get Cloud Resource",
        "description": "Get one resource after the provider verifies its profile scope.",
        "operation_id": "cloud.resource.get",
        "input_schema": _BASE_TARGET_ARGUMENTS,
    },
    {
        "provider_tool_id": "cloud_resource_create",
        "display_name": "Create Cloud Resource",
        "description": "Create one provider-supported resource using signed parameters.",
        "operation_id": "cloud.resource.create",
        "input_schema": _PARAMETER_ARGUMENTS,
    },
    {
        "provider_tool_id": "cloud_resource_deploy",
        "display_name": "Deploy Cloud Resource",
        "description": "Deploy a bound Azure template, Compute Engine instance, or Runpod Pod.",
        "operation_id": "cloud.resource.deploy",
        "input_schema": _PARAMETER_ARGUMENTS,
    },
    {
        "provider_tool_id": "cloud_resource_start",
        "display_name": "Start Cloud Resource",
        "description": "Start one supported resource within the profile's immutable scope.",
        "operation_id": "cloud.resource.start",
        "input_schema": _BASE_TARGET_ARGUMENTS,
    },
    {
        "provider_tool_id": "cloud_resource_stop",
        "display_name": "Stop Cloud Resource",
        "description": "Stop one supported resource and report its observed state.",
        "operation_id": "cloud.resource.stop",
        "input_schema": _BASE_TARGET_ARGUMENTS,
    },
    {
        "provider_tool_id": "cloud_resource_restart",
        "display_name": "Restart Runpod Pod",
        "description": "Restart one profile-scoped Runpod Pod and poll its actual state.",
        "operation_id": "cloud.resource.restart",
        "input_schema": _BASE_TARGET_ARGUMENTS,
    },
    {
        "provider_tool_id": "cloud_resource_update",
        "display_name": "Update Cloud Resource",
        "description": "Update one supported resource using signed parameters.",
        "operation_id": "cloud.resource.update",
        "input_schema": _PARAMETER_ARGUMENTS,
    },
    {
        "provider_tool_id": "cloud_resource_delete",
        "display_name": "Delete Cloud Resource",
        "description": "Delete one supported resource and report read-back evidence.",
        "operation_id": "cloud.resource.delete",
        "input_schema": _BASE_TARGET_ARGUMENTS,
    },
    {
        "provider_tool_id": "cloud_operation_status",
        "display_name": "Read Cloud Operation Status",
        "description": "Poll a signed provider operation reference without replaying its write.",
        "operation_id": "cloud.operation.status.read",
        "input_schema": _OPERATION_ARGUMENTS,
    },
    {
        "provider_tool_id": "cloud_operation_logs",
        "display_name": "Read Cloud Operation Logs",
        "description": "Read bounded provider operation diagnostics or Pod logs.",
        "operation_id": "cloud.operation.logs.read",
        "input_schema": _OPERATION_ARGUMENTS,
    },
    {
        "provider_tool_id": "cloud_operation_cancel",
        "display_name": "Cancel Cloud Operation",
        "description": (
            "Cancel only provider operations whose APIs explicitly support cancellation."
        ),
        "operation_id": "cloud.operation.cancel",
        "input_schema": _OPERATION_ARGUMENTS,
    },
    {
        "provider_tool_id": "cloud_vendor_cli",
        "display_name": "Run Cloud Vendor CLI",
        "description": (
            "Run the fixed vendor CLI under profile-level authorization with a complete "
            "bounded argv vector. "
            "Raw stdout and stderr are suppressed."
        ),
        "operation_id": "cloud.vendor.cli.execute",
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_ref": _PROFILE,
                "argv": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 128,
                    "items": {"type": "string", "maxLength": 4096},
                },
            },
            "required": ["profile_ref", "argv"],
            "additionalProperties": False,
        },
    },
)

_OPERATION_ARGUMENTS = {
    "type": "object",
    "properties": {"profile_ref": _PROFILE, "operation_ref": {"type": "string", "maxLength": 8192}},
    "required": ["profile_ref", "operation_ref"],
    "additionalProperties": False,
}

TOOL_BY_ID = {str(tool["provider_tool_id"]): tool for tool in CANONICAL_TOOLS}
TOOL_OPERATIONS = {tool_id: str(tool["operation_id"]) for tool_id, tool in TOOL_BY_ID.items()}

for _tool_id, _operation_id in TOOL_OPERATIONS.items():
    if CONTRACT_TOOL_OPERATIONS.get(_tool_id) != _operation_id:
        raise RuntimeError(f"missing fixed integration contract operation for {_tool_id}")
