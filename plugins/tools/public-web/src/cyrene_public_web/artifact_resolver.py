"""Read task-authorized attachment bytes through the Navigator loopback bridge."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from cyrene_integration_contracts import payload_sha256

_ARTIFACT_ID = re.compile(r"[0-9a-f]{64}\Z")
_BRIDGE_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_MAX_BRIDGE_JSON = 14 * 1024 * 1024
_DOCUMENT_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
        "text/html",
        "text/markdown",
        "text/plain",
        "image/png",
        "image/jpeg",
    }
)


class ArtifactReadError(RuntimeError):
    """The current task cannot read a source attachment through its host bridge."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    """Attachment bytes and host-verified immutable digest metadata."""

    artifact_id: str
    data: bytes
    mime_type: str
    sha256: str
    size: int


class WorkArtifactResolver:
    """Use a binding bridge; the host holds actor tokens and checks source ACLs."""

    def __init__(
        self,
        *,
        endpoint: str,
        bridge_token: str,
        timeout_seconds: float = 8,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            raise ValueError("artifact bridge must be a fixed loopback HTTP origin")
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            if parsed.hostname != "localhost":
                raise ValueError("artifact bridge host must be loopback") from None
        if not isinstance(bridge_token, str) or _BRIDGE_TOKEN.fullmatch(bridge_token) is None:
            raise ValueError("artifact bridge token must be 32-byte lowercase hex")
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("artifact bridge timeout is outside its supported range")
        self._origin = endpoint.rstrip("/")
        self._token = bridge_token
        self._timeout = timeout_seconds
        self._client = httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds, connect=min(5, timeout_seconds)),
        )

    def close(self) -> None:
        self._client.close()

    def read(
        self,
        artifact_id: str,
        *,
        tool_id: str,
        arguments: Mapping[str, Any],
        caller_context: Mapping[str, Any],
        cancellation: Any = None,
    ) -> ResolvedArtifact:
        if not isinstance(artifact_id, str) or _ARTIFACT_ID.fullmatch(artifact_id) is None:
            raise ArtifactReadError("INVALID_INPUT")
        if tool_id != "document.read" or arguments.get("artifact_id") != artifact_id:
            raise ArtifactReadError("INVALID_INPUT")
        if (
            caller_context.get("method") != tool_id
            or caller_context.get("payload_sha256") != payload_sha256(dict(arguments))
            or not isinstance(caller_context.get("request_id"), str)
            or not isinstance(caller_context.get("subject"), str)
        ):
            raise ArtifactReadError("AUTHENTICATION_DENIED")
        _check_cancelled(cancellation)
        try:
            with self._client.stream(
                "POST",
                self._origin + "/v1/artifacts/read",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "artifactId": artifact_id,
                    "toolId": tool_id,
                    "arguments": dict(arguments),
                    "callerContext": dict(caller_context),
                },
                timeout=self._timeout,
            ) as response:
                if response.is_redirect:
                    raise ArtifactReadError("REDIRECT_REJECTED")
                if response.status_code != 200:
                    raise ArtifactReadError("ARTIFACT_UNAVAILABLE")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_BRIDGE_JSON:
                        raise ArtifactReadError("ARTIFACT_RESPONSE_LIMIT_EXCEEDED")
                body_bytes = bytes(body)
        except httpx.TimeoutException:
            raise ArtifactReadError("REQUEST_TIMEOUT") from None
        except httpx.HTTPError:
            raise ArtifactReadError("ARTIFACT_BRIDGE_UNAVAILABLE") from None
        _check_cancelled(cancellation)
        try:
            receipt = json.loads(body_bytes, object_pairs_hook=_unique_object)
            if not isinstance(receipt, Mapping) or set(receipt) != {
                "artifactId",
                "sha256",
                "mimeType",
                "size",
                "contentBase64",
            }:
                raise ValueError("invalid bridge receipt")
            encoded = receipt["contentBase64"]
            if not isinstance(encoded, str) or len(encoded) > _MAX_BRIDGE_JSON:
                raise ValueError("attachment body exceeds bound")
            data = base64.b64decode(encoded, validate=True)
            size = receipt["size"]
            digest = receipt["sha256"]
            mime = receipt["mimeType"]
        except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
            raise ArtifactReadError("ARTIFACT_RESPONSE_INVALID") from None
        if (
            receipt["artifactId"] != artifact_id
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size != len(data)
            or size > _MAX_ATTACHMENT_BYTES
            or not isinstance(digest, str)
            or _ARTIFACT_ID.fullmatch(digest) is None
            or digest != artifact_id
            or hashlib.sha256(data).hexdigest() != digest
            or mime not in _DOCUMENT_MEDIA_TYPES
        ):
            raise ArtifactReadError("INTEGRITY_FAILURE")
        return ResolvedArtifact(artifact_id, data, mime, digest, size)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


def _check_cancelled(cancellation: Any) -> None:
    if cancellation is None:
        return
    is_set = getattr(cancellation, "is_set", None)
    if (callable(is_set) and is_set()) or getattr(cancellation, "cancelled", False):
        raise InterruptedError("document operation cancelled")
