"""Signed, registry-bound DirectPluginRuntime knowledge search provider.

中文：验签后解析服务端主体映射和固定 ArtifactRef，再检索授权分块。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any

import jsonschema
from cyrene_integration_contracts import (
    TOOL_OPERATIONS,
    canonical_json,
    parse_trusted_caller_context,
    serialize_trusted_caller_context,
    verify_context,
)
from cyrene_tool_provider_contracts import (
    CALL_TOOL_REQUEST_TYPE_URL,
    CAPABILITY_ID,
    LIST_TOOLS_REQUEST_TYPE_URL,
    METHOD_CALL_TOOL,
    METHOD_LIST_TOOLS,
    ToolProviderCodecError,
    TypedPayload,
    decode_request,
    encode_response,
    tool_provider_pb2,
)

from .bundle import MAX_SEARCH_OFFSET, BundleError, resolve_bundle_path, search_bundle
from .settings import BundleArtifact, ConfigurationError, RetrievalSettings

PLUGIN_ID = "cyrene.providers.knowledge-retrieval"
PLUGIN_VERSION = "0.1.0"
TOOL_ID = "knowledge.search"
CALLER_CONTEXT_METHOD = "knowledge.search"
CALLER_CONTEXT_OPERATION = "knowledge.search"
_MAX_CURSOR_CHARS = 1_024
_MAX_CURSOR_PAYLOAD_BYTES = 512
_CURSOR_ENCODING = re.compile(r"[A-Za-z0-9_-]+\Z")
_SELECTOR = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "librarySelector": {"type": "string", "minLength": 1, "maxLength": 128},
        "query": {"type": "string", "minLength": 1, "maxLength": 16_384},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "chunkCursor": {"type": "string", "minLength": 1, "maxLength": _MAX_CURSOR_CHARS},
    },
    "required": ["librarySelector", "query", "limit"],
    "additionalProperties": False,
}
_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "librarySelector",
        "packageDigest",
        "manifestDigest",
        "contentRevisionId",
        "usePurpose",
        "query",
        "hits",
    ],
    "properties": {
        "librarySelector": {"type": "string", "minLength": 1, "maxLength": 128},
        "packageDigest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "manifestDigest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "contentRevisionId": {"type": "string", "minLength": 1},
        "usePurpose": {"const": "knowledge_retrieval"},
        "query": {"type": "string"},
        "hits": {"type": "array", "items": {"type": "object"}},
        "nextCursor": {"type": "string", "maxLength": _MAX_CURSOR_CHARS},
    },
    "additionalProperties": False,
}
_TOOL_DEFINITION = {
    "provider_tool_id": TOOL_ID,
    "display_name": "Search Authorized Knowledge",
    "description": (
        "Search one configured knowledge library. The host supplies a signed caller context; "
        "the provider resolves the library and principal grants from binding configuration."
    ),
}


class KnowledgeRetrievalProvider:
    """Serve one fixed search tool after authenticating the host caller context."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(self, *, settings: RetrievalSettings | None = None) -> None:
        self._settings = settings

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        **_: Any,
    ) -> tuple[bool, Any]:
        """Dispatch canonical tool catalog and signed invocation payloads."""

        if capability != CAPABILITY_ID:
            return False, "INVALID_REQUEST: unsupported capability"
        try:
            if action == METHOD_LIST_TOOLS:
                if request_type_url not in (None, LIST_TOOLS_REQUEST_TYPE_URL):
                    return False, "INVALID_REQUEST: catalog payload type mismatch"
                typed_request = TypedPayload(
                    type_url=request_type_url or LIST_TOOLS_REQUEST_TYPE_URL,
                    value=payload,
                )
                return self._list_tools(typed_request)
            if action == METHOD_CALL_TOOL:
                if request_type_url not in (None, CALL_TOOL_REQUEST_TYPE_URL):
                    return False, "INVALID_REQUEST: tool payload type mismatch"
                typed_request = TypedPayload(
                    type_url=request_type_url or CALL_TOOL_REQUEST_TYPE_URL,
                    value=payload,
                )
                return self._call_tool(typed_request)
            return False, "METHOD_NOT_FOUND: unsupported provider method"
        except ConfigurationError:
            return False, "INVALID_CONFIGURATION: provider binding is unavailable"

    def _list_tools(self, payload: TypedPayload) -> tuple[bool, Any]:
        try:
            request = decode_request(METHOD_LIST_TOOLS, payload)
        except ToolProviderCodecError:
            return False, "INVALID_REQUEST: catalog request is malformed"
        settings = self._require_settings()
        if request.HasField("binding_id") and request.binding_id != settings.binding_id:
            return False, "INVALID_REQUEST: binding ID does not match"
        response = tool_provider_pb2.ListToolsResponse()
        response.catalog.catalog_version = PLUGIN_VERSION
        descriptor = response.catalog.tools.add()
        descriptor.binding_id = settings.binding_id
        descriptor.provider_tool_id = TOOL_ID
        descriptor.display_name = _TOOL_DEFINITION["display_name"]
        descriptor.description = _TOOL_DEFINITION["description"]
        descriptor.input_schema_json = json.dumps(
            _search_schema(settings), ensure_ascii=False, separators=(",", ":")
        )
        descriptor.output_schema_json = json.dumps(
            _OUTPUT_SCHEMA, ensure_ascii=False, separators=(",", ":")
        )
        return True, encode_response(METHOD_LIST_TOOLS, response)

    def _call_tool(self, payload: TypedPayload) -> tuple[bool, Any]:
        try:
            request = decode_request(METHOD_CALL_TOOL, payload)
        except ToolProviderCodecError:
            return False, "INVALID_REQUEST: tool call payload is malformed"
        settings = self._require_settings()
        if request.binding_id != settings.binding_id:
            return False, "INVALID_REQUEST: binding ID does not match"
        if request.provider_tool_id != TOOL_ID:
            return False, "METHOD_NOT_FOUND: provider tool is not available"

        try:
            envelope = _loads_strict(request.arguments_json)
            if not isinstance(envelope, Mapping) or set(envelope) != {
                "caller_context",
                "arguments",
            }:
                raise _InvalidArguments("signed caller_context envelope is required")
            arguments = envelope["arguments"]
            _validate_arguments(arguments, settings)
        except (_InvalidArguments, TypeError, ValueError) as error:
            return True, _tool_error(
                tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS,
                str(error),
            )

        try:
            context = parse_trusted_caller_context(envelope["caller_context"])
            context_value = serialize_trusted_caller_context(context)
        except (TypeError, ValueError, jsonschema.ValidationError):
            return True, _tool_error(
                tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_PROVIDER_ERROR,
                "caller context is invalid or expired",
            )
        if not _verify_caller(settings, arguments, context, context_value):
            return True, _tool_error(
                tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_PROVIDER_ERROR,
                "caller context is invalid or expired",
            )

        try:
            principal_refs = settings.principals_for(
                issuer=context.issuer,
                subject=context.subject,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id or "",
            )
        except ConfigurationError:
            return True, _tool_error(
                tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_PROVIDER_ERROR,
                "verified caller has no configured principal grant",
            )

        selector = arguments["librarySelector"]
        try:
            artifact = settings.bundle(selector)
        except ConfigurationError:
            return True, _tool_error(
                tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS,
                "librarySelector is not configured for this binding",
            )
        try:
            bundle_path = resolve_bundle_path(settings.artifact_root, artifact.bundle_locator)
            if bundle_path.stat().st_size != artifact.size_bytes:
                raise BundleError("configured ArtifactRef size no longer matches the artifact")
            scope = _cursor_scope(settings, artifact, context_value, arguments, principal_refs)
            cursor_key = _cursor_key(settings.trusted_caller.verification_key)
            offset = _decode_chunk_cursor(arguments.get("chunkCursor"), scope, cursor_key)
            result = search_bundle(
                bundle_path,
                arguments["query"],
                principal_refs=principal_refs,
                use_purpose="knowledge_retrieval",
                package_digest=artifact.digest,
                expected_manifest_digest=artifact.manifest_digest,
                limit=arguments["limit"],
                offset=offset,
                max_bundle_bytes=settings.max_bundle_bytes,
                max_member_bytes=settings.max_member_bytes,
                max_records=settings.max_jsonl_records,
                max_limit=settings.max_search_limit,
                max_query_chars=settings.max_query_chars,
            )
        except _InvalidArguments as error:
            return True, _tool_error(
                tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS,
                str(error),
            )
        except (BundleError, OSError):
            return True, _tool_error(
                tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_PROVIDER_ERROR,
                "configured knowledge bundle failed verification or could not be read",
                retryable=False,
            )

        next_offset = result.pop("nextOffset", None)
        result["librarySelector"] = selector
        if next_offset is not None:
            result["nextCursor"] = _encode_chunk_cursor(next_offset, scope, cursor_key)
        response = tool_provider_pb2.CallToolResponse()
        response.outcome.content.add().json.json = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return True, encode_response(METHOD_CALL_TOOL, response)

    def _require_settings(self) -> RetrievalSettings:
        if self._settings is None:
            self._settings = RetrievalSettings.from_environment()
        return self._settings


class _InvalidArguments(ValueError):
    """A valid protobuf request with an invalid inner knowledge search object."""


def _search_schema(settings: RetrievalSettings) -> dict[str, Any]:
    schema = json.loads(json.dumps(_SEARCH_SCHEMA))
    schema["properties"]["librarySelector"]["enum"] = sorted(settings.bundle_registry)
    schema["properties"]["query"]["maxLength"] = settings.max_query_chars
    schema["properties"]["limit"]["maximum"] = settings.max_search_limit
    return schema


def _validate_arguments(value: Any, settings: RetrievalSettings) -> None:
    required = {"librarySelector", "query", "limit"}
    allowed = required | {"chunkCursor"}
    if not isinstance(value, Mapping) or not required.issubset(value) or set(value) - allowed:
        raise _InvalidArguments(
            "arguments must contain only librarySelector, query, limit, and chunkCursor"
        )
    selector = value["librarySelector"]
    if not isinstance(selector, str) or _SELECTOR.fullmatch(selector) is None:
        raise _InvalidArguments("librarySelector is invalid")
    query = value["query"]
    if not isinstance(query, str) or not query.strip() or len(query) > settings.max_query_chars:
        raise _InvalidArguments("query exceeds the configured limit")
    limit = value["limit"]
    if type(limit) is not int or not 1 <= limit <= settings.max_search_limit:
        raise _InvalidArguments("limit exceeds the configured bound")
    if "chunkCursor" in value and (
        not isinstance(value["chunkCursor"], str)
        or not value["chunkCursor"]
        or len(value["chunkCursor"]) > _MAX_CURSOR_CHARS
    ):
        raise _InvalidArguments("chunkCursor exceeds the configured limit")


def _verify_caller(
    settings: RetrievalSettings,
    arguments: Mapping[str, Any],
    context: Any,
    context_value: Mapping[str, Any],
) -> bool:
    caller = settings.trusted_caller
    if TOOL_OPERATIONS.get(CALLER_CONTEXT_METHOD) != CALLER_CONTEXT_OPERATION:
        return False
    return (
        verify_context(
            method=CALLER_CONTEXT_METHOD,
            operation_id=CALLER_CONTEXT_OPERATION,
            payload=dict(arguments),
            context=context,
            key=caller.verification_key,
            expected_caller_workload=caller.caller_workload,
            expected_audience=caller.audience,
            expected_binding_id=settings.binding_id,
            expected_organization_id=caller.organization_id,
            expected_workspace_id=caller.workspace_id,
            expected_issuer=caller.issuer,
        )
        and context_value.get("binding_id") == settings.binding_id
    )


def _cursor_scope(
    settings: RetrievalSettings,
    artifact: BundleArtifact,
    context: Mapping[str, Any],
    arguments: Mapping[str, Any],
    principal_refs: tuple[str, ...],
) -> str:
    scope = {
        "binding_id": settings.binding_id,
        "issuer": context["issuer"],
        "subject": context["subject"],
        "organization_id": context["organization_id"],
        "workspace_id": context["workspace_id"],
        "library_selector": artifact.library_selector,
        "package_digest": artifact.digest,
        "query": arguments["query"],
        "limit": arguments["limit"],
        "principal_refs": sorted(principal_refs),
    }
    return hashlib.sha256(canonical_json(scope)).hexdigest()


def _cursor_key(caller_verification_key: bytes) -> bytes:
    """Derive a cursor-only signing key with explicit domain separation."""

    return hmac.new(
        caller_verification_key,
        b"cyrene.knowledge-search.cursor.v1",
        hashlib.sha256,
    ).digest()


def _encode_chunk_cursor(offset: int, scope: str, key: bytes) -> str:
    if type(offset) is not int or not 1 <= offset <= MAX_SEARCH_OFFSET:
        raise _InvalidArguments("next search cursor exceeds the configured bound")
    payload = canonical_json({"v": 1, "offset": offset, "scope": scope})
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")
    if len(token) > _MAX_CURSOR_CHARS:
        raise _InvalidArguments("next search cursor exceeds the configured limit")
    return token


def _decode_chunk_cursor(value: Any, scope: str, key: bytes) -> int:
    if value is None or value == "":
        return 0
    if (
        not isinstance(value, str)
        or len(value) > _MAX_CURSOR_CHARS
        or _CURSOR_ENCODING.fullmatch(value) is None
    ):
        raise _InvalidArguments("chunkCursor encoding is invalid")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise _InvalidArguments("chunkCursor encoding is invalid") from error
    if (
        len(raw) <= 32
        or len(raw) > _MAX_CURSOR_PAYLOAD_BYTES + 32
        or base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value
    ):
        raise _InvalidArguments("chunkCursor size or encoding is invalid")
    payload, signature = raw[:-32], raw[-32:]
    if not hmac.compare_digest(signature, hmac.new(key, payload, hashlib.sha256).digest()):
        raise _InvalidArguments("chunkCursor signature is invalid")
    try:
        decoded = _loads_strict(payload.decode("utf-8"))
    except (UnicodeDecodeError, BundleError) as error:
        raise _InvalidArguments("chunkCursor payload is malformed") from error
    if (
        not isinstance(decoded, Mapping)
        or set(decoded) != {"v", "offset", "scope"}
        or type(decoded.get("v")) is not int
        or decoded["v"] != 1
        or type(decoded.get("offset")) is not int
        or not 1 <= decoded["offset"] <= MAX_SEARCH_OFFSET
        or decoded.get("scope") != scope
        or canonical_json(dict(decoded)) != payload
    ):
        raise _InvalidArguments("chunkCursor does not match this query scope")
    return decoded["offset"]


def _loads_strict(value: str) -> Any:
    if not isinstance(value, str) or not value:
        raise _InvalidArguments("tool arguments JSON is required")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise _InvalidArguments(f"duplicate tool argument {key!r}")
            result[key] = item
        return result

    def invalid_constant(value: str) -> None:
        raise _InvalidArguments(f"invalid JSON constant {value}")

    try:
        return json.loads(value, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    except json.JSONDecodeError as error:
        raise _InvalidArguments("tool arguments JSON is malformed") from error


def _tool_error(code: int, message: str, *, retryable: bool = False) -> TypedPayload:
    response = tool_provider_pb2.CallToolResponse()
    response.error.code = code
    response.error.message = message[:1_024]
    response.error.retryable = retryable
    return encode_response(METHOD_CALL_TOOL, response)
