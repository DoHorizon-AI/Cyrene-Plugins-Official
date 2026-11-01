"""Closed schemas for the public search and source-reading operations."""

from __future__ import annotations

from typing import Any

PLUGIN_ID = "cyrene.tools.public-web"
PLUGIN_VERSION = "0.1.0"


def _object(properties: dict[str, Any], required: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_CURSOR = {"type": "string", "minLength": 1, "maxLength": 8192}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 20}
_URL = {"type": "string", "minLength": 8, "maxLength": 4096}

CANONICAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "provider_tool_id": "web.search",
        "display_name": "Search the Public Web",
        "description": (
            "Search through the configured backend and return a bounded, traceable page."
        ),
        "input_schema": _object(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "cursor": _CURSOR,
                "limit": _LIMIT,
            },
            ("query",),
        ),
    },
    {
        "provider_tool_id": "web.open",
        "display_name": "Open a Public Web Page",
        "description": (
            "Read visible text from one public HTTPS page with a source digest "
            "and bounded pagination."
        ),
        "input_schema": _object(
            {
                "url": _URL,
                "cursor": _CURSOR,
                "chunk_chars": {"type": "integer", "minimum": 1000, "maximum": 20000},
            },
            ("url",),
        ),
    },
    {
        "provider_tool_id": "document.read",
        "display_name": "Read a Public Document",
        "description": (
            "Read source-backed blocks from a public document URL or a task-authorized attachment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": _URL,
                "artifact_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "cursor": _CURSOR,
                "limit": _LIMIT,
            },
            "oneOf": [{"required": ["url"]}, {"required": ["artifact_id"]}],
            "additionalProperties": False,
        },
    },
)

TOOL_BY_ID = {str(item["provider_tool_id"]): item for item in CANONICAL_TOOLS}


def operation_for(tool_id: str) -> str:
    if tool_id not in TOOL_BY_ID:
        raise ValueError("unknown public web operation")
    return tool_id
