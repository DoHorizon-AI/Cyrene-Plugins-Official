"""Official GitHub App authorization request contract tests."""

from __future__ import annotations

import base64
import json
from typing import Self
from urllib.parse import parse_qs, urlparse

import pytest
from account_center.github import (
    GitHubCredentialPolicyError,
    GitHubOAuthClient,
    pkce_s256_challenge,
)


class CapturedResponse:
    """Small context-manager response used to capture official HTTP requests."""

    status = 200

    def __init__(self, body: dict[str, object], *, status: int = 200) -> None:
        self._body = json.dumps(body).encode("utf-8")
        self.status = status

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _maximum: int) -> bytes:
        return self._body


class CapturedOpener:
    """Return token and user responses while preserving request details."""

    def __init__(self) -> None:
        self.requests: list[object] = []
        self.responses = [
            CapturedResponse(
                {
                    "access_token": "ghu_captured_access",
                    "refresh_token": "ghr_captured_refresh",
                    "expires_in": 28_800,
                    "refresh_token_expires_in": 15_897_600,
                    "token_type": "bearer",
                }
            ),
            CapturedResponse({"id": 12345}),
        ]

    def open(self, request: object, *, timeout: float) -> CapturedResponse:
        assert timeout > 0
        self.requests.append(request)
        return self.responses.pop(0)


def test_github_app_authorization_and_exchange_capture_pkce_offline_access() -> None:
    client = GitHubOAuthClient(client_id="client-id", client_secret="client-secret")
    opener = CapturedOpener()
    client._opener = opener
    verifier = "A" * 43
    challenge = pkce_s256_challenge(verifier)
    redirect_uri = "https://exchange.example/api/v1/accounts/github/callback"

    authorization_url = client.authorization_url(
        redirect_uri=redirect_uri,
        state="s" * 43,
        code_challenge=challenge,
    )
    authorization = parse_qs(urlparse(authorization_url).query)
    assert authorization["scope"] == ["offline_access"]
    assert authorization["code_challenge"] == [challenge]
    assert authorization["code_challenge_method"] == ["S256"]
    assert authorization["redirect_uri"] == [redirect_uri]
    assert "read:user" not in authorization
    assert "repo" not in authorization

    credential, subject = client.exchange_code(
        "one-time-code", redirect_uri=redirect_uri, code_verifier=verifier
    )

    token_request, user_request = opener.requests
    assert token_request.full_url == "https://github.com/login/oauth/access_token"
    assert token_request.get_method() == "POST"
    token_form = parse_qs(token_request.data.decode("ascii"))
    assert token_form["code"] == ["one-time-code"]
    assert token_form["redirect_uri"] == [redirect_uri]
    assert token_form["code_verifier"] == [verifier]
    assert token_form["client_id"] == ["client-id"]
    assert token_form["client_secret"] == ["client-secret"]
    assert user_request.full_url == "https://api.github.com/user"
    assert credential.refresh_token == "ghr_captured_refresh"
    assert subject == "12345"


def test_github_app_revocation_uses_official_rest_grant_endpoint() -> None:
    client = GitHubOAuthClient(client_id="client-id", client_secret="client-secret")
    opener = CapturedOpener()
    opener.responses = [CapturedResponse({}, status=204)]
    client._opener = opener

    client.revoke("ghu_captured_access")

    request = opener.requests[0]
    assert request.full_url == "https://api.github.com/applications/client-id/grant"
    assert request.get_method() == "DELETE"
    assert json.loads(request.data) == {"access_token": "ghu_captured_access"}
    assert request.get_header("X-github-api-version") == "2026-03-10"
    authorization = request.get_header("Authorization")
    assert authorization is not None and authorization.startswith("Basic ")
    decoded = base64.b64decode(authorization.removeprefix("Basic ")).decode()
    assert decoded == "client-id:client-secret"


def test_offline_access_token_without_refresh_material_is_rejected() -> None:
    client = GitHubOAuthClient(client_id="client-id", client_secret="client-secret")
    opener = CapturedOpener()
    opener.responses = [CapturedResponse({"access_token": "ghu_without_refresh"})]
    client._opener = opener

    with pytest.raises(GitHubCredentialPolicyError) as caught:
        client.exchange_code(
            "one-time-code",
            redirect_uri="https://exchange.example/api/v1/accounts/github/callback",
            code_verifier="A" * 43,
        )
    assert caught.value.credential.access_token == "ghu_without_refresh"
