"""GitHub App installation authorization over the fixed GitHub Cloud API.

This module creates short-lived installation tokens in memory and never writes
private-key material or provider response bodies to logs. 中文：只签发短期安装令牌。
"""

from __future__ import annotations

import base64
import http.client
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

GITHUB_API_HOST = "api.github.com"
GITHUB_API_VERSION = "2022-11-28"
MAX_RESPONSE_BYTES = 1_000_000


class GitHubAppError(RuntimeError):
    """Sanitized GitHub App failure suitable for the Account Center boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class InstallationToken:
    """Short-lived installation credential returned only to provider code."""

    access_token: str
    expires_at: int


class GitHubAppClient:
    """Verify app installations and mint repository-scoped installation tokens."""

    def __init__(
        self,
        app_id: str,
        private_key: str,
        *,
        transport: Any | None = None,
        now: Any = time.time,
    ) -> None:
        if not app_id.isdecimal() or int(app_id) < 1 or not private_key.startswith("-----BEGIN"):
            raise GitHubAppError("provider_not_configured")
        self._app_id = app_id
        try:
            key = serialization.load_pem_private_key(
                private_key.encode("utf-8"), password=None
            )
        except (ValueError, TypeError):
            raise GitHubAppError("provider_not_configured") from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise GitHubAppError("provider_not_configured")
        self._private_key = key
        self._transport = transport
        self._now = now

    def verify_installation(self, installation_id: str) -> dict[str, str]:
        """Confirm the installation belongs to this GitHub App and return its owner."""

        if not installation_id.isdecimal() or int(installation_id) < 1:
            raise GitHubAppError("invalid_installation")
        response = self._request(
            "GET",
            f"/app/installations/{quote(installation_id, safe='')}",
            self._app_jwt(),
        )
        value = _json_object(response)
        account = value.get("account")
        if not isinstance(account, Mapping):
            raise GitHubAppError("invalid_provider_response")
        account_id = account.get("id")
        login = account.get("login")
        if (
            isinstance(account_id, bool)
            or not isinstance(account_id, int)
            or account_id < 1
            or not isinstance(login, str)
            or not login
            or len(login) > 200
        ):
            raise GitHubAppError("invalid_provider_response")
        return {"account_id": str(account_id), "login": login}

    def create_installation_token(
        self, installation_id: str, repository_ids: list[str]
    ) -> InstallationToken:
        """Mint one cached token restricted to the linked repository IDs."""

        if not installation_id.isdecimal() or not repository_ids:
            raise GitHubAppError("invalid_installation")
        if any(not item.isdecimal() or int(item) < 1 for item in repository_ids):
            raise GitHubAppError("invalid_repository_scope")
        body = json.dumps(
            {"repository_ids": sorted({int(item) for item in repository_ids})},
            separators=(",", ":"),
        ).encode("utf-8")
        response = self._request(
            "POST",
            f"/app/installations/{quote(installation_id, safe='')}/access_tokens",
            self._app_jwt(),
            body=body,
        )
        value = _json_object(response)
        token = value.get("token")
        expires_text = value.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expires_text, str):
            raise GitHubAppError("invalid_provider_response")
        try:
            expires_at = int(
                datetime.fromisoformat(expires_text).timestamp()
            )
        except ValueError:
            raise GitHubAppError("invalid_provider_response") from None
        if expires_at <= int(self._now()) + 30:
            raise GitHubAppError("invalid_provider_response")
        return InstallationToken(token, expires_at)

    def verify_repositories(
        self,
        installation_token: str,
        repositories: list[dict[str, str]],
    ) -> None:
        """Confirm each requested stable ID/name is visible through the token."""

        response = self._request(
            "GET",
            "/installation/repositories?per_page=100",
            installation_token,
        )
        value = _json_object(response)
        available = value.get("repositories")
        if not isinstance(available, list) or len(available) > 100:
            raise GitHubAppError("invalid_provider_response")
        by_id = {
            str(item.get("id")): item
            for item in available
            if isinstance(item, Mapping) and isinstance(item.get("id"), int)
        }
        for repository in repositories:
            if set(repository) != {"repository_id", "owner", "name"}:
                raise GitHubAppError("invalid_repository_scope")
            repository_id = repository["repository_id"]
            item = by_id.get(repository_id)
            if (
                item is None
                or item.get("full_name") != f"{repository['owner']}/{repository['name']}"
            ):
                raise GitHubAppError("repository_identity_mismatch")

    def _app_jwt(self) -> str:
        """Sign a GitHub App JWT with a narrow ten-minute lifetime."""

        now = int(self._now())
        header = _base64url(b'{"alg":"RS256","typ":"JWT"}')
        payload = _base64url(
            json.dumps(
                {"iat": now - 30, "exp": now + 540, "iss": self._app_id},
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signing_input = f"{header}.{payload}".encode("ascii")
        signature = self._private_key.sign(
            signing_input, padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{header}.{payload}.{_base64url(signature)}"

    def _request(
        self,
        method: str,
        path: str,
        bearer: str,
        *,
        body: bytes | None = None,
    ) -> Any:
        """Call a fixed GitHub API path once and map failures without response text."""

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "Cyrene-Account-Center",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            if self._transport is not None:
                response = self._transport(method, GITHUB_API_HOST, path, headers, body)
            else:
                connection = http.client.HTTPSConnection(GITHUB_API_HOST, timeout=15)
                try:
                    connection.request(method, path, body=body, headers=headers)
                    response = connection.getresponse()
                    response = type(
                        "Response",
                        (),
                        {
                            "status": response.status,
                            "headers": {k.lower(): v for k, v in response.getheaders()},
                            "body": response.read(MAX_RESPONSE_BYTES + 1),
                        },
                    )()
                finally:
                    connection.close()
        except (OSError, TimeoutError, http.client.HTTPException):
            raise GitHubAppError(
                "credential_refresh_unknown" if method == "POST" else "provider_unavailable"
            ) from None
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise GitHubAppError("invalid_provider_response")
        if 200 <= response.status < 300:
            return response
        if response.status == 401:
            raise GitHubAppError("provider_authentication_failed")
        if response.status == 403:
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            if headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers:
                raise GitHubAppError("provider_rate_limited")
            raise GitHubAppError("provider_permission_denied")
        if response.status == 404:
            raise GitHubAppError("installation_not_found")
        if response.status == 429:
            raise GitHubAppError("provider_rate_limited")
        if response.status >= 500:
            raise GitHubAppError(
                "credential_refresh_unknown" if method == "POST" else "provider_unavailable"
            )
        raise GitHubAppError("provider_rejected")


def _base64url(value: bytes) -> str:
    """Encode one JWT segment without padding."""

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _json_object(response: Any) -> dict[str, Any]:
    """Decode one bounded JSON object without exposing provider response details."""

    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GitHubAppError("invalid_provider_response") from None
    if not isinstance(value, dict):
        raise GitHubAppError("invalid_provider_response")
    return value
