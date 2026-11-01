"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 plugin.py                                                        │
│  Module: cyrene_media.plugin                                        │
│  Role: Expose signed media tools through DirectPluginRuntime.       │
│                                                                      │
│  模块职责：通过 DirectPluginRuntime 暴露签名媒体工具。               │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from cyrene_integration_contracts import (
    TOOL_OPERATIONS,
    parse_trusted_caller_context,
    serialize_trusted_caller_context,
    verify_context,
)
from cyrene_tool_provider_contracts import (
    CAPABILITY_ID,
    METHOD_CALL_TOOL,
    METHOD_LIST_TOOLS,
    REQUEST_TYPE_URLS,
    TypedPayload,
    decode_request,
    encode_response,
)
from cyrene_tool_provider_contracts import (
    tool_provider_pb2 as wire,
)
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .artifact_resolver import ArtifactReceipt
from .library import MAX_ASSET_BYTES, MediaError, MediaLibrary
from .settings import MediaSettings
from .tool_catalog import (
    CANONICAL_TOOLS,
    MAX_TOOL_JSON_BYTES,
    MAX_TOOL_REQUEST_BYTES,
    PLUGIN_ID,
    PLUGIN_VERSION,
    TOOL_BY_ID,
    operation_for,
)

_MAX_LIBRARY_FILE_BYTES = 10 * 1024 * 1024


class MediaProvider:
    """Serve actor-scoped media catalog operations over tool.provider.v1."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: MediaSettings | None = None,
        library: MediaLibrary | None = None,
        artifact_resolver: Any = None,
    ) -> None:
        self._settings = settings or MediaSettings.from_environment()
        self._library_override = library
        self._library_cache: OrderedDict[str, MediaLibrary] = OrderedDict()
        self._library_lock = threading.Lock()
        self._resolver = artifact_resolver or self._settings.artifact_resolver

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        cancellation: Any = None,
        cancellation_event: Any = None,
        **_: Any,
    ) -> tuple[bool, Any]:
        """Dispatch one typed catalog or signed tool request."""

        if cancellation_event is None:
            cancellation_event = cancellation
        if capability != CAPABILITY_ID:
            return False, "INVALID_REQUEST: unsupported capability"
        if len(payload) > MAX_TOOL_REQUEST_BYTES:
            return False, "INVALID_REQUEST: tool request exceeds its byte limit"
        if action == METHOD_LIST_TOOLS:
            if request_type_url not in (None, REQUEST_TYPE_URLS[METHOD_LIST_TOOLS]):
                return False, "INVALID_REQUEST: catalog payload type mismatch"
            return self._list_tools(payload)
        if action == METHOD_CALL_TOOL:
            if request_type_url not in (None, REQUEST_TYPE_URLS[METHOD_CALL_TOOL]):
                return False, "INVALID_REQUEST: tool payload type mismatch"
            return self._call_tool(payload, cancellation_event=cancellation_event)
        return False, "METHOD_NOT_FOUND: unsupported provider method"

    def _list_tools(self, payload: bytes) -> tuple[bool, Any]:
        """Return the static media tool catalog for this exact binding."""

        try:
            request = decode_request(
                METHOD_LIST_TOOLS,
                TypedPayload(REQUEST_TYPE_URLS[METHOD_LIST_TOOLS], payload),
            )
        except (ValueError, TypeError):
            return False, "INVALID_REQUEST: catalog request is malformed"
        if (
            request.HasField("binding_id")
            and request.binding_id != self._settings.binding_id
        ):
            return False, "INVALID_REQUEST: binding ID does not match"
        response = wire.ListToolsResponse()
        response.catalog.catalog_version = PLUGIN_VERSION
        for definition in CANONICAL_TOOLS:
            item = response.catalog.tools.add()
            item.binding_id = self._settings.binding_id
            item.provider_tool_id = str(definition["provider_tool_id"])
            item.display_name = str(definition["display_name"])
            item.description = str(definition["description"])
            item.input_schema_json = json.dumps(
                definition["input_schema"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return True, encode_response(METHOD_LIST_TOOLS, response)

    def _call_tool(
        self,
        payload: bytes,
        *,
        cancellation_event: Any = None,
    ) -> tuple[bool, Any]:
        """Verify the signed caller and validate exact arguments before execution."""

        try:
            request = decode_request(
                METHOD_CALL_TOOL,
                TypedPayload(REQUEST_TYPE_URLS[METHOD_CALL_TOOL], payload),
            )
            envelope = json.loads(
                request.arguments_json,
                object_pairs_hook=_unique_object,
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            return False, "INVALID_REQUEST: tool request is malformed"
        if request.binding_id != self._settings.binding_id:
            return False, "INVALID_REQUEST: binding ID does not match"
        if (
            request.HasField("catalog_version")
            and request.catalog_version != PLUGIN_VERSION
        ):
            return False, "INVALID_REQUEST: tool catalog version does not match"
        tool_id = request.provider_tool_id
        definition = TOOL_BY_ID.get(tool_id)
        if definition is None:
            return True, _tool_error("TOOL_NOT_FOUND")
        if not isinstance(envelope, Mapping) or set(envelope) != {
            "caller_context",
            "arguments",
        }:
            return True, _tool_error("AUTHENTICATION_DENIED")
        arguments = envelope.get("arguments")
        if not isinstance(arguments, Mapping):
            return True, _tool_error("INVALID_ARGUMENTS")
        try:
            Draft202012Validator(definition["input_schema"]).validate(arguments)
            context = parse_trusted_caller_context(envelope.get("caller_context"))
            self._verify_caller(tool_id, arguments, context)
            serialized_context = serialize_trusted_caller_context(context)
        except ValidationError:
            return True, _tool_error("INVALID_ARGUMENTS")
        except (KeyError, TypeError, ValueError):
            return True, _tool_error("AUTHENTICATION_DENIED")

        try:
            _check_cancelled(cancellation_event)
            result = self._execute(
                tool_id,
                arguments,
                caller_context=serialized_context,
                cancellation_event=cancellation_event,
            )
            _check_cancelled(cancellation_event)
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        except InterruptedError:
            raise
        except MediaError as error:
            return True, _tool_error(
                error.code, retryable=error.code == "PROVIDER_UNAVAILABLE"
            )
        except Exception:
            return True, _tool_error("PROVIDER_ERROR", retryable=True)
        if len(encoded.encode("utf-8")) > MAX_TOOL_JSON_BYTES:
            return True, _tool_error("OUTPUT_LIMIT_EXCEEDED")
        return True, _tool_response(encoded)

    def _verify_caller(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        context: Any,
    ) -> None:
        """Verify Exchange HMAC and immutable workspace/binding claims."""

        operation_id = operation_for(tool_id)
        if TOOL_OPERATIONS.get(tool_id) != operation_id:
            raise ValueError("media operation is not registered")
        for caller in self._settings.trusted_callers:
            if (
                caller.caller_workload == context.caller_workload
                and caller.audience == context.audience
                and caller.organization_id == context.organization_id
                and caller.workspace_id == context.workspace_id
                and caller.issuer == context.issuer
                and verify_context(
                    method=tool_id,
                    operation_id=operation_id,
                    payload=dict(arguments),
                    context=context,
                    key=caller.verification_key,
                    expected_caller_workload=caller.caller_workload,
                    expected_audience=caller.audience,
                    expected_binding_id=self._settings.binding_id,
                    expected_organization_id=caller.organization_id,
                    expected_workspace_id=caller.workspace_id,
                    expected_issuer=caller.issuer,
                )
            ):
                return
        raise ValueError("trusted caller context is invalid")

    def _execute(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        *,
        caller_context: Mapping[str, Any],
        cancellation_event: Any,
    ) -> dict[str, Any]:
        """Run one signed operation in the caller subject's private catalog."""

        library = self._library_for(caller_context)
        if tool_id == "media.asset.import":
            artifact = self._resolver.read(
                str(arguments["artifact_id"]),
                tool_id=tool_id,
                arguments=dict(arguments),
                caller_context=caller_context,
                cancellation_event=cancellation_event,
            )
            asset = library.put(
                artifact.data,
                title=str(arguments["title"]),
                alt_text=str(arguments.get("alt_text", "")),
                tags=tuple(arguments.get("tags", ())),
                expected_mime_type=artifact.mime_type,
            )
            return asset.as_dict()
        if tool_id == "media.asset.search":
            return library.list_assets(
                query=str(arguments.get("query", "")),
                limit=int(arguments.get("limit", 20)),
                cursor=arguments.get("cursor"),
            )
        if tool_id == "media.asset.get":
            asset, data = library.get(str(arguments["asset_id"]))
            receipt = self._resolver.write(
                data,
                asset.mime_type,
                {
                    "name": _artifact_name(
                        arguments.get("name"),
                        asset.asset_id,
                        asset.mime_type,
                    )
                },
                tool_id=tool_id,
                arguments=dict(arguments),
                caller_context=caller_context,
                cancellation_event=cancellation_event,
            )
            return {"asset": asset.as_dict(), "artifact": _receipt_dict(receipt)}
        if tool_id == "media.asset.delete":
            return {
                "asset_id": str(arguments["asset_id"]),
                "deleted": library.delete(str(arguments["asset_id"])),
            }
        if tool_id == "media.asset.restore":
            return library.restore(str(arguments["asset_id"])).as_dict()
        if tool_id == "media.card.render":
            asset, data = library.render_card(
                title=str(arguments["title"]),
                body=str(arguments["body"]),
                theme=str(arguments.get("theme", "light")),
            )
            receipt = self._resolver.write(
                data,
                "image/png",
                {"name": f"card-{asset.asset_id}.png"},
                tool_id=tool_id,
                arguments=dict(arguments),
                caller_context=caller_context,
                cancellation_event=cancellation_event,
            )
            return {"asset": asset.as_dict(), "artifact": _receipt_dict(receipt)}
        raise MediaError("TOOL_NOT_FOUND")

    def _library_for(self, caller_context: Mapping[str, Any]) -> MediaLibrary:
        """Partition state by signed identity, never request-selected paths."""

        if self._library_override is not None:
            return self._library_override
        scope_key = self._settings.actor_scope_key(
            issuer=str(caller_context["issuer"]),
            subject=str(caller_context["subject"]),
            organization_id=str(caller_context["organization_id"]),
            workspace_id=str(caller_context["workspace_id"]),
        )
        return self._library_for_scope(scope_key)

    def _library_for_scope(self, scope_key: str) -> MediaLibrary:
        """Cache a bounded number of lightweight SQLite catalog handles."""

        with self._library_lock:
            cached = self._library_cache.get(scope_key)
            if cached is not None:
                self._library_cache.move_to_end(scope_key)
                return cached
            library = MediaLibrary(
                self._settings.state_dir / "media-library" / scope_key,
                max_asset_bytes=min(MAX_ASSET_BYTES, _MAX_LIBRARY_FILE_BYTES),
            )
            self._library_cache[scope_key] = library
            if len(self._library_cache) > 64:
                self._library_cache.popitem(last=False)
            return library


def _tool_response(value: str) -> TypedPayload:
    """Wrap one bounded JSON operation result."""

    response = wire.CallToolResponse()
    response.outcome.is_error = False
    response.outcome.content.add().json.json = value
    return encode_response(METHOD_CALL_TOOL, response)


def _tool_error(code: str, *, retryable: bool = False) -> TypedPayload:
    """Return one sanitized fixed error envelope."""

    response = wire.CallToolResponse()
    response.outcome.is_error = True
    response.outcome.content.add().json.json = json.dumps(
        {"error": {"code": code, "retryable": retryable}},
        separators=(",", ":"),
    )
    return encode_response(METHOD_CALL_TOOL, response)


def _receipt_dict(receipt: ArtifactReceipt) -> dict[str, Any]:
    """Project only the opaque artifact reference and verified metadata."""

    return receipt.as_dict()


def _artifact_name(value: Any, asset_id: str, mime_type: str) -> str:
    """Choose bounded attachment display metadata without interpreting paths."""

    if isinstance(value, str) and value.strip():
        name = " ".join(value.split()).replace("/", "_").replace("\\", "_")
        if len(name) <= 512:
            return name
    suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime_type]
    return f"media-{asset_id}.{suffix}"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys in untrusted tool requests."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _check_cancelled(cancellation_event: Any) -> None:
    """Propagate host cancellation before output is reported as successful."""

    if cancellation_event is None:
        return
    is_set = getattr(cancellation_event, "is_set", None)
    if (callable(is_set) and is_set()) or getattr(
        cancellation_event, "cancelled", False
    ):
        raise InterruptedError("media operation cancelled")


__all__ = ["MediaProvider"]
