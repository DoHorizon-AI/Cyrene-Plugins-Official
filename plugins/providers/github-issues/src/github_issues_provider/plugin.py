"""Fixed GitHub Issues tools and authenticated webhook normalization.

中文:提供固定 GitHub Issues 工具与经过认证的 Webhook 规范化。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any

from cyrene_account_center_contracts import canonical_json
from cyrene_plugin_runtime.configuration import read_environment_settings

from ._generated import tool_provider_pb2 as wire
from .account_center import (
    AccountCenterDirectLeaseClient,
    AccountCenterLeaseClient,
    AccountCenterUnavailable,
    require_credential,
)
from .github_api import GitHubApi, GitHubApiFailure
from .settings import ConfigurationError, ProviderSettings
from .trust import (
    TrustedContextError,
    account_center_context,
    verify_exchange_context,
)

CAPABILITY_ID = "tool.provider.v1"
INTERFACE_VERSION = "1"
PLUGIN_ID = "cyrene.providers.github-issues"
PLUGIN_VERSION = "0.1.0"
LIST_TOOLS_METHOD = "list_tools"
CALL_TOOL_METHOD = "call_tool"
WEBHOOK_METHOD = "normalize_webhook"
LIST_TOOLS_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest"
LIST_TOOLS_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse"
CALL_TOOL_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest"
CALL_TOOL_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse"
WEBHOOK_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.github.webhook.v1.WebhookRequest"
WEBHOOK_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.github.webhook.v1.WebhookResponse"
_WEBHOOK_ACTIONS = {
    "issues": frozenset({"opened", "edited", "closed", "reopened"}),
    "issue_comment": frozenset({"created", "edited", "deleted"}),
}

_COMMON_PROPERTIES = {
    "repository_id": {
        "type": "string",
        "pattern": "^[0-9]+$",
        "description": "Stable numeric GitHub repository ID from the binding allowlist.",
    },
    "repository": {
        "type": "string",
        "pattern": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        "description": "Configured owner/name slug; it is not an arbitrary API URL.",
    },
    "issue_number": {"type": "integer", "minimum": 1},
}

CANONICAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "provider_tool_id": "github.issue.get",
        "display_name": "Get GitHub Issue",
        "description": "Read one issue from a repository configured on this binding.",
        "input_schema": {
            "type": "object",
            "properties": dict(_COMMON_PROPERTIES),
            "required": ["repository_id", "repository", "issue_number"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "github.issue.comment.create",
        "display_name": "Create GitHub Issue Comment",
        "description": "Create one comment using the linked user's GitHub authorization.",
        "input_schema": {
            "type": "object",
            "properties": {
                **_COMMON_PROPERTIES,
                "body": {"type": "string", "minLength": 1, "maxLength": 65536},
            },
            "required": ["repository_id", "repository", "issue_number", "body"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "github.issue.state.update",
        "display_name": "Update GitHub Issue State",
        "description": "Set a configured repository issue to OPEN or CLOSED.",
        "input_schema": {
            "type": "object",
            "properties": {
                **_COMMON_PROPERTIES,
                "state": {"type": "string", "enum": ["OPEN", "CLOSED"]},
            },
            "required": ["repository_id", "repository", "issue_number", "state"],
            "additionalProperties": False,
        },
    },
)
_TOOL_BY_ID = {item["provider_tool_id"]: item for item in CANONICAL_TOOLS}


class GitHubIssuesProvider:
    """Serve fixed GitHub tools through the canonical direct runtime contract."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: ProviderSettings | None = None,
        account_center: AccountCenterLeaseClient | None = None,
        github_api: GitHubApi | None = None,
    ) -> None:
        self._settings = settings
        loaded_from_environment = False
        if self._settings is None:
            environment_settings = read_environment_settings()
            if environment_settings is None:
                raise ConfigurationError("provider activation configuration is required")
            self._settings = ProviderSettings.from_environment()
            loaded_from_environment = True
        self._account_center = account_center
        self._github_api = github_api
        self._account_center_error: str | None = None
        if self._account_center is None and loaded_from_environment:
            try:
                self._account_center = AccountCenterDirectLeaseClient(self._settings)
            except AccountCenterUnavailable:
                raise ConfigurationError(
                    "resolved Account Center connection is unavailable"
                ) from None

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        **_: Any,
    ) -> tuple[bool, Any]:
        """Dispatch catalog, user tool, or internal signed webhook requests."""

        if capability != CAPABILITY_ID:
            return False, "INVALID_REQUEST: unsupported capability"
        try:
            if action == LIST_TOOLS_METHOD:
                if request_type_url not in (None, LIST_TOOLS_REQUEST_TYPE_URL):
                    return False, "INVALID_REQUEST: catalog payload type mismatch"
                return self._list_tools(payload)
            if action == CALL_TOOL_METHOD:
                if request_type_url not in (None, CALL_TOOL_REQUEST_TYPE_URL):
                    return False, "INVALID_REQUEST: tool payload type mismatch"
                return self._call_tool(payload)
            if action == WEBHOOK_METHOD:
                if request_type_url not in (None, WEBHOOK_REQUEST_TYPE_URL):
                    return False, "INVALID_REQUEST: webhook payload type mismatch"
                return self._normalize_webhook(payload)
            return False, "METHOD_NOT_FOUND: unsupported provider method"
        except ConfigurationError:
            return False, "INVALID_CONFIGURATION: provider binding is unavailable"

    def _list_tools(self, payload: bytes) -> tuple[bool, Any]:
        request = wire.ListToolsRequest()
        try:
            request.ParseFromString(payload)
        except Exception:
            return False, "INVALID_REQUEST: catalog request is malformed"
        response = wire.ListToolsResponse()
        catalog = response.catalog
        catalog.catalog_version = PLUGIN_VERSION
        binding_id = self._require_settings().binding_id
        if request.HasField("binding_id") and request.binding_id != binding_id:
            return False, "INVALID_REQUEST: binding ID does not match"
        for definition in CANONICAL_TOOLS:
            descriptor = catalog.tools.add()
            descriptor.binding_id = binding_id
            descriptor.provider_tool_id = str(definition["provider_tool_id"])
            descriptor.display_name = str(definition["display_name"])
            descriptor.description = str(definition["description"])
            descriptor.input_schema_json = json.dumps(
                definition["input_schema"], ensure_ascii=False, separators=(",", ":")
            )
        return True, _TypedPayload(response.SerializeToString(), LIST_TOOLS_RESPONSE_TYPE_URL)

    def _call_tool(self, payload: bytes) -> tuple[bool, Any]:
        request = wire.CallToolRequest()
        try:
            request.ParseFromString(payload)
            envelope = _parse_json(request.arguments_json)
        except (ValueError, TypeError):
            return False, "INVALID_REQUEST: tool request is malformed"
        settings = self._require_settings()
        if request.binding_id != settings.binding_id:
            return False, "INVALID_REQUEST: binding ID does not match"
        tool_id = request.provider_tool_id
        if tool_id not in _TOOL_BY_ID:
            return False, "METHOD_NOT_FOUND: provider tool is not available"
        if not isinstance(envelope, Mapping) or set(envelope) != {
            "payload",
            "lease_ref",
            "trusted_context",
        }:
            return False, "INVALID_REQUEST: trusted invocation envelope is required"
        arguments = envelope.get("payload")
        lease_ref = envelope.get("lease_ref")
        if not isinstance(arguments, Mapping) or not isinstance(lease_ref, str):
            return False, "INVALID_REQUEST: tool arguments and lease reference are required"
        if not _valid_arguments(tool_id, arguments):
            return False, "INVALID_ARGUMENTS: tool arguments do not match the schema"
        try:
            context_payload = {"payload": dict(arguments), "lease_ref": lease_ref}
            actor = verify_exchange_context(settings, context_payload, envelope["trusted_context"])
            if envelope["trusted_context"].get("operation_id") != tool_id:
                raise TrustedContextError("trusted operation does not match provider tool")
            repository = settings.repository(
                str(arguments["repository_id"]), str(arguments["repository"])
            )
            issue_number = int(arguments["issue_number"])
            lease_payload = _validate_lease(lease_ref, tool_id, repository, issue_number, arguments)
            account_context = account_center_context(settings, actor, lease_payload)
            if self._account_center is None:
                raise AccountCenterUnavailable(
                    self._account_center_error
                    or "resolved Account Center capability is unavailable"
                )
            credential = require_credential(
                self._account_center.redeem_credential_lease(lease_payload, account_context)
            )
            api = self._require_api()
            if tool_id == "github.issue.get":
                result = api.get_issue(repository, issue_number, credential.access_token)
            elif tool_id == "github.issue.comment.create":
                result = api.create_comment(
                    repository,
                    issue_number,
                    str(arguments["body"]),
                    credential.access_token,
                )
            else:
                result = api.update_state(
                    repository,
                    issue_number,
                    str(arguments["state"]),
                    credential.access_token,
                )
            return True, _tool_response(result)
        except TrustedContextError:
            return True, _tool_error("AUTHENTICATION_DENIED")
        except AccountCenterUnavailable:
            return True, _tool_error("AUTHORIZATION_UNAVAILABLE", retryable=True)
        except (GitHubApiFailure, ValueError) as error:
            if isinstance(error, GitHubApiFailure):
                return True, _tool_error(
                    error.code,
                    retryable=error.retryable,
                    retry_after=error.retry_after,
                    uncertain=error.uncertain,
                )
            return True, _tool_error("INVALID_ARGUMENTS")
        except Exception:
            # Internal adapters may contain credential or transport details.
            # Keep them out of protobuf errors, tool results, and logs.
            return True, _tool_error(
                "PROVIDER_FAILURE",
                uncertain=tool_id != "github.issue.get",
            )

    def _normalize_webhook(self, payload: bytes) -> tuple[bool, Any]:
        """Authenticate original raw bytes before decoding or normalizing JSON."""

        settings = self._require_settings()
        try:
            request = _parse_json(payload)
        except (ValueError, TypeError):
            return True, _webhook_error("INVALID_REQUEST")
        if not isinstance(request, Mapping) or set(request) != {
            "delivery_id",
            "event",
            "signature",
            "raw_body_base64",
        }:
            return True, _webhook_error("INVALID_REQUEST")
        delivery_id = request.get("delivery_id")
        event = request.get("event")
        signature = request.get("signature")
        encoded = request.get("raw_body_base64")
        if (
            not isinstance(delivery_id, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", delivery_id)
            or not isinstance(event, str)
            or event not in settings.allowed_events
            or not isinstance(signature, str)
            or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature)
            or not isinstance(encoded, str)
        ):
            return True, _webhook_error("INVALID_REQUEST")
        try:
            raw_body = base64.b64decode(encoded, validate=True)
        except ValueError:
            return True, _webhook_error("INVALID_REQUEST")
        if len(raw_body) > 1_000_000:
            return True, _webhook_error("INVALID_REQUEST")
        expected = (
            "sha256="
            + hmac.new(settings.github_webhook_secret, raw_body, hashlib.sha256).hexdigest()
        )
        if not hmac.compare_digest(signature, expected):
            return True, _webhook_error("INVALID_SIGNATURE")
        try:
            event_body = _parse_json(raw_body)
        except (ValueError, TypeError):
            return True, _webhook_error("INVALID_EVENT_BODY")
        if not isinstance(event_body, Mapping):
            return True, _webhook_error("INVALID_EVENT_BODY")
        installation = event_body.get("installation")
        repository_data = event_body.get("repository")
        issue = event_body.get("issue")
        sender = event_body.get("sender")
        action = event_body.get("action")
        installation_id = installation.get("id") if isinstance(installation, Mapping) else None
        repository_id = repository_data.get("id") if isinstance(repository_data, Mapping) else None
        full_name = (
            repository_data.get("full_name") if isinstance(repository_data, Mapping) else None
        )
        sender_id = sender.get("id") if isinstance(sender, Mapping) else None
        sender_login = sender.get("login") if isinstance(sender, Mapping) else None
        if (
            str(installation_id) not in settings.allowed_installation_ids
            or not isinstance(action, str)
            or action not in settings.allowed_actions
            or action not in _WEBHOOK_ACTIONS.get(event, frozenset())
            or not isinstance(repository_id, int)
            or isinstance(repository_id, bool)
            or not isinstance(full_name, str)
            or not isinstance(issue, Mapping)
            or not isinstance(sender_id, int)
            or isinstance(sender_id, bool)
            or sender_id < 1
            or not isinstance(sender_login, str)
            or not sender_login
            or len(sender_login) > 200
        ):
            return True, _webhook_error("EVENT_NOT_ALLOWED")
        try:
            repository = settings.repository(str(repository_id), full_name)
        except ValueError:
            return True, _webhook_error("REPOSITORY_NOT_ALLOWED")
        issue_number = issue.get("number")
        issue_id = issue.get("id")
        title = issue.get("title")
        comment = event_body.get("comment")
        event_text = (
            comment.get("body")
            if event == "issue_comment" and isinstance(comment, Mapping)
            else issue.get("body")
        )
        if event_text is None:
            event_text = ""
        if (
            isinstance(issue_number, bool)
            or not isinstance(issue_number, int)
            or issue_number < 1
            or not isinstance(issue_id, int)
            or isinstance(issue_id, bool)
            or not isinstance(title, str)
            or len(title) > 500
            or not isinstance(event_text, str)
            or len(event_text) > 4000
            or (event == "issue_comment" and not isinstance(comment, Mapping))
        ):
            return True, _webhook_error("INVALID_EVENT_BODY")
        normalized = {
            "event_type": event,
            "action": action,
            "repository_id": str(repository.repository_id),
            "issue_number": issue_number,
            "sender_id": str(sender_id),
            "sender_login": sender_login,
            "title": title,
            "body": event_text,
            # Webhooks carry no actor binding, so the provider cannot attest self-authorship.
            "self_authored": False,
        }
        return True, _webhook_response(normalized)

    def _require_settings(self) -> ProviderSettings:
        if self._settings is None:
            self._settings = ProviderSettings.from_environment()
        return self._settings

    def _require_api(self) -> GitHubApi:
        if self._github_api is None:
            self._github_api = GitHubApi(self._require_settings())
        return self._github_api

    def on_shutdown(self, timeout_ms: int) -> None:
        """Close the private Account Center direct channel during runtime stop."""

        del timeout_ms
        close = getattr(self._account_center, "close", None)
        if callable(close):
            close()


class _TypedPayload:
    """Byte payload wrapper consumed by DirectPluginRuntime."""

    def __init__(self, value: bytes, type_url: str) -> None:
        self.value = value
        self.type_url = type_url


def _parse_json(data: bytes | str) -> Any:
    """Parse JSON while rejecting duplicate keys before trust decisions."""

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON keys are not allowed")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=object_pairs)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("invalid JSON") from error


def _valid_arguments(tool_id: str, arguments: Mapping[str, Any]) -> bool:
    """Enforce the static input contract before hashing or provider access."""

    common = {"repository_id", "repository", "issue_number"}
    required = set(_TOOL_BY_ID[tool_id]["input_schema"]["required"])
    if set(arguments) != required:
        return False
    if not isinstance(arguments.get("repository_id"), str) or not re.fullmatch(
        r"[0-9]+", arguments["repository_id"]
    ):
        return False
    if not isinstance(arguments.get("repository"), str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", arguments["repository"]
    ):
        return False
    issue_number = arguments.get("issue_number")
    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number < 1:
        return False
    if not common.issubset(arguments):
        return False
    if tool_id == "github.issue.comment.create":
        body = arguments.get("body")
        return isinstance(body, str) and 1 <= len(body) <= 65_536
    if tool_id == "github.issue.state.update":
        return arguments.get("state") in {"OPEN", "CLOSED"}
    return True


def _validate_lease(
    lease_ref: str,
    operation_id: str,
    repository: Any,
    issue_number: int,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind lease metadata to the exact tool, resource, and canonical args."""

    digest = hashlib.sha256(canonical_json(dict(arguments))).hexdigest()
    expected_resource = {
        "repository_id": repository.repository_id,
        "repository": repository.slug,
        "issue_number": issue_number,
    }
    if not re.fullmatch(r"[0-9a-f-]{36}", lease_ref):
        raise ValueError("lease reference is invalid")
    return {
        "lease_ref": lease_ref,
        "operation_id": operation_id,
        "resource": expected_resource,
        "arguments_sha256": digest,
    }


def _tool_response(result: Mapping[str, Any]) -> _TypedPayload:
    """Encode one allowlisted result as canonical ToolJsonContent."""

    response = wire.CallToolResponse()
    response.outcome.is_error = False
    response.outcome.content.add().json.json = json.dumps(
        result, ensure_ascii=False, separators=(",", ":")
    )
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


def _tool_error(
    code: str,
    *,
    retryable: bool = False,
    retry_after: int | None = None,
    uncertain: bool = False,
) -> _TypedPayload:
    """Return a bounded machine-readable failure with no vendor text or token."""

    error: dict[str, Any] = {"code": code, "retryable": retryable}
    if retry_after is not None:
        error["retry_after_seconds"] = retry_after
    if uncertain:
        error["outcome"] = "UNKNOWN"
    response = wire.CallToolResponse()
    response.outcome.is_error = True
    response.outcome.content.add().json.json = json.dumps({"error": error}, separators=(",", ":"))
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


def _webhook_error(code: str) -> _TypedPayload:
    response = _json_webhook_response({"error": {"code": code}})
    return response


def _webhook_response(value: Mapping[str, Any]) -> _TypedPayload:
    return _json_webhook_response(dict(value))


def _json_webhook_response(value: Mapping[str, Any]) -> _TypedPayload:
    return _TypedPayload(
        json.dumps(value, separators=(",", ":")).encode("utf-8"),
        WEBHOOK_RESPONSE_TYPE_URL,
    )
