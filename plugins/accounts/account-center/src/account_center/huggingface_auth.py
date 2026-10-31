"""Verify Hugging Face tokens against the fixed Hub identity endpoint.

Tokens are supplied by host secret references and response bodies are never
included in errors. 中文：只校验宿主密钥引用中的 HF token。
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Mapping
from typing import Any

HUGGINGFACE_HOST = "huggingface.co"
MAX_RESPONSE_BYTES = 256_000


class HuggingFaceAuthError(RuntimeError):
    """Sanitized identity or Hub transport failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HuggingFaceIdentityClient:
    """Resolve a fine-grained token to a stable Hugging Face account name."""

    def __init__(self, *, transport: Any | None = None) -> None:
        self._transport = transport

    def whoami(self, token: str) -> str:
        """Verify one bearer token without returning the token or raw response."""

        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise HuggingFaceAuthError("credential_unavailable")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "Cyrene-Account-Center",
        }
        try:
            if self._transport is not None:
                response = self._transport("GET", HUGGINGFACE_HOST, "/api/whoami-v2", headers)
            else:
                connection = http.client.HTTPSConnection(HUGGINGFACE_HOST, timeout=15)
                try:
                    connection.request("GET", "/api/whoami-v2", headers=headers)
                    reply = connection.getresponse()
                    response = type(
                        "Response",
                        (),
                        {
                            "status": reply.status,
                            "headers": {k.lower(): v for k, v in reply.getheaders()},
                            "body": reply.read(MAX_RESPONSE_BYTES + 1),
                        },
                    )()
                finally:
                    connection.close()
        except (OSError, TimeoutError, http.client.HTTPException):
            raise HuggingFaceAuthError("provider_unavailable") from None
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise HuggingFaceAuthError("invalid_provider_response")
        if response.status == 401:
            raise HuggingFaceAuthError("credential_rejected")
        if response.status == 403:
            raise HuggingFaceAuthError("provider_permission_denied")
        if response.status == 429:
            raise HuggingFaceAuthError("provider_rate_limited")
        if response.status < 200 or response.status >= 300:
            raise HuggingFaceAuthError("provider_rejected")
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HuggingFaceAuthError("invalid_provider_response") from None
        if not isinstance(value, Mapping):
            raise HuggingFaceAuthError("invalid_provider_response")
        name = value.get("name")
        if not isinstance(name, str) or not name or len(name) > 200:
            raise HuggingFaceAuthError("invalid_provider_response")
        return name
