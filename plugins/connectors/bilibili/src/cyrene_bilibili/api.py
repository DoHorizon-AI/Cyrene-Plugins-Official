"""Bounded client for the fixed Bilibili web and QR endpoints."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
import qrcode

API_HOST = "https://api.bilibili.com"
PASSPORT_HOST = "https://passport.bilibili.com"
VC_HOST = "https://api.vc.bilibili.com"
QR_TTL_SECONDS = 600
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SUBTITLE_BYTES = 1024 * 1024
MAX_SUBTITLE_SEGMENTS = 500
MAX_SUBTITLE_TEXT = 100_000
_BVID = re.compile(r"BV[0-9A-Za-z]{10}\Z")
_AVID = re.compile(r"av[1-9][0-9]{0,19}\Z")
_NUMERIC_ID = re.compile(r"[0-9]{1,20}\Z")
_COOKIE_NAMES = frozenset(
    {
        "SESSDATA",
        "bili_jct",
        "DedeUserID",
        "DedeUserID__ckMd5",
        "sid",
        "buvid3",
        "buvid4",
        "buvid_fp",
        "buvid_fp_plain",
        "b_nut",
        "_uuid",
    }
)
_EMPTY_COOKIE_JAR = '{"schemaVersion":1,"cookies":[]}'
_MIXIN_KEY_ENC_TAB = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)


class BilibiliFailure(RuntimeError):
    """A bounded Bilibili request failed without leaking response details."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        unknown_write: bool = False,
        external_id: str | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.unknown_write = unknown_write
        self.external_id = external_id


@dataclass(frozen=True, slots=True)
class QrTicket:
    """A real pending QR ticket and its private visitor cookies."""

    qrcode_key: str
    qr_url: str
    qr_png: bytes
    qr_png_sha256: str
    cookies_json: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class QrPoll:
    """One poll result; cookies are private call-local data."""

    status: str
    cookies_json: str
    external_subject: str | None = None


class BilibiliApi:
    """Call fixed Bilibili origins with bounded bodies and no redirects/proxies."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 12,
        client: httpx.Client | None = None,
    ) -> None:
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5))
        self._client = client or httpx.Client(
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com/",
            },
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def generate_qr(self) -> QrTicket:
        """Obtain a real Bilibili QR ticket and render its payload as PNG."""

        value, response = self._json_request(
            "GET", PASSPORT_HOST, "/x/passport-login/web/qrcode/generate"
        )
        data = _mapping(value.get("data"))
        key = data.get("qrcode_key")
        qr_url = data.get("url")
        if (
            value.get("code") != 0
            or not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", key)
        ):
            raise BilibiliFailure("PROVIDER_REJECTED")
        if not isinstance(qr_url, str) or not _valid_qr_url(qr_url):
            raise BilibiliFailure("PROVIDER_RESPONSE_INVALID")
        output = __import__("io").BytesIO()
        qrcode.make(qr_url).save(output, format="PNG")
        png = output.getvalue()
        if not 1 <= len(png) <= 256 * 1024:
            raise BilibiliFailure("OUTPUT_LIMIT_EXCEEDED")
        cookies = _cookies_from_response(response.headers.get_list("set-cookie"), _EMPTY_COOKIE_JAR)
        return QrTicket(
            qrcode_key=key,
            qr_url=qr_url,
            qr_png=png,
            qr_png_sha256=hashlib.sha256(png).hexdigest(),
            cookies_json=cookies,
            expires_at=int(time.time()) + QR_TTL_SECONDS,
        )

    def poll_qr(self, qrcode_key: str, cookies_json: str) -> QrPoll:
        """Poll one ticket, validate the authenticated MID, and keep cookies private."""

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", qrcode_key):
            raise BilibiliFailure("INVALID_INPUT")
        _, response = self._json_request(
            "GET",
            PASSPORT_HOST,
            "/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qrcode_key},
            cookies_json=cookies_json,
        )
        value = _response_json(response)
        code = value.get("code")
        updated_cookies = _cookies_from_response(
            response.headers.get_list("set-cookie"), cookies_json
        )
        if code == 86101:
            return QrPoll("PENDING", updated_cookies)
        if code == 86090:
            return QrPoll("SCANNED", updated_cookies)
        if code == 86038:
            return QrPoll("EXPIRED", updated_cookies)
        if code != 0:
            if isinstance(code, int) and code in {-412, -101}:
                raise BilibiliFailure("AUTHORIZATION_REQUIRED")
            raise BilibiliFailure("PROVIDER_REJECTED")
        # Never trust QR poll's redirect URL; identity is read back from fixed nav.
        nav, _ = self._json_request(
            "GET", API_HOST, "/x/web-interface/nav", cookies_json=updated_cookies
        )
        nav_data = _mapping(nav.get("data"))
        external_subject = nav_data.get("mid")
        if nav.get("code") != 0 or not _is_numeric_id(external_subject):
            raise BilibiliFailure("IDENTITY_VERIFICATION_FAILED")
        return QrPoll("LINKED", updated_cookies, str(external_subject))

    def search(
        self,
        keyword: str,
        search_type: str,
        page: int,
        cookies_json: str,
    ) -> dict[str, Any]:
        """Search one bounded page and project only stable display fields."""

        nav, _ = self._json_request(
            "GET", API_HOST, "/x/web-interface/nav", cookies_json=cookies_json
        )
        mixin = _wbi_mixin_key(_mapping(nav.get("data")))
        if nav.get("code") != 0 or mixin is None:
            raise BilibiliFailure("PROVIDER_RESPONSE_INVALID")
        api_type = {"video": "video", "user": "bili_user", "article": "article"}[search_type]
        raw, _ = self._json_request(
            "GET",
            API_HOST,
            "/x/web-interface/wbi/search/type",
            params={
                "search_type": api_type,
                "keyword": keyword,
                "page": page,
                "page_size": 20,
            },
            cookies_json=cookies_json,
            wbi_mixin_key=mixin,
        )
        if raw.get("code") != 0:
            raise _api_failure(raw)
        data = _mapping(raw.get("data"))
        items = data.get("result", [])
        if not isinstance(items, list):
            raise BilibiliFailure("PROVIDER_RESPONSE_INVALID")
        return {
            "items": [
                _search_item(item, search_type) for item in items[:20] if isinstance(item, Mapping)
            ],
            "page": page,
            "has_more": _boolish(data.get("numPages"), page),
        }

    def user_find(self, user_id: str, cookies_json: str) -> dict[str, Any]:
        raw, _ = self._json_request(
            "GET", API_HOST, "/x/space/acc/info", params={"mid": user_id}, cookies_json=cookies_json
        )
        if raw.get("code") != 0:
            raise _api_failure(raw)
        data = _mapping(raw.get("data"))
        return {
            "user_id": str(data.get("mid", user_id)),
            "name": _text(data.get("name"), 128),
            "signature": _text(data.get("sign"), 1000),
            "level": _small_int(data.get("level")),
            "is_following": bool(data.get("is_followed", False)),
        }

    def video_read(self, video_id: str, cookies_json: str) -> dict[str, Any]:
        params = {"bvid": video_id[2:]} if video_id.startswith("av") else {"bvid": video_id}
        if video_id.startswith("av"):
            params = {"aid": video_id[2:]}
        raw, _ = self._json_request(
            "GET", API_HOST, "/x/web-interface/view", params=params, cookies_json=cookies_json
        )
        if raw.get("code") != 0:
            raise _api_failure(raw)
        data = _mapping(raw.get("data"))
        owner = _mapping(data.get("owner"))
        stat = _mapping(data.get("stat"))
        result: dict[str, Any] = {
            "metadata": {
                "video_id": str(data.get("bvid") or video_id),
                "title": _text(data.get("title"), 512),
                "description": _text(data.get("desc"), 4000),
                "author_id": str(owner.get("mid", "")),
                "author": _text(owner.get("name"), 128),
                "duration_seconds": _small_int(data.get("duration")),
                "published_at": _small_int(data.get("pubdate")),
                "views": _small_int(stat.get("view")),
                "comments": _small_int(stat.get("reply")),
                "cid": str(data.get("cid", "")),
            },
            "subtitles": [],
            "content_read_state": "METADATA_ONLY",
        }
        cid = data.get("cid")
        aid = data.get("aid")
        if isinstance(cid, int) and cid > 0 and isinstance(aid, int) and aid > 0:
            result["subtitles"] = self._read_subtitles(video_id, cid, cookies_json)
            if result["subtitles"]:
                result["content_read_state"] = "SUBTITLES_READ"
        result["metadata"].pop("cid", None)
        return result

    def comments_list(
        self,
        video_id: str,
        root_comment_id: str | None,
        page: int,
        limit: int,
        cookies_json: str,
    ) -> dict[str, Any]:
        video = self._video_target(video_id, cookies_json)
        params = {
            "oid": str(video["aid"]),
            "type": "1",
            "mode": "3",
            "next": str(page),
            "ps": str(limit),
        }
        if root_comment_id is not None:
            params["root"] = root_comment_id
        raw, _ = self._json_request(
            "GET", API_HOST, "/x/v2/reply/main", params=params, cookies_json=cookies_json
        )
        if raw.get("code") != 0:
            raise _api_failure(raw)
        data = _mapping(raw.get("data"))
        cursor = _mapping(data.get("cursor"))
        replies = data.get("replies") or []
        return {
            "items": [_comment_item(item) for item in replies[:limit] if isinstance(item, Mapping)],
            "next_page": int(cursor.get("next", 0)) if cursor.get("is_end") is False else None,
        }

    def dynamics_list(
        self, user_id: str, cursor: str | None, limit: int, cookies_json: str
    ) -> dict[str, Any]:
        params = {"host_mid": user_id, "offset": cursor or "", "timezone_offset": "-480"}
        raw, _ = self._json_request(
            "GET",
            API_HOST,
            "/x/polymer/web-dynamic/v1/feed/space",
            params=params,
            cookies_json=cookies_json,
        )
        if raw.get("code") != 0:
            raise _api_failure(raw)
        data = _mapping(raw.get("data"))
        cards = data.get("items") or data.get("cards") or []
        if not isinstance(cards, list):
            raise BilibiliFailure("PROVIDER_RESPONSE_INVALID")
        next_cursor = data.get("offset")
        return {
            "items": [_dynamic_item(item) for item in cards[:limit] if isinstance(item, Mapping)],
            "next_cursor": _text(next_cursor, 1024) if next_cursor else None,
        }

    def dynamic_read(self, dynamic_id: str, cookies_json: str) -> dict[str, Any]:
        raw, _ = self._json_request(
            "GET",
            API_HOST,
            "/x/polymer/web-dynamic/v1/detail",
            params={"id": dynamic_id},
            cookies_json=cookies_json,
        )
        if raw.get("code") != 0:
            raise _api_failure(raw)
        data = _mapping(raw.get("data"))
        item = data.get("item")
        return {"item": _dynamic_item(_mapping(item) or data)}

    def dm_list(
        self, peer_user_id: str, cursor: str | None, limit: int, cookies_json: str, subject: str
    ) -> dict[str, Any]:
        messages = self._dm_messages(peer_user_id, cursor, limit, cookies_json)
        return {
            "items": [
                _dm_item(item, subject)
                for item in messages.get("messages", [])[:limit]
                if isinstance(item, Mapping)
            ],
            "next_cursor": _text(messages.get("next_cursor"), 1024)
            if messages.get("next_cursor")
            else None,
        }

    def dm_read(
        self, peer_user_id: str, message_id: str, cookies_json: str, subject: str
    ) -> dict[str, Any]:
        page = self._dm_messages(peer_user_id, message_id, 50, cookies_json)
        for message in page.get("messages", []):
            if isinstance(message, Mapping) and str(message.get("msg_seqno")) == message_id:
                return {"item": _dm_item(message, subject)}
        raise BilibiliFailure("RESOURCE_NOT_FOUND")

    def comment_create(
        self,
        *,
        video_id: str,
        root_comment_id: str,
        parent_comment_id: str,
        content: str,
        cookies_json: str,
    ) -> dict[str, Any]:
        cookie = _parse_cookie_jar(cookies_json)
        video = self._video_target(video_id, cookies_json)
        form = {
            "oid": str(video["aid"]),
            "type": "1",
            "message": content,
            "root": root_comment_id,
            "parent": parent_comment_id,
            "plat": "1",
            "csrf": cookie["bili_jct"],
        }
        raw, _ = self._json_request(
            "POST", API_HOST, "/x/v2/reply/add", data=form, cookies_json=cookies_json, mutation=True
        )
        if raw.get("code") != 0:
            raise _api_failure(raw, mutation=True)
        data = _mapping(raw.get("data"))
        reply_id = data.get("rpid")
        if not _is_numeric_id(reply_id):
            raise BilibiliFailure("UNKNOWN_RESULT", unknown_write=True)
        try:
            readback = self._comment_readback(
                video_id,
                str(reply_id),
                cookies_json,
                aid=int(video["aid"]),
            )
        except BilibiliFailure as error:
            raise BilibiliFailure(
                "UNKNOWN_RESULT", unknown_write=True, external_id=str(reply_id)
            ) from error
        if readback is None or readback.get("content") != content:
            raise BilibiliFailure("UNKNOWN_RESULT", unknown_write=True, external_id=str(reply_id))
        digest = hashlib.sha256(readback["content"].encode("utf-8")).hexdigest()
        return {"external_id": str(reply_id), "readback_verified": True, "readback_sha256": digest}

    def publish_dynamic(
        self, user_id: str, content: str, cookies_json: str, subject: str
    ) -> dict[str, Any]:
        if user_id != subject:
            raise BilibiliFailure("PROFILE_IDENTITY_MISMATCH")
        cookie = _parse_cookie_jar(cookies_json)
        form = {
            "csrf": cookie["bili_jct"],
            "csrf_token": cookie["bili_jct"],
            "type": "4",
            "rid": "0",
            "content": content,
            "extension": json.dumps({"emoji_type": 1}, separators=(",", ":")),
        }
        raw, _ = self._json_request(
            "POST",
            API_HOST,
            "/x/dynamic/feed/create/dyn",
            data=form,
            cookies_json=cookies_json,
            mutation=True,
        )
        if raw.get("code") != 0:
            raise _api_failure(raw, mutation=True)
        data = _mapping(raw.get("data"))
        dynamic_id = data.get("dynamic_id") or data.get("dyn_id_str")
        if not _is_numeric_id(dynamic_id):
            raise BilibiliFailure("UNKNOWN_RESULT", unknown_write=True)
        try:
            readback = self.dynamic_read(str(dynamic_id), cookies_json)
        except BilibiliFailure as error:
            raise BilibiliFailure(
                "UNKNOWN_RESULT", unknown_write=True, external_id=str(dynamic_id)
            ) from error
        item = readback.get("item")
        if not isinstance(item, Mapping) or item.get("text") != content:
            raise BilibiliFailure("UNKNOWN_RESULT", unknown_write=True, external_id=str(dynamic_id))
        digest = hashlib.sha256(str(item["text"]).encode("utf-8")).hexdigest()
        return {
            "external_id": str(dynamic_id),
            "readback_verified": True,
            "readback_sha256": digest,
        }

    def send_dm(
        self, peer_user_id: str, content: str, cookies_json: str, subject: str
    ) -> dict[str, Any]:
        cookie = _parse_cookie_jar(cookies_json)
        msg = {
            "sender_uid": int(subject),
            "receiver_id": int(peer_user_id),
            "receiver_type": 1,
            "msg_type": 1,
            "msg_status": 0,
            "content": json.dumps({"content": content}, ensure_ascii=False, separators=(",", ":")),
            "timestamp": int(time.time()),
            "new_face_version": 1,
            "dev_id": "cyrene-bilibili-connector",
        }
        form = {
            "msg": json.dumps(msg, ensure_ascii=False, separators=(",", ":")),
            "csrf": cookie["bili_jct"],
            "csrf_token": cookie["bili_jct"],
            "build": "0",
            "mobi_app": "web",
        }
        raw, _ = self._json_request(
            "POST",
            VC_HOST,
            "/web_im/v1/web_im/send_msg",
            data=form,
            cookies_json=cookies_json,
            mutation=True,
        )
        if raw.get("code") != 0:
            raise _api_failure(raw, mutation=True)
        data = _mapping(raw.get("data"))
        message_id = data.get("msg_seqno") or data.get("msg_key")
        if not _is_numeric_id(message_id):
            raise BilibiliFailure("UNKNOWN_RESULT", unknown_write=True)
        try:
            readback = self.dm_read(peer_user_id, str(message_id), cookies_json, subject)
        except BilibiliFailure as error:
            raise BilibiliFailure(
                "UNKNOWN_RESULT", unknown_write=True, external_id=str(message_id)
            ) from error
        item = readback.get("item")
        if (
            not isinstance(item, Mapping)
            or item.get("direction") != "OUTBOUND"
            or item.get("text") != content
        ):
            raise BilibiliFailure("UNKNOWN_RESULT", unknown_write=True, external_id=str(message_id))
        digest = hashlib.sha256(str(item["text"]).encode("utf-8")).hexdigest()
        return {
            "external_id": str(message_id),
            "readback_verified": True,
            "readback_sha256": digest,
        }

    def _read_subtitles(self, video_id: str, cid: int, cookies_json: str) -> list[dict[str, Any]]:
        params = {"cid": cid}
        if video_id.startswith("BV"):
            params["bvid"] = video_id
        else:
            params["aid"] = video_id[2:]
        raw, _ = self._json_request(
            "GET", API_HOST, "/x/player/v2", params=params, cookies_json=cookies_json
        )
        if raw.get("code") != 0:
            return []
        data = _mapping(raw.get("data"))
        subtitles = _mapping(data.get("subtitle")).get("subtitles") or []
        if not isinstance(subtitles, list):
            return []
        result: list[dict[str, Any]] = []
        total_text = 0
        for subtitle in subtitles[:8]:
            if not isinstance(subtitle, Mapping):
                continue
            url = subtitle.get("subtitle_url")
            if not isinstance(url, str) or not _valid_subtitle_url(url):
                continue
            document, _ = self._json_request(
                "GET",
                "https://aisubtitle.hdslb.com",
                urlsplit(url).path,
                raw_query=urlsplit(url).query,
                max_bytes=MAX_SUBTITLE_BYTES,
            )
            body = document.get("body") or []
            if not isinstance(body, list):
                continue
            segments: list[dict[str, Any]] = []
            for part in body[:MAX_SUBTITLE_SEGMENTS]:
                if not isinstance(part, Mapping):
                    continue
                text = _text(part.get("content"), MAX_SUBTITLE_TEXT)
                total_text += len(text)
                if total_text > MAX_SUBTITLE_TEXT:
                    break
                segments.append(
                    {
                        "start_seconds": _small_number(part.get("from")),
                        "end_seconds": _small_number(part.get("to")),
                        "text": text,
                    }
                )
            if segments:
                result.append(
                    {
                        "language": _text(subtitle.get("lan_doc") or subtitle.get("lan"), 64),
                        "segments": segments,
                    }
                )
        return result

    def _video_target(self, video_id: str, cookies_json: str) -> dict[str, int]:
        params = {"bvid": video_id} if video_id.startswith("BV") else {"aid": video_id[2:]}
        raw, _ = self._json_request(
            "GET", API_HOST, "/x/web-interface/view", params=params, cookies_json=cookies_json
        )
        if raw.get("code") != 0:
            raise _api_failure(raw)
        data = _mapping(raw.get("data"))
        aid, cid = data.get("aid"), data.get("cid")
        if not isinstance(aid, int) or aid <= 0 or not isinstance(cid, int) or cid <= 0:
            raise BilibiliFailure("PROVIDER_RESPONSE_INVALID")
        return {"aid": aid, "cid": cid}

    def _comment_readback(
        self,
        video_id: str,
        reply_id: str,
        cookies_json: str,
        *,
        aid: int,
    ) -> dict[str, Any] | None:
        raw, _ = self._json_request(
            "GET",
            API_HOST,
            "/x/v2/reply/reply",
            params={"oid": aid, "type": 1, "root": reply_id, "ps": 20},
            cookies_json=cookies_json,
        )
        if raw.get("code") != 0:
            return None
        data = _mapping(raw.get("data"))
        item = data.get("root")
        return _comment_item(item) if isinstance(item, Mapping) else None

    def _dm_messages(
        self,
        peer_user_id: str,
        cursor: str | None,
        limit: int,
        cookies_json: str,
    ) -> dict[str, Any]:
        params = {
            "talker_id": peer_user_id,
            "session_type": 1,
            "size": str(limit),
            "sender_device_id": "1",
            "build": "0",
            "mobi_app": "web",
        }
        if cursor is not None:
            params["begin_seqno"] = cursor
        raw, _ = self._json_request(
            "GET",
            VC_HOST,
            "/svr_sync/v1/svr_sync/fetch_session_msgs",
            params=params,
            cookies_json=cookies_json,
        )
        if raw.get("code") != 0:
            raise _api_failure(raw)
        data = _mapping(raw.get("data"))
        messages = data.get("messages") or []
        if not isinstance(messages, list):
            raise BilibiliFailure("PROVIDER_RESPONSE_INVALID")
        next_cursor = data.get("min_seqno")
        return {"messages": messages, "next_cursor": str(next_cursor) if next_cursor else None}

    def _json_request(
        self,
        method: str,
        origin: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        raw_query: str | None = None,
        data: Mapping[str, Any] | None = None,
        cookies_json: str = _EMPTY_COOKIE_JAR,
        wbi_mixin_key: str | None = None,
        mutation: bool = False,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> tuple[dict[str, Any], httpx.Response]:
        if origin not in {API_HOST, PASSPORT_HOST, VC_HOST, "https://aisubtitle.hdslb.com"}:
            raise BilibiliFailure("INVALID_TARGET")
        if not path.startswith("/") or ".." in path or len(path) > 256:
            raise BilibiliFailure("INVALID_TARGET")
        query = raw_query
        if params is not None:
            normalized = {str(key): str(value) for key, value in params.items()}
            if wbi_mixin_key:
                normalized["wts"] = str(int(time.time()))
                canonical = urlencode(sorted(normalized.items()), quote_via=quote)
                normalized["w_rid"] = hashlib.md5((canonical + wbi_mixin_key).encode()).hexdigest()
            query = urlencode(normalized, quote_via=quote)
        headers: dict[str, str] = {}
        cookie_header = _cookie_header(cookies_json)
        if cookie_header:
            headers["Cookie"] = cookie_header
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        url = urlunsplit(("https", urlsplit(origin).hostname or "", path, query or "", ""))
        try:
            with self._client.stream(
                method, url, params=None, data=data, headers=headers, timeout=self._timeout
            ) as response:
                if response.is_redirect:
                    raise BilibiliFailure(
                        "REDIRECT_REJECTED", retryable=False, unknown_write=mutation
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    raise BilibiliFailure(
                        "HTTP_FAILURE",
                        retryable=response.status_code >= 500,
                        unknown_write=mutation,
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise BilibiliFailure("RESPONSE_LIMIT_EXCEEDED", unknown_write=mutation)
                response_content = bytes(body)
                response_headers = httpx.Headers(response.headers)
                response_status = response.status_code
                response_url = response.url
        except BilibiliFailure:
            raise
        except httpx.TimeoutException:
            raise BilibiliFailure(
                "REQUEST_TIMEOUT", retryable=not mutation, unknown_write=mutation
            ) from None
        except httpx.HTTPError:
            raise BilibiliFailure(
                "NETWORK_FAILURE", retryable=not mutation, unknown_write=mutation
            ) from None
        if response_status != 200:
            raise BilibiliFailure(
                "HTTP_FAILURE", retryable=response_status >= 500, unknown_write=mutation
            )
        if urlsplit(str(response_url)).hostname not in _allowed_hostnames(origin):
            raise BilibiliFailure("RESPONSE_HOST_MISMATCH", unknown_write=mutation)
        response = httpx.Response(
            response_status,
            headers=response_headers,
            content=response_content,
            request=httpx.Request(method, url),
        )
        try:
            value = _response_json(response)
        except BilibiliFailure as error:
            if mutation:
                raise BilibiliFailure("UNKNOWN_RESULT", unknown_write=True) from error
            raise
        return value, response


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        value = json.loads(response.content, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise BilibiliFailure("PROVIDER_RESPONSE_INVALID") from None
    if not isinstance(value, dict):
        raise BilibiliFailure("PROVIDER_RESPONSE_INVALID")
    return value


def _api_failure(value: Mapping[str, Any], *, mutation: bool = False) -> BilibiliFailure:
    code = value.get("code")
    if code in {-101, -111, -400, -403, -412}:
        return BilibiliFailure(
            "AUTHORIZATION_REQUIRED" if code in {-101, -111} else "PROVIDER_REJECTED"
        )
    return BilibiliFailure(
        "UNKNOWN_RESULT" if mutation else "PROVIDER_REJECTED", unknown_write=mutation
    )


def _cookies_from_response(headers: list[str], existing: str) -> str:
    values = _parse_cookie_jar(existing, allow_missing_auth=True)
    for header in headers:
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except Exception:
            continue
        for name, cookie in parsed.items():
            domain = cookie["domain"].lower().lstrip(".")
            if name not in _COOKIE_NAMES or domain not in {
                "",
                "bilibili.com",
                "passport.bilibili.com",
            }:
                continue
            path = cookie["path"] or "/"
            if path != "/" or not _cookie_value_valid(cookie.value):
                continue
            item: dict[str, Any] = {
                "name": name,
                "value": cookie.value,
                "domain": ".bilibili.com",
                "path": "/",
            }
            expires = cookie["expires"]
            if expires:
                try:
                    parsed_expiry = parsedate_to_datetime(expires)
                    item["expires_at"] = int(parsed_expiry.astimezone(UTC).timestamp())
                except (TypeError, ValueError, OverflowError):
                    pass
            values[name] = item
    serialized = json.dumps(
        {"schemaVersion": 1, "cookies": list(values.values())},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(serialized.encode("utf-8")) > 16 * 1024:
        raise BilibiliFailure("CREDENTIAL_LIMIT_EXCEEDED")
    return serialized


def _parse_cookie_jar(value: str, *, allow_missing_auth: bool = False) -> dict[str, dict[str, Any]]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 16 * 1024:
        raise BilibiliFailure("INVALID_CREDENTIAL")
    try:
        parsed = json.loads(value, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError):
        raise BilibiliFailure("INVALID_CREDENTIAL") from None
    if (
        not isinstance(parsed, Mapping)
        or set(parsed) != {"schemaVersion", "cookies"}
        or parsed.get("schemaVersion") != 1
    ):
        raise BilibiliFailure("INVALID_CREDENTIAL")
    cookies = parsed.get("cookies")
    if not isinstance(cookies, list) or len(cookies) > len(_COOKIE_NAMES):
        raise BilibiliFailure("INVALID_CREDENTIAL")
    result: dict[str, dict[str, Any]] = {}
    for item in cookies:
        if not isinstance(item, Mapping) or set(item) - {
            "name",
            "value",
            "domain",
            "path",
            "expires_at",
        }:
            raise BilibiliFailure("INVALID_CREDENTIAL")
        name, content = item.get("name"), item.get("value")
        if (
            name not in _COOKIE_NAMES
            or not isinstance(content, str)
            or not _cookie_value_valid(content)
            or item.get("domain") not in {".bilibili.com", "bilibili.com"}
            or item.get("path") != "/"
            or name in result
        ):
            raise BilibiliFailure("INVALID_CREDENTIAL")
        expiry = item.get("expires_at")
        if expiry is not None and (
            isinstance(expiry, bool) or not isinstance(expiry, int) or expiry <= 0
        ):
            raise BilibiliFailure("INVALID_CREDENTIAL")
        result[name] = dict(item)
    if not allow_missing_auth and not {"SESSDATA", "bili_jct"} <= set(result):
        raise BilibiliFailure("INVALID_CREDENTIAL")
    return result


def _cookie_header(cookie_json: str) -> str:
    values = _parse_cookie_jar(cookie_json, allow_missing_auth=True)
    pairs = [f"{name}={item['value']}" for name, item in sorted(values.items())]
    return "; ".join(pairs)


def _cookie_value_valid(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 4096
        and not any(ord(char) < 0x20 or char in ";,\r\n" for char in value)
    )


def _valid_qr_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == "passport.bilibili.com"
        and parts.username is None
        and parts.password is None
        and parts.port in {None, 443}
        and parts.path == "/h5-app/passport/login"
        and not parts.fragment
        and len(value) <= 2048
    )


def _valid_subtitle_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == "aisubtitle.hdslb.com"
        and parts.username is None
        and parts.password is None
        and parts.port in {None, 443}
        and parts.path.startswith("/bfs/subtitle/")
        and not parts.fragment
        and len(value) <= 4096
    )


def _wbi_mixin_key(data: Mapping[str, Any]) -> str | None:
    wbi = _mapping(data.get("wbi_img"))
    img_key = _key_from_url(wbi.get("img_url"))
    sub_key = _key_from_url(wbi.get("sub_url"))
    if not img_key or not sub_key:
        return None
    source = img_key + sub_key
    return "".join(source[index] for index in _MIXIN_KEY_ENC_TAB if index < len(source))[:32]


def _key_from_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    path = urlsplit(value).path
    leaf = path.rsplit("/", 1)[-1]
    return leaf.split(".", 1)[0]


def _search_item(value: Mapping[str, Any], search_type: str) -> dict[str, Any]:
    return {
        "id": _text(value.get("bvid") or value.get("mid") or value.get("id"), 64),
        "type": search_type,
        "title": _text(value.get("title") or value.get("uname"), 512),
        "description": _text(value.get("description") or value.get("sign"), 1000),
        "author": _text(value.get("author") or value.get("uname"), 128),
        "url": _safe_bilibili_url(value.get("arcurl") or value.get("url")),
    }


def _comment_item(value: Mapping[str, Any]) -> dict[str, Any]:
    member = _mapping(value.get("member"))
    content = _mapping(value.get("content"))
    return {
        "id": str(value.get("rpid", "")),
        "root_id": str(value.get("root", "0")),
        "parent_id": str(value.get("parent", "0")),
        "user_id": str(member.get("mid", "")),
        "user": _text(member.get("uname"), 128),
        "content": _text(content.get("message"), 5000),
        "created_at": _small_int(value.get("ctime")),
    }


def _dynamic_item(value: Mapping[str, Any]) -> dict[str, Any]:
    item = _mapping(value.get("modules"))
    module_dynamic = _mapping(item.get("module_dynamic"))
    desc = _mapping(module_dynamic.get("desc"))
    major = _mapping(module_dynamic.get("major"))
    archive = _mapping(major.get("archive"))
    author = _mapping(_mapping(value.get("modules")).get("module_author"))
    return {
        "id": _text(value.get("id_str") or value.get("id"), 64),
        "user_id": str(author.get("mid", "")),
        "user": _text(author.get("name"), 128),
        "text": _text(desc.get("text") or archive.get("desc"), 5000),
        "published_at": _small_int(author.get("pub_ts")),
        "video_title": _text(archive.get("title"), 512) or None,
        "video_url": _safe_bilibili_url(archive.get("jump_url"))
        if archive.get("jump_url")
        else None,
    }


def _dm_item(value: Mapping[str, Any], subject: str) -> dict[str, Any]:
    content = value.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}
    content_map = _mapping(content)
    sender = str(value.get("sender_uid", ""))
    return {
        "id": str(value.get("msg_seqno", value.get("msg_key", ""))),
        "sender_user_id": sender,
        "direction": "OUTBOUND" if sender == subject else "INBOUND",
        "text": _text(content_map.get("content"), 5000),
        "created_at": _small_int(value.get("timestamp")),
    }


def _safe_bilibili_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or parts.hostname not in {"www.bilibili.com", "space.bilibili.com"}
        or parts.username
        or parts.password
        or parts.port not in {None, 443}
    ):
        return None
    return urlunsplit(("https", parts.hostname, parts.path, parts.query, ""))[:2048]


def _allowed_hostnames(origin: str) -> set[str]:
    return {urlsplit(origin).hostname or ""}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    text = str(value)
    return text[:maximum]


def _small_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        return None
    return value


def _small_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if 0 <= result <= 10_000_000 else None


def _boolish(value: object, page: int) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return page < value
    return False


def _is_numeric_id(value: object) -> bool:
    return (
        isinstance(value, (str, int))
        and not isinstance(value, bool)
        and bool(_NUMERIC_ID.fullmatch(str(value)))
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result
