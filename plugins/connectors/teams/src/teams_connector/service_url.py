"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 service_url.py                                                   │
│  Module: teams_connector.service_url                                │
│  Role: Validate trusted Bot Framework service routing URLs.          │
│                                                                      │
│  模块职责：校验可信 Bot Framework service routing URL                  │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import urlsplit


class ServiceUrlError(ValueError):
    """Raised when a Bot Framework service URL is outside the fixed allowlist.

    中文：Bot Framework service URL 超出固定 allowlist 时抛出。
    """


BOT_FRAMEWORK_HOSTS = frozenset(
    {"smba.trafficmanager.net", "smba.infra.teams.microsoft.com"}
)
REGIONAL_ROUTES = frozenset({"amer", "emea", "apac", "in"})
_CLEAN_SEGMENT = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_CONTROL_OR_WHITESPACE = re.compile(r"[\x00-\x20\x7f]")


def validate_bot_service_url(value: str, *, tenant_id: str) -> str:
    """Validate and return one exact, trusted Microsoft Bot Framework base URL.

    Only Microsoft's Teams Bot Connector hosts and known public routing paths
    are accepted. The original URL is returned unchanged so a regional path
    and trailing slash survive the round trip to the SDK.

    The caller must additionally ensure a Bot Framework JWT serviceurl claim,
    when present, matches the activity's ``serviceUrl`` before trusting it.

    中文：校验并原样返回 Microsoft Bot Framework base URL。仅允许 Teams Bot Connector
    官方 host 与已知公共路由路径，并保留区域路径及末尾斜线。调用者还必须确认 JWT 的
    serviceurl claim（若存在）与 activity 的 `serviceUrl` 匹配。
    """

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or value != value.strip()
        or _CONTROL_OR_WHITESPACE.search(value)
        or any(character.isspace() for character in value)
    ):
        raise ServiceUrlError("serviceUrl must be bounded text without whitespace")

    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ServiceUrlError("serviceUrl is malformed") from exc

    if (
        parsed.scheme != "https"
        or host is None
        or host.lower() not in BOT_FRAMEWORK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.netloc.lower()
        not in {
            "smba.trafficmanager.net",
            "smba.trafficmanager.net:443",
            "smba.infra.teams.microsoft.com",
            "smba.infra.teams.microsoft.com:443",
        }
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        raise ServiceUrlError(
            "serviceUrl must use an allowlisted HTTPS Bot Framework host"
        )

    path_segments = _path_segments(parsed.path)
    if host.lower() == "smba.infra.teams.microsoft.com":
        if path_segments != ["teams"]:
            raise ServiceUrlError(
                "serviceUrl path is not allowed for the Teams infrastructure host"
            )
        return value

    if path_segments == ["teams"]:
        return value
    if len(path_segments) not in {1, 2} or path_segments[0] not in REGIONAL_ROUTES:
        raise ServiceUrlError("serviceUrl regional path is not allowed")
    if len(path_segments) == 2:
        try:
            route_tenant = str(uuid.UUID(path_segments[1]))
            expected_tenant = str(uuid.UUID(tenant_id))
        except ValueError as exc:
            raise ServiceUrlError(
                "regional serviceUrl tenant segment must be a UUID"
            ) from exc
        if route_tenant != expected_tenant:
            raise ServiceUrlError(
                "regional serviceUrl tenant does not match the binding"
            )
    return value


def _path_segments(path: str) -> list[str]:
    """Parse clean path segments while preserving the original URL elsewhere.

    中文：解析无歧义路径片段，同时由调用者保留原始 URL。
    """

    if not path.startswith("/") or "%" in path or "\\" in path:
        raise ServiceUrlError("serviceUrl path is not canonical")
    segments = path[1:].split("/")
    if segments and segments[-1] == "":
        segments.pop()
    if not segments or any(
        not _CLEAN_SEGMENT.fullmatch(segment) or segment in {".", ".."}
        for segment in segments
    ):
        raise ServiceUrlError("serviceUrl path contains invalid segments")
    return segments
