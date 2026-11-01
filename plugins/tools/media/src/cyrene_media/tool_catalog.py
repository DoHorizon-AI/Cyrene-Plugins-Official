"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 tool_catalog.py                                                  │
│  Module: cyrene_media.tool_catalog                                  │
│  Role: Declare fixed media tool IDs and strict JSON arguments.       │
│                                                                      │
│  模块职责：声明固定媒体工具 ID 和严格 JSON 参数。                     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from typing import Any

from cyrene_integration_contracts import TOOL_OPERATIONS

PLUGIN_ID = "cyrene.tools.media"
PLUGIN_VERSION = "0.1.0"
MAX_TOOL_REQUEST_BYTES = 64 * 1024
MAX_TOOL_JSON_BYTES = 48 * 1024

_ASSET_ID = {
    "type": "string",
    "pattern": (
        "^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
}
_ARTIFACT_ID = {"type": "string", "pattern": "^[0-9a-f]{64}$"}

CANONICAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "provider_tool_id": "media.asset.import",
        "display_name": "Import Media Asset",
        "description": (
            "Import one host-authorized actor-scoped image artifact into the catalog."
        ),
        "operation_id": "media.asset.import",
        "input_schema": {
            "type": "object",
            "properties": {
                "artifact_id": _ARTIFACT_ID,
                "title": {"type": "string", "minLength": 1, "maxLength": 160},
                "alt_text": {"type": "string", "maxLength": 1000},
                "tags": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {"type": "string", "minLength": 1, "maxLength": 48},
                    "uniqueItems": True,
                },
            },
            "required": ["artifact_id", "title"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "media.asset.search",
        "display_name": "Search Media Assets",
        "description": ("Search the current actor binding's bounded media catalog."),
        "operation_id": "media.asset.search",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 128},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "cursor": {"type": "string", "maxLength": 256},
            },
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "media.asset.get",
        "display_name": "Get Media Artifact",
        "description": (
            "Verify and export one media asset as an actor-scoped Work artifact."
        ),
        "operation_id": "media.asset.get",
        "input_schema": {
            "type": "object",
            "properties": {
                "asset_id": _ASSET_ID,
                "name": {"type": "string", "minLength": 1, "maxLength": 512},
            },
            "required": ["asset_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "media.asset.delete",
        "display_name": "Delete Media Asset",
        "description": "Soft-delete one media asset while retaining its recovery data.",
        "operation_id": "media.asset.delete",
        "input_schema": {
            "type": "object",
            "properties": {"asset_id": _ASSET_ID},
            "required": ["asset_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "media.asset.restore",
        "display_name": "Restore Media Asset",
        "description": (
            "Restore one soft-deleted asset after verifying its stored digest."
        ),
        "operation_id": "media.asset.restore",
        "input_schema": {
            "type": "object",
            "properties": {"asset_id": _ASSET_ID},
            "required": ["asset_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "media.card.render",
        "display_name": "Render Media Card",
        "description": (
            "Render a bounded PNG card and return an artifact reference "
            "without sending it."
        ),
        "operation_id": "media.card.render",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "body": {"type": "string", "minLength": 1, "maxLength": 800},
                "theme": {"type": "string", "enum": ["light", "dark", "warm"]},
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
    },
)

TOOL_BY_ID = {str(tool["provider_tool_id"]): tool for tool in CANONICAL_TOOLS}
EXPECTED_OPERATIONS = {
    str(tool["provider_tool_id"]): str(tool["operation_id"]) for tool in CANONICAL_TOOLS
}


def operation_for(tool_id: str) -> str:
    """Return only a registry-matched fixed operation ID."""

    expected = EXPECTED_OPERATIONS.get(tool_id)
    if expected is None or TOOL_OPERATIONS.get(tool_id) != expected:
        raise ValueError("media tool operation is not registered")
    return expected


__all__ = [
    "CANONICAL_TOOLS",
    "EXPECTED_OPERATIONS",
    "MAX_TOOL_JSON_BYTES",
    "MAX_TOOL_REQUEST_BYTES",
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "TOOL_BY_ID",
    "operation_for",
]
