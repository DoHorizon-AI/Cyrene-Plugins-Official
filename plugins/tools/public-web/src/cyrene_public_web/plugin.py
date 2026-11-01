"""Signed public search, page reading, and source-backed document tools."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from cyrene_integration_contracts import TOOL_OPERATIONS, canonical_json, verify_context
from cyrene_plugin_runtime.configuration import read_environment_settings
from cyrene_tool_provider_contracts import (
    CAPABILITY_ID,
    METHOD_CALL_TOOL,
    METHOD_LIST_TOOLS,
    REQUEST_TYPE_URLS,
    TypedPayload,
    decode_request,
    encode_response,
)
from cyrene_tool_provider_contracts import tool_provider_pb2 as wire
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .artifact_resolver import ArtifactReadError, WorkArtifactResolver
from .cursors import CursorError, actor_scope_digest, decode_cursor, encode_cursor, query_digest
from .html_text import extract_visible_text
from .public_http import PublicHttpError, fetch_get, validate_result_url
from .settings import ConfigurationError, PublicWebSettings
from .tool_catalog import CANONICAL_TOOLS, PLUGIN_ID, PLUGIN_VERSION, TOOL_BY_ID, operation_for

MAX_TOOL_REQUEST_BYTES = 512 * 1024
MAX_TOOL_OUTPUT_BYTES = 512 * 1024
MAX_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DOCUMENT_SOURCE_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BLOCKS_BYTES = 16 * 1024 * 1024
MAX_SEARCH_QUERY_CHARS = 500
MAX_SEARCH_BACKEND_CURSOR = 2048
MAX_BLOCK_TEXT_CHARS = 12_000
MAX_DOCUMENT_BLOCKS = 100_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_MEDIA_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
_SUPPORTED_DOCUMENT_TYPES = frozenset(_MEDIA_BY_EXTENSION.values())


class PublicWebError(RuntimeError):
    """A public-web request failed without exposing provider or private details."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class PublicWebProvider:
    """Implement fixed tool operations over the shared signed ToolEnvelope."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: PublicWebSettings | None = None,
        artifact_resolver: Any = None,
        document_parser: Any = None,
    ) -> None:
        if settings is None:
            if read_environment_settings() is None:
                raise ConfigurationError("public web activation is required")
            settings = PublicWebSettings.from_environment()
        self._settings = settings
        self._resolver = artifact_resolver or WorkArtifactResolver(
            endpoint=settings.artifact_bridge_origin,
            bridge_token=settings.artifact_bridge_token,
            timeout_seconds=settings.timeout_seconds,
        )
        self._owns_resolver = artifact_resolver is None
        self._document_parser = document_parser

    def close(self) -> None:
        if self._owns_resolver:
            self._resolver.close()

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
        if cancellation_event is None:
            cancellation_event = cancellation
        if capability != CAPABILITY_ID:
            return False, "INVALID_REQUEST: unsupported capability"
        if len(payload) > MAX_TOOL_REQUEST_BYTES:
            return False, "INVALID_REQUEST: request exceeds its size limit"
        if action == METHOD_LIST_TOOLS:
            if request_type_url not in (None, REQUEST_TYPE_URLS[METHOD_LIST_TOOLS]):
                return False, "INVALID_REQUEST: catalog type does not match"
            return self._list_tools(payload)
        if action == METHOD_CALL_TOOL:
            if request_type_url not in (None, REQUEST_TYPE_URLS[METHOD_CALL_TOOL]):
                return False, "INVALID_REQUEST: tool request type does not match"
            return self._call_tool(payload, cancellation_event)
        return False, "METHOD_NOT_FOUND: unsupported provider method"

    def _list_tools(self, payload: bytes) -> tuple[bool, Any]:
        try:
            request = decode_request(
                METHOD_LIST_TOOLS,
                TypedPayload(REQUEST_TYPE_URLS[METHOD_LIST_TOOLS], payload),
            )
        except (TypeError, ValueError):
            return False, "INVALID_REQUEST: catalog request is malformed"
        if request.HasField("binding_id") and request.binding_id != self._settings.binding_id:
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
                definition["input_schema"], ensure_ascii=False, separators=(",", ":")
            )
        return True, encode_response(METHOD_LIST_TOOLS, response)

    def _call_tool(self, payload: bytes, cancellation: Any) -> tuple[bool, Any]:
        try:
            request = decode_request(
                METHOD_CALL_TOOL,
                TypedPayload(REQUEST_TYPE_URLS[METHOD_CALL_TOOL], payload),
            )
            envelope = json.loads(request.arguments_json, object_pairs_hook=_unique_object)
        except (ValueError, TypeError, json.JSONDecodeError):
            return False, "INVALID_REQUEST: tool request is malformed"
        if request.binding_id != self._settings.binding_id:
            return False, "INVALID_REQUEST: binding ID does not match"
        if request.HasField("catalog_version") and request.catalog_version != PLUGIN_VERSION:
            return False, "INVALID_REQUEST: catalog version does not match"
        tool_id = request.provider_tool_id
        definition = TOOL_BY_ID.get(tool_id)
        if definition is None:
            return True, _tool_error("TOOL_NOT_FOUND")
        if TOOL_OPERATIONS.get(tool_id) != operation_for(tool_id):
            return True, _tool_error("NOT_CONFIGURED", message="operation registry is unavailable")
        try:
            arguments, caller_context, actor = self._authorize(tool_id, envelope)
            Draft202012Validator(definition["input_schema"]).validate(arguments)
        except ValidationError:
            return True, _tool_error("INVALID_ARGUMENTS")
        except (KeyError, TypeError, ValueError):
            return True, _tool_error("AUTHENTICATION_DENIED")

        try:
            _check_cancelled(cancellation)
            if tool_id == "web.search":
                result = self._search(arguments, actor, cancellation)
            elif tool_id == "web.open":
                result = self._open(arguments, actor, cancellation)
            else:
                result = self._read_document(arguments, actor, caller_context, cancellation)
            _check_cancelled(cancellation)
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        except InterruptedError:
            raise
        except CursorError:
            return True, _tool_error("INVALID_CURSOR")
        except ArtifactReadError as error:
            return True, _tool_error(error.code, retryable=error.code.endswith("UNAVAILABLE"))
        except PublicHttpError as error:
            return True, _tool_error(error.code, retryable=error.retryable)
        except PublicWebError as error:
            return True, _tool_error(error.code, retryable=error.retryable)
        except Exception:
            return True, _tool_error("PROVIDER_FAILURE", retryable=True)
        if len(encoded.encode("utf-8")) > MAX_TOOL_OUTPUT_BYTES:
            return True, _tool_error("OUTPUT_LIMIT_EXCEEDED")
        return True, _tool_response(encoded)

    def _authorize(
        self,
        tool_id: str,
        envelope: Any,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], dict[str, str | None]]:
        if not isinstance(envelope, Mapping) or set(envelope) != {"caller_context", "arguments"}:
            raise ValueError("shared ToolEnvelope is invalid")
        context = envelope.get("caller_context")
        arguments = envelope.get("arguments")
        if not isinstance(context, Mapping) or not isinstance(arguments, Mapping):
            raise ValueError("shared ToolEnvelope values must be objects")
        actor: dict[str, str | None] | None = None
        for caller in self._settings.trusted_callers:
            if (
                context.get("caller_workload") != caller.caller_workload
                or context.get("audience") != caller.audience
                or context.get("organization_id") != caller.organization_id
                or context.get("workspace_id") != caller.workspace_id
                or context.get("issuer") != caller.issuer
            ):
                continue
            try:
                valid = verify_context(
                    method=tool_id,
                    operation_id=tool_id,
                    payload=dict(arguments),
                    context=dict(context),
                    key=caller.verification_key,
                    expected_caller_workload=caller.caller_workload,
                    expected_audience=caller.audience,
                    expected_binding_id=self._settings.binding_id,
                    expected_organization_id=caller.organization_id,
                    expected_workspace_id=caller.workspace_id,
                    expected_issuer=caller.issuer,
                )
            except (TypeError, ValueError):
                valid = False
            if valid:
                actor = {
                    "issuer": caller.issuer,
                    "subject": _required_claim(context, "subject"),
                    "organization_id": caller.organization_id,
                    "workspace_id": caller.workspace_id,
                    "binding_id": self._settings.binding_id,
                }
                break
        if actor is None:
            raise ValueError("caller signature or binding is invalid")
        return arguments, context, actor

    def _search(
        self,
        arguments: Mapping[str, Any],
        actor: Mapping[str, str | None],
        cancellation: Any,
    ) -> dict[str, Any]:
        endpoint = self._settings.search_endpoint
        if endpoint is None:
            raise PublicWebError("NOT_CONFIGURED")
        query = _normalize_query(arguments["query"])
        limit = int(arguments.get("limit", self._settings.max_page_size))
        query_value = {"query": query, "limit": limit}
        scope = self._cursor_scope(actor)
        digest = query_digest(query_value)
        backend_cursor: str | None = None
        if "cursor" in arguments:
            payload = decode_cursor(
                str(arguments["cursor"]),
                key=self._settings.cursor_key,
                expected_kind="web.search",
                expected_scope=scope,
                expected_query_digest=digest,
            )
            backend_cursor = str(payload["position"])
        search_url = _append_query(
            endpoint,
            {
                "q": query,
                "limit": str(limit),
                **({"cursor": backend_cursor} if backend_cursor else {}),
            },
        )
        headers = {}
        if self._settings.search_bearer_token is not None:
            headers["Authorization"] = f"Bearer {self._settings.search_bearer_token}"
        _check_cancelled(cancellation)
        response = fetch_get(
            search_url,
            timeout_seconds=self._settings.timeout_seconds,
            max_response_bytes=MAX_SEARCH_RESPONSE_BYTES,
            loopback_test_origins=self._settings.loopback_test_origins,
            extra_headers=headers,
            accept="application/json",
            cancellation=cancellation,
        )
        _check_cancelled(cancellation)
        document = _json_object(response.body)
        if set(document) - {"results", "next_cursor", "total"}:
            raise PublicWebError("SEARCH_RESPONSE_INVALID")
        results = document.get("results")
        if not isinstance(results, list) or len(results) > 100:
            raise PublicWebError("SEARCH_RESPONSE_INVALID")
        items: list[dict[str, Any]] = []
        rejected = 0
        for item in results:
            if len(items) >= limit:
                break
            if not isinstance(item, Mapping) or set(item) - {
                "title",
                "url",
                "snippet",
                "source",
                "published_at",
            }:
                rejected += 1
                continue
            title = _bounded_text(item.get("title"), 512)
            raw_url = _bounded_text(item.get("url"), 4096)
            snippet = _plain_result_text(item.get("snippet"), 2000)
            try:
                canonical_url = validate_result_url(
                    raw_url, self._settings.loopback_test_origins
                )
            except PublicHttpError:
                rejected += 1
                continue
            source = _bounded_text(item.get("source", ""), 256)
            published_at = _bounded_text(item.get("published_at", ""), 64)
            source_ref = _source_ref(
                {
                    "url": canonical_url,
                    "title": title,
                    "snippet_sha256": hashlib.sha256(snippet.encode("utf-8")).hexdigest(),
                }
            )
            items.append(
                {
                    "title": title,
                    "url": canonical_url,
                    "snippet": snippet,
                    "source": source or _host(canonical_url),
                    "published_at": published_at or None,
                    "source_ref": source_ref,
                }
            )
        next_backend_cursor = document.get("next_cursor")
        next_cursor = None
        if next_backend_cursor is not None:
            if (
                not isinstance(next_backend_cursor, str)
                or not next_backend_cursor
                or len(next_backend_cursor) > MAX_SEARCH_BACKEND_CURSOR
            ):
                raise PublicWebError("SEARCH_RESPONSE_INVALID")
            next_cursor = _encode_cursor(
                self._settings,
                kind="web.search",
                scope=scope,
                digest=digest,
                position=next_backend_cursor,
            )
        return {
            "query": query,
            "items": items,
            "next_cursor": next_cursor,
            "rejected_result_count": rejected,
            "backend_total": _optional_bounded_int(document.get("total"), 0, 1_000_000),
        }

    def _open(
        self,
        arguments: Mapping[str, Any],
        actor: Mapping[str, str | None],
        cancellation: Any,
    ) -> dict[str, Any]:
        url = str(arguments["url"])
        chunk_chars = int(arguments.get("chunk_chars", 12_000))
        _check_cancelled(cancellation)
        response = fetch_get(
            url,
            timeout_seconds=self._settings.timeout_seconds,
            max_response_bytes=MAX_SEARCH_RESPONSE_BYTES,
            loopback_test_origins=self._settings.loopback_test_origins,
            cancellation=cancellation,
        )
        _check_cancelled(cancellation)
        if response.content_type not in _HTML_TYPES:
            raise PublicWebError("NOT_HTML", retryable=False)
        text, title, text_truncated = extract_visible_text(response.body, response.charset)
        content_digest = hashlib.sha256(response.body).hexdigest()
        source_ref = _source_ref(
            {"kind": "web.open", "url": response.requested_url, "content_sha256": content_digest}
        )
        scope = self._cursor_scope(actor)
        digest = query_digest(
            {
                "url": response.requested_url,
                "content_sha256": content_digest,
                "chunk_chars": chunk_chars,
            }
        )
        offset = _cursor_offset(
            self._settings,
            arguments.get("cursor"),
            kind="web.open",
            scope=scope,
            digest=digest,
        )
        if offset > len(text):
            raise CursorError("cursor exceeds visible text length")
        end = min(len(text), offset + chunk_chars)
        next_cursor = None
        if end < len(text):
            next_cursor = _encode_cursor(
                self._settings,
                kind="web.open",
                scope=scope,
                digest=digest,
                position=end,
            )
        return {
            "source_ref": source_ref,
            "url": response.requested_url,
            "title": title,
            "content_type": response.content_type,
            "content_sha256": content_digest,
            "content": text[offset:end],
            "char_offset": offset,
            "total_chars": len(text),
            "content_truncated": text_truncated,
            "next_cursor": next_cursor,
        }

    def _read_document(
        self,
        arguments: Mapping[str, Any],
        actor: Mapping[str, str | None],
        caller_context: Mapping[str, Any],
        cancellation: Any,
    ) -> dict[str, Any]:
        if "artifact_id" in arguments:
            _check_cancelled(cancellation)
            artifact = self._resolver.read(
                str(arguments["artifact_id"]),
                tool_id="document.read",
                arguments=dict(arguments),
                caller_context=caller_context,
                cancellation=cancellation,
            )
            source_bytes = artifact.data
            media_type = artifact.mime_type
            filename = f"attachment-{artifact.artifact_id[:16]}{_extension_for(media_type)}"
            source_identity = {"artifact_id": artifact.artifact_id}
        else:
            response = fetch_get(
                str(arguments["url"]),
                timeout_seconds=self._settings.timeout_seconds,
                max_response_bytes=MAX_DOCUMENT_SOURCE_BYTES,
                loopback_test_origins=self._settings.loopback_test_origins,
                cancellation=cancellation,
            )
            _check_cancelled(cancellation)
            source_bytes = response.body
            media_type = _document_media_type(response.content_type, response.requested_url)
            filename = _filename_from_url(response.requested_url, media_type)
            source_identity = {"url": response.requested_url}
        if not source_bytes or len(source_bytes) > MAX_DOCUMENT_SOURCE_BYTES:
            raise PublicWebError("DOCUMENT_SIZE_LIMIT")
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        scope = self._cursor_scope(actor)
        source_ref = _source_ref(
            {"scope_digest": scope, **source_identity, "source_digest": source_digest}
        )
        if media_type in _HTML_TYPES:
            text, title, text_truncated = extract_visible_text(source_bytes, None)
            blocks = _html_blocks(text, source_ref, source_digest)
            report = {
                "source_format": "HTML",
                "page_count": 1,
                "warnings": (
                    ["Visible HTML text was truncated at the configured extraction limit."]
                    if text_truncated
                    else []
                ),
                "diagnostics": [],
                "unsupported_content": [],
            }
            if title is not None:
                filename = filename or f"{title[:80]}.html"
        elif media_type in _SUPPORTED_DOCUMENT_TYPES:
            blocks, report = self._parse_document_bytes(
                source_bytes,
                media_type,
                filename,
                source_ref,
                f"sha256:{source_digest}",
                cancellation,
            )
        else:
            raise PublicWebError("UNSUPPORTED_MEDIA")
        if len(blocks) > MAX_DOCUMENT_BLOCKS:
            blocks = blocks[:MAX_DOCUMENT_BLOCKS]
            report["warnings"] = list(report.get("warnings", [])) + [
                "Block result limit reached; remaining source blocks were omitted."
            ]
        limit = int(arguments.get("limit", self._settings.max_page_size))
        cursor_digest = query_digest(
            {
                "source_ref": source_ref,
                "source_digest": source_digest,
                "source_format": report.get("source_format"),
                "limit": limit,
            }
        )
        offset = _cursor_offset(
            self._settings,
            arguments.get("cursor"),
            kind="document.read",
            scope=scope,
            digest=cursor_digest,
        )
        if offset > len(blocks):
            raise CursorError("cursor exceeds document block count")
        page = [_public_block(block) for block in blocks[offset : offset + limit]]
        next_offset = offset + len(page)
        next_cursor = None
        if next_offset < len(blocks):
            next_cursor = _encode_cursor(
                self._settings,
                kind="document.read",
                scope=scope,
                digest=cursor_digest,
                position=next_offset,
            )
        return {
            "source_ref": source_ref,
            "source_url": source_identity.get("url"),
            "source_artifact_id": source_identity.get("artifact_id"),
            "source_digest": f"sha256:{source_digest}",
            "filename": filename,
            "media_type": media_type,
            "source_format": report.get("source_format"),
            "block_count": len(blocks),
            "page_count": report.get("page_count"),
            "slide_count": report.get("slide_count"),
            "sheet_count": report.get("sheet_count"),
            "warnings": _bounded_text_list(report.get("warnings"), 20, 1000),
            "diagnostics": _bounded_diagnostic_list(report.get("diagnostics"), 20),
            "unsupported_content": _bounded_json_list(report.get("unsupported_content"), 20),
            "items": page,
            "offset": offset,
            "next_cursor": next_cursor,
        }

    def _parse_document_bytes(
        self,
        source_bytes: bytes,
        media_type: str,
        filename: str,
        source_ref: str,
        source_digest: str,
        cancellation: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        _check_cancelled(cancellation)
        parser = self._document_parser
        parser_error_type: type[BaseException] | None = None
        if parser is None:
            try:
                from document_parsing import DocumentParsingPlugin, ParserExecutionError

                parser = DocumentParsingPlugin()
                parser_error_type = ParserExecutionError
            except ImportError:
                raise PublicWebError("DOCUMENT_PARSER_UNAVAILABLE") from None
        try:
            with tempfile.TemporaryDirectory(
                prefix="public-web-", dir=self._settings.state_dir
            ) as raw_dir:
                staging = Path(raw_dir)
                source_path = staging / ("source" + _extension_for(media_type))
                payload_path = staging / "payload.json"
                blocks_path = staging / "blocks.json"
                result_path = staging / "result.json"
                descriptor = os.open(
                    source_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(source_bytes)
                parse_summary = parser.parse_document(
                    source_path=source_path,
                    payload_path=payload_path,
                    blocks_path=blocks_path,
                    result_path=result_path,
                    source_ref=source_ref,
                    source_digest=source_digest,
                    filename=filename,
                    media_type=media_type,
                )
                _check_cancelled(cancellation)
                raw_blocks = _read_json_file(blocks_path, MAX_DOCUMENT_BLOCKS_BYTES)
                report = _read_json_file(result_path, MAX_DOCUMENT_BLOCKS_BYTES)
        except InterruptedError:
            raise
        except PublicWebError:
            raise
        except Exception as error:
            if parser_error_type is not None and isinstance(error, parser_error_type):
                raise PublicWebError("PARSER_FAILURE") from None
            raise PublicWebError("DOCUMENT_READ_FAILED") from None
        if (
            not isinstance(parse_summary, Mapping)
            or not isinstance(raw_blocks, Mapping)
            or raw_blocks.get("source_ref") != source_ref
            or raw_blocks.get("source_digest") != source_digest
            or not isinstance(raw_blocks.get("blocks"), list)
            or not isinstance(report, Mapping)
            or report.get("source_ref") != source_ref
            or report.get("source_digest") != source_digest
        ):
            raise PublicWebError("PARSER_RESULT_INVALID")
        blocks = raw_blocks["blocks"]
        if len(blocks) > MAX_DOCUMENT_BLOCKS:
            raise PublicWebError("PARSER_RESULT_LIMIT")
        return list(blocks), {
            "source_format": parse_summary.get("source_format"),
            "page_count": parse_summary.get("page_count"),
            "slide_count": parse_summary.get("slide_count"),
            "sheet_count": parse_summary.get("sheet_count"),
            "warnings": report.get("warnings", []),
            "diagnostics": report.get("diagnostics", []),
            "unsupported_content": report.get("unsupported_content", []),
        }

    def _cursor_scope(self, actor: Mapping[str, str | None]) -> str:
        return actor_scope_digest(
            issuer=str(actor["issuer"]),
            subject=str(actor["subject"]),
            organization_id=str(actor["organization_id"]),
            workspace_id=actor.get("workspace_id"),
            binding_id=self._settings.binding_id,
            key=self._settings.cursor_key,
        )


def _tool_response(value: str) -> TypedPayload:
    response = wire.CallToolResponse()
    response.outcome.is_error = False
    response.outcome.content.add().json.json = value
    return encode_response(METHOD_CALL_TOOL, response)


def _tool_error(
    code: str,
    *,
    retryable: bool = False,
    message: str | None = None,
) -> TypedPayload:
    response = wire.CallToolResponse()
    response.outcome.is_error = True
    error: dict[str, Any] = {"code": code, "retryable": retryable}
    if message is not None:
        error["message"] = message
    response.outcome.content.add().json.json = json.dumps(
        {"error": error}, ensure_ascii=False, separators=(",", ":")
    )
    return encode_response(METHOD_CALL_TOOL, response)


def _check_cancelled(cancellation: Any) -> None:
    if cancellation is None:
        return
    is_set = getattr(cancellation, "is_set", None)
    if (callable(is_set) and is_set()) or getattr(cancellation, "cancelled", False):
        raise InterruptedError("public web operation cancelled")


def _normalize_query(value: object) -> str:
    if not isinstance(value, str):
        raise PublicWebError("INVALID_ARGUMENTS")
    query = " ".join(value.split())
    if not query or len(query) > MAX_SEARCH_QUERY_CHARS or any(ord(char) < 0x20 for char in query):
        raise PublicWebError("INVALID_ARGUMENTS")
    return query


def _append_query(endpoint: str, parameters: Mapping[str, str]) -> str:
    parts = urlsplit(endpoint)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(parameters), ""))


def _json_object(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise PublicWebError("SEARCH_RESPONSE_INVALID") from None
    if not isinstance(value, dict):
        raise PublicWebError("SEARCH_RESPONSE_INVALID")
    return value


def _plain_result_text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]{0,1024}>", " ", value)
    return " ".join(html.unescape(text).split())[:maximum]


def _bounded_text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise PublicWebError("SEARCH_RESPONSE_INVALID")
    if any(ord(char) < 0x20 and char not in "\t\n\r" for char in value):
        raise PublicWebError("SEARCH_RESPONSE_INVALID")
    return value.strip()


def _source_ref(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(dict(value))).hexdigest()


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""


def _encode_cursor(
    settings: PublicWebSettings,
    *,
    kind: str,
    scope: str,
    digest: str,
    position: str | int,
) -> str:
    return encode_cursor(
        {
            "kind": kind,
            "scope_digest": scope,
            "query_digest": digest,
            "position": position,
            "expires_at": int(time.time()) + 30 * 60,
        },
        settings.cursor_key,
    )


def _cursor_offset(
    settings: PublicWebSettings,
    value: object,
    *,
    kind: str,
    scope: str,
    digest: str,
) -> int:
    if value is None:
        return 0
    payload = decode_cursor(
        str(value),
        key=settings.cursor_key,
        expected_kind=kind,
        expected_scope=scope,
        expected_query_digest=digest,
    )
    position = payload["position"]
    if isinstance(position, bool) or not isinstance(position, int):
        raise CursorError("cursor position must be numeric")
    return position


def _optional_bounded_int(value: object, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PublicWebError("SEARCH_RESPONSE_INVALID")
    return value


def _document_media_type(content_type: str, url: str) -> str:
    normalized = content_type.split(";", maxsplit=1)[0].strip().lower()
    if normalized in _HTML_TYPES:
        return normalized
    if normalized == "application/json":
        return "text/plain"
    if normalized in _SUPPORTED_DOCUMENT_TYPES:
        return normalized
    extension = Path(urlsplit(url).path).suffix.lower()
    fallback = _MEDIA_BY_EXTENSION.get(extension)
    if fallback is not None:
        return fallback
    raise PublicWebError("UNSUPPORTED_MEDIA")


def _extension_for(media_type: str) -> str:
    if media_type in _HTML_TYPES:
        return ".html"
    for extension, candidate in _MEDIA_BY_EXTENSION.items():
        if candidate == media_type:
            return extension
    return ".bin"


def _filename_from_url(url: str, media_type: str) -> str:
    leaf = Path(urlsplit(url).path).name
    leaf = re.sub(r"[^A-Za-z0-9._-]+", "_", leaf)[:100].strip("._-")
    if not leaf:
        return "web-document" + _extension_for(media_type)
    if not Path(leaf).suffix:
        leaf += _extension_for(media_type)
    return leaf


def _read_json_file(path: Path, maximum_bytes: int) -> dict[str, Any]:
    import stat

    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > maximum_bytes:
            raise PublicWebError("PARSER_RESULT_LIMIT")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            body = stream.read(maximum_bytes + 1)
    except PublicWebError:
        raise
    except OSError:
        raise PublicWebError("PARSER_RESULT_INVALID") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(body) > maximum_bytes:
        raise PublicWebError("PARSER_RESULT_LIMIT")
    try:
        result = json.loads(body, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise PublicWebError("PARSER_RESULT_INVALID") from None
    if not isinstance(result, dict):
        raise PublicWebError("PARSER_RESULT_INVALID")
    return result


def _html_blocks(text: str, source_ref: str, source_digest: str) -> list[dict[str, Any]]:
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    blocks: list[dict[str, Any]] = []
    for paragraph_index, paragraph in enumerate(paragraphs):
        for chunk_index, start in enumerate(range(0, len(paragraph), MAX_BLOCK_TEXT_CHARS)):
            body = paragraph[start : start + MAX_BLOCK_TEXT_CHARS]
            ordinal = len(blocks)
            item_ref = f"paragraph:{paragraph_index}:chunk:{chunk_index}"
            identity = hashlib.sha256(
                f"{source_ref}\0{source_digest}\0{item_ref}".encode()
            ).hexdigest()
            blocks.append(
                {
                    "id": "sha256:" + identity,
                    "ordinal": ordinal,
                    "kind": "paragraph",
                    "text": body,
                    "locator": {
                        "source_pages": [],
                        "section_path": [],
                        "item_ref": item_ref,
                        "tree_level": 0,
                    },
                }
            )
            if len(blocks) > MAX_DOCUMENT_BLOCKS:
                return blocks
    return blocks


def _public_block(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PublicWebError("PARSER_RESULT_INVALID")
    block_id = value.get("id")
    ordinal = value.get("ordinal")
    kind = value.get("kind")
    text = value.get("text")
    locator = value.get("locator")
    if (
        not isinstance(block_id, str)
        or len(block_id) > 128
        or isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or not isinstance(kind, str)
        or len(kind) > 64
        or not isinstance(text, str)
        or not isinstance(locator, Mapping)
    ):
        raise PublicWebError("PARSER_RESULT_INVALID")
    return {
        "id": block_id,
        "ordinal": ordinal,
        "kind": kind,
        "text": text[:MAX_BLOCK_TEXT_CHARS],
        "locator": _public_locator(locator),
    }


def _public_locator(value: Mapping[str, Any]) -> dict[str, Any]:
    pages = value.get("source_pages", [])
    sections = value.get("section_path", [])
    item_ref = value.get("item_ref", "")
    tree_level = value.get("tree_level", 0)
    if not isinstance(pages, list) or len(pages) > 100:
        pages = []
    pages = [
        page for page in pages if isinstance(page, int) and not isinstance(page, bool) and page > 0
    ][:100]
    if not isinstance(sections, list) or len(sections) > 32:
        sections = []
    sections = [section[:256] for section in sections if isinstance(section, str)][:32]
    if not isinstance(item_ref, str):
        item_ref = ""
    if isinstance(tree_level, bool) or not isinstance(tree_level, int) or not 0 <= tree_level <= 64:
        tree_level = 0
    result: dict[str, Any] = {
        "source_pages": pages,
        "section_path": sections,
        "item_ref": item_ref[:512],
        "tree_level": tree_level,
    }
    table_index = value.get("table_index")
    if isinstance(table_index, int) and not isinstance(table_index, bool) and table_index >= 0:
        result["table_index"] = table_index
    return result


def _bounded_text_list(value: object, maximum_count: int, maximum_text: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:maximum_text] for item in value[:maximum_count] if isinstance(item, str)]


def _bounded_diagnostic_list(value: object, maximum_count: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:maximum_count]:
        if not isinstance(item, Mapping):
            continue
        code = item.get("code")
        message = item.get("message")
        kind = item.get("kind")
        if not isinstance(code, str) or not isinstance(message, str):
            continue
        result.append(
            {
                "code": code[:128],
                "message": message[:1000],
                "kind": kind[:64] if isinstance(kind, str) else "parser",
            }
        )
    return result


def _bounded_json_list(value: object, maximum_count: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:maximum_count]:
        if not isinstance(item, Mapping):
            continue
        projected: dict[str, Any] = {}
        for key in ("kind", "code", "message"):
            entry = item.get(key)
            if isinstance(entry, str):
                projected[key] = entry[:1000]
        locator = item.get("locator")
        if isinstance(locator, Mapping):
            projected["locator"] = _public_locator(locator)
        if projected:
            result.append(projected)
    return result


def _required_claim(context: Mapping[str, Any], name: str) -> str:
    value = context.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError("signed caller identity is incomplete")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object field")
        result[key] = value
    return result
