from __future__ import annotations

import base64
import hashlib
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cyrene_integration_contracts import payload_sha256

from cyrene_public_web.artifact_resolver import ArtifactReadError, WorkArtifactResolver


class _BridgeHandler(BaseHTTPRequestHandler):
    body = b"source text"
    wrong_digest = False
    seen_authorization: str | None = None
    seen_request: dict | None = None

    def do_POST(self):
        type(self).seen_authorization = self.headers.get("Authorization")
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen_request = request
        digest = hashlib.sha256(type(self).body).hexdigest()
        response = {
            "artifactId": request["artifactId"],
            "sha256": ("0" * 64) if type(self).wrong_digest else digest,
            "mimeType": "text/plain",
            "size": len(type(self).body),
            "contentBase64": base64.b64encode(type(self).body).decode("ascii"),
        }
        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


@pytest.fixture
def bridge():
    _BridgeHandler.wrong_digest = False
    _BridgeHandler.seen_authorization = None
    _BridgeHandler.seen_request = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _call_context(arguments):
    return {
        "method": "document.read",
        "request_id": str(uuid.uuid4()),
        "subject": "actor-one",
        "payload_sha256": payload_sha256(arguments),
    }


def test_artifact_bridge_receipt_is_digest_bound_and_source_scoped(bridge):
    body = b"source text"
    _BridgeHandler.body = body
    artifact_id = hashlib.sha256(body).hexdigest()
    arguments = {"artifact_id": artifact_id}
    context = _call_context(arguments)
    resolver = WorkArtifactResolver(
        endpoint=bridge,
        bridge_token="a" * 64,
        timeout_seconds=3,
    )
    result = resolver.read(
        artifact_id,
        tool_id="document.read",
        arguments=arguments,
        caller_context=context,
    )
    assert result.data == body
    assert result.mime_type == "text/plain"
    assert _BridgeHandler.seen_authorization == "Bearer " + "a" * 64
    assert _BridgeHandler.seen_request["callerContext"] == context
    assert _BridgeHandler.seen_request["toolId"] == "document.read"
    resolver.close()


def test_artifact_bridge_digest_mismatch_fails_closed(bridge):
    body = b"source text"
    _BridgeHandler.body = body
    _BridgeHandler.wrong_digest = True
    artifact_id = hashlib.sha256(body).hexdigest()
    arguments = {"artifact_id": artifact_id}
    resolver = WorkArtifactResolver(endpoint=bridge, bridge_token="a" * 64)
    with pytest.raises(ArtifactReadError, match="INTEGRITY_FAILURE"):
        resolver.read(
            artifact_id,
            tool_id="document.read",
            arguments=arguments,
            caller_context=_call_context(arguments),
        )
    resolver.close()


def test_artifact_bridge_rejects_non_loopback_endpoint_and_host_selected_id():
    with pytest.raises(ValueError):
        WorkArtifactResolver(endpoint="http://10.0.0.1:4321", bridge_token="a" * 64)
    resolver = WorkArtifactResolver(endpoint="http://127.0.0.1:4321", bridge_token="a" * 64)
    arguments = {"artifact_id": "a" * 64}
    with pytest.raises(ArtifactReadError, match="AUTHENTICATION_DENIED"):
        resolver.read(
            "a" * 64,
            tool_id="document.read",
            arguments=arguments,
            caller_context={"method": "document.read", "request_id": "request"},
        )
    resolver.close()
