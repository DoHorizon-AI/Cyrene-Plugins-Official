"""GitHub App JWT and installation-token transport regression tests.

Tests use an ephemeral RSA key and a local HTTP transport stub. 中文：不访问真实 GitHub。
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from account_center.github_app import GitHubAppClient, GitHubAppError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

FIXED_NOW = 1_797_000_000


class Response:
    """Minimal HTTP response value accepted by the injected transport."""

    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.body = body


def _private_key() -> tuple[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return pem, key.public_key()


def _decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def test_github_app_jwt_verifies_and_installation_token_is_repo_scoped() -> None:
    private_pem, public_key = _private_key()
    calls: list[tuple[str, str, str, dict[str, str], bytes | None]] = []
    expiry = datetime.fromtimestamp(FIXED_NOW + 3600, UTC).isoformat().replace(
        "+00:00", "Z"
    )

    def transport(
        method: str,
        host: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> Response:
        calls.append((method, host, path, headers, body))
        assert host == "api.github.com"
        token = headers["Authorization"].removeprefix("Bearer ")
        if path == "/installation/repositories?per_page=100":
            assert token == "ghs_short_lived"
            return Response(
                200,
                b'{"repositories":[{"id":123,"full_name":"acme/alpha"},{"id":456,"full_name":"acme/beta"}]}',
            )
        header, claims, signature = token.split(".")
        signing_input = f"{header}.{claims}".encode("ascii")
        public_key.verify(
            _decode(signature), signing_input, padding.PKCS1v15(), hashes.SHA256()
        )
        assert json.loads(_decode(claims)) == {
            "iat": FIXED_NOW - 30,
            "exp": FIXED_NOW + 540,
            "iss": "12345",
        }
        if method == "GET":
            return Response(200, b'{"account":{"id":88,"login":"acme"}}')
        assert method == "POST"
        assert json.loads(body or b"") == {"repository_ids": [123, 456]}
        return Response(
            201,
            json.dumps({"token": "ghs_short_lived", "expires_at": expiry}).encode(),
        )

    client = GitHubAppClient(
        "12345", private_pem, transport=transport, now=lambda: FIXED_NOW
    )
    assert client.verify_installation("9001") == {"account_id": "88", "login": "acme"}
    token = client.create_installation_token("9001", ["456", "123", "123"])
    assert token.access_token == "ghs_short_lived"
    assert token.expires_at == FIXED_NOW + 3600
    client.verify_repositories(
        token.access_token,
        [
            {"repository_id": "123", "owner": "acme", "name": "alpha"},
            {"repository_id": "456", "owner": "acme", "name": "beta"},
        ],
    )
    assert [call[:3] for call in calls] == [
        ("GET", "api.github.com", "/app/installations/9001"),
        ("POST", "api.github.com", "/app/installations/9001/access_tokens"),
        ("GET", "api.github.com", "/installation/repositories?per_page=100"),
    ]
    assert all(call[3]["X-GitHub-Api-Version"] == "2022-11-28" for call in calls)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "provider_authentication_failed"), (403, "provider_permission_denied"), (429, "provider_rate_limited")],
)
def test_github_app_failures_are_structured_without_upstream_bodies(
    status: int, expected: str
) -> None:
    private_pem, _public_key = _private_key()

    def transport(*_: Any) -> Response:
        return Response(status, b"private GitHub response body")

    client = GitHubAppClient("12345", private_pem, transport=transport, now=lambda: FIXED_NOW)
    with pytest.raises(GitHubAppError, match=expected) as caught:
        client.verify_installation("9001")
    assert "private GitHub response body" not in str(caught.value)


def test_github_app_rejects_bad_installation_ids_and_non_rsa_keys() -> None:
    private_pem, _public_key = _private_key()
    client = GitHubAppClient("12345", private_pem, transport=lambda *_: None)
    with pytest.raises(GitHubAppError, match="invalid_installation"):
        client.verify_installation("../9001")
    with pytest.raises(GitHubAppError, match="provider_not_configured"):
        GitHubAppClient("12345", "not a private key")
