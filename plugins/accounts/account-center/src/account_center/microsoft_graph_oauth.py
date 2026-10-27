"""Fixed-endpoint Microsoft Graph delegated OAuth and identity checks.

This module never persists or returns credentials; Account Center owns their
encrypted storage and exposes them only through provider-scoped leases.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

AUTHORIZE_ENDPOINT = (
    "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize"
)
TOKEN_ENDPOINT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
JWKS_ENDPOINT = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
GRAPH_ME_ENDPOINT = "https://graph.microsoft.com/v1.0/me?$select=id"
AZURE_DEVOPS_RESOURCE_SCOPE = "https://app.vssps.visualstudio.com/.default"
INITIAL_GRAPH_SCOPES = frozenset({"User.Read", "Tasks.Read"})
ALLOWED_GRAPH_SCOPES = frozenset(
    {"User.Read", "Tasks.Read", "Tasks.ReadWrite", "Sites.Selected"}
)
MAX_TOKEN_LIFETIME_SECONDS = 86_400


class MicrosoftGraphOAuthError(RuntimeError):
    """Sanitized OAuth failure with a stable Account Center error code."""

    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


@dataclass(frozen=True, slots=True)
class MicrosoftGraphToken:
    """Short-lived in-memory delegated token response."""

    access_token: str
    refresh_token: str | None
    expires_at: int
    refresh_expires_at: int | None
    scopes: tuple[str, ...]
    identity: MicrosoftGraphIdentity | None = None


@dataclass(frozen=True, slots=True)
class MicrosoftGraphIdentity:
    """Stable tenant and object identifiers proven by Microsoft responses."""

    tenant_id: str
    object_id: str

    @property
    def external_subject(self) -> str:
        """Return the canonical immutable tenant/object tuple."""

        return f"{self.tenant_id}:{self.object_id}"


class MicrosoftGraphOAuthClient:
    """Use authorization-code PKCE and verify the same-token `/me` identity."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        now: Any = time.time,
    ) -> None:
        if not client_id or not client_secret:
            raise MicrosoftGraphOAuthError("provider_not_configured")
        self.client_id = client_id
        self._client_secret = client_secret
        self._now = now

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
        scopes: tuple[str, ...],
        prompt: str | None = None,
    ) -> str:
        """Build a fixed Microsoft authorization URL with PKCE and OIDC nonce."""

        _validate_scope_request(scopes)
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "response_mode": "query",
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if prompt is not None:
            if prompt not in {"consent", "select_account"}:
                raise MicrosoftGraphOAuthError("authorization_denied")
            params["prompt"] = prompt
        return f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        nonce_digest: str,
        requested_scopes: tuple[str, ...],
    ) -> tuple[MicrosoftGraphToken, MicrosoftGraphIdentity]:
        """Exchange one PKCE code, verify its ID token, then check Graph `/me`."""

        _validate_scope_request(requested_scopes)
        response = self._post_token(
            "organizations",
            {
                "client_id": self.client_id,
                "client_secret": self._client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "scope": " ".join(requested_scopes),
            },
        )
        token = self._parse_token(response, requested_scopes)
        id_token = response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise MicrosoftGraphOAuthError("identity_verification_failed")
        identity = self._verify_id_token(id_token, nonce_digest)
        graph_identity = self.verify_identity(token.access_token, identity.tenant_id)
        if graph_identity != identity:
            raise MicrosoftGraphOAuthError("external_identity_mismatch")
        return token, graph_identity

    def refresh(
        self,
        *,
        tenant_id: str,
        refresh_token: str,
        requested_scopes: tuple[str, ...],
    ) -> MicrosoftGraphToken:
        """Refresh only through the already-bound tenant and saved scope set."""

        tenant = _canonical_uuid(tenant_id)
        _validate_scope_request(requested_scopes)
        response = self._post_token(
            tenant,
            {
                "client_id": self.client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": " ".join(("openid", *requested_scopes)),
            },
        )
        token = self._parse_token(response, requested_scopes, require_id_token=False)
        id_token = response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise MicrosoftGraphOAuthError("identity_verification_failed")
        identity = self._verify_id_token(id_token, expected_nonce_digest=None)
        return MicrosoftGraphToken(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=token.expires_at,
            refresh_expires_at=token.refresh_expires_at,
            scopes=token.scopes,
            identity=identity,
        )

    def verify_identity(
        self, access_token: str, tenant_id: str
    ) -> MicrosoftGraphIdentity:
        """Resolve `/me` with the supplied token and require an object UUID."""

        tenant = _canonical_uuid(tenant_id)
        value = self._request_json(
            GRAPH_ME_ENDPOINT,
            method="GET",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        object_id = _canonical_uuid(value.get("id"))
        return MicrosoftGraphIdentity(tenant, object_id)

    def _post_token(self, tenant: str, values: dict[str, str]) -> dict[str, Any]:
        return self._request_json(
            TOKEN_ENDPOINT.format(tenant=tenant),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode(values).encode("ascii"),
        )

    def _parse_token(
        self,
        value: dict[str, Any],
        requested_scopes: tuple[str, ...],
        *,
        require_id_token: bool = True,
    ) -> MicrosoftGraphToken:
        access_token = value.get("access_token")
        refresh_token = value.get("refresh_token")
        expires_in = value.get("expires_in")
        scope_text = value.get("scope")
        if (
            not isinstance(access_token, str)
            or not access_token
            or len(access_token) > 16_384
            or value.get("token_type") != "Bearer"
            or (
                require_id_token
                and (
                    not isinstance(refresh_token, str)
                    or not refresh_token
                    or len(refresh_token) > 16_384
                )
            )
            or (
                refresh_token is not None
                and (
                    not isinstance(refresh_token, str)
                    or not refresh_token
                    or len(refresh_token) > 16_384
                )
            )
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or not 1 <= expires_in <= MAX_TOKEN_LIFETIME_SECONDS
            or not isinstance(scope_text, str)
        ):
            raise MicrosoftGraphOAuthError("credential_unavailable")
        if require_id_token and not isinstance(value.get("id_token"), str):
            raise MicrosoftGraphOAuthError("identity_verification_failed")
        scopes = _validate_scope_response(scope_text, requested_scopes)
        now = int(self._now())
        return MicrosoftGraphToken(
            access_token=access_token,
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
            expires_at=now + expires_in,
            refresh_expires_at=None,
            scopes=tuple(sorted(scopes)),
        )

    def _verify_id_token(
        self, token: str, expected_nonce_digest: str | None
    ) -> MicrosoftGraphIdentity:
        """Verify RS256, issuer, audience, freshness, nonce, tenant, and object."""

        parts = token.split(".")
        if len(parts) != 3:
            raise MicrosoftGraphOAuthError("identity_verification_failed")
        try:
            header = _decode_json_segment(parts[0])
            claims = _decode_json_segment(parts[1])
            signature = _decode_segment(parts[2])
            kid = header["kid"]
            algorithm = header["alg"]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            raise MicrosoftGraphOAuthError("identity_verification_failed") from None
        if not isinstance(kid, str) or not kid or algorithm != "RS256":
            raise MicrosoftGraphOAuthError("identity_verification_failed")
        jwks = self._request_json(JWKS_ENDPOINT, method="GET")
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            raise MicrosoftGraphOAuthError("identity_verification_failed")
        key = next(
            (
                item
                for item in keys
                if isinstance(item, dict)
                and item.get("kid") == kid
                and item.get("kty") == "RSA"
                and item.get("use", "sig") == "sig"
                and item.get("alg", "RS256") == "RS256"
            ),
            None,
        )
        if key is None:
            raise MicrosoftGraphOAuthError("identity_verification_failed")
        try:
            public_key = rsa.RSAPublicNumbers(
                int.from_bytes(_decode_segment(key["e"]), "big"),
                int.from_bytes(_decode_segment(key["n"]), "big"),
            ).public_key()
            if public_key.key_size < 2048:
                raise MicrosoftGraphOAuthError("identity_verification_failed")
            public_key.verify(
                signature,
                f"{parts[0]}.{parts[1]}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            tenant_id = _canonical_uuid(claims.get("tid"))
            object_id = _canonical_uuid(claims.get("oid"))
            nonce = claims.get("nonce")
            expires_at = claims.get("exp")
            issued_at = claims.get("iat")
            not_before = claims.get("nbf")
            audience = claims.get("aud")
            expected_issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
            nonce_matches = expected_nonce_digest is None or (
                isinstance(nonce, str)
                and hmac.compare_digest(
                    hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
                    expected_nonce_digest,
                )
            )
            now = int(self._now())
            if (
                claims.get("iss") != expected_issuer
                or audience != self.client_id
                or not nonce_matches
                or not isinstance(expires_at, int)
                or isinstance(expires_at, bool)
                or expires_at <= now
                or not isinstance(issued_at, int)
                or isinstance(issued_at, bool)
                or issued_at > now + 60
                or now - issued_at > 600
                or (
                    not_before is not None
                    and (
                        not isinstance(not_before, int)
                        or isinstance(not_before, bool)
                        or not_before > now + 60
                    )
                )
            ):
                raise MicrosoftGraphOAuthError("identity_verification_failed")
        except (InvalidSignature, KeyError, TypeError, ValueError, OverflowError):
            raise MicrosoftGraphOAuthError("identity_verification_failed") from None
        return MicrosoftGraphIdentity(tenant_id, object_id)

    @staticmethod
    def _request_json(
        url: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        """Use a fixed HTTPS endpoint and bound response size and redirects."""

        request = Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                if response.geturl().split("/", 3)[:3] != url.split("/", 3)[:3]:
                    raise MicrosoftGraphOAuthError("provider_unavailable")
                raw = response.read(1_048_577)
        except HTTPError as error:
            try:
                payload = json.loads(error.read(65_537))
            except Exception:  # noqa: BLE001 - provider response is not exposed.
                payload = {}
            error_code = payload.get("error") if isinstance(payload, dict) else None
            if error_code == "invalid_grant":
                raise MicrosoftGraphOAuthError("credential_rejected") from None
            raise MicrosoftGraphOAuthError(
                "provider_unavailable", uncertain=method == "POST"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise MicrosoftGraphOAuthError(
                "provider_unavailable", uncertain=method == "POST"
            ) from None
        if len(raw) > 1_048_576:
            raise MicrosoftGraphOAuthError("provider_response_invalid")
        try:
            value = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise MicrosoftGraphOAuthError("provider_response_invalid") from None
        if not isinstance(value, dict):
            raise MicrosoftGraphOAuthError("provider_response_invalid")
        return value


class AzureDevOpsOAuthClient(MicrosoftGraphOAuthClient):
    """Use the shared Entra code flow for Azure DevOps' separate resource."""

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
        scopes: tuple[str, ...],
        prompt: str | None = None,
    ) -> str:
        """Build the exact ADO `.default` grant without Graph resource scopes."""

        if (
            set(scopes)
            != {
                "openid",
                "profile",
                "offline_access",
                AZURE_DEVOPS_RESOURCE_SCOPE,
            }
            or len(scopes) != 4
        ):
            raise MicrosoftGraphOAuthError("authorization_denied")
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "response_mode": "query",
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if prompt is not None:
            if prompt not in {"consent", "select_account"}:
                raise MicrosoftGraphOAuthError("authorization_denied")
            params["prompt"] = prompt
        return f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        nonce_digest: str,
        requested_scopes: tuple[str, ...],
    ) -> tuple[MicrosoftGraphToken, MicrosoftGraphIdentity]:
        """Exchange the ADO code and bind it to signed Entra OIDC identity."""

        if (
            set(requested_scopes)
            != {
                "openid",
                "profile",
                "offline_access",
                AZURE_DEVOPS_RESOURCE_SCOPE,
            }
            or len(requested_scopes) != 4
        ):
            raise MicrosoftGraphOAuthError("authorization_denied")
        response = self._post_token(
            "organizations",
            {
                "client_id": self.client_id,
                "client_secret": self._client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "scope": AZURE_DEVOPS_RESOURCE_SCOPE,
            },
        )
        token = _parse_azure_devops_token(response, now=int(self._now()))
        id_token = response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise MicrosoftGraphOAuthError("identity_verification_failed")
        identity = self._verify_id_token(id_token, nonce_digest)
        return token, identity

    def refresh(
        self,
        *,
        tenant_id: str,
        refresh_token: str,
        requested_scopes: tuple[str, ...],
    ) -> MicrosoftGraphToken:
        """Refresh only the tenant-bound Azure DevOps resource token."""

        tenant = _canonical_uuid(tenant_id)
        if requested_scopes != (AZURE_DEVOPS_RESOURCE_SCOPE,):
            raise MicrosoftGraphOAuthError("authorization_denied")
        response = self._post_token(
            tenant,
            {
                "client_id": self.client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": f"openid {AZURE_DEVOPS_RESOURCE_SCOPE}",
            },
        )
        token = _parse_azure_devops_token(response, now=int(self._now()))
        id_token = response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise MicrosoftGraphOAuthError("identity_verification_failed")
        identity = self._verify_id_token(id_token, expected_nonce_digest=None)
        return MicrosoftGraphToken(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=token.expires_at,
            refresh_expires_at=token.refresh_expires_at,
            scopes=token.scopes,
            identity=identity,
        )


def _parse_azure_devops_token(
    value: dict[str, Any], *, now: int
) -> MicrosoftGraphToken:
    """Validate opaque ADO bearer material and token lifetime without decoding it."""

    access_token = value.get("access_token")
    refresh_token = value.get("refresh_token")
    expires_in = value.get("expires_in")
    if (
        not isinstance(access_token, str)
        or not access_token
        or len(access_token) > 16_384
        or value.get("token_type") != "Bearer"
        or (
            refresh_token is not None
            and (
                not isinstance(refresh_token, str)
                or not refresh_token
                or len(refresh_token) > 16_384
            )
        )
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or not 1 <= expires_in <= MAX_TOKEN_LIFETIME_SECONDS
    ):
        raise MicrosoftGraphOAuthError("credential_unavailable")
    return MicrosoftGraphToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=now + expires_in,
        refresh_expires_at=None,
        scopes=(AZURE_DEVOPS_RESOURCE_SCOPE,),
    )


def _validate_scope_request(scopes: tuple[str, ...]) -> None:
    if (
        not scopes
        or len(scopes) != len(set(scopes))
        or any(
            scope not in ALLOWED_GRAPH_SCOPES
            and scope not in {"openid", "profile", "offline_access"}
            for scope in scopes
        )
        or not INITIAL_GRAPH_SCOPES.issubset(scopes)
    ):
        raise MicrosoftGraphOAuthError("authorization_denied")


def _validate_scope_response(
    scope_text: str, requested_scopes: tuple[str, ...]
) -> set[str]:
    actual = set(scope_text.split())
    requested_graph = set(requested_scopes) & ALLOWED_GRAPH_SCOPES
    if (
        not actual
        or not actual <= ALLOWED_GRAPH_SCOPES
        or not requested_graph <= actual
        or any(
            "mail" in scope.lower() or "calendar" in scope.lower() for scope in actual
        )
        or "Tasks.ReadWrite.All" in actual
    ):
        raise MicrosoftGraphOAuthError("scope_rejected")
    return actual


def _canonical_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("invalid identifier")
    parsed = uuid.UUID(value)
    normalized = str(parsed)
    if normalized != value.lower():
        raise ValueError("noncanonical identifier")
    return normalized


def _decode_segment(value: str) -> bytes:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _decode_json_segment(value: str) -> dict[str, Any]:
    decoded = json.loads(
        _decode_segment(value), object_pairs_hook=_reject_duplicate_pairs
    )
    if not isinstance(decoded, dict):
        raise TypeError("invalid JWT object")
    return decoded


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result
