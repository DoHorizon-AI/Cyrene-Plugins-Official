"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 artifact_resolver.py                                             │
│  Module: cyrene_media.artifact_resolver                             │
│  Role: Call the task-scoped Navigator artifact bridge over loopback. │
│                                                                      │
│  模块职责：通过 loopback 调用按任务绑定的 Navigator 附件桥。         │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cyrene_integration_contracts import payload_sha256

from .library import MAX_ASSET_BYTES, MediaError, _validate_image

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BRIDGE_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_MAX_JSON_BYTES = 14 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    """Bytes and verified metadata returned by the task-scoped host bridge."""

    artifact_id: str
    data: bytes
    mime_type: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    """Verified opaque reference created by the current task's Work client."""

    artifact_id: str
    sha256: str
    mime_type: str
    size: int

    def as_dict(self) -> dict[str, Any]:
        """Return metadata only; never include content or credentials."""

        return {
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "size": self.size,
        }


class WorkArtifactResolver:
    """Use one loopback bridge fixed to the current actor, task, and workspace.

    The host owns Work access tokens and source authorization. This process gets
    only a binding-scoped bridge endpoint plus its 32-byte service bearer secret.
    Request data carries an opaque artifact ID or bounded image bytes; endpoint,
    actor, workspace, and Work credentials are never model-selected.
    中文：宿主保管 Work token 与来源授权；插件只持有单任务 loopback 桥短期凭据。
    """

    def __init__(
        self,
        *,
        endpoint: str,
        bridge_token: str,
        timeout_seconds: float = 8,
    ) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not _is_loopback(parsed.hostname)
        ):
            raise ValueError("artifact bridge must be a fixed loopback HTTP origin")
        if not isinstance(bridge_token, str) or not _BRIDGE_TOKEN.fullmatch(
            bridge_token
        ):
            raise ValueError("artifact bridge token must be 32-byte lowercase hex")
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("artifact bridge timeout is outside the supported range")
        self._origin = endpoint.rstrip("/")
        self._bridge_token = bridge_token
        self._timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    def read(
        self,
        artifact_id: str,
        *,
        tool_id: str,
        arguments: Mapping[str, Any],
        caller_context: Mapping[str, Any],
        cancellation_event: Any = None,
    ) -> ResolvedArtifact:
        """Resolve one host-authorized source attachment and verify every receipt."""

        if not isinstance(artifact_id, str) or not _DIGEST.fullmatch(artifact_id):
            raise MediaError("INVALID_INPUT")
        _validate_call_context(
            tool_id,
            arguments,
            caller_context,
            allowed_tool_ids={"media.asset.import"},
        )
        if arguments.get("artifact_id") != artifact_id:
            raise MediaError("INVALID_INPUT")
        payload = self._request_json(
            "/v1/artifacts/read",
            {
                "artifactId": artifact_id,
                "toolId": tool_id,
                "arguments": dict(arguments),
                "callerContext": dict(caller_context),
            },
            cancellation_event=cancellation_event,
        )
        try:
            returned_id = payload["artifactId"]
            digest = payload["sha256"]
            mime_type = payload["mimeType"]
            size = int(payload["size"])
            encoded = payload["contentBase64"]
            if not isinstance(encoded, str) or len(encoded) > _MAX_JSON_BYTES:
                raise ValueError("attachment body exceeds its encoded size limit")
            data = base64.b64decode(encoded, validate=True)
        except (KeyError, TypeError, ValueError) as error:
            raise MediaError("PROVIDER_ERROR") from error
        if (
            returned_id != artifact_id
            or digest != artifact_id
            or mime_type not in _IMAGE_MIME_TYPES
            or size != len(data)
            or size > MAX_ASSET_BYTES
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise MediaError("INTEGRITY_FAILURE")
        detected_mime, _, _ = _validate_image(data, MAX_ASSET_BYTES)
        if detected_mime != mime_type:
            raise MediaError("UNSUPPORTED_MEDIA")
        return ResolvedArtifact(returned_id, data, mime_type, digest, size)

    def write(
        self,
        data: bytes,
        mime_type: str,
        metadata: dict[str, str],
        *,
        tool_id: str,
        arguments: Mapping[str, Any],
        caller_context: Mapping[str, Any],
        cancellation_event: Any = None,
    ) -> ArtifactReceipt:
        """Write one verified image through the current task's actor-bound client."""

        if not isinstance(data, bytes) or not data:
            raise MediaError("INVALID_INPUT")
        if len(data) > MAX_ASSET_BYTES:
            raise MediaError("QUOTA_EXCEEDED")
        normalized_mime = mime_type.strip().lower()
        if normalized_mime not in _IMAGE_MIME_TYPES:
            raise MediaError("UNSUPPORTED_MEDIA")
        detected_mime, _, _ = _validate_image(data, MAX_ASSET_BYTES)
        if detected_mime != normalized_mime:
            raise MediaError("UNSUPPORTED_MEDIA")
        if not isinstance(metadata, dict) or set(metadata) != {"name"}:
            raise MediaError("INVALID_INPUT")
        _validate_call_context(
            tool_id,
            arguments,
            caller_context,
            allowed_tool_ids={"media.asset.get", "media.card.render"},
        )
        name = metadata["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 512:
            raise MediaError("INVALID_INPUT")
        digest = hashlib.sha256(data).hexdigest()
        payload = self._request_json(
            "/v1/artifacts/write",
            {
                "mimeType": normalized_mime,
                "name": name.strip(),
                "contentBase64": base64.b64encode(data).decode("ascii"),
                "toolId": tool_id,
                "arguments": dict(arguments),
                "callerContext": dict(caller_context),
            },
            cancellation_event=cancellation_event,
        )
        try:
            returned_id = payload["artifactId"]
            returned_digest = payload["sha256"]
            returned_mime = payload["mimeType"]
            returned_size = int(payload["size"])
        except (KeyError, TypeError, ValueError) as error:
            raise MediaError("PROVIDER_ERROR") from error
        if (
            returned_id != digest
            or returned_digest != digest
            or returned_mime != normalized_mime
            or returned_size != len(data)
        ):
            raise MediaError("INTEGRITY_FAILURE")
        return ArtifactReceipt(
            returned_id,
            returned_digest,
            returned_mime,
            returned_size,
        )

    def _request_json(
        self,
        route: str,
        body: dict[str, Any],
        *,
        cancellation_event: Any,
    ) -> dict[str, Any]:
        """Call only one fixed bridge route and bound both request and response."""

        _check_cancelled(cancellation_event)
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_JSON_BYTES:
            raise MediaError("QUOTA_EXCEEDED")
        request = urllib.request.Request(
            self._origin + route,
            data=encoded,
            headers={
                "Authorization": f"Bearer {self._bridge_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                data = response.read(_MAX_JSON_BYTES + 1)
                if len(data) > _MAX_JSON_BYTES:
                    raise MediaError("QUOTA_EXCEEDED")
        except MediaError:
            raise
        except urllib.error.HTTPError as error:
            raise _http_error(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise MediaError("PROVIDER_UNAVAILABLE") from error
        _check_cancelled(cancellation_event)
        try:
            result = json.loads(data)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise MediaError("PROVIDER_ERROR") from error
        if not isinstance(result, dict):
            raise MediaError("PROVIDER_ERROR")
        return result


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent forwarding task secrets to any redirect target."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def _check_cancelled(cancellation_event: Any) -> None:
    """Reject bridge work at call boundaries when the host cancels the task."""

    if cancellation_event is None:
        return
    is_set = getattr(cancellation_event, "is_set", None)
    if (callable(is_set) and is_set()) or getattr(
        cancellation_event, "cancelled", False
    ):
        raise InterruptedError("artifact bridge operation cancelled")


def _http_error(status: int) -> MediaError:
    """Map bridge HTTP status without exposing private response details."""

    if status in {401, 403}:
        return MediaError("AUTHORIZATION_DENIED")
    if status == 404:
        return MediaError("NOT_FOUND")
    if status == 413:
        return MediaError("QUOTA_EXCEEDED")
    if status in {400, 422}:
        return MediaError("INVALID_INPUT")
    return MediaError("PROVIDER_UNAVAILABLE")


def _is_loopback(hostname: str) -> bool:
    """Allow only loopback IPs or the local host alias."""

    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_call_context(
    tool_id: str,
    arguments: Mapping[str, Any],
    caller_context: Mapping[str, Any],
    *,
    allowed_tool_ids: set[str],
) -> None:
    """Bind bridge work to the already-verified signed tool call and request ID."""

    if (
        tool_id not in allowed_tool_ids
        or caller_context.get("method") != tool_id
        or not isinstance(caller_context.get("request_id"), str)
        or not caller_context.get("request_id")
        or not isinstance(caller_context.get("subject"), str)
        or not caller_context.get("subject")
        or caller_context.get("payload_sha256") != payload_sha256(dict(arguments))
    ):
        raise MediaError("AUTHENTICATION_DENIED")
