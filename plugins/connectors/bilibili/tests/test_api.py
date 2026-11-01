from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from cyrene_bilibili.api import (
    _MIXIN_KEY_ENC_TAB,
    BilibiliApi,
    BilibiliFailure,
    _parse_cookie_jar,
    _wbi_mixin_key,
)

_COOKIES = json.dumps(
    {
        "schemaVersion": 1,
        "cookies": [
            {
                "name": "SESSDATA",
                "value": "session-cookie",
                "domain": ".bilibili.com",
                "path": "/",
            },
            {
                "name": "bili_jct",
                "value": "csrf-token",
                "domain": ".bilibili.com",
                "path": "/",
            },
            {
                "name": "DedeUserID",
                "value": "12345",
                "domain": ".bilibili.com",
                "path": "/",
            },
        ],
    },
    separators=(",", ":"),
)


def _client(handler):
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        trust_env=False,
        follow_redirects=False,
    )


def test_wbi_mixin_key_matches_bilibili_permutation_vector() -> None:
    assert len(_MIXIN_KEY_ENC_TAB) == 64
    assert len(set(_MIXIN_KEY_ENC_TAB)) == 64
    result = _wbi_mixin_key(
        {
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
            }
        }
    )
    assert result == "ea1db124af3c7062474693fa704f4ff8"


def test_qr_begin_renders_real_png_and_keeps_only_allowlisted_cookies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "passport.bilibili.com"
        assert request.url.path == "/x/passport-login/web/qrcode/generate"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "qrcode_key": "ticket-abc",
                    "url": "https://passport.bilibili.com/h5-app/passport/login?foo=bar",
                },
            },
            headers={"set-cookie": "buvid3=visitor; Domain=.bilibili.com; Path=/; Secure"},
        )

    api = BilibiliApi(client=_client(handler))
    ticket = api.generate_qr()
    png = ticket.qr_png
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert hashlib.sha256(ticket.qr_png).hexdigest() == ticket.qr_png_sha256
    cookies = _parse_cookie_jar(ticket.cookies_json, allow_missing_auth=True)
    assert cookies["buvid3"]["expires_at"] is None


def test_qr_response_cannot_supply_arbitrary_browser_or_network_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "qrcode_key": "ticket-abc",
                    "url": "https://attacker.example/h5-app/passport/login?next=x",
                },
            },
        )

    with pytest.raises(BilibiliFailure, match="PROVIDER_RESPONSE_INVALID"):
        BilibiliApi(client=_client(handler)).generate_qr()


def test_comment_post_accepted_but_readback_failure_is_unknown_with_external_id() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(200, json={"code": 0, "data": {"aid": 987, "cid": 123}})
        if request.url.path == "/x/v2/reply/add":
            return httpx.Response(200, json={"code": 0, "data": {"rpid": 456}})
        if request.url.path == "/x/v2/reply/reply":
            return httpx.Response(503, json={"code": -500, "message": "temporary"})
        raise AssertionError(f"unexpected fixed endpoint: {request.url}")

    api = BilibiliApi(client=_client(handler))
    with pytest.raises(BilibiliFailure) as raised:
        api.comment_create(
            video_id="BV1xx411c7mD",
            root_comment_id="0",
            parent_comment_id="0",
            content="hello from test",
            cookies_json=_COOKIES,
        )
    assert raised.value.code == "UNKNOWN_RESULT"
    assert raised.value.unknown_write is True
    assert raised.value.external_id == "456"
    assert calls == [
        ("GET", "/x/web-interface/view"),
        ("POST", "/x/v2/reply/add"),
        ("GET", "/x/v2/reply/reply"),
    ]


def test_explicit_platform_write_rejection_is_not_classified_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(200, json={"code": 0, "data": {"aid": 987, "cid": 123}})
        return httpx.Response(200, json={"code": -412, "message": "rejected"})

    with pytest.raises(BilibiliFailure) as raised:
        BilibiliApi(client=_client(handler)).comment_create(
            video_id="BV1xx411c7mD",
            root_comment_id="0",
            parent_comment_id="0",
            content="hello from test",
            cookies_json=_COOKIES,
        )
    assert raised.value.unknown_write is False


def test_cookie_jar_is_closed_and_account_scoped() -> None:
    assert set(_parse_cookie_jar(_COOKIES)) == {"SESSDATA", "bili_jct", "DedeUserID"}
    with pytest.raises(BilibiliFailure, match="INVALID_CREDENTIAL"):
        _parse_cookie_jar(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "cookies": [
                        {
                            "name": "SESSDATA",
                            "value": "secret",
                            "domain": ".attacker.invalid",
                            "path": "/",
                        }
                    ],
                }
            )
        )


def test_dm_page_is_peer_scoped_without_truncating_to_visible_sessions() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.vc.bilibili.com"
        assert request.url.path == "/svr_sync/v1/svr_sync/fetch_session_msgs"
        assert request.url.params["talker_id"] == "9988"
        assert request.url.params["begin_seqno"] == "42"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "messages": [
                        {
                            "msg_seqno": 43,
                            "sender_uid": 12345,
                            "content": '{"content":"hello"}',
                            "timestamp": 100,
                        }
                    ],
                    "min_seqno": 43,
                },
            },
        )

    result = BilibiliApi(client=_client(handler)).dm_list(
        peer_user_id="9988",
        cursor="42",
        limit=10,
        cookies_json=_COOKIES,
        subject="12345",
    )
    assert len(requests) == 1
    assert result == {
        "items": [
            {
                "id": "43",
                "sender_user_id": "12345",
                "direction": "OUTBOUND",
                "text": "hello",
                "created_at": 100,
            }
        ],
        "next_cursor": "43",
    }
