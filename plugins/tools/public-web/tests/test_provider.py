from __future__ import annotations

import hashlib
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cyrene_integration_contracts import ToolEnvelope, serialize_tool_envelope, sign_context
from cyrene_tool_provider_contracts import (
    CAPABILITY_ID,
    METHOD_CALL_TOOL,
    decode_response,
    encode_request,
)
from cyrene_tool_provider_contracts import (
    tool_provider_pb2 as wire,
)

from cyrene_public_web.artifact_resolver import ResolvedArtifact
from cyrene_public_web.plugin import PublicWebProvider
from cyrene_public_web.settings import PublicWebSettings, TrustedCaller
from cyrene_public_web.tool_catalog import PLUGIN_VERSION

_WORKLOAD = "cyrene.exchange.integrations"
_AUDIENCE = "cyrene.tools.public-web"
_ISSUER = "https://id.example.test"
_SUBJECT = "actor-one"
_ORG = "org-one"
_WORKSPACE = "workspace-one"
_BINDING = "public-web-binding"
_KEY = b"v" * 32
_CURSOR_KEY = b"c" * 32


class _Handler(BaseHTTPRequestHandler):
    page = (
        b"<html><head><title>Public Test Page</title></head><body>"
        b"<script>private-js</script><p>first page</p><p>second page</p>"
        b"</body></html>"
    )
    cursor_seen: str | None = None

    def do_GET(self):
        if self.path.startswith("/search?"):
            self.__class__.cursor_seen = self.path
            from urllib.parse import parse_qs, urlsplit

            query = parse_qs(urlsplit(self.path).query)
            if query.get("cursor") == ["backend-next"]:
                body = json.dumps(
                    {
                        "results": [
                            {
                                "title": "second result",
                                "url": f"http://127.0.0.1:{self.server.server_port}/page",
                                "snippet": "another snippet",
                                "source": "local peer",
                            }
                        ],
                        "next_cursor": None,
                        "total": 2,
                    }
                ).encode()
            else:
                body = json.dumps(
                    {
                        "results": [
                            {
                                "title": "<b>first</b> result",
                                "url": f"http://127.0.0.1:{self.server.server_port}/page",
                                "snippet": "<b>visible</b> snippet",
                                "source": "local peer",
                            }
                        ],
                        "next_cursor": "backend-next",
                        "total": 2,
                    }
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/page":
            body = self.page
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/document.txt":
            body = b"first document block\nsecond document block\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        else:
            self.send_response(404)
            body = b"missing"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class _Peer:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


class _FakeParser:
    def parse_document(self, **kwargs):
        source_path = kwargs["source_path"]
        source_digest = kwargs["source_digest"]
        source_ref = kwargs["source_ref"]
        text = source_path.read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if line]
        blocks = [
            {
                "id": "block-" + str(index),
                "ordinal": index,
                "kind": "line",
                "text": line,
                "locator": {
                    "source_pages": [],
                    "section_path": [],
                    "item_ref": f"line:{index + 1}",
                    "tree_level": 0,
                    "provenance": [{"adapter": "test", "local_path": str(source_path)}],
                },
            }
            for index, line in enumerate(lines)
        ]
        kwargs["blocks_path"].write_text(
            json.dumps(
                {
                    "schema_version": "cyrene.document.blocks.v1",
                    "source_ref": source_ref,
                    "source_digest": source_digest,
                    "blocks": blocks,
                }
            ),
            encoding="utf-8",
        )
        kwargs["result_path"].write_text(
            json.dumps(
                {
                    "source_ref": source_ref,
                    "source_digest": source_digest,
                    "warnings": [],
                    "diagnostics": [],
                    "unsupported_content": [],
                }
            ),
            encoding="utf-8",
        )
        kwargs["payload_path"].write_text("{}", encoding="utf-8")
        return {
            "source_format": "TXT",
            "page_count": None,
            "slide_count": None,
            "sheet_count": None,
        }


class _FakeArtifactResolver:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def read(self, artifact_id, *, tool_id, arguments, caller_context, cancellation=None):
        self.calls.append((artifact_id, dict(arguments), dict(caller_context)))
        return ResolvedArtifact(
            artifact_id=artifact_id,
            data=self.payload,
            mime_type="text/plain",
            sha256=hashlib.sha256(self.payload).hexdigest(),
            size=len(self.payload),
        )


def _settings(tmp_path: Path, peer: _Peer) -> PublicWebSettings:
    state_dir = tmp_path / "public-web-state"
    state_dir.mkdir(mode=0o700)
    return PublicWebSettings(
        binding_id=_BINDING,
        trusted_callers=(
            TrustedCaller(
                caller_workload=_WORKLOAD,
                audience=_AUDIENCE,
                organization_id=_ORG,
                workspace_id=_WORKSPACE,
                issuer=_ISSUER,
                verification_key=_KEY,
            ),
        ),
        state_dir=state_dir,
        search_endpoint=peer.origin + "/search",
        search_bearer_token="peer-secret",
        artifact_bridge_origin="http://127.0.0.1:45678",
        artifact_bridge_token="b" * 64,
        cursor_key=_CURSOR_KEY,
        loopback_test_origins=frozenset({peer.origin}),
        timeout_seconds=3,
        max_page_size=10,
    )


def _invoke(
    provider: PublicWebProvider,
    tool_id: str,
    arguments: dict[str, Any],
    *,
    subject: str = _SUBJECT,
    request_id: str | None = None,
):
    context = sign_context(
        method=tool_id,
        operation_id=tool_id,
        payload=arguments,
        caller_workload=_WORKLOAD,
        audience=_AUDIENCE,
        issuer=_ISSUER,
        subject=subject,
        organization_id=_ORG,
        workspace_id=_WORKSPACE,
        binding_id=_BINDING,
        request_id=request_id or str(uuid.uuid4()),
        key=_KEY,
    )
    envelope = serialize_tool_envelope(ToolEnvelope(caller_context=context, arguments=arguments))
    request = wire.CallToolRequest(
        binding_id=_BINDING,
        provider_tool_id=tool_id,
        catalog_version=PLUGIN_VERSION,
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )
    typed = encode_request(METHOD_CALL_TOOL, request)
    ok, response = provider.on_invoke(
        CAPABILITY_ID,
        METHOD_CALL_TOOL,
        typed.value,
        request_type_url=typed.type_url,
    )
    assert ok is True
    decoded = decode_response(METHOD_CALL_TOOL, response)
    return decoded.outcome.is_error, json.loads(decoded.outcome.content[0].json.json)


@pytest.fixture
def peer():
    instance = _Peer()
    try:
        yield instance
    finally:
        instance.close()


def test_web_search_returns_sanitized_results_and_actor_bound_cursor(tmp_path, peer):
    provider = PublicWebProvider(settings=_settings(tmp_path, peer), document_parser=_FakeParser())
    first_error, first = _invoke(provider, "web.search", {"query": "public test", "limit": 1})
    assert first_error is False
    assert first["items"][0]["title"] == "<b>first</b> result"
    assert first["items"][0]["snippet"] == "visible snippet"
    assert first["items"][0]["source_ref"].startswith("sha256:")
    assert first["next_cursor"]

    second_error, second = _invoke(
        provider,
        "web.search",
        {"query": "public test", "limit": 1, "cursor": first["next_cursor"]},
    )
    assert second_error is False
    assert second["items"][0]["title"] == "second result"
    assert second["next_cursor"] is None
    assert "cursor=backend-next" in _Handler.cursor_seen

    tampered_error, tampered = _invoke(
        provider,
        "web.search",
        {"query": "different query", "limit": 1, "cursor": first["next_cursor"]},
    )
    assert tampered_error is True
    assert tampered["error"]["code"] == "INVALID_CURSOR"


def test_web_open_extracts_visible_content_and_paginates(tmp_path, peer):
    provider = PublicWebProvider(settings=_settings(tmp_path, peer), document_parser=_FakeParser())
    first_error, first = _invoke(
        provider,
        "web.open",
        {"url": peer.origin + "/page", "chunk_chars": 1000},
    )
    assert first_error is False
    assert first["title"] == "Public Test Page"
    assert "first page" in first["content"]
    assert "private-js" not in first["content"]
    assert first["content_sha256"]
    assert first["source_ref"].startswith("sha256:")
    assert first["next_cursor"] is None


def test_document_read_reuses_parser_and_returns_source_locators(tmp_path, peer):
    provider = PublicWebProvider(settings=_settings(tmp_path, peer), document_parser=_FakeParser())
    first_error, first = _invoke(
        provider,
        "document.read",
        {"url": peer.origin + "/document.txt", "limit": 1},
    )
    assert first_error is False
    assert first["source_format"] == "TXT"
    assert first["items"][0]["text"] == "first document block"
    assert first["items"][0]["locator"]["item_ref"] == "line:1"
    assert "local_path" not in json.dumps(first)
    assert first["next_cursor"]

    second_error, second = _invoke(
        provider,
        "document.read",
        {"url": peer.origin + "/document.txt", "limit": 1, "cursor": first["next_cursor"]},
    )
    assert second_error is False
    assert second["items"][0]["text"] == "second document block"
    assert second["next_cursor"] is None


def test_document_read_calls_existing_document_parser_for_text(tmp_path, peer):
    provider = PublicWebProvider(settings=_settings(tmp_path, peer))
    is_error, result = _invoke(
        provider,
        "document.read",
        {"url": peer.origin + "/document.txt", "limit": 10},
    )
    assert is_error is False
    assert result["source_format"] == "TXT"
    assert [item["text"].strip() for item in result["items"]] == [
        "first document block",
        "second document block",
    ]


def test_document_read_artifact_uses_only_task_resolver(tmp_path, peer):
    content = b"authorized attachment\n"
    artifact_id = hashlib.sha256(content).hexdigest()
    resolver = _FakeArtifactResolver(content)
    provider = PublicWebProvider(
        settings=_settings(tmp_path, peer),
        artifact_resolver=resolver,
        document_parser=_FakeParser(),
    )
    is_error, result = _invoke(
        provider,
        "document.read",
        {"artifact_id": artifact_id, "limit": 5},
    )
    assert is_error is False
    assert result["items"][0]["text"] == "authorized attachment"
    assert resolver.calls[0][0] == artifact_id
    assert resolver.calls[0][2]["subject"] == _SUBJECT


def test_provider_fails_closed_when_search_backend_missing(tmp_path, peer):
    settings = _settings(tmp_path, peer)
    settings = PublicWebSettings(
        binding_id=settings.binding_id,
        trusted_callers=settings.trusted_callers,
        state_dir=settings.state_dir,
        search_endpoint=None,
        search_bearer_token=None,
        artifact_bridge_origin=settings.artifact_bridge_origin,
        artifact_bridge_token=settings.artifact_bridge_token,
        cursor_key=settings.cursor_key,
        loopback_test_origins=settings.loopback_test_origins,
    )
    provider = PublicWebProvider(settings=settings, document_parser=_FakeParser())
    is_error, result = _invoke(provider, "web.search", {"query": "test"})
    assert is_error is True
    assert result["error"]["code"] == "NOT_CONFIGURED"


def test_cursor_cannot_be_reused_by_another_actor(tmp_path, peer):
    provider = PublicWebProvider(settings=_settings(tmp_path, peer), document_parser=_FakeParser())
    _, first = _invoke(provider, "web.search", {"query": "public test", "limit": 1})
    is_error, result = _invoke(
        provider,
        "web.search",
        {"query": "public test", "limit": 1, "cursor": first["next_cursor"]},
        subject="actor-two",
    )
    assert is_error is True
    assert result["error"]["code"] == "INVALID_CURSOR"
