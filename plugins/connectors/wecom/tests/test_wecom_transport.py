"""Real HTTP tests for the urllib WeCom transport against a local fake API."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from wecom_connector import (
    ConnectorError,
    UrllibWeComTransport,
    WeComConnector,
    WeComInstanceConfig,
)


class _FakeWeComApi(BaseHTTPRequestHandler):
    """Minimal WeCom REST surface: gettoken plus message/send."""

    token_requests: list[dict[str, list[str]]] = []
    message_requests: list[dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:  # noqa: D102 - silence test output
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(self.path).query)
        type(self).token_requests.append(query)
        self._respond(
            {
                "errcode": 0,
                "errmsg": "ok",
                "access_token": "tok-http",
                "expires_in": 7200,
            }
        )

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        from urllib.parse import parse_qs, urlsplit

        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        query = parse_qs(urlsplit(self.path).query)
        type(self).message_requests.append({"query": query, "body": body})
        if body.get("touser") == "boom":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"internal error")
            return
        if body.get("touser") == "garbage":
            self._respond_raw(b"not-json")
            return
        self._respond({"errcode": 0, "errmsg": "ok", "msgid": "MSG-HTTP"})

    def _respond(self, payload: dict[str, Any]) -> None:
        self._respond_raw(json.dumps(payload).encode("utf-8"))

    def _respond_raw(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def fake_api() -> Any:
    _FakeWeComApi.token_requests = []
    _FakeWeComApi.message_requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWeComApi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _connector(base_url: str) -> WeComConnector:
    config = WeComInstanceConfig(
        binding_id="wecom.http",
        corp_id="ww-http",
        corp_secret="http-secret",
        agent_id=7,
        base_url=base_url,
    )
    return WeComConnector(config, transport=UrllibWeComTransport())


def _request(target: str, text: str = "over http") -> dict[str, Any]:
    return {
        "conversation": {
            "vendor": "wecom.app",
            "account_id": "agent:7",
            "conversation_id": target,
            "kind": "private",
        },
        "content": [{"kind": "text", "text": text}],
    }


def test_urllib_transport_performs_token_and_send_over_http(fake_api: str) -> None:
    result = _connector(fake_api).send_message(_request("zhangsan"))

    assert result["status"] == "accepted"
    assert result["vendor_message_id"] == "MSG-HTTP"
    assert _FakeWeComApi.token_requests[0]["corpid"] == ["ww-http"]
    assert _FakeWeComApi.token_requests[0]["corpsecret"] == ["http-secret"]
    sent = _FakeWeComApi.message_requests[0]
    assert sent["query"]["access_token"] == ["tok-http"]
    assert sent["body"]["touser"] == "zhangsan"
    assert sent["body"]["text"]["content"] == "over http"
    assert sent["body"]["agentid"] == 7


def test_http_failures_and_non_json_bodies_are_unavailable(fake_api: str) -> None:
    plugin = _connector(fake_api)
    with pytest.raises(ConnectorError) as http_error:
        plugin.send_message(_request("boom"))
    assert http_error.value.code == "UNAVAILABLE"

    with pytest.raises(ConnectorError) as json_error:
        plugin.send_message(_request("garbage"))
    assert json_error.value.code == "UNAVAILABLE"
    assert "non-JSON" in json_error.value.message


def test_plain_http_is_loopback_only() -> None:
    with pytest.raises(ConnectorError) as error:
        WeComInstanceConfig.from_mapping(
            {
                "binding_id": "b",
                "corp_id": "c",
                "corp_secret": "s",
                "agent_id": 1,
                "base_url": "http://qyapi.example.com",
            }
        )
    assert error.value.code == "INVALID_REQUEST"
    assert "loopback" in error.value.message
