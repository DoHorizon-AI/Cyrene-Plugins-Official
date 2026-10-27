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
import stat
import uuid
from dataclasses import dataclass, field
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
    personal_chat_enabled: bool = False
    port: int = 3978
    navigator_timeout_seconds: float = 5.0
    delivery_state_dir: Path | None = None
    delivery_admin_token: bytes | None = field(default=None, repr=False)

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
        personal_chat_enabled = _parse_bool(
            os.environ.get("TEAMS_PERSONAL_CHAT_ENABLED", "false"),
            "TEAMS_PERSONAL_CHAT_ENABLED",
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

        state_dir_value = os.environ.get("TEAMS_NAVIGATOR_DELIVERY_STATE_DIR")
        admin_token_path = os.environ.get("TEAMS_DELIVERY_ADMIN_TOKEN_FILE")
        delivery_state_dir: Path | None = None
        delivery_admin_token: bytes | None = None
        if state_dir_value is None and admin_token_path is not None:
            raise ConfigurationError(
                "TEAMS_DELIVERY_ADMIN_TOKEN_FILE requires durable delivery state"
            )
        if state_dir_value is not None:
            state_dir_text = _required_text(
                state_dir_value, "TEAMS_NAVIGATOR_DELIVERY_STATE_DIR"
            )
            if admin_token_path is None:
                raise ConfigurationError(
                    "durable delivery requires TEAMS_DELIVERY_ADMIN_TOKEN_FILE"
                )
            admin_token_text = _required_text(
                admin_token_path, "TEAMS_DELIVERY_ADMIN_TOKEN_FILE"
            )
            delivery_state_dir = Path(state_dir_text)
            if not delivery_state_dir.is_absolute():
                raise ConfigurationError(
                    "TEAMS_NAVIGATOR_DELIVERY_STATE_DIR must be absolute"
                )
            delivery_admin_token = read_admin_token_file(admin_token_text)

        return cls(
            bot_app_id=bot_app_id,
            tenant_id=tenant_id,
            allowed_team_ids=allowed_team_ids,
            navigator_base_url=base_url.rstrip("/"),
            navigator_hmac_key_id=hmac_key_id,
            navigator_hmac_key=hmac_key,
            personal_chat_enabled=personal_chat_enabled,
            port=port,
            navigator_timeout_seconds=timeout,
            delivery_state_dir=delivery_state_dir,
            delivery_admin_token=delivery_admin_token,
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


def read_admin_token_file(path_value: str) -> bytes:
    """Read an independent owner-only admin token without following symlinks.

    The file must belong to the service UID, have no group/world permissions,
    and contain a bounded printable bearer token. Its value is never logged.

    中文：读取独立的 owner-only admin token，不跟随符号链接；文件属于服务 UID、权限
    不向 group/world 开放，token 长度有界且可安全放入 Bearer header。
    """

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path_value, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != getattr(os, "geteuid", lambda: info.st_uid)()
                or info.st_mode & 0o077
                or info.st_size > 4096
            ):
                raise ConfigurationError(
                    "TEAMS_DELIVERY_ADMIN_TOKEN_FILE must be an owner-only regular file"
                )
            value = os.read(descriptor, 4097).rstrip(b"\r\n")
        finally:
            os.close(descriptor)
    except ConfigurationError:
        raise
    except OSError as exc:
        raise ConfigurationError(
            "TEAMS_DELIVERY_ADMIN_TOKEN_FILE could not be read"
        ) from exc
    if not re.fullmatch(rb"[A-Za-z0-9._~-]{32,256}", value):
        raise ConfigurationError(
            "TEAMS_DELIVERY_ADMIN_TOKEN_FILE must contain a 32-256 byte bearer token"
        )
    return value


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


def _parse_bool(value: str, name: str) -> bool:
    """Parse one explicit boolean flag without truthy-value guessing.

    中文：严格解析显式布尔值，不猜测其他 truthy 字符串。
    """

    normalized = value.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ConfigurationError(f"{name} must be true or false")


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
