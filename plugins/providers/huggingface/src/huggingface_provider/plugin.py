"""Fixed Hugging Face Hub tools with Account Center profile leases.

Tool arguments never contain bearer credentials. 中文：令牌只在私有租约兑换后留在内存。
"""

from __future__ import annotations

import base64
import binascii
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
)
from .huggingface_api import HuggingFaceApi, HuggingFaceApiFailure
from .settings import ConfigurationError, ProviderSettings, repository_scope_sha256
from .trust import TrustedContextError, account_center_context, verify_exchange_context
from .vendor_cli import HUGGINGFACE_CLI_EXECUTABLE, validate_huggingface_cli_argv

CAPABILITY_ID = "tool.provider.v1"
PLUGIN_ID = "cyrene.providers.huggingface"
PLUGIN_VERSION = "0.1.0"
LIST_TOOLS_METHOD = "list_tools"
CALL_TOOL_METHOD = "call_tool"
LIST_TOOLS_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest"
LIST_TOOLS_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse"
CALL_TOOL_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest"
CALL_TOOL_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse"
MAX_CONTENT_BASE64_CHARS = 1_398_104

_REPO_PROPERTIES = {
    "repo_type": {"type": "string", "enum": ["model", "dataset", "space"]},
    "repo_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"},
}
_FILE_PROPERTIES = {
    **_REPO_PROPERTIES,
    "path": {"type": "string", "minLength": 1, "maxLength": 1024},
    "revision": {"type": "string", "minLength": 1, "maxLength": 128},
}

CANONICAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "provider_tool_id": "huggingface.models.list",
        "display_name": "List Hugging Face Models",
        "description": "Search public models on the Hugging Face Hub.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "huggingface.datasets.list",
        "display_name": "List Hugging Face Datasets",
        "description": "Search public datasets on the Hugging Face Hub.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "huggingface.spaces.list",
        "display_name": "List Hugging Face Spaces",
        "description": "Search public Spaces on the Hugging Face Hub.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "huggingface.repo.get",
        "display_name": "Get Hugging Face Repository",
        "description": (
            "Read metadata for one configured model, dataset, or Space. When the Hub returns "
            "a siblings file list, the result includes its file_count; an unavailable count "
            "is unknown, not zero."
        ),
        "input_schema": {
            "type": "object",
            "properties": dict(_REPO_PROPERTIES),
            "required": ["repo_type", "repo_id"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "huggingface.file.download",
        "display_name": "Download Hugging Face File",
        "description": "Download one small file from a configured Hub repository.",
        "input_schema": {
            "type": "object",
            "properties": dict(_FILE_PROPERTIES),
            "required": ["repo_type", "repo_id", "path"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "huggingface.file.upload",
        "display_name": "Upload Hugging Face File",
        "description": "Upload and verify one file up to one mebibyte.",
        "input_schema": {
            "type": "object",
            "properties": {
                **_FILE_PROPERTIES,
                "content_base64": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_CONTENT_BASE64_CHARS,
                },
            },
            "required": ["repo_type", "repo_id", "path", "content_base64"],
            "additionalProperties": False,
        },
    },
    {
        "provider_tool_id": "hf_vendor_cli",
        "display_name": "Run Hugging Face CLI Command",
        "description": (
            "Run an authorized Hugging Face CLI command with this profile's fine-grained token. "
            "Target one configured model, dataset, or Space; unscoped search, Jobs, inference, "
            "and bucket commands are unsupported. Prefer the API tools for query results; this "
            "tool returns only a bounded command receipt and suppresses all CLI output."
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
_TOOLS_BY_ID = {item["provider_tool_id"]: item for item in CANONICAL_TOOLS}
_LIST_OPERATION_TYPE = {
    "huggingface.models.list": "model",
    "huggingface.datasets.list": "dataset",
    "huggingface.spaces.list": "space",
}
_READ_ONLY_TOOL_IDS = frozenset(
    {
        "huggingface.models.list",
        "huggingface.datasets.list",
        "huggingface.spaces.list",
        "huggingface.repo.get",
        "huggingface.file.download",
    }
)
_OPERATION_ID_BY_TOOL = {
    "hf_vendor_cli": "huggingface.vendor.cli.execute",
}


class ProfileScopeMismatch(ValueError):
    """Account Center and host repository scopes do not match exactly."""


class HuggingFaceProvider:
    """Serve fixed Hub tools and redeem tokens only through Account Center."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: ProviderSettings | None = None,
        account_center: AccountCenterLeaseClient | None = None,
        hub_api: HuggingFaceApi | None = None,
        cli_runner: VendorCliRunner | None = None,
    ) -> None:
        self._settings = settings
        loaded_from_environment = False
        if settings is None:
            environment = read_environment_settings()
            if environment is None:
                raise ConfigurationError("provider activation configuration is required")
            self._settings = ProviderSettings.from_environment()
            loaded_from_environment = True
        self._account_center = account_center
        self._hub_api = hub_api
        self._cli_runner = cli_runner or VendorCliRunner()
        if self._account_center is None and loaded_from_environment:
            try:
                self._account_center = AccountCenterDirectLeaseClient(self._require_settings())
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
        """Dispatch provider catalog or one signed Hub tool invocation."""

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
            return False, "METHOD_NOT_FOUND: unsupported provider method"
        except ConfigurationError:
            return False, "INVALID_CONFIGURATION: provider binding is unavailable"

    def _list_tools(self, payload: bytes) -> tuple[bool, Any]:
        """Return the fixed catalog with closed JSON input schemas."""

        request = wire.ListToolsRequest()
        try:
            request.ParseFromString(payload)
        except Exception:
            return False, "INVALID_REQUEST: catalog request is malformed"
        settings = self._require_settings()
        if request.HasField("binding_id") and request.binding_id != settings.binding_id:
            return False, "INVALID_REQUEST: binding ID does not match"
        response = wire.ListToolsResponse()
        response.catalog.catalog_version = PLUGIN_VERSION
        for definition in CANONICAL_TOOLS:
            if settings.read_only and definition["provider_tool_id"] not in _READ_ONLY_TOOL_IDS:
                continue
            descriptor = response.catalog.tools.add()
            descriptor.binding_id = settings.binding_id
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
        """Validate signed actor/target claims, redeem once, then call the fixed API."""

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
        if tool_id not in _TOOLS_BY_ID:
            return False, "METHOD_NOT_FOUND: provider tool is not available"
        if not isinstance(envelope, Mapping) or set(envelope) != {
            "payload",
            "profile_ref",
            "lease_ref",
            "trusted_context",
        }:
            return False, "INVALID_REQUEST: signed profile lease is required"
        arguments = envelope.get("payload")
        profile_ref = envelope.get("profile_ref")
        lease_ref = envelope.get("lease_ref")
        if (
            not isinstance(arguments, Mapping)
            or not _uuid_text(profile_ref)
            or not _uuid_text(lease_ref)
        ):
            return False, "INVALID_ARGUMENTS: tool arguments do not match the schema"
        try:
            signed_payload = {
                "payload": dict(arguments),
                "profile_ref": profile_ref,
                "lease_ref": lease_ref,
            }
            actor = verify_exchange_context(settings, signed_payload, envelope["trusted_context"])
            operation_id = _OPERATION_ID_BY_TOOL.get(tool_id, tool_id)
            if envelope["trusted_context"].get("operation_id") != operation_id:
                raise TrustedContextError("trusted operation does not match the selected tool")
            if settings.read_only and tool_id not in _READ_ONLY_TOOL_IDS:
                return True, _tool_error("READ_ONLY_PROFILE")
            if not _valid_arguments(tool_id, arguments, settings):
                return False, "INVALID_ARGUMENTS: tool arguments do not match the schema"
            target = _target(tool_id, arguments)
            lease_payload = {
                "lease_ref": lease_ref,
                "profile_ref": profile_ref,
                "operation_id": operation_id,
                "target": target,
                "arguments_sha256": payload_sha256(dict(arguments)),
            }
            if self._account_center is None:
                raise AccountCenterUnavailable("Account Center lease channel is unavailable")
            credential = self._account_center.redeem_provider_credential_lease(
                lease_payload,
                account_center_context(settings, actor, lease_payload),
            )
            if not isinstance(credential.access_token, str) or not credential.access_token:
                raise AccountCenterUnavailable("Account Center returned invalid auth material")
            if tool_id == "hf_vendor_cli":
                scope_digest = getattr(credential, "profile_scope_sha256", None)
                expected_scope_digest = repository_scope_sha256(settings.repositories)
                if (
                    not isinstance(scope_digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", scope_digest) is None
                    or not hmac.compare_digest(scope_digest, expected_scope_digest)
                ):
                    raise ProfileScopeMismatch("profile scope digest does not match host binding")
                return True, _tool_response(
                    self._run_vendor_cli(
                        arguments,
                        credential.access_token,
                        trusted_context=envelope["trusted_context"],
                        cancellation=cancellation,
                    )
                )
            api = self._require_api()
            if tool_id in _LIST_OPERATION_TYPE:
                result = api.list_repositories(
                    _LIST_OPERATION_TYPE[tool_id],
                    credential.access_token,
                    search=str(arguments["search"]) if "search" in arguments else None,
                    limit=int(arguments.get("limit", 20)),
                )
            elif tool_id == "huggingface.repo.get":
                result = api.get_repository(
                    str(arguments["repo_type"]),
                    str(arguments["repo_id"]),
                    credential.access_token,
                )
            elif tool_id == "huggingface.file.download":
                result = api.download_file(
                    str(arguments["repo_id"]),
                    str(arguments.get("revision", "main")),
                    str(arguments["path"]),
                    credential.access_token,
                )
            else:
                content = _decode_content(
                    str(arguments["content_base64"]), settings.max_artifact_bytes
                )
                result = api.upload_file(
                    str(arguments["repo_type"]),
                    str(arguments["repo_id"]),
                    str(arguments.get("revision", "main")),
                    str(arguments["path"]),
                    content,
                    credential.access_token,
                )
            return True, _tool_response(result)
        except TrustedContextError:
            return True, _tool_error("AUTHENTICATION_DENIED")
        except AccountCenterUnavailable:
            return True, _tool_error("AUTHORIZATION_UNAVAILABLE", retryable=True)
        except ProfileScopeMismatch:
            return True, _tool_error("PROFILE_SCOPE_MISMATCH")
        except FileNotFoundError:
            return True, _tool_error("CLI_UNAVAILABLE")
        except VendorCliConfigurationError:
            return True, _tool_error("CLI_REJECTED")
        except (HuggingFaceApiFailure, ValueError) as error:
            if isinstance(error, HuggingFaceApiFailure):
                return True, _tool_error(
                    error.code,
                    retryable=error.retryable,
                    retry_after=error.retry_after,
                    uncertain=error.uncertain,
                )
            return True, _tool_error("INVALID_ARGUMENTS")
        except Exception:
            return True, _tool_error(
                "PROVIDER_FAILURE", uncertain=tool_id == "huggingface.file.upload"
            )

    def _require_settings(self) -> ProviderSettings:
        """Return the resolved provider settings or reload standard activation."""

        if self._settings is None:
            self._settings = ProviderSettings.from_environment()
        return self._settings

    def _require_api(self) -> HuggingFaceApi:
        """Construct the fixed-host API client on first use."""

        if self._hub_api is None:
            self._hub_api = HuggingFaceApi(self._require_settings())
        return self._hub_api

    def _run_vendor_cli(
        self,
        arguments: Mapping[str, Any],
        token: str,
        *,
        trusted_context: Mapping[str, Any],
        cancellation: Any,
    ) -> dict[str, Any]:
        """Run Hugging Face CLI with a profile token and return only metadata."""

        argv = arguments["argv"]
        try:
            read_only = validate_huggingface_cli_argv(argv, self._require_settings().repositories)
        except ValueError as error:
            raise ValueError("CLI arguments are not authorized") from error
        result = self._cli_runner.run(
            HUGGINGFACE_CLI_EXECUTABLE,
            argv,
            credential_environment={"HF_TOKEN": token},
            timeout_seconds=self._require_settings().timeout_seconds,
            cancellation=cancellation,
        )
        return _vendor_cli_receipt(
            "hf_vendor_cli",
            arguments,
            result,
            read_only=read_only,
            request_id=trusted_context.get("request_id"),
        )

    def on_shutdown(self, timeout_ms: int) -> None:
        """Close the private Account Center channel during runtime shutdown."""

        del timeout_ms
        close = getattr(self._account_center, "close", None)
        if callable(close):
            close()


def _valid_arguments(
    tool_id: str,
    arguments: Mapping[str, Any],
    settings: ProviderSettings,
) -> bool:
    """Enforce tool schema, static target allowlist, and artifact size limits."""

    schema = _TOOLS_BY_ID[tool_id]["input_schema"]
    fields = set(schema["properties"])
    required = set(schema.get("required", []))
    if not required.issubset(arguments) or set(arguments) - fields:
        return False
    if tool_id == "hf_vendor_cli":
        argv = arguments.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 128
            or not all(isinstance(item, str) and len(item) <= 4096 for item in argv)
        ):
            return False
        try:
            validate_huggingface_cli_argv(argv, settings.repositories)
        except ValueError:
            return False
        return True
    if tool_id in _LIST_OPERATION_TYPE:
        search = arguments.get("search")
        limit = arguments.get("limit", 20)
        return (
            (search is None or isinstance(search, str) and len(search) <= 200)
            and isinstance(limit, int)
            and not isinstance(limit, bool)
            and 1 <= limit <= 100
            and bool(settings.repositories_for_type(_LIST_OPERATION_TYPE[tool_id]))
        )
    repo_type = arguments.get("repo_type")
    repo_id = arguments.get("repo_id")
    if repo_type not in {"model", "dataset", "space"} or not isinstance(repo_id, str):
        return False
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo_id):
        return False
    permission = "write" if tool_id == "huggingface.file.upload" else "read"
    try:
        settings.repository(str(repo_type), repo_id, permission)
    except ValueError:
        return False
    if "path" in arguments and not _valid_path(arguments["path"]):
        return False
    if "revision" in arguments and not _valid_revision(arguments["revision"]):
        return False
    if tool_id == "huggingface.file.upload":
        content = arguments.get("content_base64")
        if not isinstance(content, str) or not 1 <= len(content) <= MAX_CONTENT_BASE64_CHARS:
            return False
        try:
            _decode_content(content, settings.max_artifact_bytes)
        except ValueError:
            return False
        return True
    return True


def _target(tool_id: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the exact non-secret Hub resource target for the credential lease."""

    if tool_id == "hf_vendor_cli":
        return {}
    if tool_id in _LIST_OPERATION_TYPE:
        return {"repo_type": _LIST_OPERATION_TYPE[tool_id]}
    target = {"repo_type": str(arguments["repo_type"]), "repo_id": str(arguments["repo_id"])}
    if "path" in arguments:
        target["path"] = str(arguments["path"])
    if "revision" in arguments:
        target["revision"] = str(arguments["revision"])
    return target


def _decode_content(value: str, maximum: int) -> bytes:
    """Decode a strict base64 artifact within the configured transfer cap."""

    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("file content is not valid Base64") from None
    if len(result) > maximum:
        raise ValueError("file content exceeds the configured size limit")
    return result


def _valid_path(value: Any) -> bool:
    """Reject empty, traversal, control, or oversized Hub file paths."""

    return (
        isinstance(value, str)
        and 0 < len(value) <= 1024
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and not any(ord(char) < 32 for char in value)
    )


def _valid_revision(value: Any) -> bool:
    """Validate a branch, tag, or immutable Hub commit selector."""

    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", value) is not None
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _uuid_text(value: Any) -> bool:
    """Return whether a value is a canonical opaque UUID reference."""

    return (
        isinstance(value, str)
        and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            value,
        )
        is not None
    )


def _parse_json(payload: bytes | str) -> Any:
    """Parse JSON while rejecting duplicate keys before trust decisions."""

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(payload, object_pairs_hook=unique)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("invalid JSON") from error


def _tool_response(result: Mapping[str, Any]) -> Any:
    """Encode one allowlisted JSON result with no auth material."""

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
    """Report local command evidence without implying remote mutation success."""

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
        receipt["operation_ref"] = f"hfcli_{reference}"
    return receipt


def _tool_error(
    code: str,
    *,
    retryable: bool = False,
    retry_after: int | None = None,
    uncertain: bool = False,
) -> Any:
    """Return a structured sanitized provider error."""

    error: dict[str, Any] = {"code": code, "retryable": retryable}
    if retry_after is not None:
        error["retry_after_seconds"] = retry_after
    if uncertain:
        error["outcome"] = "UNKNOWN"
    response = wire.CallToolResponse()
    response.outcome.is_error = True
    response.outcome.content.add().json.json = json.dumps({"error": error}, separators=(",", ":"))
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


class _TypedPayload:
    """Byte payload wrapper consumed by DirectPluginRuntime."""

    def __init__(self, value: bytes, type_url: str) -> None:
        self.value = value
        self.type_url = type_url
