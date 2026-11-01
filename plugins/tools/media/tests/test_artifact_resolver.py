"""Local HTTP peer acceptance for the context-routed Navigator artifact bridge.

The same resolver instance exercises two actors, concurrent calls, and stale
request revocation. The peer simulates the host bridge; this is not live Navigator
acceptance. 中文：同一resolver覆盖多actor/并发/过期请求，peer仅模拟宿主桥。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from cyrene_integration_contracts import payload_sha256
from PIL import Image

from cyrene_media import MediaError
from cyrene_media.artifact_resolver import WorkArtifactResolver

_BRIDGE_TOKEN = "22" * 32


def _png(color: tuple[int, int, int]) -> bytes:
    """Return one small local PNG."""

    stream = io.BytesIO()
    Image.new("RGB", (3, 4), color).save(stream, format="PNG")
    return stream.getvalue()


def _context(
    tool_id: str,
    arguments: dict[str, Any],
    *,
    request_id: str,
    subject: str,
) -> dict[str, Any]:
    """Build minimal signed-context fields consumed by the local bridge peer."""

    return {
        "method": tool_id,
        "operation_id": tool_id,
        "caller_workload": "cyrene-navigator",
        "audience": "media-library",
        "issuer": "https://identity.local",
        "subject": subject,
        "organization_id": "org-1",
        "workspace_id": "workspace-1",
        "binding_id": "media-binding-1",
        "request_id": request_id,
        "issued_at": "2026-10-31T12:00:00Z",
        "expires_at": "2026-10-31T12:02:00Z",
        "payload_sha256": payload_sha256(arguments),
        "signature": "local-peer-fixture",
    }


@pytest.fixture
def local_bridge() -> tuple[str, dict[str, Any], Any]:
    """Start a host peer with an active request-to-actor registry."""

    actor_a = "https://identity.local/subjects/actor-a"
    actor_b = "https://identity.local/subjects/actor-b"
    content_a = _png((120, 20, 30))
    content_b = _png((10, 80, 140))
    digest_a = hashlib.sha256(content_a).hexdigest()
    digest_b = hashlib.sha256(content_b).hexdigest()
    state: dict[str, Any] = {
        "active": {"request-a": actor_a, "request-b": actor_b},
        "authorized": {
            "request-a": {digest_a},
            "request-b": {digest_b},
        },
        "content": {actor_a: content_a, actor_b: content_b},
        "writes": {},
        "requests": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            state["requests"].append((self.path, payload))
            if self.headers.get("Authorization") != f"Bearer {_BRIDGE_TOKEN}":
                self._json(401, {"error": "denied"})
                return
            context = payload.get("callerContext")
            arguments = payload.get("arguments")
            tool_id = payload.get("toolId")
            if not isinstance(context, dict) or not isinstance(arguments, dict):
                self._json(422, {"error": "missing call context"})
                return
            request_id = context.get("request_id")
            subject = context.get("subject")
            if state["active"].get(request_id) != subject:
                self._json(403, {"error": "request context expired"})
                return
            if context.get("method") != tool_id or context.get(
                "payload_sha256"
            ) != payload_sha256(arguments):
                self._json(403, {"error": "context does not cover payload"})
                return

            if self.path == "/v1/artifacts/read":
                artifact_id = payload.get("artifactId")
                if (
                    tool_id != "media.asset.import"
                    or arguments.get("artifact_id") != artifact_id
                    or artifact_id not in state["authorized"].get(request_id, set())
                ):
                    self._json(403, {"error": "source is not authorized"})
                    return
                content = state["content"][subject]
                digest = hashlib.sha256(content).hexdigest()
                if digest != artifact_id:
                    self._json(404, {"error": "artifact not found"})
                    return
                self._json(
                    200,
                    {
                        "artifactId": artifact_id,
                        "sha256": digest,
                        "mimeType": "image/png",
                        "size": len(content),
                        "contentBase64": base64.b64encode(content).decode("ascii"),
                    },
                )
                return

            if self.path == "/v1/artifacts/write":
                if tool_id not in {"media.asset.get", "media.card.render"}:
                    self._json(403, {"error": "write operation is not allowed"})
                    return
                content = base64.b64decode(payload["contentBase64"], validate=True)
                digest = hashlib.sha256(content).hexdigest()
                mime_type = str(payload["mimeType"])
                state["writes"][(request_id, digest)] = content
                self._json(
                    201,
                    {
                        "artifactId": digest,
                        "sha256": digest,
                        "mimeType": mime_type,
                        "size": len(content),
                    },
                )
                return
            self._json(404, {"error": "unknown route"})

        def _json(self, status: int, value: dict[str, Any]) -> None:
            encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    try:
        yield endpoint, state, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _resolver(endpoint: str) -> WorkArtifactResolver:
    """Create one long-lived provider client with a binding-scoped secret."""

    return WorkArtifactResolver(
        endpoint=endpoint,
        bridge_token=_BRIDGE_TOKEN,
    )


def _read(
    resolver: WorkArtifactResolver,
    artifact_id: str,
    *,
    request_id: str,
    subject: str,
) -> Any:
    """Resolve one attachment under the specified verified tool call."""

    arguments = {"artifact_id": artifact_id}
    return resolver.read(
        artifact_id,
        tool_id="media.asset.import",
        arguments=arguments,
        caller_context=_context(
            "media.asset.import",
            arguments,
            request_id=request_id,
            subject=subject,
        ),
    )


def test_one_provider_routes_two_actors_without_cross_read(
    local_bridge: tuple[str, dict[str, Any], Any],
) -> None:
    endpoint, state, _server = local_bridge
    resolver = _resolver(endpoint)
    actor_a = "https://identity.local/subjects/actor-a"
    actor_b = "https://identity.local/subjects/actor-b"
    digest_a = hashlib.sha256(state["content"][actor_a]).hexdigest()
    digest_b = hashlib.sha256(state["content"][actor_b]).hexdigest()

    read_a = _read(
        resolver,
        digest_a,
        request_id="request-a",
        subject=actor_a,
    )
    read_b = _read(
        resolver,
        digest_b,
        request_id="request-b",
        subject=actor_b,
    )

    assert read_a.data == state["content"][actor_a]
    assert read_b.data == state["content"][actor_b]
    with pytest.raises(MediaError, match="AUTHORIZATION_DENIED"):
        _read(
            resolver,
            digest_b,
            request_id="request-a",
            subject=actor_a,
        )


def test_request_context_revocation_invalidates_old_task(
    local_bridge: tuple[str, dict[str, Any], Any],
) -> None:
    endpoint, state, _server = local_bridge
    resolver = _resolver(endpoint)
    actor_a = "https://identity.local/subjects/actor-a"
    actor_b = "https://identity.local/subjects/actor-b"
    digest_a = hashlib.sha256(state["content"][actor_a]).hexdigest()
    digest_b = hashlib.sha256(state["content"][actor_b]).hexdigest()

    _read(resolver, digest_a, request_id="request-a", subject=actor_a)
    state["active"].pop("request-a")
    with pytest.raises(MediaError, match="AUTHORIZATION_DENIED"):
        _read(resolver, digest_a, request_id="request-a", subject=actor_a)
    assert (
        _read(
            resolver,
            digest_b,
            request_id="request-b",
            subject=actor_b,
        ).data
        == state["content"][actor_b]
    )


def test_parallel_task_calls_remain_actor_scoped(
    local_bridge: tuple[str, dict[str, Any], Any],
) -> None:
    endpoint, state, _server = local_bridge
    resolver = _resolver(endpoint)
    actor_a = "https://identity.local/subjects/actor-a"
    actor_b = "https://identity.local/subjects/actor-b"
    digest_a = hashlib.sha256(state["content"][actor_a]).hexdigest()
    digest_b = hashlib.sha256(state["content"][actor_b]).hexdigest()

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(
            _read,
            resolver,
            digest_a,
            request_id="request-a",
            subject=actor_a,
        )
        future_b = executor.submit(
            _read,
            resolver,
            digest_b,
            request_id="request-b",
            subject=actor_b,
        )

    assert future_a.result().data == state["content"][actor_a]
    assert future_b.result().data == state["content"][actor_b]


def test_write_verifies_image_and_host_receipt(
    local_bridge: tuple[str, dict[str, Any], Any],
) -> None:
    endpoint, state, _server = local_bridge
    resolver = _resolver(endpoint)
    actor_a = "https://identity.local/subjects/actor-a"
    arguments = {"asset_id": "asset-a"}
    content = _png((30, 50, 70))
    context = _context(
        "media.asset.get",
        arguments,
        request_id="request-a",
        subject=actor_a,
    )

    receipt = resolver.write(
        content,
        "image/png",
        {"name": "generated-card.png"},
        tool_id="media.asset.get",
        arguments=arguments,
        caller_context=context,
    )

    assert receipt.sha256 == hashlib.sha256(content).hexdigest()
    assert receipt.artifact_id == receipt.sha256
    assert receipt.size == len(content)
    assert state["writes"][("request-a", receipt.sha256)] == content


def test_invalid_artifact_id_never_reaches_bridge(
    local_bridge: tuple[str, dict[str, Any], Any],
) -> None:
    endpoint, state, _server = local_bridge
    resolver = _resolver(endpoint)
    arguments = {"artifact_id": "../../etc/passwd"}
    context = _context(
        "media.asset.import",
        arguments,
        request_id="request-a",
        subject="https://identity.local/subjects/actor-a",
    )

    with pytest.raises(MediaError, match="INVALID_INPUT"):
        resolver.read(
            arguments["artifact_id"],
            tool_id="media.asset.import",
            arguments=arguments,
            caller_context=context,
        )

    assert state["requests"] == []


def test_plain_http_remote_endpoint_is_rejected() -> None:
    with pytest.raises(ValueError, match="loopback"):
        WorkArtifactResolver(
            endpoint="http://example.invalid",
            bridge_token=_BRIDGE_TOKEN,
        )


def test_write_rejects_mime_mismatch_before_bridge(
    local_bridge: tuple[str, dict[str, Any], Any],
) -> None:
    endpoint, state, _server = local_bridge
    resolver = _resolver(endpoint)
    arguments = {"asset_id": "asset-a"}
    context = _context(
        "media.asset.get",
        arguments,
        request_id="request-a",
        subject="https://identity.local/subjects/actor-a",
    )

    with pytest.raises(MediaError, match="UNSUPPORTED_MEDIA"):
        resolver.write(
            _png((1, 2, 3)),
            "image/jpeg",
            {"name": "wrong.jpg"},
            tool_id="media.asset.get",
            arguments=arguments,
            caller_context=context,
        )
    assert state["requests"] == []
