"""Closed schemas for the Bilibili connector's fixed operation catalog."""

from __future__ import annotations

from typing import Any

PLUGIN_ID = "cyrene.connectors.bilibili"
PLUGIN_VERSION = "0.1.0"
_USER_ID = {"type": "string", "pattern": "^[0-9]{1,20}$"}
_DYNAMIC_ID = {"type": "string", "pattern": "^[0-9]{1,20}$"}
_VIDEO_ID = {
    "type": "string",
    "pattern": "^(BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,19})$",
}
_PAGE = {"type": "integer", "minimum": 1, "maximum": 100}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 50}
_CURSOR = {"type": "string", "minLength": 1, "maxLength": 1024}
_CONTENT = {"type": "string", "minLength": 1, "maxLength": 5000}


def _object(
    properties: dict[str, Any],
    required: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


CANONICAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "provider_tool_id": "bilibili.auth.qr.begin",
        "display_name": "Link Bilibili Account",
        "description": (
            "Create a real Bilibili QR login flow for an owner-authorized Account Center profile."
        ),
        "input_schema": _object(
            {
                "profile_ref": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                },
                "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
            },
            ("profile_ref", "idempotency_key"),
        ),
    },
    {
        "provider_tool_id": "bilibili.auth.qr.status",
        "display_name": "Check Bilibili QR Login",
        "description": "Poll one private QR login flow and return only safe account metadata.",
        "input_schema": _object(
            {"login_flow_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
            ("login_flow_id",),
        ),
    },
    {
        "provider_tool_id": "bilibili.search",
        "display_name": "Search Bilibili",
        "description": "Search a bounded page of Bilibili videos, users, or articles.",
        "input_schema": _object(
            {
                "keyword": {"type": "string", "minLength": 1, "maxLength": 120},
                "search_type": {"type": "string", "enum": ["video", "user", "article"]},
                "page": _PAGE,
            },
            ("keyword", "search_type"),
        ),
    },
    {
        "provider_tool_id": "bilibili.user.find",
        "display_name": "Find Bilibili User",
        "description": "Read public profile metadata for one numeric Bilibili user ID.",
        "input_schema": _object({"user_id": _USER_ID}, ("user_id",)),
    },
    {
        "provider_tool_id": "bilibili.video.read",
        "display_name": "Read Bilibili Video Details",
        "description": (
            "Read video metadata and available subtitles; this does not download "
            "or watch the video."
        ),
        "input_schema": _object({"video_id": _VIDEO_ID}, ("video_id",)),
    },
    {
        "provider_tool_id": "bilibili.comments.list",
        "display_name": "List Bilibili Video Comments",
        "description": "Read one bounded page of comments for a video or comment thread.",
        "input_schema": _object(
            {
                "video_id": _VIDEO_ID,
                "root_comment_id": _DYNAMIC_ID,
                "page": _PAGE,
                "limit": _LIMIT,
            },
            ("video_id",),
        ),
    },
    {
        "provider_tool_id": "bilibili.dynamics.list",
        "display_name": "List Bilibili User Dynamics",
        "description": "Read one bounded page of public dynamics for a Bilibili user.",
        "input_schema": _object(
            {"user_id": _USER_ID, "cursor": _CURSOR, "limit": _LIMIT},
            ("user_id",),
        ),
    },
    {
        "provider_tool_id": "bilibili.dynamic.read",
        "display_name": "Read Bilibili Dynamic",
        "description": "Read one public dynamic by its numeric identifier.",
        "input_schema": _object({"dynamic_id": _DYNAMIC_ID}, ("dynamic_id",)),
    },
    {
        "provider_tool_id": "bilibili.dm.list",
        "display_name": "List Bilibili Private Messages",
        "description": "Read one bounded page of messages for a linked account and one peer.",
        "input_schema": _object(
            {"peer_user_id": _USER_ID, "cursor": _CURSOR, "limit": _LIMIT},
            ("peer_user_id",),
        ),
    },
    {
        "provider_tool_id": "bilibili.dm.read",
        "display_name": "Read Bilibili Private Message",
        "description": "Read one message in the linked account's conversation with one peer.",
        "input_schema": _object(
            {"peer_user_id": _USER_ID, "message_id": _DYNAMIC_ID},
            ("peer_user_id", "message_id"),
        ),
    },
    {
        "provider_tool_id": "bilibili.comment.create",
        "display_name": "Create Bilibili Comment",
        "description": (
            "Create a comment or reply under an exact video comment target and "
            "verify it by reading back."
        ),
        "input_schema": _object(
            {
                "video_id": _VIDEO_ID,
                "root_comment_id": {"type": "string", "pattern": "^(0|[0-9]{1,20})$"},
                "parent_comment_id": {"type": "string", "pattern": "^(0|[0-9]{1,20})$"},
                "content": _CONTENT,
            },
            ("video_id", "root_comment_id", "parent_comment_id", "content"),
        ),
    },
    {
        "provider_tool_id": "bilibili.dynamic.publish",
        "display_name": "Publish Bilibili Dynamic",
        "description": (
            "Publish a text dynamic as the linked account and verify it by reading back."
        ),
        "input_schema": _object(
            {"user_id": _USER_ID, "content": _CONTENT},
            ("user_id", "content"),
        ),
    },
    {
        "provider_tool_id": "bilibili.dm.send",
        "display_name": "Send Bilibili Private Message",
        "description": (
            "Send one text message from the linked account to an exact peer and "
            "verify it by reading back."
        ),
        "input_schema": _object(
            {
                "peer_user_id": _USER_ID,
                "content": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            ("peer_user_id", "content"),
        ),
    },
)

TOOL_BY_ID = {str(item["provider_tool_id"]): item for item in CANONICAL_TOOLS}


def operation_for(tool_id: str) -> str:
    """Return the canonical operation paired with one provider tool ID."""

    if tool_id not in TOOL_BY_ID:
        raise ValueError("unknown Bilibili operation")
    return tool_id
