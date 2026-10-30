"""Synthetic Entra OIDC and resource-scope validation coverage."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlparse

import pytest
from account_center.github import pkce_s256_challenge
from account_center.microsoft_graph_oauth import (
    ALLOWED_GRAPH_SCOPES,
    AZURE_DEVOPS_RESOURCE_SCOPE,
    GRAPH_ME_ENDPOINT,
    JWKS_ENDPOINT,
    TOKEN_ENDPOINT,
    AzureDevOpsOAuthClient,
    MicrosoftGraphOAuthClient,
    MicrosoftGraphOAuthError,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

TENANT_ID = "11111111-2222-4333-8444-555555555555"
OBJECT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CLIENT_ID = "synthetic-entra-client"
NOW = 1_800_000_000


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _int_b64url(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return _b64url(value.to_bytes(length, "big"))


def _jwt(private_key: Any, claims: Mapping[str, Any], *, kid: str = "test-key") -> str:
    header = {"alg": "RS256", "kid": kid, "typ": "JWT"}
    encoded_header = _b64url(json.dumps(header, separators=(",", ":")).encode())
    encoded_claims = _b64url(json.dumps(dict(claims), separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = private_key.sign(
        signing_input,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{encoded_header}.{encoded_claims}.{_b64url(signature)}"


def _id_token(private_key: Any, nonce: str, *, tenant_id: str = TENANT_ID) -> str:
    return _jwt(
        private_key,
        {
            "iss": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
            "aud": CLIENT_ID,
            "tid": tenant_id,
            "oid": OBJECT_ID,
            "nonce": nonce,
            "iat": NOW,
            "exp": NOW + 3600,
        },
    )


def _jwks(private_key: Any) -> dict[str, Any]:
    public = private_key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kid": "test-key",
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "n": _int_b64url(public.n),
                "e": _int_b64url(public.e),
            }
        ]
    }


def test_graph_code_exchange_verifies_oidc_nonce_and_same_token_me(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = MicrosoftGraphOAuthClient(
        client_id=CLIENT_ID,
        client_secret="synthetic-secret",
        now=lambda: NOW,
    )
    scopes = ("openid", "profile", "offline_access", "Tasks.Read", "User.Read")
    authorization_url = client.authorization_url(
        redirect_uri="https://account.example.test/oauth/entra/callback",
        state="random-state-value-with-enough-entropy",
        nonce="random-nonce-value-with-enough-entropy",
        code_challenge=pkce_s256_challenge("r" * 43),
        scopes=scopes,
    )
    authorization = parse_qs(urlparse(authorization_url).query)
    assert authorization["code_challenge_method"] == ["S256"]
    assert set(authorization["scope"][0].split()) == set(scopes)
    assert not set(authorization["scope"][0].split()) - {
        "openid",
        "profile",
        "offline_access",
        *ALLOWED_GRAPH_SCOPES,
    }

    requests: list[tuple[str, str, dict[str, str] | None]] = []

    def request_json(
        url: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        requests.append((url, method, headers))
        if url == TOKEN_ENDPOINT.format(tenant="organizations"):
            assert method == "POST"
            assert body is not None
            form = dict(parse_qsl(body.decode("ascii")))
            assert form["scope"] == " ".join(scopes)
            assert form["code_verifier"] == "r" * 43
            return {
                "access_token": "opaque-graph-access-token",
                "refresh_token": "opaque-graph-refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "Tasks.Read User.Read",
                "id_token": _id_token(private_key, "random-nonce-value-with-enough-entropy"),
            }
        if url == JWKS_ENDPOINT:
            return _jwks(private_key)
        if url == GRAPH_ME_ENDPOINT:
            assert headers == {"Authorization": "Bearer opaque-graph-access-token"}
            return {"id": OBJECT_ID}
        raise AssertionError(f"unexpected request URL: {url}")

    monkeypatch.setattr(client, "_request_json", request_json)
    token, identity = client.exchange_code(
        code="synthetic-code",
        redirect_uri="https://account.example.test/oauth/entra/callback",
        code_verifier="r" * 43,
        nonce_digest=hashlib.sha256(b"random-nonce-value-with-enough-entropy").hexdigest(),
        requested_scopes=scopes,
    )
    assert identity.external_subject == f"{TENANT_ID}:{OBJECT_ID}"
    assert token.access_token == "opaque-graph-access-token"
    assert token.refresh_token == "opaque-graph-refresh-token"
    assert token.expires_at == NOW + 3600
    assert [url for url, _, _ in requests].count(GRAPH_ME_ENDPOINT) == 1


def test_graph_rejects_invalid_nonce_or_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = MicrosoftGraphOAuthClient(
        client_id=CLIENT_ID,
        client_secret="synthetic-secret",
        now=lambda: NOW,
    )
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda url, **kwargs: (
            _jwks(private_key)
            if url == JWKS_ENDPOINT
            else pytest.fail("only JWKS should be fetched before nonce validation")
        ),
    )
    token = _id_token(private_key, "signed-nonce")
    with pytest.raises(MicrosoftGraphOAuthError, match="identity_verification_failed"):
        client._verify_id_token(token, hashlib.sha256(b"wrong-nonce").hexdigest())
    tampered = token.rsplit(".", 1)[0] + "." + _b64url(b"invalid-signature")
    with pytest.raises(MicrosoftGraphOAuthError, match="identity_verification_failed"):
        client._verify_id_token(tampered, hashlib.sha256(b"signed-nonce").hexdigest())


def test_azure_devops_uses_its_own_default_resource_and_verified_oidc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = AzureDevOpsOAuthClient(
        client_id=CLIENT_ID,
        client_secret="synthetic-secret",
        now=lambda: NOW,
    )
    scopes = ("openid", "profile", "offline_access", AZURE_DEVOPS_RESOURCE_SCOPE)
    authorization_url = client.authorization_url(
        redirect_uri="https://account.example.test/oauth/entra/callback",
        state="random-state-value-with-enough-entropy",
        nonce="random-ado-nonce-value-with-enough-entropy",
        code_challenge=pkce_s256_challenge("a" * 43),
        scopes=scopes,
    )
    requested = set(parse_qs(urlparse(authorization_url).query)["scope"][0].split())
    assert requested == set(scopes)
    assert "User.Read" not in requested
    assert not any(scope.startswith("vso.") for scope in requested)

    requested_urls: list[str] = []

    def request_json(
        url: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        if url.startswith("https://login.microsoftonline.com/") and url.endswith("/token"):
            form = dict(parse_qsl((body or b"").decode("ascii")))
            requested_urls.append(form["scope"])
            assert form["scope"] == AZURE_DEVOPS_RESOURCE_SCOPE
            return {
                "access_token": "opaque-ado-resource-token",
                "refresh_token": "opaque-ado-refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "id_token": _id_token(private_key, "random-ado-nonce-value-with-enough-entropy"),
            }
        if url == JWKS_ENDPOINT:
            return _jwks(private_key)
        raise AssertionError(f"unexpected ADO OAuth URL: {url}")

    monkeypatch.setattr(client, "_request_json", request_json)
    token, identity = client.exchange_code(
        code="synthetic-ado-code",
        redirect_uri="https://account.example.test/oauth/entra/callback",
        code_verifier="a" * 43,
        nonce_digest=hashlib.sha256(b"random-ado-nonce-value-with-enough-entropy").hexdigest(),
        requested_scopes=scopes,
    )
    assert requested_urls == [AZURE_DEVOPS_RESOURCE_SCOPE]
    assert token.access_token == "opaque-ado-resource-token"
    assert token.scopes == (AZURE_DEVOPS_RESOURCE_SCOPE,)
    assert identity.external_subject == f"{TENANT_ID}:{OBJECT_ID}"


def test_azure_devops_refresh_uses_the_bound_tenant_and_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = AzureDevOpsOAuthClient(
        client_id=CLIENT_ID,
        client_secret="synthetic-secret",
        now=lambda: NOW,
    )
    requests: list[tuple[str, dict[str, str]]] = []

    def request_json(
        url: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        if url == JWKS_ENDPOINT:
            return _jwks(private_key)
        form = dict(parse_qsl((body or b"").decode("ascii")))
        requests.append((url, form))
        return {
            "access_token": "opaque-refreshed-ado-token",
            "refresh_token": "opaque-rotated-ado-refresh-token",
            "token_type": "Bearer",
            "expires_in": 1800,
            "id_token": _jwt(
                private_key,
                {
                    "iss": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
                    "aud": CLIENT_ID,
                    "tid": TENANT_ID,
                    "oid": OBJECT_ID,
                    "iat": NOW,
                    "exp": NOW + 1800,
                },
            ),
        }

    monkeypatch.setattr(client, "_request_json", request_json)
    token = client.refresh(
        tenant_id=TENANT_ID,
        refresh_token="opaque-old-ado-refresh-token",
        requested_scopes=(AZURE_DEVOPS_RESOURCE_SCOPE,),
    )
    assert requests[0][0] == TOKEN_ENDPOINT.format(tenant=TENANT_ID)
    assert requests[0][1]["scope"] == f"openid {AZURE_DEVOPS_RESOURCE_SCOPE}"
    assert token.access_token == "opaque-refreshed-ado-token"
    assert token.refresh_token == "opaque-rotated-ado-refresh-token"
    assert token.expires_at == NOW + 1800
    assert token.identity is not None
    assert token.identity.external_subject == f"{TENANT_ID}:{OBJECT_ID}"


def test_graph_refresh_requires_verified_id_token_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = MicrosoftGraphOAuthClient(
        client_id=CLIENT_ID,
        client_secret="synthetic-secret",
        now=lambda: NOW,
    )
    requests: list[tuple[str, dict[str, str]]] = []

    def request_json(
        url: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        if url == JWKS_ENDPOINT:
            return _jwks(private_key)
        form = dict(parse_qsl((body or b"").decode("ascii")))
        requests.append((url, form))
        return {
            "access_token": "opaque-refreshed-graph-token",
            "refresh_token": "opaque-rotated-graph-refresh",
            "token_type": "Bearer",
            "expires_in": 1800,
            "scope": "Tasks.Read User.Read",
            "id_token": _jwt(
                private_key,
                {
                    "iss": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
                    "aud": CLIENT_ID,
                    "tid": TENANT_ID,
                    "oid": OBJECT_ID,
                    "iat": NOW,
                    "exp": NOW + 1800,
                },
            ),
        }

    monkeypatch.setattr(client, "_request_json", request_json)
    token = client.refresh(
        tenant_id=TENANT_ID,
        refresh_token="opaque-graph-refresh-token",
        requested_scopes=("Tasks.Read", "User.Read"),
    )
    assert requests[0][0] == TOKEN_ENDPOINT.format(tenant=TENANT_ID)
    assert requests[0][1]["scope"] == "openid Tasks.Read User.Read"
    assert token.identity is not None
    assert token.identity.external_subject == f"{TENANT_ID}:{OBJECT_ID}"


def test_office_v2_oauth_is_explicit_and_never_accepts_mail_scopes() -> None:
    scopes = (
        "openid",
        "profile",
        "offline_access",
        "User.Read",
        "Tasks.ReadWrite",
        "Calendars.ReadWrite",
    )
    parameters = {
        "redirect_uri": "http://localhost:52191/oauth/callback",
        "state": "office-v2-state-with-sufficient-entropy",
        "nonce": "office-v2-nonce-with-sufficient-entropy",
        "code_challenge": "a" * 43,
        "scopes": scopes,
    }
    legacy = MicrosoftGraphOAuthClient(
        client_id=CLIENT_ID,
        client_secret="synthetic-secret",
        now=lambda: NOW,
    )
    with pytest.raises(MicrosoftGraphOAuthError, match="authorization_denied"):
        legacy.authorization_url(**parameters)

    office_v2 = MicrosoftGraphOAuthClient(
        client_id=CLIENT_ID,
        client_secret="synthetic-secret",
        office_v2_enabled=True,
        now=lambda: NOW,
    )
    authorization = parse_qs(urlparse(office_v2.authorization_url(**parameters)).query)
    assert set(authorization["scope"][0].split()) == set(scopes)
    with pytest.raises(MicrosoftGraphOAuthError, match="authorization_denied"):
        office_v2.authorization_url(**{**parameters, "scopes": (*scopes, "Mail.ReadBasic")})
