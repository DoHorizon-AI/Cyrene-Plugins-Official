from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cyrene_public_web.public_http import PublicHttpError, fetch_get


class _PeerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.org/private")
            self.end_headers()
            return
        if self.path in {"/slow", "/cancel"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "8")
            self.end_headers()
            try:
                for _ in range(8):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                return
            return
        body = b"x" * 128 if self.path == "/large" else b"<html><body>local peer</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def peer_origin():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PeerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield origin
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_local_http_peer_is_allowed_only_by_exact_private_test_origin(peer_origin) -> None:
    response = fetch_get(
        peer_origin + "/page",
        timeout_seconds=2,
        max_response_bytes=1024,
        loopback_test_origins=frozenset({peer_origin}),
    )
    assert response.body == b"<html><body>local peer</body></html>"
    assert response.content_type == "text/html"
    assert response.charset == "utf-8"

    with pytest.raises(PublicHttpError, match="INSECURE_URL"):
        fetch_get(
            peer_origin + "/page",
            timeout_seconds=2,
            max_response_bytes=1024,
            loopback_test_origins=frozenset(),
        )


def test_redirect_is_not_followed_and_private_content_is_not_fetched(peer_origin) -> None:
    with pytest.raises(PublicHttpError, match="REDIRECT_REJECTED"):
        fetch_get(
            peer_origin + "/redirect",
            timeout_seconds=2,
            max_response_bytes=1024,
            loopback_test_origins=frozenset({peer_origin}),
        )


def test_response_body_has_a_hard_read_limit(peer_origin) -> None:
    with pytest.raises(PublicHttpError, match="RESPONSE_LIMIT_EXCEEDED"):
        fetch_get(
            peer_origin + "/large",
            timeout_seconds=2,
            max_response_bytes=16,
            loopback_test_origins=frozenset({peer_origin}),
        )


def test_total_request_deadline_stops_a_slow_response(peer_origin) -> None:
    started = time.monotonic()
    with pytest.raises(PublicHttpError, match="REQUEST_TIMEOUT"):
        fetch_get(
            peer_origin + "/slow",
            timeout_seconds=0.5,
            max_response_bytes=1024,
            loopback_test_origins=frozenset({peer_origin}),
        )
    assert time.monotonic() - started < 1.5


def test_cancellation_closes_an_inflight_request(peer_origin) -> None:
    cancellation = threading.Event()
    timer = threading.Timer(0.1, cancellation.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(InterruptedError):
            fetch_get(
                peer_origin + "/cancel",
                timeout_seconds=3,
                max_response_bytes=1024,
                loopback_test_origins=frozenset({peer_origin}),
                cancellation=cancellation,
            )
    finally:
        timer.cancel()
    assert time.monotonic() - started < 1.5


def test_private_dns_answer_is_rejected_before_connect(monkeypatch) -> None:
    def fake_getaddrinfo(*_args, **_kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr("cyrene_public_web.public_http.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(PublicHttpError, match="PRIVATE_ADDRESS_REJECTED"):
        fetch_get(
            "https://docs.example.test/page",
            timeout_seconds=2,
            max_response_bytes=1024,
            loopback_test_origins=frozenset(),
        )


def test_secret_query_parameters_and_userinfo_are_rejected(peer_origin) -> None:
    for url in (
        "https://example.com/page?access_token=secret",
        "https://example.com/page?X-API-Key=secret",
        "https://example.com/page?client_secret=secret",
        "https://user:pass@example.com/page",
    ):
        with pytest.raises(PublicHttpError, match="INVALID_URL|SENSITIVE_URL_REJECTED"):
            fetch_get(
                url,
                timeout_seconds=2,
                max_response_bytes=1024,
                loopback_test_origins=frozenset({peer_origin}),
            )
