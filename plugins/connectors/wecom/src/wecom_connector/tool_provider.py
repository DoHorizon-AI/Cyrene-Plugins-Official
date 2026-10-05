"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 tool_provider.py                                                │
│  Package: wecom_connector                                           │
│  Role: Implementation of tool.provider.v1 for WeCom CLI tools.      │
│                                                                     │
│  模块职责：企业微信工具提供者 (tool.provider.v1) 实现。                 │
│  · 暴露固定工具目录 (ListTools): auth、contact、calendar、todo、doc  │
│    查询以及连接诊断；写操作要求精确 binding 与 invocation 批准        │
│  · 执行工具调用 (CallTool): 将允许的参数映射到固定 CLI argv vector    │
│  · 双向数据转换：支持 Protobuf typed payload 与普通 JSON Mapping 适配  │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from ._generated import tool_provider_pb2 as tool_contract
from .cli import WeComCliClient, WeComCliError

logger = logging.getLogger(__name__)

TOOL_PROVIDER_CAPABILITY_ID = "tool.provider.v1"
LIST_TOOLS_METHOD = "list_tools"
CALL_TOOL_METHOD = "call_tool"

LIST_TOOLS_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest"
LIST_TOOLS_RESPONSE_TYPE_URL = (
    "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse"
)
CALL_TOOL_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest"
CALL_TOOL_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse"

CANONICAL_TOOLS = [
    {
        "provider_tool_id": "wecom_auth_status",
        "display_name": "WeCom Auth Status",
        "description": "Check auth status of Enterprise WeChat (WeCom) CLI.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "provider_tool_id": "wecom_contact_search",
        "display_name": "WeCom Contact Search",
        "description": "Search members in WeCom organization by keywords.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "Name, alias, or search keyword",
                }
            },
            "required": ["keywords"],
        },
    },
    {
        "provider_tool_id": "wecom_calendar_list",
        "display_name": "WeCom Calendar List",
        "description": "Query schedule list from WeCom calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of items to return",
                    "default": 20,
                }
            },
        },
    },
    {
        "provider_tool_id": "wecom_calendar_create",
        "display_name": "WeCom Calendar Create",
        "description": (
            "Create a WeCom schedule. Requires a binding-level approved operation "
            "and matching write_approval value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Schedule title"},
                "start_time": {"type": "string", "description": "Start ISO time"},
                "end_time": {"type": "string", "description": "End ISO time"},
                "description": {"type": "string", "description": "Optional notes"},
                "write_approval": {
                    "type": "string",
                    "const": "calendar.schedules.create",
                    "description": (
                        "Required operator approval for this exact write operation"
                    ),
                },
            },
            "required": ["title", "write_approval"],
        },
    },
    {
        "provider_tool_id": "wecom_todo_list",
        "display_name": "WeCom Todo List",
        "description": "Query pending todo tasks in WeCom.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max items",
                    "default": 20,
                }
            },
        },
    },
    {
        "provider_tool_id": "wecom_todo_create",
        "display_name": "WeCom Todo Create",
        "description": (
            "Create a WeCom todo. Requires a binding-level approved operation "
            "and matching write_approval value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title of the task"},
                "write_approval": {
                    "type": "string",
                    "const": "todo.create",
                    "description": (
                        "Required operator approval for this exact write operation"
                    ),
                },
            },
            "required": ["title", "write_approval"],
        },
    },
    {
        "provider_tool_id": "wecom_todo_finish",
        "display_name": "WeCom Todo Finish",
        "description": (
            "Finish a WeCom todo. Requires a binding-level approved operation "
            "and matching write_approval value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "todo_id": {"type": "string", "description": "Todo task ID"},
                "write_approval": {
                    "type": "string",
                    "const": "todo.finish",
                    "description": (
                        "Required operator approval for this exact write operation"
                    ),
                },
            },
            "required": ["todo_id", "write_approval"],
        },
    },
    {
        "provider_tool_id": "wecom_doc_search",
        "display_name": "WeCom Doc Search",
        "description": "Search documents and spreadsheets in WeCom.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Search keyword"}
            },
            "required": ["keyword"],
        },
    },
    {
        "provider_tool_id": "wecom_operation_catalog",
        "display_name": "WeCom Operation Catalog",
        "description": (
            "List bounded CLI operations, configured profile state, and write "
            "approval requirements. No arbitrary CLI executor is exposed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "provider_tool_id": "wecom_connection_health",
        "display_name": "WeCom Connection Health",
        "description": (
            "Report app, bot, human CLI-profile, and local Navigator readiness "
            "without returning credentials."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

_WRITE_OPERATIONS = {
    "wecom_calendar_create": "calendar.schedules.create",
    "wecom_todo_create": "todo.create",
    "wecom_todo_finish": "todo.finish",
}
WRITE_TOOL_IDS = frozenset(_WRITE_OPERATIONS)
_CLI_TOOL_IDS = {
    "wecom_auth_status",
    "wecom_contact_search",
    "wecom_calendar_list",
    "wecom_calendar_create",
    "wecom_todo_list",
    "wecom_todo_create",
    "wecom_todo_finish",
    "wecom_doc_search",
    "wecom_operation_catalog",
}


class WeComToolProvider:
    """Implements tool.provider.v1 for WeCom CLI integration."""

    def __init__(
        self,
        binding_id: str = "wecom.tools",
        cli_client: WeComCliClient | None = None,
        *,
        cli_enabled: bool = True,
        approved_write_operations: tuple[str, ...] = (),
        health_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._binding_id = binding_id
        self._cli = cli_client or WeComCliClient()
        self._cli_enabled = cli_enabled
        self._approved_write_operations = frozenset(approved_write_operations)
        self._health_provider = health_provider

    @property
    def binding_id(self) -> str:
        return self._binding_id

    def list_tools(
        self,
        request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the dictionary catalog of available tools."""
        catalog_version = "v1.4.0"
        tools: list[dict[str, Any]] = []

        for defn in CANONICAL_TOOLS:
            if defn["provider_tool_id"] in _CLI_TOOL_IDS and not self._cli_enabled:
                continue
            tools.append(
                {
                    "binding_id": self._binding_id,
                    "provider_tool_id": defn["provider_tool_id"],
                    "display_name": defn["display_name"],
                    "description": defn["description"],
                    "input_schema_json": json.dumps(
                        defn["input_schema"], ensure_ascii=False
                    ),
                }
            )

        return {
            "catalog": {
                "catalog_version": catalog_version,
                "tools": tools,
            }
        }

    def call_tool(
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Dispatch a tool call to the WeCom CLI."""
        tool_id = request.get("provider_tool_id", "")
        args_raw = request.get("arguments_json", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
        except Exception as exc:
            return {
                "is_error": True,
                "content_json": json.dumps(
                    {"error": f"Failed to parse arguments_json: {exc}"},
                    ensure_ascii=False,
                ),
            }

        try:
            result = self._dispatch_tool(tool_id, args)
            return {
                "is_error": False,
                "content_json": json.dumps(result, ensure_ascii=False),
            }
        except WeComCliError as exc:
            return {
                "is_error": True,
                "content_json": json.dumps(
                    {
                        "error": str(exc),
                        "exit_code": exc.returncode,
                        "stderr": exc.stderr,
                        "stdout": exc.stdout,
                    },
                    ensure_ascii=False,
                ),
            }
        except Exception as exc:
            logger.error("Error executing tool %s: %s", tool_id, exc, exc_info=True)
            return {
                "is_error": True,
                "content_json": json.dumps({"error": str(exc)}, ensure_ascii=False),
            }

    def _dispatch_tool(self, tool_id: str, args: dict[str, Any]) -> Any:
        if tool_id == "wecom_auth_status":
            if not self._cli_enabled:
                return {
                    "enabled": False,
                    "available": False,
                    "auth_status": "disabled",
                }
            status = self._cli.get_status()
            return {
                "enabled": True,
                "available": status.available,
                "version": status.version,
                "auth_status": status.auth_status,
                "executable_path": status.executable_path,
            }

        if tool_id == "wecom_contact_search":
            self._require_cli_enabled()
            kw = _argument_text(args, "keywords")
            return self._cli.execute("contact", "users", ["search", "--keywords", kw])

        if tool_id == "wecom_calendar_list":
            self._require_cli_enabled()
            return self._cli.execute("calendar", "schedules", ["list"])

        if tool_id == "wecom_calendar_create":
            self._require_cli_enabled()
            self._require_write_approval(tool_id, args)
            cmd_args = ["create", "--title", _argument_text(args, "title")]
            if "start_time" in args:
                cmd_args.extend(["--start-time", _argument_text(args, "start_time")])
            if "end_time" in args:
                cmd_args.extend(["--end-time", _argument_text(args, "end_time")])
            if "description" in args:
                cmd_args.extend(["--description", _argument_text(args, "description")])
            return self._cli.execute("calendar", "schedules", cmd_args)

        if tool_id == "wecom_todo_list":
            self._require_cli_enabled()
            return self._cli.execute("todo", "list", [])

        if tool_id == "wecom_todo_create":
            self._require_cli_enabled()
            self._require_write_approval(tool_id, args)
            title = _argument_text(args, "title")
            return self._cli.execute("todo", "create", ["--title", title])

        if tool_id == "wecom_todo_finish":
            self._require_cli_enabled()
            self._require_write_approval(tool_id, args)
            todo_id = _argument_text(args, "todo_id")
            return self._cli.execute("todo", "finish", ["--todo-id", todo_id])

        if tool_id == "wecom_doc_search":
            self._require_cli_enabled()
            keyword = _argument_text(args, "keyword")
            return self._cli.execute("doc", "search", ["--keyword", keyword])

        if tool_id == "wecom_operation_catalog":
            self._require_cli_enabled()
            return {
                "enabled_operations": [
                    "auth.status",
                    "contact.users.search",
                    "calendar.schedules.list",
                    "todo.list",
                    "doc.search",
                    *sorted(self._approved_write_operations),
                ],
                "available_write_operations": sorted(_WRITE_OPERATIONS.values()),
                "approved_write_operations": sorted(
                    self._approved_write_operations
                ),
                "write_policy": (
                    "A write requires that exact operation in binding configuration "
                    "and the same write_approval value in the invocation."
                ),
                "arbitrary_executor": False,
            }

        if tool_id == "wecom_connection_health":
            if self._health_provider is None:
                return {
                    "human_cli": {
                        "enabled": self._cli_enabled,
                        "auth_status": "unknown",
                    }
                }
            return dict(self._health_provider())

        raise ValueError(f"Unknown WeCom tool: {tool_id!r}")

    def _require_cli_enabled(self) -> None:
        if not self._cli_enabled:
            raise WeComCliError("WeCom human CLI profile is disabled for this binding")

    def _require_write_approval(
        self, tool_id: str, args: Mapping[str, Any]
    ) -> None:
        operation = _WRITE_OPERATIONS[tool_id]
        if operation not in self._approved_write_operations:
            raise WeComCliError(
                "operator approval is required for "
                f"{operation}; add that exact operation to approved_write_operations"
            )
        if args.get("write_approval") != operation:
            raise WeComCliError(
                f"write_approval must exactly match approved operation {operation}"
            )

    # ── Protobuf Adapters ──────────────────────────────────────────────

    def list_tools_proto(
        self,
        request_bytes: bytes,
    ) -> bytes:
        """Decode ListToolsRequest, invoke, and return serialized response."""
        req = tool_contract.ListToolsRequest()
        if request_bytes:
            req.ParseFromString(request_bytes)

        bid = req.binding_id if req.HasField("binding_id") else None
        dict_res = self.list_tools({"binding_id": bid})
        resp = tool_contract.ListToolsResponse()
        catalog_dict = dict_res["catalog"]
        resp.catalog.catalog_version = catalog_dict["catalog_version"]
        for t in catalog_dict["tools"]:
            td = resp.catalog.tools.add()
            td.binding_id = t["binding_id"]
            td.provider_tool_id = t["provider_tool_id"]
            td.display_name = t["display_name"]
            td.description = t["description"]
            td.input_schema_json = t["input_schema_json"]

        return resp.SerializeToString()

    def call_tool_proto(
        self,
        request_bytes: bytes,
    ) -> bytes:
        """Decode CallToolRequest, invoke, and return serialized response."""
        req = tool_contract.CallToolRequest()
        req.ParseFromString(request_bytes)

        dict_res = self.call_tool(
            {
                "binding_id": req.binding_id,
                "provider_tool_id": req.provider_tool_id,
                "arguments_json": req.arguments_json,
            }
        )

        resp = tool_contract.CallToolResponse()
        resp.outcome.is_error = dict_res.get("is_error", False)
        part = resp.outcome.content.add()
        part.json.json = dict_res.get("content_json", "{}")

        return resp.SerializeToString()


def _argument_text(args: Mapping[str, Any], name: str) -> str:
    """Validate one bounded CLI argument before constructing the argv vector."""
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise WeComCliError(f"tool argument {name!r} must be non-empty text")
    if len(value.encode("utf-8")) > 4096 or any(ord(char) < 32 for char in value):
        raise WeComCliError(f"tool argument {name!r} contains invalid text")
    return value
