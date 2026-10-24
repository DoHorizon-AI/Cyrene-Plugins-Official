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

from cyrene_account_center_contracts import payload_sha256
from cyrene_plugin_runtime.configuration import read_environment_settings
from cyrene_plugin_runtime.vendor_cli import (
    VendorCliConfigurationError,
    VendorCliResult,
    VendorCliRunner,
)

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
from .vendor_cli import (
    GITHUB_CLI_EXECUTABLE,
    github_cli_is_read_only,
    validate_github_cli_argv,
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
_REPOSITORY_PROPERTIES = {
    "repository_id": _COMMON_PROPERTIES["repository_id"],
    "repository": _COMMON_PROPERTIES["repository"],
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
    {
        "provider_tool_id": "github.repository.list",
        "display_name": "List GitHub Repositories",
        "description": "List repositories within this binding's configured allowlist.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "provider_tool_id": "github.repository.get",
        "display_name": "Get GitHub Repository",
        "description": "Read metadata for one configured repository.",
        "input_schema": {
            "type": "object",
            "properties": dict(_REPOSITORY_PROPERTIES),
            "required": ["repository_id", "repository"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "github.branches.list",
        "display_name": "List GitHub Branches",
        "description": "List branch names and commit SHAs for one configured repository.",
        "input_schema": {
            "type": "object",
            "properties": dict(_REPOSITORY_PROPERTIES),
            "required": ["repository_id", "repository"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "github.pull_requests.list",
        "display_name": "List GitHub Pull Requests",
        "description": "List a bounded page of pull requests for one configured repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                **_REPOSITORY_PROPERTIES,
                "state": {"type": "string", "enum": ["OPEN", "CLOSED", "ALL"]},
            },
            "required": ["repository_id", "repository", "state"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "github.pull_request.get",
        "display_name": "Get GitHub Pull Request",
        "description": "Read one pull request from a configured repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                **_REPOSITORY_PROPERTIES,
                "pull_number": {"type": "integer", "minimum": 1},
            },
            "required": ["repository_id", "repository", "pull_number"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "github.actions.runs.list",
        "display_name": "List GitHub Actions Runs",
        "description": "Read recent workflow runs for one configured repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                **_REPOSITORY_PROPERTIES,
                "branch": {"type": "string", "minLength": 1, "maxLength": 255},
                "commit_sha": {"type": "string", "pattern": "^[0-9a-fA-F]{7,64}$"},
            },
            "required": ["repository_id", "repository"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "github.ci.status",
        "display_name": "Get GitHub CI Status",
        "description": "Read GitHub commit statuses and check runs for one configured commit.",
        "input_schema": {
            "type": "object",
            "properties": {
                **_REPOSITORY_PROPERTIES,
                "commit_sha": {"type": "string", "pattern": "^[0-9a-fA-F]{7,64}$"},
            },
            "required": ["repository_id", "repository", "commit_sha"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "github_vendor_cli",
        "display_name": "Run GitHub CLI Command",
        "description": (
            "Run an authorized GitHub CLI command with this profile's installation token. "
            "Prefer the API tools for query results; this tool returns only a bounded command "
            "receipt and suppresses all CLI output."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 128,
                    "items": {"type": "string", "maxLength": 4096},
                }
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
    },
)
_TOOL_BY_ID = {item["provider_tool_id"]: item for item in CANONICAL_TOOLS}
_OPERATION_ID_BY_TOOL = {
    "github_vendor_cli": "github.vendor.cli.execute",
}


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
        cli_runner: VendorCliRunner | None = None,
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
        self._cli_runner = cli_runner or VendorCliRunner()
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
        cancellation: Any = None,
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
                return self._call_tool(payload, cancellation=cancellation)
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

    def _call_tool(
        self,
        payload: bytes,
        *,
        cancellation: Any = None,
    ) -> tuple[bool, Any]:
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
        legacy_fields = {"payload", "lease_ref", "trusted_context"}
        profile_fields = {"payload", "profile_ref", "lease_ref", "trusted_context"}
        if not isinstance(envelope, Mapping) or frozenset(envelope) not in {
            frozenset(legacy_fields),
            frozenset(profile_fields),
        }:
            return False, "INVALID_REQUEST: trusted invocation envelope is required"
        arguments = envelope.get("payload")
        lease_ref = envelope.get("lease_ref")
        if not isinstance(arguments, Mapping) or not isinstance(lease_ref, str):
            return False, "INVALID_REQUEST: tool arguments and lease reference are required"
        profile_ref = envelope.get("profile_ref")
        if profile_ref is not None and (
            not isinstance(profile_ref, str)
            or not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                profile_ref,
            )
        ):
            return False, "INVALID_REQUEST: provider profile reference is invalid"
        if not _valid_arguments(tool_id, arguments, settings):
            return False, "INVALID_ARGUMENTS: tool arguments do not match the schema"
        try:
            context_payload = {"payload": dict(arguments), "lease_ref": lease_ref}
            if profile_ref is not None:
                context_payload["profile_ref"] = profile_ref
            actor = verify_exchange_context(settings, context_payload, envelope["trusted_context"])
            operation_id = _OPERATION_ID_BY_TOOL.get(tool_id, tool_id)
            if envelope["trusted_context"].get("operation_id") != operation_id:
                raise TrustedContextError("trusted operation does not match provider tool")
            if tool_id in {"github.repository.list", "github_vendor_cli"}:
                repository = None
            else:
                repository = settings.repository(
                    str(arguments["repository_id"]), str(arguments["repository"])
                )
            target = _github_target(tool_id, arguments, repository)
            if profile_ref is not None:
                if not re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    lease_ref,
                ):
                    raise ValueError("profile lease reference is invalid")
                lease_payload = {
                    "lease_ref": lease_ref,
                    "profile_ref": profile_ref,
                    "operation_id": operation_id,
                    "target": target,
                    "arguments_sha256": payload_sha256(dict(arguments)),
                }
                account_context = account_center_context(
                    settings,
                    actor,
                    lease_payload,
                    method="redeem_provider_credential_lease",
                )
            else:
                if (
                    tool_id
                    not in {
                        "github.issue.get",
                        "github.issue.comment.create",
                        "github.issue.state.update",
                    }
                    or repository is None
                ):
                    raise TrustedContextError("provider profile lease is required")
                issue_number = int(arguments["issue_number"])
                lease_payload = _validate_lease(
                    lease_ref, tool_id, repository, issue_number, arguments
                )
                account_context = account_center_context(settings, actor, lease_payload)
            if self._account_center is None:
                raise AccountCenterUnavailable(
                    self._account_center_error
                    or "resolved Account Center capability is unavailable"
                )
            if profile_ref is not None:
                credential = require_credential(
                    self._account_center.redeem_provider_credential_lease(
                        lease_payload, account_context
                    )
                )
            else:
                credential = require_credential(
                    self._account_center.redeem_credential_lease(lease_payload, account_context)
                )
            if tool_id == "github_vendor_cli":
                return True, _tool_response(
                    self._run_vendor_cli(
                        arguments,
                        credential.access_token,
                        trusted_context=envelope["trusted_context"],
                        cancellation=cancellation,
                    )
                )
            api = self._require_api()
            if tool_id == "github.repository.list":
                result = api.list_repositories(credential.access_token)
            elif tool_id == "github.repository.get" and repository is not None:
                result = api.get_repository(repository, credential.access_token)
            elif tool_id == "github.branches.list" and repository is not None:
                result = api.list_branches(repository, credential.access_token)
            elif tool_id == "github.pull_requests.list" and repository is not None:
                result = api.list_pull_requests(
                    repository, str(arguments["state"]), credential.access_token
                )
            elif tool_id == "github.pull_request.get" and repository is not None:
                result = api.get_pull_request(
                    repository, int(arguments["pull_number"]), credential.access_token
                )
            elif tool_id == "github.actions.runs.list" and repository is not None:
                result = api.list_workflow_runs(
                    repository,
                    credential.access_token,
                    branch=str(arguments["branch"]) if "branch" in arguments else None,
                    commit_sha=(
                        str(arguments["commit_sha"]) if "commit_sha" in arguments else None
                    ),
                )
            elif tool_id == "github.ci.status" and repository is not None:
                result = api.get_ci_status(
                    repository, str(arguments["commit_sha"]), credential.access_token
                )
            elif tool_id == "github.issue.get" and repository is not None:
                issue_number = int(arguments["issue_number"])
                result = api.get_issue(repository, issue_number, credential.access_token)
            elif tool_id == "github.issue.comment.create" and repository is not None:
                issue_number = int(arguments["issue_number"])
                result = api.create_comment(
                    repository,
                    issue_number,
                    str(arguments["body"]),
                    credential.access_token,
                )
            elif tool_id == "github.issue.state.update" and repository is not None:
                issue_number = int(arguments["issue_number"])
                result = api.update_state(
                    repository,
                    issue_number,
                    str(arguments["state"]),
                    credential.access_token,
                )
            else:
                raise ValueError("provider tool dispatch is incomplete")
            return True, _tool_response(result)
        except TrustedContextError:
            return True, _tool_error("AUTHENTICATION_DENIED")
        except AccountCenterUnavailable:
            return True, _tool_error("AUTHORIZATION_UNAVAILABLE", retryable=True)
        except FileNotFoundError:
            return True, _tool_error("CLI_UNAVAILABLE")
        except VendorCliConfigurationError:
            return True, _tool_error("CLI_REJECTED")
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

    def _run_vendor_cli(
        self,
        arguments: Mapping[str, Any],
        token: str,
        *,
        trusted_context: Mapping[str, Any],
        cancellation: Any,
    ) -> dict[str, Any]:
        """Run GitHub CLI with an installation token and return a safe receipt."""

        settings = self._require_settings()
        argv = arguments["argv"]
        try:
            validate_github_cli_argv(
                argv, tuple(repository.slug for repository in settings.repositories)
            )
        except ValueError as error:
            raise ValueError("CLI arguments are not authorized") from error
        result = self._cli_runner.run(
            GITHUB_CLI_EXECUTABLE,
            argv,
            credential_environment={"GH_TOKEN": token},
            timeout_seconds=settings.timeout_seconds,
            cancellation=cancellation,
        )
        return _vendor_cli_receipt(
            "github_vendor_cli",
            arguments,
            result,
            read_only=github_cli_is_read_only(argv),
            request_id=trusted_context.get("request_id"),
        )

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


def _valid_arguments(
    tool_id: str,
    arguments: Mapping[str, Any],
    settings: ProviderSettings,
) -> bool:
    """Enforce the static input contract before hashing or provider access."""

    schema = _TOOL_BY_ID[tool_id]["input_schema"]
    required = set(schema["required"])
    allowed = set(schema["properties"])
    supplied = set(arguments)
    if not required.issubset(supplied) or supplied - allowed:
        return False
    if tool_id == "github_vendor_cli":
        argv = arguments.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 128
            or not all(isinstance(item, str) and len(item) <= 4096 for item in argv)
        ):
            return False
        try:
            validate_github_cli_argv(
                argv,
                tuple(repository.slug for repository in settings.repositories),
            )
        except ValueError:
            return False
        return True
    if tool_id == "github.repository.list":
        return not arguments
    if not isinstance(arguments.get("repository_id"), str) or not re.fullmatch(
        r"[0-9]+", str(arguments["repository_id"])
    ):
        return False
    if not isinstance(arguments.get("repository"), str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", arguments["repository"]
    ):
        return False
    for number_field in ("issue_number", "pull_number"):
        number = arguments.get(number_field)
        if number_field in arguments and (
            isinstance(number, bool) or not isinstance(number, int) or number < 1
        ):
            return False
    if tool_id == "github.issue.comment.create":
        body = arguments.get("body")
        return isinstance(body, str) and 1 <= len(body) <= 65_536
    if tool_id == "github.issue.state.update":
        return arguments.get("state") in {"OPEN", "CLOSED"}
    if tool_id == "github.pull_requests.list":
        return arguments.get("state") in {"OPEN", "CLOSED", "ALL"}
    if "branch" in arguments and (
        not isinstance(arguments["branch"], str)
        or not re.fullmatch(r"[A-Za-z0-9._/-]{1,255}", arguments["branch"])
        or ".." in arguments["branch"].split("/")
    ):
        return False
    return not (
        "commit_sha" in arguments
        and (
            not isinstance(arguments["commit_sha"], str)
            or not re.fullmatch(r"[0-9a-fA-F]{7,64}", arguments["commit_sha"])
        )
    )


def _github_target(
    tool_id: str,
    arguments: Mapping[str, Any],
    repository: Any | None,
) -> dict[str, Any]:
    """Derive the exact provider target from validated arguments and binding data."""

    if tool_id in {"github.repository.list", "github_vendor_cli"}:
        return {}
    if repository is None:
        raise ValueError("configured repository is required")
    target: dict[str, Any] = {
        "repository_id": repository.repository_id,
        "repository": repository.slug,
    }
    if tool_id.startswith("github.issue."):
        target["issue_number"] = int(arguments["issue_number"])
    if tool_id == "github.pull_request.get":
        target["pull_number"] = int(arguments["pull_number"])
    if tool_id == "github.actions.runs.list":
        if "branch" in arguments:
            target["branch"] = str(arguments["branch"])
        if "commit_sha" in arguments:
            target["commit_sha"] = str(arguments["commit_sha"])
    if tool_id == "github.ci.status":
        target["commit_sha"] = str(arguments["commit_sha"])
    return target


def _validate_lease(
    lease_ref: str,
    operation_id: str,
    repository: Any,
    issue_number: int,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind lease metadata to the exact tool, resource, and canonical args."""

    digest = payload_sha256(dict(arguments))
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


def _vendor_cli_receipt(
    tool_id: str,
    arguments: Mapping[str, Any],
    result: VendorCliResult,
    *,
    read_only: bool,
    request_id: Any,
) -> dict[str, Any]:
    """Report command metadata and leave all remote writes unverified."""

    if result.cancelled:
        command_state = "COMMAND_CANCELLED"
    elif result.timed_out or result.exit_code is None:
        command_state = "COMMAND_INTERRUPTED"
    else:
        command_state = "COMMAND_COMPLETED" if result.exit_code == 0 else "COMMAND_FAILED"

    receipt: dict[str, Any] = {
        "state": command_state
        if read_only
        else (
            "REMOTE_STATE_UNVERIFIED" if command_state == "COMMAND_COMPLETED" else "UNKNOWN_RESULT"
        ),
        "command_state": command_state,
        "remote_state": "NOT_APPLICABLE" if read_only else "UNKNOWN_RESULT",
        "exit_code": result.exit_code,
        "argv_count": len(arguments["argv"]),
        "arguments_sha256": payload_sha256(dict(arguments)),
        "stdout_bytes": result.stdout_bytes,
        "stderr_bytes": result.stderr_bytes,
        "stdout_suppressed": True,
        "stderr_suppressed": True,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
        "cancelled": result.cancelled,
        "reconciliation_available": False,
    }
    if not read_only and isinstance(request_id, str) and request_id:
        reference = hashlib.sha256(f"{tool_id}\0{request_id}".encode()).hexdigest()[:32]
        receipt["operation_ref"] = f"ghcli_{reference}"
    return receipt


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
