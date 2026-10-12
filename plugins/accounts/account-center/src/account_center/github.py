"""Fixed-endpoint GitHub App OAuth and user identity transport.

Requests are restricted to GitHub's official OAuth and API hosts. 中文：仅访问 GitHub 官方端点。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from .models import AccountCenterError, OAuthCredential

OAUTH_BASE = "https://github.com/login/oauth"
API_BASE = "https://api.github.com"
USER_AGENT = "Cyrene-Account-Center/1.0"


def pkce_s256_challenge(verifier: str) -> str:
    """Return the RFC 7636 S256 challenge for a private verifier."""

    if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
        raise AccountCenterError("invalid_request")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so bearer and OAuth secrets stay on fixed hosts."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class GitHubIdentityVerificationError(AccountCenterError):
    """The OAuth token was minted but GitHub identity verification failed."""

    def __init__(self, credential: OAuthCredential) -> None:
        super().__init__("identity_verification_failed")
        self.credential = credential


class GitHubCredentialPolicyError(AccountCenterError):
    """GitHub minted a token that does not satisfy the offline token policy."""

    def __init__(self, credential: OAuthCredential) -> None:
        super().__init__("provider_rejected")
        self.credential = credential


class GitHubCredentialPersistenceError(AccountCenterError):
    """A minted token could not be persisted for callback crash recovery."""

    def __init__(self, credential: OAuthCredential) -> None:
        super().__init__("credential_persistence_failed")
        self.credential = credential


class GitHubOAuthClient:
    """Call only fixed GitHub OAuth and `/user` endpoints with bounded timeouts."""

    def __init__(
        self, *, client_id: str, client_secret: str, timeout: float = 8.0
    ) -> None:
        if not client_id or not client_secret or not 0 < timeout <= 30:
            raise AccountCenterError("configuration_unavailable")
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())

    def authorization_url(
        self, *, redirect_uri: str, state: str, code_challenge: str
    ) -> str:
        """Build a fixed GitHub App authorization URL without user-supplied endpoints."""

        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge):
            raise AccountCenterError("invalid_request")
        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": "offline_access",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "allow_signup": "false",
            }
        )
        return f"{OAUTH_BASE}/authorize?{query}"

    def exchange_code(
        self,
        code: str,
        *,
        redirect_uri: str,
        code_verifier: str,
        on_credential: Callable[[OAuthCredential], None] | None = None,
    ) -> tuple[OAuthCredential, str]:
        """Exchange a one-time code and verify the stable numeric GitHub user ID."""

        if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier):
            raise AccountCenterError("provider_rejected")
        token_value = self._oauth_post(
            "/access_token",
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        credential = _credential(token_value, require_refresh=True)
        if on_credential is not None:
            on_credential(credential)
        try:
            user = self._request_json(
                f"{API_BASE}/user",
                headers={
                    "Authorization": f"Bearer {credential.access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except AccountCenterError:
            raise GitHubIdentityVerificationError(credential) from None
        subject = user.get("id")
        if not isinstance(subject, int) or isinstance(subject, bool) or subject <= 0:
            raise GitHubIdentityVerificationError(credential)
        return credential, str(subject)

    def refresh(self, refresh_token: str) -> OAuthCredential:
        """Refresh an expiring GitHub App user token; fail closed if unsupported."""

        token_value = self._oauth_post(
            "/access_token",
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        return _credential(token_value, require_refresh=True)

    def revoke(self, access_token: str) -> None:
        """Revoke the user grant through GitHub's official application endpoint."""

        request = urllib.request.Request(
            f"{API_BASE}/applications/{urllib.parse.quote(self.client_id, safe='')}/grant",
            data=json.dumps({"access_token": access_token}).encode("utf-8"),
            headers={
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": "2026-03-10",
            },
            method="DELETE",
        )
        auth = self.client_id + ":" + self.client_secret
        request.add_header(
            "Authorization", "Basic " + base64.b64encode(auth.encode()).decode()
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 204:
                    raise AccountCenterError("provider_rejected")
        except urllib.error.HTTPError:
            raise AccountCenterError("provider_rejected") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise AccountCenterError("provider_unavailable") from None

    def _oauth_post(self, path: str, form: Mapping[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{OAUTH_BASE}{path}",
            data=urllib.parse.urlencode(form).encode("ascii"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        return self._read_json(request)

    def _request_json(self, url: str, *, headers: Mapping[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url, headers={**headers, "User-Agent": USER_AGENT}, method="GET"
        )
        return self._read_json(request)

    def _read_json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status < 200 or response.status >= 300:
                    raise AccountCenterError("provider_rejected")
                raw = response.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise AccountCenterError("provider_rejected")
            value = json.loads(raw)
            if not isinstance(value, dict) or "error" in value:
                raise AccountCenterError("provider_rejected")
            return value
        except AccountCenterError:
            raise
        except urllib.error.HTTPError:
            raise AccountCenterError("provider_rejected") from None
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ):
            raise AccountCenterError("provider_unavailable") from None


def _credential(
    value: Mapping[str, Any], *, require_refresh: bool = False
) -> OAuthCredential:
    """Validate token payload fields without retaining provider error text."""

    access_token = value.get("access_token")
    if (
        not isinstance(access_token, str)
        or not access_token
        or len(access_token) > 16_384
    ):
        raise AccountCenterError("provider_rejected")
    refresh_token = value.get("refresh_token")
    if refresh_token is not None and (
        not isinstance(refresh_token, str) or len(refresh_token) > 16_384
    ):
        if not require_refresh:
            raise AccountCenterError("provider_rejected")
        refresh_token = None
    now = int(time.time())
    expires_in = value.get("expires_in")
    refresh_expires_in = value.get("refresh_token_expires_in")
    expires_at = (
        now + expires_in
        if isinstance(expires_in, int)
        and not isinstance(expires_in, bool)
        and expires_in > 0
        else None
    )
    refresh_expires_at = (
        now + refresh_expires_in
        if isinstance(refresh_expires_in, int)
        and not isinstance(refresh_expires_in, bool)
        and refresh_expires_in > 0
        else None
    )
    token_type = value.get("token_type", "bearer")
    credential = OAuthCredential(
        access_token, refresh_token, expires_at, refresh_expires_at
    )
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        if require_refresh:
            raise GitHubCredentialPolicyError(credential)
        raise AccountCenterError("provider_rejected")
    if require_refresh and (
        not isinstance(refresh_token, str)
        or not refresh_token
        or refresh_expires_at is None
        or expires_at is None
    ):
        raise GitHubCredentialPolicyError(credential)
    return credential
