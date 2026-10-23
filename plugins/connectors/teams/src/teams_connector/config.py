"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 config.py                                                        │
│  Module: teams_connector.config                                      │
│  Role: Load fail-closed private Teams ingress settings.               │
│                                                                      │
│  模块职责：严格加载 Teams 入站侧车私有配置                             │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when a required Teams setting is missing or unsafe.

    中文：Teams 必需配置缺失或不安全时抛出。
    """


@dataclass(frozen=True)
class IngressSettings:
    """Private settings for the Teams callback sidecar.

    中文：Teams 回调侧车的私有设置。
    """

    bot_app_id: str
    tenant_id: str
    allowed_team_ids: frozenset[str]
    navigator_base_url: str
    navigator_hmac_key_id: str
    navigator_hmac_key: bytes
    port: int = 3978
    navigator_timeout_seconds: float = 5.0

    @classmethod
    def from_environment(cls) -> IngressSettings:
        """Read the sidecar configuration and resolve the HMAC key in memory.

        中文：读取侧车配置，并仅在进程内解析 HMAC key。
        """

        bot_app_id = _required_uuid(
            os.environ.get("TEAMS_BOT_APP_ID"), "TEAMS_BOT_APP_ID"
        )
        tenant_id = _required_uuid(os.environ.get("TEAMS_TENANT_ID"), "TEAMS_TENANT_ID")
        allowed_team_ids = _parse_identifiers(
            os.environ.get("TEAMS_ALLOWED_TEAM_IDS", ""), "TEAMS_ALLOWED_TEAM_IDS"
        )
        base_url = _required_text(
            os.environ.get("NAVIGATOR_INTERNAL_BASE_URL"),
            "NAVIGATOR_INTERNAL_BASE_URL",
        )
        _validate_internal_origin(base_url)
        hmac_key_id = os.environ.get("TEAMS_NAVIGATOR_KEY_ID", "teams-v1").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", hmac_key_id):
            raise ConfigurationError("TEAMS_NAVIGATOR_KEY_ID is invalid")

        key_file = _required_text(
            os.environ.get("TEAMS_NAVIGATOR_HMAC_KEY_FILE"),
            "TEAMS_NAVIGATOR_HMAC_KEY_FILE",
        )
        hmac_key = read_secret_file(key_file, "TEAMS_NAVIGATOR_HMAC_KEY_FILE")
        if len(hmac_key) < 32:
            raise ConfigurationError(
                "Teams Navigator HMAC key must contain at least 32 bytes"
            )

        port_value = os.environ.get("PORT", "3978")
        try:
            port = int(port_value)
        except ValueError as exc:
            raise ConfigurationError("PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ConfigurationError("PORT is outside the valid TCP port range")

        timeout_value = os.environ.get("TEAMS_NAVIGATOR_TIMEOUT_SECONDS", "5")
        try:
            timeout = float(timeout_value)
        except ValueError as exc:
            raise ConfigurationError(
                "TEAMS_NAVIGATOR_TIMEOUT_SECONDS must be numeric"
            ) from exc
        if not 0.1 <= timeout <= 12:
            raise ConfigurationError(
                "TEAMS_NAVIGATOR_TIMEOUT_SECONDS must be between 0.1 and 12"
            )

        return cls(
            bot_app_id=bot_app_id,
            tenant_id=tenant_id,
            allowed_team_ids=allowed_team_ids,
            navigator_base_url=base_url.rstrip("/"),
            navigator_hmac_key_id=hmac_key_id,
            navigator_hmac_key=hmac_key,
            port=port,
            navigator_timeout_seconds=timeout,
        )


def read_secret_file(path_value: str, setting_name: str) -> bytes:
    """Read one bounded host-mounted secret without exposing its value.

    中文：读取有大小上限的主机挂载 secret，不暴露其值。
    """

    try:
        value = Path(path_value).read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"{setting_name} could not be read") from exc
    if len(value) > 16 * 1024:
        raise ConfigurationError(f"{setting_name} exceeds the allowed secret size")
    return value.rstrip(b"\r\n")


def _required_text(value: str | None, name: str) -> str:
    """Return one trimmed, non-empty setting without echoing its value.

    中文：返回经过 trim 且非空的配置，不回显配置内容。
    """

    if value is None or not value.strip():
        raise ConfigurationError(f"{name} is required")
    return value.strip()


def _required_uuid(value: str | None, name: str) -> str:
    """Validate and canonicalize one Entra UUID setting.

    中文：校验并规范化一个 Entra UUID 配置。
    """

    raw = _required_text(value, name)
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a UUID") from exc


def _parse_identifiers(value: str, name: str) -> frozenset[str]:
    """Parse a non-empty JSON array or comma-separated identifier list.

    中文：解析非空 JSON 数组或逗号分隔的标识列表。
    """

    raw = value.strip()
    if not raw:
        raise ConfigurationError(f"{name} must contain at least one identifier")
    if raw.startswith("["):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"{name} must be a JSON array") from exc
        if not isinstance(decoded, list):
            raise ConfigurationError(f"{name} must be a JSON array")
        values = decoded
    else:
        values = raw.split(",")
    if not values or any(
        not isinstance(item, str) or not item.strip() for item in values
    ):
        raise ConfigurationError(f"{name} contains an empty or invalid identifier")
    identifiers = frozenset(item.strip() for item in values)
    if len(identifiers) != len(values) or any(len(item) > 512 for item in identifiers):
        raise ConfigurationError(f"{name} contains duplicate or oversized identifiers")
    return identifiers


def _validate_internal_origin(value: str) -> None:
    """Allow HTTPS origins or explicit private sidecar origins only.

    中文：只允许 HTTPS origin 或明确的私有侧车 origin。
    """

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("NAVIGATOR_INTERNAL_BASE_URL is invalid") from exc
    if (
        parsed.scheme not in {"https", "http"}
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("NAVIGATOR_INTERNAL_BASE_URL must be an origin")
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise ConfigurationError("NAVIGATOR_INTERNAL_BASE_URL port is invalid")
    if parsed.scheme == "https":
        return
    if hostname in {"localhost", "navigator"}:
        return
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        # Single-label names are Docker/private service names; dotted DNS must use TLS.
        if "." not in hostname:
            return
    else:
        if address.is_loopback or address.is_private or address.is_link_local:
            return
    raise ConfigurationError(
        "plain HTTP is allowed only for private local Navigator services"
    )
