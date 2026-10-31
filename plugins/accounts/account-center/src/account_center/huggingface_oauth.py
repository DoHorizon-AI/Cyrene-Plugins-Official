"""Hugging Face device OAuth seed validation and refresh support.

OAuth credentials enter Account Center through a launcher-owned private secret
file. Hub endpoints stay fixed here; callers cannot select an OAuth host.
中文：HF OAuth 凭据只经宿主管理的私有文件进入账号中心。
"""

from __future__ import annotations

import http.client
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

HUGGINGFACE_HOST = "huggingface.co"
OAUTH_TOKEN_PATH = "/oauth/token"
OAUTH_DEVICE_PATH = "/oauth/device"
MAX_RESPONSE_BYTES = 256_000
MAX_CREDENTIAL_BYTES = 64_000
ALLOWED_DEVICE_VERIFICATION_URIS = frozenset(
    {"https://hf.co/oauth/device", "https://huggingface.co/oauth/device"}
)
ALLOWED_OAUTH_SCOPES = frozenset(
    {"openid", "profile", "read-repos", "gated-repos", "write-repos"}
)
_OAUTH_ERROR_CODES = frozenset(
    {
        "authorization_pending",
        "slow_down",
        "expired_token",
        "access_denied",
        "invalid_grant",
        "invalid_client",
        "invalid_scope",
    }
)


class HuggingFaceOAuthError(RuntimeError):
    """Sanitized OAuth failure with refresh-rotation certainty."""

    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


@dataclass(frozen=True, slots=True)
class HuggingFaceOAuthSeed:
    """Initial device OAuth material loaded from the private host secret."""

    client_id: str
    access_token: str
    refresh_token: str
    access_token_expires_at: int
    refresh_token_expires_at: int | None
    oauth_scopes: tuple[str, ...] | None
    target_scope: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HuggingFaceOAuthToken:
    """A validated token refresh result held only in process memory."""

    access_token: str
    refresh_token: str | None
    access_token_expires_at: int
    refresh_token_expires_at: int | None
    oauth_scopes: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class HuggingFaceDeviceCode:
    """Short-lived device authorization facts; device_code remains private."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval_seconds: int


def parse_device_oauth_seed(
    value: str,
    *,
    expected_secret_ref: str,
) -> HuggingFaceOAuthSeed:
    """Parse the closed host secret DTO without exposing credential contents."""

    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
        raise HuggingFaceOAuthError("credential_unavailable")
    try:
        payload = json.loads(value, object_pairs_hook=_unique_pairs)
    except (UnicodeEncodeError, json.JSONDecodeError, ValueError):
        raise HuggingFaceOAuthError("credential_unavailable") from None
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "provider",
        "auth_mode",
        "client_id",
        "oauth_scopes",
        "access_token",
        "refresh_token",
        "access_token_expires_at",
        "refresh_token_expires_at",
        "target_scope",
        "credential_secret_ref",
    }:
        raise HuggingFaceOAuthError("credential_unavailable")
    if (
        payload["schema_version"] != 1
        or isinstance(payload["schema_version"], bool)
        or payload["provider"] != "huggingface"
        or payload["auth_mode"] != "device_oauth"
        or payload["credential_secret_ref"] != expected_secret_ref
    ):
        raise HuggingFaceOAuthError("credential_unavailable")
    client_id = payload["client_id"]
    access_token = payload["access_token"]
    refresh_token = payload["refresh_token"]
    access_expires_at = payload["access_token_expires_at"]
    refresh_expires_at = payload["refresh_token_expires_at"]
    scopes = payload["oauth_scopes"]
    target_scope = payload["target_scope"]
    target_repositories = (
        target_scope.get("repositories") if isinstance(target_scope, Mapping) else None
    )
    if (
        not isinstance(client_id, str)
        or not 1 <= len(client_id) <= 2048
        or not client_id.isprintable()
        or any(character.isspace() for character in client_id)
        or not _safe_token(access_token)
        or not _safe_token(refresh_token)
        or not _timestamp(access_expires_at)
        or (refresh_expires_at is not None and not _timestamp(refresh_expires_at))
        or (
            scopes is not None
            and (
                not isinstance(scopes, list)
                or any(not isinstance(scope, str) for scope in scopes)
                or len(scopes) != len(set(scopes))
                or any(scope not in ALLOWED_OAUTH_SCOPES for scope in scopes)
            )
        )
        or not isinstance(target_scope, Mapping)
        or not isinstance(target_repositories, list)
        or not target_repositories
        or len(target_repositories) > 100
    ):
        raise HuggingFaceOAuthError("credential_unavailable")
    return HuggingFaceOAuthSeed(
        client_id=client_id,
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_expires_at=access_expires_at,
        refresh_token_expires_at=refresh_expires_at,
        oauth_scopes=tuple(sorted(scopes)) if scopes is not None else None,
        target_scope=target_scope,
    )


class HuggingFaceOAuthClient:
    """Refresh OAuth tokens through the single fixed Hugging Face endpoint."""

    def __init__(self, *, transport: Any | None = None) -> None:
        self._transport = transport

    def refresh(
        self,
        *,
        client_id: str,
        refresh_token: str,
        now: int,
    ) -> HuggingFaceOAuthToken:
        """Exchange one refresh token, retaining sanitized error semantics."""

        if (
            not isinstance(client_id, str)
            or not client_id
            or not _safe_token(refresh_token)
        ):
            raise HuggingFaceOAuthError("credential_unavailable")
        body = urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            }
        ).encode("ascii")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Cyrene-Account-Center",
        }
        try:
            response = self._request(headers, body)
        except HuggingFaceOAuthError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException):
            # The Hub may have rotated the refresh token before this transport
            # failed, so the old material cannot be safely retried.
            raise HuggingFaceOAuthError(
                "credential_refresh_unknown", uncertain=True
            ) from None
        return _parse_oauth_token_response(response, now=now)

    def begin_device_code(
        self,
        *,
        client_id: str,
        oauth_scopes: tuple[str, ...],
    ) -> HuggingFaceDeviceCode:
        """Start one official HF device-code authorization with fixed scopes."""

        if (
            not isinstance(client_id, str)
            or not client_id
            or not oauth_scopes
            or len(oauth_scopes) != len(set(oauth_scopes))
            or any(scope not in ALLOWED_OAUTH_SCOPES for scope in oauth_scopes)
        ):
            raise HuggingFaceOAuthError("invalid_scope")
        body = urlencode(
            {"client_id": client_id, "scope": " ".join(oauth_scopes)}
        ).encode("ascii")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Cyrene-Account-Center",
        }
        try:
            response = self._request_path(OAUTH_DEVICE_PATH, headers, body)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise HuggingFaceOAuthError(
                "device_start_unknown", uncertain=True
            ) from None
        raw_body = getattr(response, "body", b"")
        if not isinstance(raw_body, bytes) or len(raw_body) > MAX_RESPONSE_BYTES:
            raise HuggingFaceOAuthError("invalid_provider_response", uncertain=True)
        if response.status < 200 or response.status >= 300:
            code = _oauth_error_code(raw_body)
            if response.status == 429:
                raise HuggingFaceOAuthError("provider_rate_limited")
            if code == "invalid_scope":
                raise HuggingFaceOAuthError("invalid_scope")
            if code == "invalid_client":
                raise HuggingFaceOAuthError("oauth_client_rejected")
            if response.status >= 500:
                raise HuggingFaceOAuthError("device_start_unknown", uncertain=True)
            raise HuggingFaceOAuthError("provider_rejected")
        try:
            payload = json.loads(
                raw_body.decode("utf-8"), object_pairs_hook=_unique_pairs
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise HuggingFaceOAuthError(
                "invalid_provider_response", uncertain=True
            ) from None
        if not isinstance(payload, Mapping):
            raise HuggingFaceOAuthError("invalid_provider_response", uncertain=True)
        device_code = payload.get("device_code")
        user_code = payload.get("user_code")
        verification_uri = payload.get("verification_uri")
        expires_in = payload.get("expires_in")
        interval = payload.get("interval", 5)
        if (
            not _safe_token(device_code)
            or not isinstance(user_code, str)
            or re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", user_code) is None
            or not _safe_verification_uri(verification_uri)
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or not 1 <= expires_in <= 3600
            or not isinstance(interval, int)
            or isinstance(interval, bool)
            or not 1 <= interval <= 60
        ):
            raise HuggingFaceOAuthError("invalid_provider_response", uncertain=True)
        return HuggingFaceDeviceCode(
            device_code=device_code,
            user_code=user_code,
            verification_uri=verification_uri,
            expires_in=expires_in,
            interval_seconds=interval,
        )

    def poll_device_code(
        self,
        *,
        client_id: str,
        device_code: str,
        now: int,
    ) -> HuggingFaceOAuthToken:
        """Poll the fixed HF token endpoint; pending outcomes stay distinguishable."""

        if (
            not isinstance(client_id, str)
            or not client_id
            or not _safe_token(device_code)
        ):
            raise HuggingFaceOAuthError("credential_unavailable")
        body = urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            }
        ).encode("ascii")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Cyrene-Account-Center",
        }
        try:
            response = self._request_path(OAUTH_TOKEN_PATH, headers, body)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise HuggingFaceOAuthError("device_poll_unknown", uncertain=True) from None
        raw_body = getattr(response, "body", b"")
        if not isinstance(raw_body, bytes) or len(raw_body) > MAX_RESPONSE_BYTES:
            raise HuggingFaceOAuthError("invalid_provider_response", uncertain=True)
        if response.status < 200 or response.status >= 300:
            code = _oauth_error_code(raw_body)
            if code in {
                "authorization_pending",
                "slow_down",
                "expired_token",
                "access_denied",
            }:
                raise HuggingFaceOAuthError(code)
            if response.status == 429:
                raise HuggingFaceOAuthError("provider_rate_limited")
            if response.status >= 500:
                raise HuggingFaceOAuthError("device_poll_unknown", uncertain=True)
            if code == "invalid_client":
                raise HuggingFaceOAuthError("oauth_client_rejected")
            raise HuggingFaceOAuthError("provider_rejected")
        return _parse_oauth_token_response(response, now=now)

    def _request(self, headers: Mapping[str, str], body: bytes) -> Any:
        return self._request_path(OAUTH_TOKEN_PATH, headers, body)

    def _request_path(self, path: str, headers: Mapping[str, str], body: bytes) -> Any:
        if self._transport is not None:
            return self._transport("POST", HUGGINGFACE_HOST, path, headers, body)
        connection = http.client.HTTPSConnection(HUGGINGFACE_HOST, timeout=15)
        try:
            connection.request("POST", path, body=body, headers=dict(headers))
            reply = connection.getresponse()
            return type(
                "Response",
                (),
                {
                    "status": reply.status,
                    "headers": {
                        key.lower(): value for key, value in reply.getheaders()
                    },
                    "body": reply.read(MAX_RESPONSE_BYTES + 1),
                },
            )()
        finally:
            connection.close()


def _parse_oauth_token_response(response: Any, *, now: int) -> HuggingFaceOAuthToken:
    """Validate one bounded OAuth token response without preserving its raw body."""

    raw_body = getattr(response, "body", b"")
    if not isinstance(raw_body, bytes) or len(raw_body) > MAX_RESPONSE_BYTES:
        raise HuggingFaceOAuthError("invalid_provider_response", uncertain=True)
    if response.status < 200 or response.status >= 300:
        code = _oauth_error_code(raw_body)
        if response.status == 429:
            raise HuggingFaceOAuthError("provider_rate_limited")
        if code == "invalid_grant":
            raise HuggingFaceOAuthError("credential_rejected")
        if code in {"invalid_client", "invalid_scope"}:
            raise HuggingFaceOAuthError("oauth_client_rejected")
        if response.status >= 500:
            raise HuggingFaceOAuthError("credential_refresh_unknown", uncertain=True)
        raise HuggingFaceOAuthError("provider_rejected")
    try:
        payload = json.loads(raw_body.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise HuggingFaceOAuthError(
            "invalid_provider_response", uncertain=True
        ) from None
    if not isinstance(payload, Mapping):
        raise HuggingFaceOAuthError("invalid_provider_response", uncertain=True)
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    refresh_expires_in = payload.get(
        "refresh_expires_in", payload.get("refresh_token_expires_in")
    )
    scope_value = payload.get("scope")
    if (
        not _safe_token(access_token)
        or (refresh_token is not None and not _safe_token(refresh_token))
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or not 1 <= expires_in <= 31_536_000
        or (
            refresh_expires_in is not None
            and (
                not isinstance(refresh_expires_in, int)
                or isinstance(refresh_expires_in, bool)
                or not 1 <= refresh_expires_in <= 31_536_000
            )
        )
        or (
            scope_value is not None
            and (
                not isinstance(scope_value, str)
                or any(
                    scope not in ALLOWED_OAUTH_SCOPES for scope in scope_value.split()
                )
                or len(scope_value.split()) != len(set(scope_value.split()))
            )
        )
    ):
        raise HuggingFaceOAuthError("invalid_provider_response", uncertain=True)
    return HuggingFaceOAuthToken(
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_expires_at=now + expires_in,
        refresh_token_expires_at=(
            now + refresh_expires_in if refresh_expires_in is not None else None
        ),
        oauth_scopes=tuple(sorted(scope_value.split()))
        if isinstance(scope_value, str)
        else None,
    )


def _safe_verification_uri(value: Any) -> bool:
    """Accept only the two observed canonical Hugging Face device pages."""

    return isinstance(value, str) and value in ALLOWED_DEVICE_VERIFICATION_URIS


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _safe_token(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 8192
        and "\r" not in value
        and "\n" not in value
        and "\x00" not in value
    )


def _timestamp(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _oauth_error_code(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    code = payload.get("error") if isinstance(payload, Mapping) else None
    return code if isinstance(code, str) and code in _OAUTH_ERROR_CODES else None
