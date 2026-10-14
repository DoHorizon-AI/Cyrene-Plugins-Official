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
from .authorization import (
    AuthorizationSessionError,
    AuthorizationTrustConfig,
    WeComAuthorizationSessions,
    owner_fingerprint,
)
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

_AUTHORIZATION_TOOL_IDS = frozenset(
    {
        "wecom_authorization_health",
        "wecom_reauthorization_start",
        "wecom_reauthorization_status",
    }
)
_AUTHORIZATION_CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "method": {"type": "string", "minLength": 1},
        "caller_workload": {"type": "string", "minLength": 1},
        "audience": {"type": "string", "minLength": 1},
        "issuer": {"type": "string", "minLength": 1},
        "subject": {"type": "string", "minLength": 1},
        "organization_id": {"type": "string", "minLength": 1},
        "workspace_id": {"type": "string", "minLength": 1},
        "binding_id": {"type": "string", "minLength": 1},
        "request_id": {"type": "string", "minLength": 1},
        "issued_at": {"type": "string", "format": "date-time"},
        "expires_at": {"type": "string", "format": "date-time"},
        "operation_id": {"type": "string", "minLength": 1},
        "payload_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "signature": {"type": "string", "minLength": 1},
    },
    "required": [
        "method",
        "caller_workload",
        "audience",
        "issuer",
        "subject",
        "organization_id",
        "workspace_id",
        "binding_id",
        "request_id",
        "issued_at",
        "expires_at",
        "operation_id",
        "payload_sha256",
        "signature",
    ],
    "additionalProperties": False,
}


def _authorization_envelope_schema(
    arguments_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the required signed-caller envelope for one fixed auth tool."""
    return {
        "type": "object",
        "properties": {
            "caller_context": _AUTHORIZATION_CONTEXT_SCHEMA,
            "arguments": dict(arguments_schema),
        },
        "required": ["caller_context", "arguments"],
        "additionalProperties": False,
    }


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
    {
        "provider_tool_id": "wecom_authorization_health",
        "display_name": "WeCom Authorization Health",
        "description": (
            "Check the signed binding's human CLI profile and the confirmed "
            "message-session read grant. Unknown expiry remains null."
        ),
        "input_schema": _authorization_envelope_schema(
            {
                "type": "object",
                "properties": {"authorization_ref": {"type": "string", "minLength": 1}},
                "required": ["authorization_ref"],
                "additionalProperties": False,
            }
        ),
    },
    {
        "provider_tool_id": "wecom_reauthorization_start",
        "display_name": "Start WeCom Reauthorization",
        "description": (
            "Start a signed, actor-owned official WeCom authorization session "
            "for this binding's native CLI profile."
        ),
        "input_schema": _authorization_envelope_schema(
            {
                "type": "object",
                "properties": {"authorization_ref": {"type": "string", "minLength": 1}},
                "required": ["authorization_ref"],
                "additionalProperties": False,
            }
        ),
    },
    {
        "provider_tool_id": "wecom_reauthorization_status",
        "display_name": "WeCom Reauthorization Status",
        "description": (
            "Poll one actor-owned WeCom authorization session and revalidate "
            "the same CLI profile and read grant."
        ),
        "input_schema": _authorization_envelope_schema(
            {
                "type": "object",
                "properties": {
                    "session_ref": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    }
                },
                "required": ["session_ref"],
                "additionalProperties": False,
            }
        ),
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
        authorization_config: AuthorizationTrustConfig | None = None,
        authorization_profile_dir: str | None = None,
        authorization_session_ttl_seconds: int = 300,
    ) -> None:
        self._binding_id = binding_id
        self._cli = cli_client or WeComCliClient()
        self._cli_enabled = cli_enabled
        self._approved_write_operations = frozenset(approved_write_operations)
        self._health_provider = health_provider
        self._authorization_config = authorization_config
        self._authorization_sessions = (
            WeComAuthorizationSessions(
                binding_id,
                self._cli,
                cli_enabled=cli_enabled,
                profile_dir=authorization_profile_dir,
                authorization_ref=authorization_config.authorization_ref,
                session_ttl_seconds=authorization_session_ttl_seconds,
            )
            if authorization_config is not None
            else None
        )

    @property
    def binding_id(self) -> str:
        return self._binding_id

    def list_tools(
        self,
        request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the dictionary catalog of available tools."""
        catalog_version = "v1.5.0"
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
        except AuthorizationToolError as exc:
            return {
                "is_error": True,
                "content_json": json.dumps(
                    {"error": exc.message, "code": exc.code}, ensure_ascii=False
                ),
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
        if tool_id in _AUTHORIZATION_TOOL_IDS:
            return self._dispatch_authorization_tool(tool_id, args)

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
                "approved_write_operations": sorted(self._approved_write_operations),
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

    def _dispatch_authorization_tool(
        self, tool_id: str, envelope: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Verify the exact signed payload before touching private session state."""
        try:
            if set(envelope) != {"caller_context", "arguments"}:
                raise AuthorizationSessionError("invalid_request")
            context_value = envelope.get("caller_context")
            inner = envelope.get("arguments")
            if not isinstance(context_value, Mapping) or not isinstance(inner, Mapping):
                raise AuthorizationSessionError("invalid_request")
            if tool_id in {
                "wecom_authorization_health",
                "wecom_reauthorization_start",
            }:
                if set(inner) != {"authorization_ref"} or not isinstance(
                    inner.get("authorization_ref"), str
                ):
                    raise AuthorizationSessionError("invalid_request")
            elif set(inner) != {"session_ref"} or not isinstance(
                inner.get("session_ref"), str
            ):
                raise AuthorizationSessionError("invalid_request")

            context = self._verify_authorization_context(tool_id, context_value, inner)
            sessions = self._authorization_sessions
            if sessions is None:
                raise AuthorizationSessionError("authorization_not_configured")
            if (
                tool_id
                in {
                    "wecom_authorization_health",
                    "wecom_reauthorization_start",
                }
                and inner["authorization_ref"] != sessions.authorization_ref
            ):
                raise AuthorizationSessionError("authorization_ref_mismatch")
            if tool_id == "wecom_authorization_health":
                import cyrene_integration_contracts as contracts

                contracts.parse_wecom_authorization_health_arguments(dict(inner))
                return _serialize_authorization_health(sessions.health())
            if tool_id == "wecom_reauthorization_start":
                import cyrene_integration_contracts as contracts

                contracts.parse_wecom_reauthorization_start_arguments(dict(inner))
                if context.subject not in self._authorization_config.operator_subjects:
                    raise AuthorizationSessionError("operator_not_authorized")
                try:
                    idempotency_key, payload_hash = contracts.write_replay_identity(
                        context, dict(inner)
                    )
                except (AttributeError, TypeError, ValueError):
                    raise AuthorizationSessionError(
                        "authorization_contracts_unavailable"
                    ) from None
                record = sessions.start(
                    owner_fingerprint(context, self._binding_id),
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    request_alias=context.request_id,
                )
                return _serialize_renewal_session(record, sessions.authorization_ref)
            import cyrene_integration_contracts as contracts

            contracts.parse_wecom_reauthorization_status_arguments(dict(inner))
            record = sessions.status(
                owner_fingerprint(context, self._binding_id),
                str(inner["session_ref"]),
            )
            return _serialize_renewal_session(record, sessions.authorization_ref)
        except AuthorizationSessionError as exc:
            raise _safe_authorization_error(exc.code) from None
        except AuthorizationToolError:
            raise
        except Exception:
            raise _safe_authorization_error("authorization_operation_failed") from None

    def _verify_authorization_context(
        self,
        tool_id: str,
        context_value: Mapping[str, Any],
        inner: Mapping[str, Any],
    ) -> Any:
        config = self._authorization_config
        if config is None or config.key is None:
            raise AuthorizationSessionError("authorization_not_configured")
        expected_context_fields = set(_AUTHORIZATION_CONTEXT_SCHEMA["required"])
        if set(context_value) != expected_context_fields:
            raise AuthorizationSessionError("caller_context_invalid")
        try:
            import cyrene_integration_contracts as contracts

            context = contracts.parse_trusted_caller_context(dict(context_value))
            operation_id = contracts.TOOL_OPERATIONS[tool_id]
            verified = contracts.verify_context(
                method=tool_id,
                operation_id=operation_id,
                payload=dict(inner),
                context=context,
                key=config.key,
                expected_caller_workload=config.caller_workload,
                expected_audience=config.audience,
                expected_binding_id=self._binding_id,
                expected_organization_id=config.organization_id,
                expected_workspace_id=config.workspace_id,
                expected_issuer=config.issuer,
            )
        except (AttributeError, ImportError, KeyError, TypeError, ValueError):
            raise AuthorizationSessionError(
                "authorization_contracts_unavailable"
            ) from None
        if not verified:
            raise AuthorizationSessionError("caller_context_invalid")
        return context

    def close(self) -> None:
        """Cancel active official CLI onboarding work during provider shutdown."""
        if self._authorization_sessions is not None:
            self._authorization_sessions.close()

    def _require_cli_enabled(self) -> None:
        if not self._cli_enabled:
            raise WeComCliError("WeCom human CLI profile is disabled for this binding")

    def _require_write_approval(self, tool_id: str, args: Mapping[str, Any]) -> None:
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


class AuthorizationToolError(RuntimeError):
    """Safe public error for the signed authorization tool family."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(code)


def _safe_authorization_error(code: str) -> AuthorizationToolError:
    """Map private failures to bounded messages with no raw vendor details."""
    messages = {
        "invalid_request": "The authorization request shape is invalid.",
        "caller_context_invalid": "The signed caller context is invalid or expired.",
        "authorization_contracts_unavailable": (
            "The shared authorization contract runtime is unavailable."
        ),
        "authorization_not_configured": (
            "Signed authorization tools are not configured for this binding."
        ),
        "authorization_ref_mismatch": (
            "The authorization reference does not match this binding."
        ),
        "authorization_operation_failed": (
            "The authorization operation could not be completed safely."
        ),
        "authorization_start_ambiguous": (
            "An earlier activation outcome is unresolved. Check this profile on its "
            "host before requesting another start."
        ),
        "operator_not_authorized": (
            "This caller is not an approved operator for the binding."
        ),
        "cli_disabled": "The binding has no enabled native WeCom CLI profile.",
        "cli_unavailable": "The official WeCom CLI is unavailable on this host.",
        "cli_version_unsupported": (
            "The installed WeCom CLI version does not support this authorization flow."
        ),
        "profile_identity_unknown": (
            "The existing CLI profile identity could not be verified."
        ),
        "profile_identity_changed": (
            "The CLI profile identity changed; an explicit relink is required."
        ),
        "login_state_unknown": "The native CLI login state could not be confirmed.",
        "authorization_already_valid": (
            "The configured message-session grant is already valid."
        ),
        "renewal_not_confirmed": (
            "The read-only probe did not confirm an expired or denied grant."
        ),
        "official_grant_url_unavailable": (
            "The official CLI exposed no supported grant URL. Authorize this same "
            "profile through WeCom's official settings, then retry."
        ),
        "session_busy": (
            "Another actor-owned renewal session is active for this binding."
        ),
        "idempotency_conflict": (
            "This renewal request ID was already used with different signed arguments."
        ),
        "session_not_found": (
            "The renewal session was not found for this caller and binding."
        ),
        "provider_closed": "The WeCom authorization provider is shutting down.",
        "private_store_unavailable": (
            "The binding's private authorization state is unavailable."
        ),
    }
    return AuthorizationToolError(
        code if code in messages else "authorization_operation_failed",
        messages.get(code, messages["authorization_operation_failed"]),
    )


def _serialize_authorization_health(value: Mapping[str, Any]) -> dict[str, Any]:
    """Construct and serialize the shared typed authorization health DTO."""
    try:
        import cyrene_integration_contracts as contracts

        capabilities = tuple(
            contracts.CapabilityAuthorizationHealth(
                capability_id=item["capability_id"],
                state=contracts.AuthorizationState(item["state"]),
                last_verified_at=item["last_verified_at"],
                expires_at=item["expires_at"],
                reason_code=item["reason_code"],
            )
            for item in value["capabilities"]
        )
        health = contracts.AuthorizationHealth(
            schema_version=value["schema_version"],
            authorization_ref=value["authorization_ref"],
            login_state=contracts.LoginState(value["login_state"]),
            authorization_state=contracts.AuthorizationState(
                value["authorization_state"]
            ),
            checked_at=value["checked_at"],
            expires_at=value["expires_at"],
            generation=value["generation"],
            capabilities=capabilities,
        )
        serialized = contracts.serialize_authorization_health(health)
    except (ImportError, KeyError, TypeError, ValueError):
        raise _safe_authorization_error("authorization_contracts_unavailable") from None
    return _json_mapping(serialized)


def _serialize_renewal_session(
    value: Mapping[str, Any], authorization_ref: str
) -> dict[str, Any]:
    """Construct and serialize one typed renewal session without private owners."""
    try:
        import cyrene_integration_contracts as contracts

        session = contracts.RenewalSession(
            session_ref=value["session_ref"],
            authorization_ref=authorization_ref,
            state=contracts.RenewalState(value["state"]),
            created_at=_parse_rfc3339(value["created_at"]),
            expires_at=_parse_rfc3339(value["expires_at"]),
            authorization_url=value.get("authorization_url"),
            qr_attachment_ref=None,
            generation=value["generation"],
        )
        serialized = contracts.serialize_renewal_session(session)
    except (ImportError, KeyError, TypeError, ValueError):
        raise _safe_authorization_error("authorization_contracts_unavailable") from None
    return _json_mapping(serialized)


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise _safe_authorization_error("authorization_contracts_unavailable")


def _parse_rfc3339(value: str) -> Any:
    from datetime import datetime

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed
