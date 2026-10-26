"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud_management_provider.providers                        │
│  Role: Call bounded Azure, GCP, and Runpod management APIs.         │
│                                                                      │
│  模块职责：通过有界 API 管理 Azure、GCP 和 Runpod 资源。               │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx
from cyrene_integration_contracts import canonical_json, payload_sha256

from .azure_templates import validate_deployment_template
from .ledger import LedgerCapacityError, ReplayConflict, RequestLedger
from .operations import InvalidOperationRef, create_operation_ref, parse_operation_ref
from .process import BoundedProcessRunner, ProcessConfigurationError, ProcessResult
from .settings import (
    CloudProfile,
    ConfigurationError,
    CredentialMissingError,
    ProfileNotFoundError,
    ProviderSettings,
)

AZURE_HOST = "management.azure.com"
AZURE_RESOURCE_API_VERSION = "2021-04-01"
AZURE_SUBSCRIPTION_API_VERSION = "2022-12-01"
AZURE_ACTIVITY_API_VERSION = "2015-04-01"
GCP_HOST = "compute.googleapis.com"
GCP_BASE = "https://compute.googleapis.com/compute/v1"
GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
RUNPOD_HOST = "api.runpod.io"
RUNPOD_BASE = "https://api.runpod.io/v2"
_MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
_MAX_TOOL_OUTPUT_BYTES = 24 * 1024
_RUNPOD_LOG_MAX_BYTES = 8192
_RUNPOD_LOG_MAX_RECORDS = 100
_RUNPOD_LOG_MAX_OUTPUT_BYTES = 16 * 1024
_RUNPOD_LOG_READ_CHUNK_BYTES = 1
_RUNPOD_LOG_READ_SECONDS = 8.0
_MAX_OPERATION_AGE = timedelta(days=30)
_RESOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_GCP_INSTANCE_NAME = re.compile(r"[a-z]([-a-z0-9]{0,61}[a-z0-9])?\Z")
_AZURE_RESOURCE_ID = re.compile(
    r"/subscriptions/(?P<subscription>[0-9a-fA-F-]{36})/"
    r"resourceGroups/(?P<group>[A-Za-z0-9_.() -]{1,90})/"
    r"providers/(?P<provider>[A-Za-z0-9.]+)/(?P<types>[A-Za-z0-9./-]+)\Z"
)
_SENSITIVE_CLI_ARGS = {
    "--account",
    "--access-token-file",
    "--access-token",
    "--auth-mode",
    "--billing-project",
    "--billing-account",
    "--cloud",
    "--client-id",
    "--client-secret",
    "--configuration",
    "--config",
    "--config-file",
    "--credential-file",
    "--credentials-file",
    "--credential-file-override",
    "--env",
    "--impersonate-service-account",
    "--key",
    "--password",
    "--network-volume-id",
    "--registry-auth-id",
    "--secret",
    "--token",
    "--token-file",
    "--endpoint",
    "--authority",
    "--quota-project",
    "--tenant",
    "--api-url",
    "--api-key",
    "--api-token",
    "--uri",
    "--headers",
    "--profile",
    "--scope",
    "--service-account",
    "--organization",
    "--folder",
    "--api-endpoint",
    "--api-endpoint-url",
    "--endpoint-override",
    "--endpoint-url",
    "--server-url",
}
_BLOCKED_CLI_COMMANDS = {
    "azure": {
        ("login",),
        ("logout",),
        ("account", "get-access-token"),
        ("account", "clear"),
        ("account", "set"),
        ("cloud",),
        ("config",),
        ("configure",),
        ("role",),
        ("storage", "account", "keys"),
        ("storage", "account", "show-connection-string"),
        ("storage", "account", "generate-sas"),
        ("storage", "container", "generate-sas"),
        ("storage", "blob", "generate-sas"),
        ("storage", "file", "generate-sas"),
        ("storage", "share", "generate-sas"),
        ("keyvault", "secret"),
        ("keyvault", "key"),
        ("keyvault", "certificate"),
        ("acr", "credential"),
        ("cosmosdb", "keys"),
        ("redis", "list-keys"),
        ("ad", "app", "credential"),
        ("ad", "sp", "credential"),
    },
    "gcp": {
        ("auth",),
        ("config",),
        ("secrets",),
        ("iam", "service-accounts", "keys"),
        ("kms", "keys", "versions", "get"),
        ("auth", "print-access-token"),
        ("auth", "application-default", "print-access-token"),
        ("add-iam-policy-binding",),
        ("remove-iam-policy-binding",),
        ("set-iam-policy",),
    },
    "runpod": {
        ("config",),
        ("login",),
        ("auth",),
        ("secret",),
        ("secrets",),
        ("serverless",),
        ("network-volume",),
        ("registry",),
        ("endpoint",),
        ("billing",),
    },
}
_CLI_READ_VERBS = {
    "check-name-availability",
    "describe",
    "exists",
    "get",
    "list",
    "search",
    "show",
    "status",
    "validate",
    "versions",
    "view",
    "wait",
}
_CLI_WRITE_VERBS = {
    "activate",
    "add",
    "apply",
    "attach",
    "cancel",
    "copy",
    "create",
    "deactivate",
    "delete",
    "deploy",
    "detach",
    "disable",
    "enable",
    "export",
    "import",
    "invoke",
    "move",
    "patch",
    "promote",
    "put",
    "remove",
    "repair",
    "replace",
    "reset",
    "resize",
    "restart",
    "restore",
    "resume",
    "run",
    "scale",
    "set",
    "start",
    "stop",
    "suspend",
    "terminate",
    "update",
}


class ProviderFailure(RuntimeError):
    """A sanitized vendor or authentication failure safe to return to Exchange."""

    def __init__(self, code: str, *, retryable: bool = False, ambiguous: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous


@dataclass(frozen=True, slots=True)
class HttpResult:
    """Bounded HTTP result with response headers retained only in memory."""

    status_code: int
    headers: Mapping[str, str]
    body: Any


class CloudManagementExecutor:
    """Execute fixed cloud APIs and CLI actions for configured immutable profiles."""

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        http_client: httpx.Client | None = None,
        process_runner: BoundedProcessRunner | None = None,
    ) -> None:
        self._settings = settings
        self._http = http_client or httpx.Client(
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
        self._owns_http = http_client is None
        self._process = process_runner or BoundedProcessRunner()
        self._ledger = RequestLedger(
            settings.state_dir,
            max_rows=settings.ledger_max_rows,
            max_bytes=settings.ledger_max_bytes,
            terminal_retention_seconds=settings.ledger_terminal_retention_seconds,
        )
        self._credentials: dict[str, Any] = {}

    def close(self) -> None:
        """Close the locally created HTTP session, if any."""

        if self._owns_http:
            self._http.close()

    def call(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        *,
        request_id: str,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Dispatch one catalogued tool, replay-guarding every mutation and CLI call."""

        from .tool_catalog import TOOL_BY_ID

        if tool_id not in TOOL_BY_ID:
            raise ProviderFailure("TOOL_NOT_FOUND")
        profile_ref = arguments.get("profile_ref")
        if not isinstance(profile_ref, str):
            raise ProviderFailure("INVALID_ARGUMENTS")
        try:
            profile = self._settings.profile(profile_ref)
        except ProfileNotFoundError as error:
            raise ProviderFailure("PROFILE_NOT_FOUND") from error
        if tool_id in _WRITE_TOOLS:
            argument_digest = payload_sha256(dict(arguments))
            write_receipt: Mapping[str, Any] | None = None
            preparation_error: ProviderFailure | None = None
            if tool_id == "cloud_resource_update" and profile.provider == "runpod":
                try:
                    _runpod_id_target(profile, arguments.get("target"))
                    update_body = _runpod_update_body(
                        profile, _object(arguments.get("parameters"), "parameters")
                    )
                    write_receipt = _runpod_update_receipt(update_body)
                except ProviderFailure as error:
                    preparation_error = error
            operation_ref = self._new_operation_ref(
                profile,
                tool_id,
                arguments.get("target"),
                request_id,
                vendor_operation=write_receipt,
            )
            try:
                existing = self._ledger.begin(request_id, argument_digest, operation_ref)
            except ReplayConflict as error:
                raise ProviderFailure("REQUEST_REPLAY_CONFLICT") from error
            except LedgerCapacityError as error:
                raise ProviderFailure("LEDGER_CAPACITY_REACHED", retryable=True) from error
            if existing is not None:
                if existing.response is not None:
                    return existing.response
                response = {
                    "state": "UNKNOWN_RESULT",
                    "reason_code": "PREVIOUS_WRITE_OUTCOME_UNKNOWN",
                }
                if existing.operation_ref:
                    response["operation_ref"] = existing.operation_ref
                return response
            try:
                if preparation_error is not None:
                    raise preparation_error
                result = self._dispatch(
                    profile,
                    tool_id,
                    arguments,
                    cancel_event=cancel_event,
                    request_id=request_id,
                    operation_ref=operation_ref,
                )
            except ProviderFailure as error:
                if error.ambiguous:
                    result = {
                        "state": "UNKNOWN_RESULT",
                        "reason_code": error.code,
                    }
                    if operation_ref:
                        result["operation_ref"] = operation_ref
                else:
                    result = {"error": {"code": error.code, "retryable": error.retryable}}
            except CredentialMissingError:
                result = {"error": {"code": "CREDENTIAL_MISSING", "retryable": False}}
            except ConfigurationError:
                result = {"error": {"code": "INVALID_CONFIGURATION", "retryable": False}}
            except Exception:
                result = {
                    "state": "UNKNOWN_RESULT",
                    "reason_code": "PROVIDER_OUTCOME_UNKNOWN",
                }
                if operation_ref:
                    result["operation_ref"] = operation_ref
            if (
                isinstance(result, Mapping)
                and result.get("state")
                in {"PENDING", "RUNNING", "UNKNOWN_RESULT", "REMOTE_STATE_UNVERIFIED"}
                and "operation_ref" not in result
                and operation_ref
            ):
                result = {**result, "operation_ref": operation_ref}
            return self._ledger.complete(request_id, result)
        return self._dispatch(profile, tool_id, arguments, cancel_event=cancel_event)

    def _dispatch(
        self,
        profile: CloudProfile,
        tool_id: str,
        arguments: Mapping[str, Any],
        *,
        cancel_event: threading.Event | None,
        request_id: str | None = None,
        operation_ref: str | None = None,
    ) -> dict[str, Any]:
        """Route one fixed tool to the matching API operation."""

        if tool_id == "cloud_provider_health":
            _require_keys(arguments, {"profile_ref"})
            return self._health(profile)
        if tool_id == "cloud_resources_list":
            _require_keys(arguments, {"profile_ref", "target"}, {"cursor"})
            return self._list_resources(profile, arguments["target"], arguments.get("cursor"))
        if tool_id == "cloud_resource_get":
            _require_keys(arguments, {"profile_ref", "target"})
            return self._get_resource(profile, arguments["target"])
        if tool_id in {"cloud_resource_create", "cloud_resource_deploy"}:
            _require_keys(arguments, {"profile_ref", "target", "parameters"})
            return self._create_resource(
                profile, tool_id, arguments["target"], arguments["parameters"]
            )
        if tool_id == "cloud_resource_update":
            _require_keys(arguments, {"profile_ref", "target", "parameters"})
            return self._update_resource(
                profile,
                arguments["target"],
                arguments["parameters"],
                operation_ref=operation_ref if profile.provider == "runpod" else None,
            )
        if tool_id in {
            "cloud_resource_start",
            "cloud_resource_stop",
            "cloud_resource_restart",
            "cloud_resource_delete",
        }:
            _require_keys(arguments, {"profile_ref", "target"})
            return self._change_state(profile, tool_id, arguments["target"])
        if tool_id == "cloud_operation_status":
            _require_keys(arguments, {"profile_ref", "operation_ref"})
            return self._operation_status(profile, arguments["operation_ref"])
        if tool_id == "cloud_operation_logs":
            _require_keys(arguments, {"profile_ref", "operation_ref"})
            return self._operation_logs(profile, arguments["operation_ref"])
        if tool_id == "cloud_operation_cancel":
            _require_keys(arguments, {"profile_ref", "operation_ref"})
            return self._operation_cancel(profile, arguments["operation_ref"])
        if tool_id == "cloud_vendor_cli":
            _require_keys(arguments, {"profile_ref", "argv"})
            return self._run_cli(
                profile,
                arguments["argv"],
                cancel_event,
                request_id=request_id,
                operation_ref=operation_ref,
            )
        raise ProviderFailure("TOOL_NOT_FOUND")

    def _health(self, profile: CloudProfile) -> dict[str, Any]:
        """Probe a harmless provider read after obtaining host-managed auth."""

        now = _now()
        expires_at: str | None = None
        try:
            if profile.provider == "azure":
                token, expires_at = self._azure_token(profile)
                sub = quote(str(profile.scope["subscription_id"]), safe="-")
                self._request(
                    "GET",
                    f"https://{AZURE_HOST}/subscriptions/{sub}",
                    token=token,
                    params={"api-version": AZURE_SUBSCRIPTION_API_VERSION},
                )
            elif profile.provider == "gcp":
                token, expires_at = self._gcp_token(profile)
                project = quote(str(profile.scope["project_id"]), safe="-")
                zone = quote(str(profile.scope["zones"][0]), safe="-")
                self._request(
                    "GET",
                    f"{GCP_BASE}/projects/{project}/zones/{zone}/instances",
                    token=token,
                    params={"maxResults": "1"},
                )
            else:
                token = self._runpod_token(profile)
                self._request(
                    "GET",
                    f"{RUNPOD_BASE}/pods",
                    token=token,
                    params={"limit": "1"},
                )
            authentication_state = "VALID"
            authorization_state = "VALID"
            reason_code = None
        except CredentialMissingError:
            authentication_state = "MISSING"
            authorization_state = "REAUTHORIZATION_REQUIRED"
            reason_code = "CREDENTIAL_MISSING"
        except ProviderFailure as error:
            if error.code == "AUTHENTICATION_FAILED":
                authentication_state = "INVALID"
                authorization_state = "REAUTHORIZATION_REQUIRED"
                reason_code = error.code
            elif error.code == "SCOPE_DENIED":
                authentication_state = "VALID"
                authorization_state = "PERMISSION_DENIED"
                reason_code = error.code
            elif error.code in {
                "CREDENTIAL_PROVIDER_UNAVAILABLE",
                "PROVIDER_REDIRECT_REJECTED",
                "PROVIDER_REJECTED",
                "PROVIDER_UNAVAILABLE",
                "PROVIDER_TIMEOUT",
                "PROVIDER_ERROR",
            }:
                authentication_state = "UNKNOWN"
                authorization_state = "UNKNOWN"
                reason_code = error.code
            else:
                raise
        return {
            "profile_ref": profile.profile_ref,
            "provider": profile.provider,
            "identity_ref": profile.identity_ref,
            "authentication_state": authentication_state,
            "authorization_state": authorization_state,
            "scope_refs": _scope_refs(profile),
            "checked_at": now,
            "expires_at": expires_at,
            "reason_code": reason_code,
        }

    def _list_resources(
        self, profile: CloudProfile, target: object, cursor: object
    ) -> dict[str, Any]:
        """List resources inside the selected group, zone, or Runpod Pod scope."""

        if profile.provider == "azure":
            group = _azure_list_target(profile, target)
            token, _ = self._azure_token(profile)
            subscription = quote(str(profile.scope["subscription_id"]), safe="-")
            group_path = quote(group, safe="")
            url = (
                f"https://{AZURE_HOST}/subscriptions/{subscription}/"
                f"resourceGroups/{group_path}/resources"
            )
            result = self._request(
                "GET",
                url,
                token=token,
                params={"api-version": AZURE_RESOURCE_API_VERSION, "$top": "100"},
            )
            items = _response_mapping_list(result.body, "value")
            next_link = result.body.get("nextLink")
            if next_link is not None and not isinstance(next_link, str):
                raise ProviderFailure("INVALID_PROVIDER_RESPONSE")
            return {
                "provider": "azure",
                "resources": [_azure_summary(item) for item in items],
                "has_more": bool(next_link),
            }
        if profile.provider == "gcp":
            zone = _gcp_list_target(profile, target)
            token, _ = self._gcp_token(profile)
            project = quote(str(profile.scope["project_id"]), safe="-")
            encoded_zone = quote(zone, safe="-")
            result = self._request(
                "GET",
                f"{GCP_BASE}/projects/{project}/zones/{encoded_zone}/instances",
                token=token,
                params={"maxResults": "100", **_gcp_page_params(cursor)},
            )
            if not isinstance(result.body, Mapping) or "error" in result.body:
                raise ProviderFailure("INVALID_PROVIDER_RESPONSE")
            body = result.body
            items = _response_mapping_list(body, "items", allow_missing=True)
            next_page_token = body.get("nextPageToken")
            if next_page_token is not None and not isinstance(next_page_token, str):
                raise ProviderFailure("INVALID_PROVIDER_RESPONSE")
            resources = [
                _gcp_summary(item)
                for item in items
                if _gcp_name_in_scope(profile, str(item.get("name", "")))
            ]
            return {
                "provider": "gcp",
                "resources": resources,
                "next_page_token": next_page_token,
            }
        _runpod_list_target(target)
        token = self._runpod_token(profile)
        query: dict[str, str] = {"limit": "100"}
        if cursor is not None:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
                raise ProviderFailure("INVALID_ARGUMENTS")
            query["cursor"] = cursor
        result = self._request("GET", f"{RUNPOD_BASE}/pods", token=token, params=query)
        if not isinstance(result.body, Mapping) or "error" in result.body:
            raise ProviderFailure("INVALID_PROVIDER_RESPONSE")
        body = result.body
        pods = [
            _runpod_summary(item)
            for item in _response_mapping_list(body, "pods")
            if _runpod_pod_in_scope(profile, item)
        ]
        pagination = body.get("pagination")
        if not isinstance(pagination, Mapping):
            raise ProviderFailure("INVALID_PROVIDER_RESPONSE")
        next_cursor = pagination.get("nextCursor")
        has_next_page = pagination.get("hasNextPage")
        if (next_cursor is not None and not isinstance(next_cursor, str)) or (
            has_next_page is not None and not isinstance(has_next_page, bool)
        ):
            raise ProviderFailure("INVALID_PROVIDER_RESPONSE")
        return {
            "provider": "runpod",
            "resources": pods,
            "next_cursor": next_cursor,
            "has_next_page": has_next_page is True,
        }

    def _get_resource(self, profile: CloudProfile, target: object) -> dict[str, Any]:
        """Get one configured Azure resource, GCE instance, or Runpod Pod."""

        if profile.provider == "azure":
            resource_id = _azure_resource_target(profile, target)
            token, _ = self._azure_token(profile)
            result = self._azure_get_resource(profile, token, resource_id)
            return {"provider": "azure", "resource": _azure_summary(result.body)}
        if profile.provider == "gcp":
            zone, name = _gcp_instance_target(profile, target)
            token, _ = self._gcp_token(profile)
            project = quote(str(profile.scope["project_id"]), safe="-")
            result = self._request(
                "GET",
                f"{GCP_BASE}/projects/{project}/zones/{quote(zone, safe='-')}"
                f"/instances/{quote(name, safe='-')}",
                token=token,
            )
            return {"provider": "gcp", "resource": _gcp_summary(result.body)}
        _runpod_id_target(profile, target)
        token = self._runpod_token(profile)
        body = self._runpod_get(profile, target, token)
        return {"provider": "runpod", "resource": _runpod_summary(body)}

    def _create_resource(
        self, profile: CloudProfile, tool_id: str, target: object, parameters: object
    ) -> dict[str, Any]:
        """Create or deploy one bounded Azure resource, GCE instance, or Pod."""

        body = _object(parameters, "parameters")
        if profile.provider == "azure":
            if tool_id == "cloud_resource_deploy":
                group, deployment_name = _azure_deployment_target(profile, target)
                deployment = _azure_deployment_body(body)
                try:
                    validate_deployment_template(
                        deployment["properties"]["template"],
                        str(profile.scope["subscription_id"]),
                        group,
                    )
                except ValueError as error:
                    raise ProviderFailure("SCOPE_DENIED") from error
                token, _ = self._azure_token(profile)
                subscription = quote(str(profile.scope["subscription_id"]), safe="-")
                url = (
                    f"https://{AZURE_HOST}/subscriptions/{subscription}/resourceGroups/"
                    f"{quote(group, safe='')}/providers/Microsoft.Resources/deployments/"
                    f"{quote(deployment_name, safe='')}"
                )
                result = self._request(
                    "PUT",
                    url,
                    token=token,
                    params={"api-version": str(profile.scope["deployment_api_version"])},
                    json_body=deployment,
                )
                target_value = {"resource_group": group, "deployment_name": deployment_name}
                return self._mutation_response(profile, "deploy", target_value, result)
            resource_id = _azure_resource_target(profile, target)
            if _is_azure_deployment_resource_id(resource_id):
                raise ProviderFailure("UNSUPPORTED_OPERATION")
            api_version = _azure_resource_api_version(profile, resource_id)
            token, _ = self._azure_token(profile)
            result = self._request(
                "PUT",
                f"https://{AZURE_HOST}{_azure_path(resource_id)}",
                token=token,
                params={"api-version": api_version},
                json_body=body,
            )
            return self._mutation_response(profile, "create", {"resource_id": resource_id}, result)
        if profile.provider == "gcp":
            zone, name = _gcp_instance_target(profile, target)
            _ensure_gcp_create_body(profile, name, body)
            token, _ = self._gcp_token(profile)
            project = quote(str(profile.scope["project_id"]), safe="-")
            result = self._request(
                "POST",
                f"{GCP_BASE}/projects/{project}/zones/{quote(zone, safe='-')}/instances",
                token=token,
                params={"requestId": str(uuid.uuid4())},
                json_body={"name": name, **body},
            )
            return self._mutation_response(
                profile, "create", {"zone": zone, "instance_name": name}, result
            )
        pod_name = _runpod_create_target(profile, target)
        runpod_body = _runpod_create_body(profile, pod_name, body)
        token = self._runpod_token(profile)
        result = self._request("POST", f"{RUNPOD_BASE}/pods", token=token, json_body=runpod_body)
        result_target = {"pod_name": pod_name}
        if isinstance(result.body, Mapping) and isinstance(result.body.get("id"), str):
            result_target["pod_id"] = result.body["id"]
        return self._mutation_response(profile, "create", result_target, result)

    def _update_resource(
        self,
        profile: CloudProfile,
        target: object,
        parameters: object,
        *,
        operation_ref: str | None = None,
    ) -> dict[str, Any]:
        """Update a supported resource through a fixed vendor REST route."""

        body = _object(parameters, "parameters")
        if profile.provider == "azure":
            resource_id = _azure_resource_target(profile, target)
            if _is_azure_deployment_resource_id(resource_id):
                raise ProviderFailure("UNSUPPORTED_OPERATION")
            token, _ = self._azure_token(profile)
            result = self._request(
                "PUT",
                f"https://{AZURE_HOST}{_azure_path(resource_id)}",
                token=token,
                params={"api-version": _azure_resource_api_version(profile, resource_id)},
                json_body=body,
            )
            return self._mutation_response(profile, "update", {"resource_id": resource_id}, result)
        if profile.provider == "gcp":
            zone, name = _gcp_instance_target(profile, target)
            raise ProviderFailure("UNSUPPORTED_OPERATION")
        pod_id = _runpod_id_target(profile, target)
        runpod_body = _runpod_update_body(profile, body)
        token = self._runpod_token(profile)
        self._runpod_get(profile, target, token)
        patch_result = self._request(
            "PATCH",
            f"{RUNPOD_BASE}/pods/{quote(pod_id, safe='-_')}",
            token=token,
            json_body=runpod_body,
        )
        operation_details = _runpod_update_receipt(runpod_body)
        try:
            observed = self._runpod_get(profile, target, token)
        except ProviderFailure:
            response = self._mutation_response(
                profile,
                "update",
                {"pod_id": pod_id},
                patch_result,
                vendor_operation=operation_details,
                operation_ref=operation_ref,
            )
            response.update(
                {
                    "state": "REMOTE_STATE_UNVERIFIED",
                    "configuration_match": False,
                    "reason_code": "READBACK_UNAVAILABLE",
                }
            )
            return response
        return self._mutation_response(
            profile,
            "update",
            {"pod_id": pod_id},
            HttpResult(200, {}, observed),
            vendor_operation=operation_details,
            operation_ref=operation_ref,
        )

    def _change_state(self, profile: CloudProfile, tool_id: str, target: object) -> dict[str, Any]:
        """Start, stop, or delete one supported resource without internal retries."""

        action = {
            "cloud_resource_start": "start",
            "cloud_resource_stop": "stop",
            "cloud_resource_restart": "restart",
            "cloud_resource_delete": "delete",
        }[tool_id]
        if profile.provider == "azure":
            if action == "restart":
                raise ProviderFailure("UNSUPPORTED_OPERATION")
            resource_id = _azure_resource_target(profile, target)
            token, _ = self._azure_token(profile)
            if action in {"start", "stop"}:
                _require_azure_vm_id(resource_id)
                verb = "start" if action == "start" else "deallocate"
                result = self._request(
                    "POST",
                    f"https://{AZURE_HOST}{_azure_path(resource_id)}/{verb}",
                    token=token,
                    params={"api-version": str(profile.scope["vm_api_version"])},
                )
            else:
                result = self._request(
                    "DELETE",
                    f"https://{AZURE_HOST}{_azure_path(resource_id)}",
                    token=token,
                    params={"api-version": _azure_resource_api_version(profile, resource_id)},
                )
            return self._mutation_response(profile, action, {"resource_id": resource_id}, result)
        if profile.provider == "gcp":
            if action == "restart":
                raise ProviderFailure("UNSUPPORTED_OPERATION")
            zone, name = _gcp_instance_target(profile, target)
            token, _ = self._gcp_token(profile)
            project = quote(str(profile.scope["project_id"]), safe="-")
            base = (
                f"{GCP_BASE}/projects/{project}/zones/{quote(zone, safe='-')}"
                f"/instances/{quote(name, safe='-')}"
            )
            if action == "delete":
                method, url = "DELETE", base
            else:
                method, url = "POST", f"{base}/{action}"
            result = self._request(
                method,
                url,
                token=token,
                params={"requestId": str(uuid.uuid4())},
            )
            return self._mutation_response(
                profile, action, {"zone": zone, "instance_name": name}, result
            )
        pod_id = _runpod_id_target(profile, target)
        token = self._runpod_token(profile)
        self._runpod_get(profile, target, token)
        if action == "delete":
            result = self._request(
                "DELETE",
                f"{RUNPOD_BASE}/pods/{quote(pod_id, safe='-_')}",
                token=token,
                allow_empty=True,
            )
            try:
                self._request(
                    "GET",
                    f"{RUNPOD_BASE}/pods/{quote(pod_id, safe='-_')}",
                    token=token,
                )
                release_confirmed = False
            except ProviderFailure as error:
                if error.code != "RESOURCE_NOT_FOUND":
                    raise
                release_confirmed = True
            return {
                "state": "SUCCEEDED" if release_confirmed else "UNKNOWN_RESULT",
                "resource_ref": pod_id,
                "expected_state": "TERMINATED",
                "actual_state": "ABSENT" if release_confirmed else "UNKNOWN",
                "readiness": "NOT_READY" if release_confirmed else "UNKNOWN",
                "release_confirmed": release_confirmed,
                "operation_ref": self._new_operation_ref(
                    profile,
                    tool_id,
                    {
                        "pod_id": pod_id,
                        **(
                            {"pod_name": target["pod_name"]}
                            if isinstance(target, Mapping)
                            and isinstance(target.get("pod_name"), str)
                            else {}
                        ),
                    },
                    str(uuid.uuid4()),
                ),
                "vendor_receipt": _redacted_runpod_receipt(result.body),
            }
        result = self._request(
            "POST",
            f"{RUNPOD_BASE}/pods/{quote(pod_id, safe='-_')}/action",
            token=token,
            json_body={"action": action},
        )
        return self._mutation_response(profile, action, {"pod_id": pod_id}, result)

    def _mutation_response(
        self,
        profile: CloudProfile,
        action: str,
        target: Mapping[str, Any],
        result: HttpResult,
        *,
        vendor_operation: Mapping[str, Any] | None = None,
        operation_ref: str | None = None,
    ) -> dict[str, Any]:
        """Normalize vendor LRO/resource responses without claiming readiness."""

        if operation_ref is not None:
            operation = self._operation_payload(profile, operation_ref)
            if (
                operation.get("provider") != profile.provider
                or operation.get("operation") != action
                or (
                    vendor_operation is not None
                    and operation.get("vendor_operation") != dict(vendor_operation)
                )
            ):
                raise ProviderFailure("OPERATION_NOT_FOUND")
            vendor_operation = operation.get("vendor_operation")
            op_ref = operation_ref
        else:
            operation = {
                "profile_ref": profile.profile_ref,
                "provider": profile.provider,
                "operation": action,
                "target": dict(target),
                "request_id": str(uuid.uuid4()),
                "created_at": _now(),
            }
            if vendor_operation is None:
                vendor_operation = self._vendor_operation(profile, result)
            if vendor_operation is not None:
                operation["vendor_operation"] = vendor_operation
            op_ref = create_operation_ref(operation, _verification_key(self._settings))
        state = _vendor_state(profile, result.body)
        response: dict[str, Any] = {
            "state": state,
            "operation_ref": op_ref,
            "resource_ref": _resource_ref(profile, target),
            "expected_state": _expected_state(profile.provider, action),
            "actual_state": _actual_state(profile.provider, action, result.body),
            "readiness": _readiness(profile.provider, action, result.body),
        }
        if profile.provider == "runpod" and response["expected_state"] is None:
            response.pop("expected_state")
        if profile.provider == "runpod":
            body = result.body if isinstance(result.body, Mapping) else {}
            actual = body.get("status", "UNKNOWN")
            expected = _expected_state(profile.provider, action)
            if action == "update":
                details = operation.get("vendor_operation")
                expected_configuration = (
                    details.get("expected_configuration")
                    if isinstance(details, Mapping) and details.get("kind") == "runpod_pod_update"
                    else None
                )
                matched = _runpod_update_matches(body, details)
                response["state"] = "SUCCEEDED" if matched else "PENDING"
                response["configuration_match"] = matched
                if isinstance(details, Mapping):
                    response["expected_configuration_sha256"] = details.get(
                        "expected_configuration_sha256"
                    )
                    response["observed_configuration"] = _runpod_configuration_snapshot(
                        body, expected_configuration
                    )
            elif action == "create":
                response["state"] = "SUCCEEDED" if isinstance(body.get("id"), str) else "PENDING"
            else:
                response["state"] = "SUCCEEDED" if actual == expected else "PENDING"
            if action != "update":
                response["desired_state"] = expected
            response["actual_state"] = actual
            response["readiness"] = "UNKNOWN" if actual == "RUNNING" else "NOT_READY"
            response["release_confirmed"] = False
        if profile.provider != "runpod" and state not in {"SUCCEEDED", "FAILED", "UNKNOWN_RESULT"}:
            response["state"] = "PENDING"
        return response

    def _operation_status(self, profile: CloudProfile, reference: object) -> dict[str, Any]:
        """Poll a signed provider operation ref after verifying its profile and age."""

        operation = self._operation_payload(profile, reference)
        provider = operation["provider"]
        if provider != profile.provider:
            raise ProviderFailure("OPERATION_NOT_FOUND")
        if operation.get("operation") == "cloud_vendor_cli":
            result = self._cli_operation_status(profile, operation, reference)
        elif profile.provider == "azure":
            result = self._azure_operation_status(profile, operation)
        elif profile.provider == "gcp":
            result = self._gcp_operation_status(profile, operation)
        else:
            result = self._runpod_operation_status(profile, operation, reference)
        if result.get("state") in {"SUCCEEDED", "FAILED"}:
            request_id = operation.get("request_id")
            if isinstance(request_id, str):
                self._ledger.mark_terminal(request_id)
        return result

    def _cli_operation_status(
        self,
        profile: CloudProfile,
        operation: Mapping[str, Any],
        reference: object,
    ) -> dict[str, Any]:
        """Reconcile a CLI receipt through a safe native reference when one exists."""

        request_id = operation.get("request_id")
        if not isinstance(request_id, str):
            raise ProviderFailure("OPERATION_NOT_FOUND")
        receipt = self._ledger.get(request_id)
        if receipt is None:
            raise ProviderFailure("OPERATION_NOT_FOUND")
        response = receipt.response
        stored_reference = response.get("operation_ref") if isinstance(response, Mapping) else None
        if reference != receipt.operation_ref and reference != stored_reference:
            raise ProviderFailure("OPERATION_NOT_FOUND")
        if response is None:
            return {
                "state": "UNKNOWN_RESULT",
                "command_state": "UNKNOWN",
                "remote_state": "UNKNOWN",
                "reason_code": "CLI_RECEIPT_INCOMPLETE",
                "operation_ref": reference,
                "reconciliation_available": False,
            }

        latest_reference = stored_reference or receipt.operation_ref
        try:
            latest_operation = parse_operation_ref(
                latest_reference, _verification_key(self._settings)
            )
        except InvalidOperationRef as error:
            raise ProviderFailure("OPERATION_NOT_FOUND") from error
        reconciliation_reference = latest_reference
        vendor_operation = latest_operation.get("vendor_operation")
        command_state = response.get("command_state", "UNKNOWN")
        if isinstance(vendor_operation, Mapping):
            kind = vendor_operation.get("kind")
            if kind == "gcp_compute_operation":
                zone = vendor_operation.get("zone")
                name = vendor_operation.get("name")
                if (
                    not isinstance(zone, str)
                    or zone not in profile.scope["zones"]
                    or not isinstance(name, str)
                    or not _RESOURCE_NAME.fullmatch(name)
                ):
                    raise ProviderFailure("OPERATION_NOT_FOUND")
                operation_body = self._gcp_operation_document(
                    profile,
                    {
                        "vendor_operation": {"name": name},
                        "target": {"zone": zone},
                    },
                )
                remote_state = _gcp_operation_state(operation_body)
                return {
                    "state": remote_state,
                    "command_state": command_state,
                    "remote_state": remote_state,
                    "resource_state": "UNKNOWN",
                    "operation_ref": reconciliation_reference,
                    "reconciliation_available": True,
                    "errors": _gcp_operation_diagnostics(operation_body),
                }
            if kind == "runpod_pod":
                return self._runpod_cli_operation_status(
                    profile, vendor_operation, reconciliation_reference, command_state
                )
        return {
            "state": "REMOTE_STATE_UNVERIFIED",
            "command_state": command_state,
            "remote_state": "UNKNOWN",
            "reason_code": "NATIVE_OPERATION_REFERENCE_UNAVAILABLE",
            "operation_ref": reconciliation_reference,
            "reconciliation_available": False,
        }

    def _runpod_cli_operation_status(
        self,
        profile: CloudProfile,
        vendor_operation: Mapping[str, Any],
        reference: object,
        command_state: object,
    ) -> dict[str, Any]:
        """Read back one CLI-created or mutated Pod using the host Runpod profile."""

        action = vendor_operation.get("action")
        pod_id = vendor_operation.get("pod_id")
        pod_name = vendor_operation.get("pod_name")
        if action not in {"create", "start", "stop", "restart", "update", "delete"}:
            raise ProviderFailure("OPERATION_NOT_FOUND")
        if isinstance(pod_id, str) and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", pod_id):
            raise ProviderFailure("OPERATION_NOT_FOUND")
        if isinstance(pod_name, str):
            _runpod_create_target(profile, {"pod_name": pod_name})
        if not isinstance(pod_id, str) and not isinstance(pod_name, str):
            raise ProviderFailure("OPERATION_NOT_FOUND")

        token = self._runpod_token(profile)
        try:
            if isinstance(pod_id, str):
                result = self._request(
                    "GET",
                    f"{RUNPOD_BASE}/pods/{quote(pod_id, safe='-_')}",
                    token=token,
                )
                body = result.body if isinstance(result.body, Mapping) else {}
                if body.get("id") != pod_id:
                    raise ProviderFailure("TARGET_IDENTITY_MISMATCH")
                if isinstance(pod_name, str) and body.get("name") != pod_name:
                    raise ProviderFailure("TARGET_IDENTITY_MISMATCH")
            else:
                found = self._find_runpod_by_name(profile, str(pod_name))
                if found is None:
                    return {
                        "state": "PENDING",
                        "command_state": command_state,
                        "remote_state": "PENDING",
                        "expected_state": _expected_state("runpod", str(action)),
                        "actual_state": "ABSENT",
                        "operation_ref": reference,
                        "reconciliation_available": True,
                    }
                body = found
                pod_id = body.get("id")
        except ProviderFailure as error:
            if error.code == "RESOURCE_NOT_FOUND" and action == "delete":
                return {
                    "state": "SUCCEEDED",
                    "command_state": command_state,
                    "remote_state": "ABSENT",
                    "expected_state": "ABSENT",
                    "actual_state": "ABSENT",
                    "release_confirmed": True,
                    "operation_ref": reference,
                    "reconciliation_available": True,
                }
            if error.code == "RESOURCE_NOT_FOUND":
                return {
                    "state": "PENDING",
                    "command_state": command_state,
                    "remote_state": "PENDING",
                    "expected_state": _expected_state("runpod", str(action)),
                    "actual_state": "ABSENT",
                    "operation_ref": reference,
                    "reconciliation_available": True,
                }
            raise

        actual_state = body.get("status", "UNKNOWN")
        expected_state = _expected_state("runpod", str(action))
        if action == "delete":
            state = "PENDING"
        elif action == "update":
            state = "REMOTE_STATE_UNVERIFIED"
        elif (
            action == "create" and actual_state in {"RUNNING", "EXITED", "STOPPED"}
        ) or expected_state == actual_state:
            state = "SUCCEEDED"
        elif actual_state in {"ERROR", "TERMINATED"}:
            state = "FAILED"
        else:
            state = "PENDING"
        return {
            "state": state,
            "command_state": command_state,
            "remote_state": actual_state,
            "expected_state": expected_state,
            "actual_state": actual_state,
            "readiness": "UNKNOWN" if actual_state == "RUNNING" else "NOT_READY",
            "release_confirmed": False,
            "resource_ref": pod_id if isinstance(pod_id, str) else None,
            "resource": _runpod_summary(body),
            "operation_ref": reference,
            "reconciliation_available": True,
        }

    def _operation_logs(self, profile: CloudProfile, reference: object) -> dict[str, Any]:
        """Return bounded provider diagnostics without accepting arbitrary URLs."""

        operation = self._operation_payload(profile, reference)
        if operation.get("operation") == "cloud_vendor_cli":
            raise ProviderFailure("UNSUPPORTED_OPERATION")
        if profile.provider == "azure":
            target = operation.get("target")
            if not isinstance(target, Mapping) or not isinstance(target.get("resource_id"), str):
                return {"provider": "azure", "logs": [], "available": False}
            resource_id = _azure_resource_target(profile, {"resource_id": target["resource_id"]})
            token, _ = self._azure_token(profile)
            subscription = quote(str(profile.scope["subscription_id"]), safe="-")
            start = str(operation.get("created_at", _now()))
            filter_value = f"eventTimestamp ge '{start}' and resourceId eq '{resource_id}'"
            result = self._request(
                "GET",
                f"https://{AZURE_HOST}/subscriptions/{subscription}/providers/microsoft.insights/eventtypes/management/values",
                token=token,
                params={
                    "api-version": AZURE_ACTIVITY_API_VERSION,
                    "$filter": filter_value,
                    "$top": "50",
                },
            )
            values = _response_mapping_list(result.body, "value")
            return {
                "provider": "azure",
                "available": True,
                "logs": [_azure_activity_summary(item) for item in values],
            }
        if profile.provider == "gcp":
            operation_body = self._gcp_operation_document(profile, operation)
            error = operation_body.get("error") if isinstance(operation_body, Mapping) else None
            return {
                "provider": "gcp",
                "available": True,
                "logs": _gcp_operation_diagnostics(operation_body),
                "error_present": isinstance(error, Mapping),
            }
        target = operation.get("target")
        if not isinstance(target, Mapping):
            raise ProviderFailure("OPERATION_NOT_FOUND")
        pod_id = target.get("pod_id")
        if not isinstance(pod_id, str):
            return {"provider": "runpod", "available": False, "logs": []}
        _runpod_id_target(
            profile,
            {
                "pod_id": pod_id,
                **(
                    {"pod_name": target["pod_name"]}
                    if isinstance(target.get("pod_name"), str)
                    else {}
                ),
            },
        )
        return self._runpod_logs(profile, pod_id)

    def _operation_cancel(self, profile: CloudProfile, reference: object) -> dict[str, Any]:
        """Cancel only the Azure template deployment operation with a vendor cancel API."""

        operation = self._operation_payload(profile, reference)
        if profile.provider != "azure" or operation.get("operation") != "deploy":
            raise ProviderFailure("UNSUPPORTED_OPERATION")
        target = operation.get("target")
        if not isinstance(target, Mapping):
            raise ProviderFailure("OPERATION_NOT_FOUND")
        group, name = _azure_deployment_target(
            profile,
            {
                "resource_group": target.get("resource_group"),
                "deployment_name": target.get("deployment_name"),
            },
        )
        token, _ = self._azure_token(profile)
        subscription = quote(str(profile.scope["subscription_id"]), safe="-")
        cancel_url = (
            f"https://{AZURE_HOST}/subscriptions/{subscription}/"
            f"resourceGroups/{quote(group, safe='')}/providers/Microsoft.Resources/deployments/"
            f"{quote(name, safe='')}/cancel"
        )
        result = self._request(
            "POST",
            cancel_url,
            token=token,
            params={"api-version": str(profile.scope["deployment_api_version"])},
            allow_empty=True,
        )
        return {
            "state": "PENDING",
            "cancel_requested": True,
            "operation_ref": reference,
            "status_code": result.status_code,
        }

    def _azure_operation_status(
        self, profile: CloudProfile, operation: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Poll Azure's fixed async operation or resource/deployment read-back."""

        token, _ = self._azure_token(profile)
        vendor_state: str | None = None
        vendor = operation.get("vendor_operation")
        if isinstance(vendor, Mapping) and isinstance(vendor.get("path"), str):
            path = _validated_azure_operation_path(profile, vendor.get("path"))
            params = vendor.get("query") if isinstance(vendor.get("query"), Mapping) else {}
            result = self._request("GET", f"https://{AZURE_HOST}{path}", token=token, params=params)
            vendor_state = _azure_operation_state(result.body)
            if vendor_state in {"FAILED", "PENDING", "UNKNOWN_RESULT"}:
                return {
                    "provider": "azure",
                    "state": vendor_state,
                    "vendor_state": _bounded_scalar(
                        result.body.get("status") if isinstance(result.body, Mapping) else None
                    ),
                }
        target = operation.get("target")
        if not isinstance(target, Mapping):
            raise ProviderFailure("OPERATION_NOT_FOUND")
        if operation.get("operation") == "deploy":
            group, name = _azure_deployment_target(profile, target)
            subscription = quote(str(profile.scope["subscription_id"]), safe="-")
            deployment_url = (
                f"https://{AZURE_HOST}/subscriptions/{subscription}/"
                f"resourceGroups/{quote(group, safe='')}/providers/Microsoft.Resources/deployments/"
                f"{quote(name, safe='')}"
            )
            result = self._request(
                "GET",
                deployment_url,
                token=token,
                params={"api-version": str(profile.scope["deployment_api_version"])},
            )
            body = result.body if isinstance(result.body, Mapping) else {}
            return {
                "provider": "azure",
                "state": _azure_provisioning_state(body),
                "vendor_state": vendor_state or "UNKNOWN",
                "actual_state": body.get("properties", {}).get("provisioningState")
                if isinstance(body.get("properties"), Mapping)
                else "UNKNOWN",
            }
        resource_id = _azure_resource_target(profile, target)
        try:
            result = self._azure_get_resource(profile, token, resource_id)
        except ProviderFailure as error:
            if error.code == "RESOURCE_NOT_FOUND" and operation.get("operation") == "delete":
                return {
                    "provider": "azure",
                    "state": "SUCCEEDED",
                    "actual_state": "ABSENT",
                    "release_confirmed": True,
                }
            if error.code == "RESOURCE_NOT_FOUND" and operation.get("operation") in {
                "create",
                "update",
            }:
                return {"provider": "azure", "state": "PENDING", "actual_state": "ABSENT"}
            raise
        body = result.body if isinstance(result.body, Mapping) else {}
        if operation.get("operation") in {"start", "stop"}:
            actual = self._azure_vm_power_state(profile, token, resource_id)
            expected = _expected_state("azure", str(operation.get("operation")))
            return {
                "provider": "azure",
                "state": "SUCCEEDED" if actual == expected else "PENDING",
                "expected_state": expected,
                "actual_state": actual,
                "readiness": "UNKNOWN" if actual == "RUNNING" else "NOT_READY",
                "vendor_state": vendor_state or "UNKNOWN",
            }
        state = _azure_provisioning_state(body)
        expected = _expected_state("azure", str(operation.get("operation")))
        actual = _azure_actual_state(body)
        if (expected == "SUCCEEDED" and state == "SUCCEEDED") or (
            expected is not None and actual.upper() == expected
        ):
            state = "SUCCEEDED"
        return {
            "provider": "azure",
            "state": state,
            "vendor_state": vendor_state or "UNKNOWN",
            "expected_state": expected,
            "actual_state": actual,
            "readiness": "UNKNOWN",
        }

    def _azure_vm_power_state(self, profile: CloudProfile, token: str, resource_id: str) -> str:
        """Read and normalize a VM's actual power state from Azure instance view."""

        _require_azure_vm_id(resource_id)
        result = self._request(
            "GET",
            f"https://{AZURE_HOST}{_azure_path(resource_id)}/instanceView",
            token=token,
            params={"api-version": str(profile.scope["vm_api_version"])},
        )
        body = result.body if isinstance(result.body, Mapping) else {}
        statuses = body.get("statuses", [])
        for item in statuses if isinstance(statuses, list) else []:
            if not isinstance(item, Mapping) or not isinstance(item.get("code"), str):
                continue
            prefix, _, value = item["code"].partition("/")
            if prefix.casefold() != "powerstate":
                continue
            normalized = value.upper()
            if normalized == "RUNNING":
                return "RUNNING"
            if normalized in {"STOPPED", "DEALLOCATED"}:
                return "STOPPED"
            return normalized
        return "UNKNOWN"

    def _gcp_operation_status(
        self, profile: CloudProfile, operation: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Poll the provider operation and confirm the target resource by read-back."""

        operation_body = self._gcp_operation_document(profile, operation)
        operation_state = _gcp_operation_state(operation_body) if operation_body else "PENDING"
        errors = _gcp_operation_diagnostics(operation_body)
        if operation_state in {"FAILED", "PENDING", "UNKNOWN_RESULT"}:
            return {
                "provider": "gcp",
                "state": operation_state,
                "vendor_state": operation_body.get("status", "UNKNOWN"),
                "errors": errors,
            }
        target = operation.get("target")
        if not isinstance(target, Mapping):
            raise ProviderFailure("OPERATION_NOT_FOUND")
        zone, name = _gcp_instance_target(profile, target)
        try:
            resource = self._get_resource(profile, {"zone": zone, "instance_name": name})[
                "resource"
            ]
        except ProviderFailure as error:
            if error.code == "RESOURCE_NOT_FOUND" and operation.get("operation") == "delete":
                return {
                    "provider": "gcp",
                    "state": "SUCCEEDED",
                    "actual_state": "ABSENT",
                    "release_confirmed": True,
                }
            if error.code == "RESOURCE_NOT_FOUND" and operation.get("operation") in {
                "create",
                "deploy",
            }:
                return {"provider": "gcp", "state": "PENDING", "actual_state": "ABSENT"}
            if error.code == "RESOURCE_NOT_FOUND" and operation_state == "SUCCEEDED":
                return {
                    "provider": "gcp",
                    "state": "UNKNOWN_RESULT",
                    "actual_state": "ABSENT",
                    "release_confirmed": False,
                }
            raise
        if operation.get("operation") == "delete":
            return {
                "provider": "gcp",
                "state": "UNKNOWN_RESULT" if operation_state == "SUCCEEDED" else "PENDING",
                "expected_state": "ABSENT",
                "actual_state": "PRESENT",
                "release_confirmed": False,
            }
        expected = _expected_state("gcp", str(operation.get("operation")))
        actual = resource.get("status", "UNKNOWN")
        state = "SUCCEEDED" if expected == actual or expected is None else "PENDING"
        return {
            "provider": "gcp",
            "state": state,
            "vendor_state": operation_body.get("status", "UNKNOWN"),
            "expected_state": expected,
            "actual_state": actual,
            "readiness": "UNKNOWN",
            "errors": errors,
        }

    def _runpod_operation_status(
        self, profile: CloudProfile, operation: Mapping[str, Any], reference: object
    ) -> dict[str, Any]:
        """Report Pod desired state, observed state, readiness, and deletion proof."""

        if operation.get("operation") == "update" and not _runpod_update_receipt_valid(
            operation.get("vendor_operation")
        ):
            raise ProviderFailure("OPERATION_NOT_FOUND")
        target = operation.get("target")
        if not isinstance(target, Mapping):
            raise ProviderFailure("OPERATION_NOT_FOUND")
        pod_id = target.get("pod_id")
        if isinstance(pod_id, str):
            if isinstance(target.get("pod_name"), str):
                _runpod_create_target(profile, {"pod_name": target["pod_name"]})
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", pod_id):
                    raise ProviderFailure("OPERATION_NOT_FOUND")
            else:
                _runpod_id_target(profile, {"pod_id": pod_id})
            try:
                body = self._runpod_get(profile, target, self._runpod_token(profile))
            except ProviderFailure as error:
                if error.code == "RESOURCE_NOT_FOUND" and operation.get("operation") == "delete":
                    return {
                        "provider": "runpod",
                        "state": "SUCCEEDED",
                        "desired_state": "TERMINATED",
                        "actual_state": "ABSENT",
                        "readiness": "NOT_READY",
                        "release_confirmed": True,
                    }
                if error.code == "RESOURCE_NOT_FOUND" and operation.get("operation") == "update":
                    return {
                        "provider": "runpod",
                        "state": "REMOTE_STATE_UNVERIFIED",
                        "reason_code": "RESOURCE_NOT_FOUND",
                        "actual_state": "ABSENT",
                        "configuration_match": False,
                        "operation_ref": reference,
                    }
                raise
        else:
            pod_name = target.get("pod_name")
            if not isinstance(pod_name, str):
                raise ProviderFailure("OPERATION_NOT_FOUND")
            body = self._find_runpod_by_name(profile, pod_name)
            if body is None:
                return {
                    "provider": "runpod",
                    "state": "PENDING",
                    "desired_state": _expected_state("runpod", str(operation.get("operation"))),
                    "actual_state": "ABSENT",
                    "readiness": "UNKNOWN",
                    "release_confirmed": False,
                }
            pod_id = str(body.get("id", ""))
        expected = _expected_state("runpod", str(operation.get("operation")))
        actual = body.get("status", "UNKNOWN")
        if operation.get("operation") == "update":
            details = operation.get("vendor_operation")
            assert isinstance(details, Mapping)
            expected_configuration = details.get("expected_configuration")
            expected_digest = details.get("expected_configuration_sha256")
            matched = _runpod_update_matches(body, details)
            return {
                "provider": "runpod",
                "state": "SUCCEEDED" if matched else "PENDING",
                "operation_ref": reference,
                "configuration_match": matched,
                "expected_configuration_sha256": expected_digest,
                "observed_configuration": _runpod_configuration_snapshot(
                    body, expected_configuration
                ),
                "actual_state": actual,
                "readiness": "UNKNOWN" if actual == "RUNNING" else "NOT_READY",
                "release_confirmed": False,
            }
        matched = expected == actual or (
            operation.get("operation") == "create" and actual in {"RUNNING", "EXITED", "STOPPED"}
        )
        return {
            "provider": "runpod",
            "state": "SUCCEEDED" if matched else "PENDING",
            "operation_ref": create_operation_ref(
                {**dict(operation), "target": {**dict(target), "pod_id": pod_id}},
                _verification_key(self._settings),
            ),
            "desired_state": expected,
            "expected_state": expected,
            "actual_state": actual,
            "readiness": "UNKNOWN" if actual == "RUNNING" else "NOT_READY",
            "release_confirmed": False,
        }

    def _runpod_get(self, profile: CloudProfile, target: object, token: str) -> Mapping[str, Any]:
        """Read one scoped Pod and confirm any supplied ID/name pair agrees."""

        pod_id = _runpod_id_target(profile, target)
        result = self._request(
            "GET",
            f"{RUNPOD_BASE}/pods/{quote(pod_id, safe='-_')}",
            token=token,
        )
        body = result.body if isinstance(result.body, Mapping) else {}
        expected_name = target.get("pod_name") if isinstance(target, Mapping) else None
        if expected_name is not None and body.get("name") != expected_name:
            raise ProviderFailure("SCOPE_DENIED")
        if body.get("id") != pod_id:
            raise ProviderFailure("TARGET_IDENTITY_MISMATCH")
        return body

    def _operation_payload(self, profile: CloudProfile, reference: object) -> dict[str, Any]:
        """Check HMAC, binding profile, and age before any operation-reference use."""

        try:
            payload = parse_operation_ref(reference, _verification_key(self._settings))
        except InvalidOperationRef as error:
            raise ProviderFailure("INVALID_ARGUMENTS") from error
        created = payload.get("created_at")
        profile_ref = payload.get("profile_ref")
        if profile_ref != profile.profile_ref or not isinstance(created, str):
            raise ProviderFailure("OPERATION_NOT_FOUND")
        try:
            created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError as error:
            raise ProviderFailure("INVALID_ARGUMENTS") from error
        if datetime.now(UTC) - created_at > _MAX_OPERATION_AGE:
            raise ProviderFailure("OPERATION_EXPIRED")
        return payload

    def _azure_get_resource(
        self, profile: CloudProfile, token: str, resource_id: str
    ) -> HttpResult:
        """Call the fixed ARM resource get endpoint for one configured target."""

        return self._request(
            "GET",
            f"https://{AZURE_HOST}{_azure_path(resource_id)}",
            token=token,
            params={"api-version": _azure_resource_api_version(profile, resource_id)},
        )

    def _gcp_operation_document(
        self, profile: CloudProfile, operation: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Read one Compute Engine zone operation using a validated operation name."""

        vendor = operation.get("vendor_operation")
        target = operation.get("target")
        if not isinstance(vendor, Mapping) or not isinstance(vendor.get("name"), str):
            return {}
        if not isinstance(target, Mapping):
            raise ProviderFailure("OPERATION_NOT_FOUND")
        zone = target.get("zone")
        if not isinstance(zone, str) or zone not in profile.scope["zones"]:
            raise ProviderFailure("OPERATION_NOT_FOUND")
        if not _RESOURCE_NAME.fullmatch(str(vendor["name"])):
            raise ProviderFailure("OPERATION_NOT_FOUND")
        token, _ = self._gcp_token(profile)
        project = quote(str(profile.scope["project_id"]), safe="-")
        operation_url = (
            f"{GCP_BASE}/projects/{project}/zones/{quote(zone, safe='-')}"
            f"/operations/{quote(str(vendor['name']), safe='-')}"
        )
        result = self._request(
            "GET",
            operation_url,
            token=token,
        )
        if not isinstance(result.body, Mapping) or "error" in result.body:
            raise ProviderFailure("INVALID_PROVIDER_RESPONSE")
        return result.body

    def _runpod_logs(self, profile: CloudProfile, pod_id: str) -> dict[str, Any]:
        """Decode a bounded Runpod SSE tail and retain completed records on timeout."""

        available = False
        timed_out = False
        truncated = False
        records: list[dict[str, Any]] = []
        buffer = bytearray()
        event_data: list[bytes] = []
        consumed = 0
        started = time.monotonic()
        total_seconds = min(float(self._settings.timeout_seconds), _RUNPOD_LOG_READ_SECONDS)
        read_timeout = min(total_seconds, 1.0)
        secret = self._settings.secret_value(profile.api_key_ref or "") or ""

        def finish_event() -> None:
            nonlocal truncated
            if event_data:
                payload = b"\n".join(event_data).decode("utf-8", errors="replace")
                event_data.clear()
                remaining = _RUNPOD_LOG_MAX_RECORDS - len(records)
                for record in _runpod_log_records(payload, max_records=remaining + 1):
                    if len(records) >= _RUNPOD_LOG_MAX_RECORDS:
                        truncated = True
                        break
                    if secret:
                        for key, value in tuple(record.items()):
                            if isinstance(value, str):
                                record[key] = value.replace(secret, "[REDACTED]")
                    if len(canonical_json([*records, record])) > _RUNPOD_LOG_MAX_OUTPUT_BYTES:
                        truncated = True
                        break
                    records.append(record)

        def process_sse_line(raw_line: bytes) -> None:
            if not raw_line:
                finish_event()
                return
            if raw_line.startswith(b":"):
                return
            field, separator, value = raw_line.partition(b":")
            if field != b"data":
                return
            if separator and value.startswith(b" "):
                value = value[1:]
            event_data.append(value)

        def process_buffer(*, flush_incomplete: bool) -> None:
            while True:
                separator = buffer.find(b"\n")
                if separator < 0:
                    break
                raw_line = bytes(buffer[:separator]).rstrip(b"\r")
                del buffer[: separator + 1]
                process_sse_line(raw_line)
            if flush_incomplete and buffer:
                raw_line = bytes(buffer).rstrip(b"\r")
                buffer.clear()
                process_sse_line(raw_line)

        try:
            with self._http.stream(
                "GET",
                f"{RUNPOD_BASE}/pods/{quote(pod_id, safe='-_')}/logs",
                headers={
                    "Authorization": f"Bearer {self._runpod_token(profile)}",
                    "Accept": "text/event-stream",
                },
                params={"tail": "100"},
                timeout=httpx.Timeout(
                    total_seconds,
                    connect=min(total_seconds, 3.0),
                    read=read_timeout,
                    write=min(total_seconds, 3.0),
                    pool=min(total_seconds, 3.0),
                ),
            ) as response:
                if response.status_code != 200:
                    self._raise_http_error(response.status_code, "GET")
                available = True
                # Small iteration quantum preserves a short SSE tail if the next read times out.
                for chunk in response.iter_bytes(chunk_size=_RUNPOD_LOG_READ_CHUNK_BYTES):
                    if total_seconds - (time.monotonic() - started) <= 0:
                        timed_out = True
                        break
                    if not chunk:
                        continue
                    room = _RUNPOD_LOG_MAX_BYTES - consumed
                    if len(chunk) > room:
                        buffer.extend(chunk[:room])
                        consumed += room
                        truncated = True
                        process_buffer(flush_incomplete=False)
                        break
                    buffer.extend(chunk)
                    consumed += len(chunk)
                    process_buffer(flush_incomplete=False)
                    if truncated:
                        break
                process_buffer(flush_incomplete=not truncated)
                if not truncated:
                    finish_event()
        except ProviderFailure:
            raise
        except httpx.TimeoutException:
            timed_out = True
            process_buffer(flush_incomplete=True)
            finish_event()
        except httpx.HTTPError as error:
            raise ProviderFailure("PROVIDER_UNAVAILABLE", retryable=True) from error
        return {
            "provider": "runpod",
            "available": available,
            "logs": records,
            "truncated": truncated,
            "timed_out": timed_out,
        }

    def _find_runpod_by_name(
        self, profile: CloudProfile, pod_name: str
    ) -> Mapping[str, Any] | None:
        """Read a bounded Pod page and resolve one exact newly created Pod name."""

        token = self._runpod_token(profile)
        result = self._request("GET", f"{RUNPOD_BASE}/pods", token=token, params={"limit": "1000"})
        if not isinstance(result.body, Mapping) or "error" in result.body:
            raise ProviderFailure("INVALID_PROVIDER_RESPONSE")
        body = result.body
        matches = [
            item
            for item in _response_mapping_list(body, "pods")
            if item.get("name") == pod_name
            and _runpod_pod_in_scope(profile, item)
        ]
        return matches[0] if len(matches) == 1 else None

    def _vendor_operation(
        self, profile: CloudProfile, result: HttpResult
    ) -> Mapping[str, Any] | None:
        """Extract a provider operation identifier while rejecting arbitrary hosts."""

        if profile.provider == "azure":
            header = result.headers.get("azure-asyncoperation") or result.headers.get("location")
            if not header:
                return None
            split = urlsplit(header)
            if (
                split.scheme != "https"
                or split.hostname != AZURE_HOST
                or split.username
                or split.password
            ):
                raise ProviderFailure("INVALID_VENDOR_OPERATION")
            prefix = f"/subscriptions/{profile.scope['subscription_id']}/"
            if not split.path.casefold().startswith(prefix.casefold()):
                raise ProviderFailure("INVALID_VENDOR_OPERATION")
            query = parse_qs(split.query, keep_blank_values=False)
            api_version = query.get("api-version", [None])[0]
            if not isinstance(api_version, str) or not _looks_like_api_version(api_version):
                raise ProviderFailure("INVALID_VENDOR_OPERATION")
            return {"path": split.path, "query": {"api-version": api_version}}
        if profile.provider == "gcp":
            body = result.body if isinstance(result.body, Mapping) else {}
            operation_name = body.get("name")
            if isinstance(operation_name, str) and _RESOURCE_NAME.fullmatch(operation_name):
                return {"name": operation_name}
        return None

    def _run_cli(
        self,
        profile: CloudProfile,
        argv: object,
        cancel_event: threading.Event | None,
        *,
        request_id: str | None,
        operation_ref: str | None,
    ) -> dict[str, Any]:
        """Execute an exact vendor argv with a bound scope and isolated config state."""

        if profile.cli_executable is None:
            raise ProviderFailure("CLI_UNAVAILABLE")
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 128
            or not all(isinstance(item, str) for item in argv)
        ):
            raise ProviderFailure("INVALID_ARGUMENTS")
        self._validate_cli_scope(profile, argv)
        home = self._cli_home(profile)
        environment = {
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        if profile.provider == "azure":
            environment["AZURE_CONFIG_DIR"] = str(home / "azure")
        elif profile.provider == "gcp":
            environment["CLOUDSDK_CONFIG"] = str(home / "gcloud")
            environment["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] = self._gcp_credential_path(
                profile
            )
            environment["CLOUDSDK_CORE_PROJECT"] = str(profile.scope["project_id"])
            environment["CLOUDSDK_COMPUTE_ZONE"] = str(profile.scope["zones"][0])
        else:
            environment["RUNPOD_API_KEY"] = self._runpod_token(profile)
        for path in {home, home / "azure", home / "gcloud"}:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if profile.provider == "azure":
            self._prepare_azure_cli_profile(profile, environment, home)
        try:
            result = self._process.run(
                profile.cli_executable,
                argv,
                cwd=home,
                environment=environment,
                timeout_seconds=profile.cli_timeout_seconds,
                max_output_bytes=8192,
                cancel_event=cancel_event,
                secrets_to_redact=[
                    value
                    for value in environment.values()
                    if value.startswith(("hf_", "ghp_", "eyJ"))
                ]
                + ([environment["RUNPOD_API_KEY"]] if "RUNPOD_API_KEY" in environment else []),
            )
        except ProcessConfigurationError as error:
            raise ProviderFailure("CLI_REJECTED") from error
        except FileNotFoundError as error:
            raise ProviderFailure("CLI_UNAVAILABLE") from error
        command_state = _process_command_state(result)
        asynchronous_requested = _cli_requests_async(argv)
        requires_remote_reconciliation = asynchronous_requested or not _cli_command_is_read_only(
            profile.provider, argv
        )
        safe_vendor_reference = (
            self._extract_cli_vendor_reference(profile, argv, result.stdout)
            if requires_remote_reconciliation and not result.stdout_truncated
            else None
        )
        result_operation_ref = operation_ref
        if safe_vendor_reference is not None and isinstance(request_id, str):
            target = (
                {"zone": safe_vendor_reference["zone"]}
                if safe_vendor_reference.get("kind") == "gcp_compute_operation"
                else {}
            )
            result_operation_ref = self._new_operation_ref(
                profile,
                "cloud_vendor_cli",
                target,
                request_id,
                vendor_operation=safe_vendor_reference,
            )
        if not requires_remote_reconciliation:
            state = command_state
            remote_state = "NOT_APPLICABLE"
        elif command_state == "COMMAND_COMPLETED" and safe_vendor_reference is not None:
            state = "PENDING"
            remote_state = "PENDING"
        elif command_state == "COMMAND_COMPLETED":
            state = "REMOTE_STATE_UNVERIFIED"
            remote_state = "UNKNOWN"
        else:
            state = "UNKNOWN_RESULT"
            remote_state = "UNKNOWN"
        response = {
            "state": state,
            "command_state": command_state,
            "remote_state": remote_state,
            "exit_code": result.exit_code,
            "stdout_bytes": result.stdout_bytes,
            "stderr_bytes": result.stderr_bytes,
            "stdout_suppressed": True,
            "stderr_suppressed": True,
            "output_truncated": result.stdout_truncated or result.stderr_truncated,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
        }
        if requires_remote_reconciliation and result_operation_ref:
            response["operation_ref"] = result_operation_ref
        if requires_remote_reconciliation:
            response["reconciliation_available"] = safe_vendor_reference is not None
        return response

    def _extract_cli_vendor_reference(
        self,
        profile: CloudProfile,
        argv: Sequence[str],
        stdout: str,
    ) -> dict[str, Any] | None:
        """Extract only validated native operation/resource IDs from CLI JSON."""

        value = _parse_cli_json(stdout)
        if not isinstance(value, Mapping):
            return None
        if profile.provider == "gcp":
            kind = value.get("kind")
            self_link = value.get("selfLink")
            if kind != "compute#operation" and not (
                isinstance(self_link, str) and "/operations/" in self_link
            ):
                return None
            name = value.get("name")
            zone = _gcp_cli_operation_zone(profile, value.get("zone"))
            if not isinstance(name, str) or not _RESOURCE_NAME.fullmatch(name) or zone is None:
                return None
            return {"kind": "gcp_compute_operation", "name": name, "zone": zone}
        if profile.provider != "runpod" or len(argv) < 2:
            return None
        if argv[0].casefold() != "pod" or argv[1].casefold() not in {
            "create",
            "start",
            "stop",
            "restart",
            "update",
            "delete",
        }:
            return None
        action = argv[1].casefold()
        pod_id = value.get("id")
        if not isinstance(pod_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", pod_id):
            pod_id = None
        pod_name = value.get("name")
        if isinstance(pod_name, str):
            try:
                _runpod_create_target(profile, {"pod_name": pod_name})
            except ProviderFailure:
                pod_name = None
        else:
            pod_name = None
        if pod_id is None and pod_name is None:
            return None
        return {
            "kind": "runpod_pod",
            "action": action,
            **({"pod_id": pod_id} if pod_id else {}),
            **({"pod_name": pod_name} if pod_name else {}),
        }

    def _validate_cli_scope(self, profile: CloudProfile, argv: Sequence[str]) -> None:
        """Enforce the host-resolved profile scope and block credential export paths."""

        if not argv or len(argv) > 128 or not all(isinstance(item, str) for item in argv):
            raise ProviderFailure("INVALID_ARGUMENTS")
        vendor = profile.provider
        if any(_contains_tokens(argv, blocked) for blocked in _BLOCKED_CLI_COMMANDS[vendor]):
            raise ProviderFailure("CLI_REJECTED")
        for argument in argv:
            if "\x00" in argument:
                raise ProviderFailure("INVALID_ARGUMENTS")
            normalized = argument.casefold()
            option = normalized.split("=", 1)[0]
            if normalized in _SENSITIVE_CLI_ARGS or normalized.startswith(
                tuple(f"{flag}=" for flag in _SENSITIVE_CLI_ARGS)
            ):
                raise ProviderFailure("CLI_REJECTED")
            if option in {"--url", "--uri"} and profile.provider != "azure":
                raise ProviderFailure("CLI_REJECTED")
            if option == "--url" and profile.provider == "azure":
                selected = _flag_values(argv, {"--url"})
                if not selected:
                    raise ProviderFailure("INVALID_ARGUMENTS")
                for value in selected:
                    _validate_azure_cli_url(value, profile)
            if vendor == "azure":
                _validate_flag_values(
                    argv,
                    {"--subscription", "-s"},
                    {str(profile.scope["subscription_id"]).casefold()},
                )
                _validate_flag_values(
                    argv,
                    {"--resource-group", "-g"},
                    {str(group).casefold() for group in profile.scope["resource_groups"]},
                )
            elif vendor == "gcp":
                _validate_flag_values(
                    argv,
                    {"--project", "-p"},
                    {str(profile.scope["project_id"])},
                    case_sensitive=True,
                )
                _validate_flag_values(
                    argv,
                    {"--zone", "-z"},
                    set(profile.scope["zones"]),
                    case_sensitive=True,
                )
        if profile.provider == "azure":
            self._validate_azure_cli_profile_scope(profile, argv)
        elif profile.provider == "runpod":
            self._validate_runpod_cli_profile_scope(profile, argv)

    def _validate_azure_cli_profile_scope(self, profile: CloudProfile, argv: Sequence[str]) -> None:
        """Keep Azure native commands within the configured subscription and resource groups."""

        if len(argv) == 1 and argv[0].casefold() == "version":
            return
        if len(argv) < 2:
            raise ProviderFailure("INVALID_ARGUMENTS")
        command = tuple(value.casefold() for value in argv[:2])
        groups = {str(group).casefold() for group in profile.scope["resource_groups"]}
        group_values = _flag_values(argv, {"--resource-group", "-g"})
        if any(value.casefold() not in groups for value in group_values):
            raise ProviderFailure("SCOPE_DENIED")
        if command == ("account", "show") or command[0] == "version":
            return
        if command == ("group", "list"):
            raise ProviderFailure("SCOPE_DENIED")
        if command[0] == "group":
            names = _flag_values(argv, {"--name", "-n"})
            if command[1] not in {"show", "delete"} or not names:
                raise ProviderFailure("UNSUPPORTED_OPERATION")
            if any(name.casefold() not in groups for name in names):
                raise ProviderFailure("SCOPE_DENIED")
            return
        for value in _flag_values(argv, {"--ids"}):
            for resource_id in value.split(","):
                parsed = _azure_resource_target(profile, {"resource_id": resource_id})
                match = _AZURE_RESOURCE_ID.fullmatch(parsed)
                if match is None or match.group("group").casefold() not in groups:
                    raise ProviderFailure("SCOPE_DENIED")
        urls = _flag_values(argv, {"--url"})
        if command[0] == "rest" and not urls:
            raise ProviderFailure("INVALID_ARGUMENTS")
        for value in urls:
            parsed_url = _validate_azure_cli_url(value, profile)
            decoded_path = unquote(parsed_url.path)
            path_parts = [part.casefold() for part in decoded_path.split("/") if part]
            sensitive_path_parts = {
                "listkeys",
                "listsecrets",
                "listtokens",
                "listcredentials",
                "listcertificates",
                "listconnectionstrings",
                "listservicekeys",
                "generatesas",
                "generatesastoken",
                "generateusersas",
                "generateaccountsas",
                "secrets",
                "tokens",
                "credentials",
                "certificates",
                "keys",
                "passwords",
                "roleassignments",
                "roledefinitions",
                "policyassignments",
                "policydefinitions",
            }
            if any(part in sensitive_path_parts for part in path_parts):
                raise ProviderFailure("CLI_REJECTED")
            collection = re.fullmatch(
                rf"/subscriptions/{re.escape(str(profile.scope['subscription_id']))}/resourceGroups/(?P<group>[^/]+)/resources",
                decoded_path,
                flags=re.IGNORECASE,
            )
            if collection is not None:
                decoded_group = collection.group("group").casefold()
                if decoded_group not in groups:
                    raise ProviderFailure("SCOPE_DENIED")
                continue
            resource_id = _azure_resource_target(profile, {"resource_id": decoded_path})
            match = _AZURE_RESOURCE_ID.fullmatch(resource_id)
            if match is None or match.group("group").casefold() not in groups:
                raise ProviderFailure("SCOPE_DENIED")
        if not group_values and not _flag_values(argv, {"--ids", "--url"}):
            raise ProviderFailure("SCOPE_DENIED")

    def _validate_runpod_cli_profile_scope(
        self, profile: CloudProfile, argv: Sequence[str]
    ) -> None:
        """Limit Runpod native CLI to Pods and non-secret catalog reads in this account."""

        if len(argv) < 2:
            raise ProviderFailure("INVALID_ARGUMENTS")
        root, command = argv[0].casefold(), argv[1].casefold()
        if root == "pod" and command == "list":
            return
        if root == "pod" and command == "create":
            names = _flag_values(argv, {"--name"})
            if len(names) != 1 or not any(
                names[0].startswith(prefix) for prefix in profile.scope["pod_name_prefixes"]
            ):
                raise ProviderFailure("SCOPE_DENIED")
            gpu_ids = _flag_values(argv, {"--gpu-id"})
            if not gpu_ids or any(
                gpu_id not in profile.scope["gpu_type_ids"]
                for value in gpu_ids
                for gpu_id in value.split(",")
            ):
                raise ProviderFailure("SCOPE_DENIED")
            for flag, maximum in (
                ("--gpu-count", profile.scope["max_gpu_count"]),
                ("--container-disk-in-gb", profile.scope["max_container_disk_gb"]),
                ("--volume-in-gb", profile.scope["max_volume_disk_gb"]),
            ):
                values = _flag_values(argv, {flag})
                if values and any(
                    not value.isdecimal() or int(value) > maximum for value in values
                ):
                    raise ProviderFailure("SCOPE_DENIED")
                if flag == "--gpu-count" and (
                    len(values) != 1 or not values[0].isdecimal() or int(values[0]) < 1
                ):
                    raise ProviderFailure("SCOPE_DENIED")
                if (
                    flag == "--container-disk-in-gb"
                    and not values
                    and profile.scope["max_container_disk_gb"] < 20
                ):
                    raise ProviderFailure("SCOPE_DENIED")
            centers = _flag_values(argv, {"--data-center-ids"})
            if not centers or any(
                center not in profile.scope["data_center_ids"]
                for value in centers
                for center in value.split(",")
            ):
                raise ProviderFailure("SCOPE_DENIED")
            return
        if root == "gpu" and command == "list":
            return
        if root == "template" and command in {"list", "search"}:
            return
        if root == "datacenter" and command == "list":
            return
        if root == "pod" and command in {"get", "start", "stop", "restart", "update", "delete"}:
            positional = next((value for value in argv[2:] if not value.startswith("-")), None)
            if not isinstance(positional, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", positional
            ):
                raise ProviderFailure("INVALID_ARGUMENTS")
            return
        raise ProviderFailure("UNSUPPORTED_OPERATION")

    def _prepare_azure_cli_profile(
        self,
        profile: CloudProfile,
        environment: Mapping[str, str],
        home: Path,
    ) -> None:
        """Authenticate the isolated Azure CLI through the bound Managed Identity."""

        executable = profile.cli_executable
        if not executable:
            raise ProviderFailure("CLI_UNAVAILABLE")
        login = ["login", "--identity"]
        if profile.client_id:
            login.extend(["--client-id", profile.client_id])
        try:
            login_result = self._process.run(
                executable,
                login,
                cwd=home,
                environment=environment,
                timeout_seconds=profile.cli_timeout_seconds,
                max_output_bytes=2048,
            )
            if login_result.timed_out or login_result.exit_code != 0:
                raise ProviderFailure("AUTHENTICATION_FAILED")
            subscription = str(profile.scope["subscription_id"])
            select_result = self._process.run(
                executable,
                ["account", "set", "--subscription", subscription],
                cwd=home,
                environment=environment,
                timeout_seconds=profile.cli_timeout_seconds,
                max_output_bytes=2048,
            )
            if select_result.timed_out or select_result.exit_code != 0:
                raise ProviderFailure("AUTHENTICATION_FAILED")
        except (ProcessConfigurationError, FileNotFoundError) as error:
            raise ProviderFailure("CLI_UNAVAILABLE") from error

    def _cli_home(self, profile: CloudProfile) -> Path:
        """Return a profile-specific isolated CLI configuration directory."""

        name = uuid.uuid5(uuid.NAMESPACE_URL, profile.profile_ref).hex
        root = self._settings.state_dir / "cli"
        root.mkdir(mode=0o700, exist_ok=True)
        home = root / name
        home.mkdir(mode=0o700, exist_ok=True)
        return home

    def _gcp_credential_path(self, profile: CloudProfile) -> str:
        """Resolve only the host-mounted external-account configuration path."""

        reference = profile.credential_config_ref
        path = self._settings.secret_value(reference or "")
        if not path or not Path(path).is_absolute():
            raise CredentialMissingError("GCP WIF credential configuration is unavailable")
        return path

    def _azure_token(self, profile: CloudProfile) -> tuple[str, str | None]:
        """Acquire an Azure Resource Manager token through Managed Identity."""

        try:
            from azure.core.exceptions import (
                ClientAuthenticationError,
                ServiceRequestError,
                ServiceResponseError,
            )
            from azure.identity import CredentialUnavailableError, ManagedIdentityCredential
        except Exception as error:
            raise ProviderFailure("PROVIDER_ERROR") from error
        try:
            credential = self._credentials.get(profile.profile_ref)
            if credential is None:
                credential = ManagedIdentityCredential(client_id=profile.client_id)
                self._credentials[profile.profile_ref] = credential
            token = credential.get_token("https://management.azure.com/.default")
        except (ServiceRequestError, ServiceResponseError) as error:
            raise ProviderFailure("PROVIDER_UNAVAILABLE", retryable=True) from error
        except CredentialUnavailableError as error:
            raise ProviderFailure("CREDENTIAL_PROVIDER_UNAVAILABLE", retryable=True) from error
        except ClientAuthenticationError as error:
            status_code = _exception_status_code(error)
            if status_code == 403:
                raise ProviderFailure("SCOPE_DENIED") from error
            if status_code in {408, 429} or (status_code is not None and status_code >= 500):
                raise ProviderFailure("PROVIDER_UNAVAILABLE", retryable=True) from error
            raise ProviderFailure("AUTHENTICATION_FAILED") from error
        except Exception as error:
            raise ProviderFailure("PROVIDER_ERROR") from error
        return token.token, datetime.fromtimestamp(token.expires_on, UTC).isoformat().replace(
            "+00:00", "Z"
        )

    def _gcp_token(self, profile: CloudProfile) -> tuple[str, str | None]:
        """Load host-configured WIF credentials and obtain a short-lived token."""

        path = self._gcp_credential_path(profile)
        try:
            from google.auth.exceptions import RefreshError, TransportError
        except Exception as error:
            raise ProviderFailure("PROVIDER_ERROR") from error
        try:
            cached = self._credentials.get(profile.profile_ref)
            if cached is None:
                import google.auth

                credentials, _ = google.auth.load_credentials_from_file(path, scopes=[GCP_SCOPE])
                self._credentials[profile.profile_ref] = credentials
            else:
                credentials = cached
            if not getattr(credentials, "valid", False):
                from google.auth.transport.requests import Request

                credentials.refresh(Request())
        except Exception as error:
            if isinstance(error, TransportError) or _exception_caused_by(error, TransportError):
                raise ProviderFailure("PROVIDER_UNAVAILABLE", retryable=True) from error
            if isinstance(error, RefreshError):
                if error.retryable:
                    raise ProviderFailure("PROVIDER_UNAVAILABLE", retryable=True) from error
                if _google_error_indicates_permission_denied(error):
                    raise ProviderFailure("SCOPE_DENIED") from error
                raise ProviderFailure("AUTHENTICATION_FAILED") from error
            if isinstance(error, OSError):
                raise ProviderFailure("CREDENTIAL_PROVIDER_UNAVAILABLE") from error
            raise ProviderFailure("PROVIDER_ERROR") from error
        expires = getattr(credentials, "expiry", None)
        expiry = expires.astimezone(UTC).isoformat().replace("+00:00", "Z") if expires else None
        token = getattr(credentials, "token", None)
        if not isinstance(token, str) or not token:
            raise ProviderFailure("AUTHENTICATION_FAILED")
        return token, expiry

    def _runpod_token(self, profile: CloudProfile) -> str:
        """Read a dedicated host-injected Runpod key without returning it to tools."""

        token = self._settings.secret_value(profile.api_key_ref or "")
        if not token:
            raise CredentialMissingError("Runpod API key is missing")
        if len(token.encode("utf-8")) > 4096:
            raise ProviderFailure("INVALID_CREDENTIAL_CONFIGURATION")
        return token

    def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        allow_empty: bool = False,
    ) -> HttpResult:
        """Call an HTTPS URL already constructed from fixed vendor hosts and IDs."""

        split = urlsplit(url)
        expected_hosts = {AZURE_HOST, GCP_HOST, RUNPOD_HOST}
        if (
            split.scheme != "https"
            or split.hostname not in expected_hosts
            or split.username
            or split.password
        ):
            raise ProviderFailure("INVALID_VENDOR_ENDPOINT")
        headers = {"Authorization": f"Bearer {token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            with self._http.stream(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=self._settings.timeout_seconds,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise ProviderFailure("PROVIDER_REDIRECT_REJECTED")
                if not 200 <= response.status_code < 300:
                    if response.status_code >= 400:
                        self._raise_http_error(response.status_code, method)
                    raise ProviderFailure("PROVIDER_REJECTED")
                data = bytearray()
                for chunk in response.iter_bytes(8192):
                    if len(data) + len(chunk) > _MAX_HTTP_RESPONSE_BYTES:
                        raise ProviderFailure("RESPONSE_TOO_LARGE")
                    data.extend(chunk)
                if not data and allow_empty:
                    body: Any = {}
                elif not data:
                    body = {}
                else:
                    try:
                        body = json.loads(data)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        if allow_empty:
                            body = {}
                        else:
                            raise ProviderFailure("INVALID_PROVIDER_RESPONSE") from None
                return HttpResult(response.status_code, response.headers, body)
        except ProviderFailure:
            raise
        except httpx.TimeoutException as error:
            raise ProviderFailure(
                "PROVIDER_TIMEOUT",
                retryable=method.upper() == "GET",
                ambiguous=method.upper() != "GET",
            ) from error
        except httpx.HTTPError as error:
            raise ProviderFailure(
                "PROVIDER_UNAVAILABLE",
                retryable=method.upper() == "GET",
                ambiguous=method.upper() != "GET",
            ) from error

    @staticmethod
    def _raise_http_error(status_code: int, method: str) -> None:
        """Map vendor status codes without including response text or identifiers."""

        if status_code == 401:
            raise ProviderFailure("AUTHENTICATION_FAILED")
        if status_code == 403:
            raise ProviderFailure("SCOPE_DENIED")
        if status_code == 404:
            raise ProviderFailure("RESOURCE_NOT_FOUND")
        if status_code == 429:
            raise ProviderFailure("RATE_LIMITED", retryable=True)
        if status_code >= 500:
            raise ProviderFailure(
                "PROVIDER_ERROR",
                retryable=method.upper() == "GET",
                ambiguous=method.upper() != "GET",
            )
        raise ProviderFailure("PROVIDER_REJECTED")

    def _new_operation_ref(
        self,
        profile: CloudProfile,
        tool_id: str,
        target: object,
        request_id: str,
        *,
        vendor_operation: Mapping[str, Any] | None = None,
    ) -> str:
        """Create a restart-stable operation token from signed request facts."""

        payload: dict[str, Any] = {
            "profile_ref": profile.profile_ref,
            "provider": profile.provider,
            "operation": tool_id.removeprefix("cloud_resource_"),
            "target": dict(target) if isinstance(target, Mapping) else {},
            "request_id": request_id,
            "created_at": _now(),
        }
        if vendor_operation is not None:
            payload["vendor_operation"] = dict(vendor_operation)
        return create_operation_ref(payload, _verification_key(self._settings))


_WRITE_TOOLS = {
    "cloud_resource_create",
    "cloud_resource_deploy",
    "cloud_resource_start",
    "cloud_resource_stop",
    "cloud_resource_restart",
    "cloud_resource_update",
    "cloud_resource_delete",
    "cloud_operation_cancel",
    "cloud_vendor_cli",
}


def _require_keys(
    arguments: Mapping[str, Any], required: set[str], optional: set[str] = frozenset()
) -> None:
    """Reject unknown/missing argument keys before any provider or process work."""

    if not required <= set(arguments) or set(arguments) - required - optional:
        raise ProviderFailure("INVALID_ARGUMENTS")


def _flag_values(argv: Sequence[str], flags: set[str]) -> list[str]:
    """Extract values for exact CLI flags without interpreting a shell command."""

    values: list[str] = []
    normalized_flags = {flag.casefold() for flag in flags}
    for index, argument in enumerate(argv):
        option, separator, inline_value = argument.partition("=")
        option = option.casefold()
        for flag in normalized_flags:
            if option != flag:
                continue
            if separator:
                values.append(inline_value)
                break
            if argument.casefold() == flag:
                if index + 1 >= len(argv):
                    raise ProviderFailure("INVALID_ARGUMENTS")
                values.append(argv[index + 1])
                break
    return values


def _validate_flag_values(
    argv: Sequence[str],
    flags: set[str],
    allowed_values: set[str],
    *,
    case_sensitive: bool = False,
) -> None:
    """Reject explicit scope flags that escape their host-bound profile values."""

    values = _flag_values(argv, flags)
    if case_sensitive:
        escaped = any(value not in allowed_values for value in values)
    else:
        normalized_allowed = {value.casefold() for value in allowed_values}
        escaped = any(value.casefold() not in normalized_allowed for value in values)
    if escaped:
        raise ProviderFailure("SCOPE_DENIED")


def _process_command_state(result: ProcessResult) -> str:
    """Describe only the local CLI process outcome, never the remote resource."""

    if result.cancelled:
        return "COMMAND_CANCELLED"
    if result.timed_out or result.exit_code is None:
        return "COMMAND_INTERRUPTED"
    return "COMMAND_COMPLETED" if result.exit_code == 0 else "COMMAND_FAILED"


def _cli_requests_async(argv: Sequence[str]) -> bool:
    """Keep vendor no-wait/async options intact while marking remote state unverified."""

    return any(
        argument.casefold() in {"--no-wait", "--async", "--no-wait=true", "--async=true"}
        for argument in argv
    )


def _cli_command_is_read_only(provider: str, argv: Sequence[str]) -> bool:
    """Recognize common read verbs conservatively; unknown commands may mutate."""

    if provider == "runpod":
        if len(argv) < 2:
            return False
        command = tuple(item.casefold() for item in argv[:2])
        return command in {
            ("pod", "get"),
            ("pod", "list"),
            ("gpu", "list"),
            ("template", "list"),
            ("template", "search"),
            ("datacenter", "list"),
        }
    if not argv:
        return False
    normalized = [item.casefold() for item in argv[:8]]
    if provider == "azure" and (normalized == ["version"] or normalized[:2] == ["account", "show"]):
        return True
    for token in normalized:
        if token.startswith("-"):
            break
        if token in _CLI_WRITE_VERBS or any(
            token.startswith(f"{verb}-") for verb in _CLI_WRITE_VERBS
        ):
            return False
        if token in _CLI_READ_VERBS or any(
            token.startswith(f"{verb}-") for verb in _CLI_READ_VERBS
        ):
            return True
    return False


def _parse_cli_json(stdout: str) -> Mapping[str, Any] | None:
    """Parse bounded CLI JSON internally while rejecting duplicate object keys."""

    if not stdout or len(stdout.encode("utf-8")) > 8192:
        return None
    try:
        value = json.loads(stdout, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys before extracting a native operation reference."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate CLI JSON property")
        result[key] = value
    return result


def _gcp_cli_operation_zone(profile: CloudProfile, value: object) -> str | None:
    """Extract one configured GCE zone from a CLI operation document."""

    if not isinstance(value, str):
        return None
    if value in profile.scope["zones"]:
        return value
    try:
        split = urlsplit(value)
    except ValueError:
        return None
    if (
        split.scheme != "https"
        or split.hostname not in {"compute.googleapis.com", "www.googleapis.com"}
        or split.port not in (None, 443)
        or split.username is not None
        or split.password is not None
    ):
        return None
    match = re.fullmatch(
        rf"/compute/v1/projects/{re.escape(str(profile.scope['project_id']))}/zones/(?P<zone>[a-z]+-[a-z0-9]+-[a-z])(?:/operations/[A-Za-z0-9._-]+)?",
        split.path,
    )
    if match is None:
        return None
    zone = match.group("zone")
    return zone if zone in profile.scope["zones"] else None


def _validate_azure_cli_url(value: str, profile: CloudProfile) -> Any:
    """Require Azure CLI REST URLs to use HTTPS on the pinned ARM authority."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ProviderFailure("SCOPE_DENIED") from error
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname != AZURE_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ProviderFailure("SCOPE_DENIED")
    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or any(part in {".", ".."} for part in decoded_path.split("/")):
        raise ProviderFailure("SCOPE_DENIED")
    subscription_prefix = f"/subscriptions/{profile.scope['subscription_id']}/"
    if not decoded_path.casefold().startswith(subscription_prefix.casefold()):
        raise ProviderFailure("SCOPE_DENIED")
    return parsed


def _contains_tokens(argv: Sequence[str], tokens: tuple[str, ...]) -> bool:
    """Find a credential-command family even when global flags lead the argv."""

    normalized = [value.casefold() for value in argv]
    length = len(tokens)
    return any(
        tuple(normalized[index : index + length]) == tokens
        for index in range(max(0, len(normalized) - length + 1))
    )


def _object(value: object, field: str) -> Mapping[str, Any]:
    """Require one JSON object and cap its canonical encoded size."""

    if not isinstance(value, Mapping) or len(canonical_json(dict(value))) > 24_000:
        raise ProviderFailure("INVALID_ARGUMENTS")
    return value


def _scope_refs(profile: CloudProfile) -> list[str]:
    """Return human-safe configured resource scope identifiers."""

    if profile.provider == "azure":
        subscription = str(profile.scope["subscription_id"])
        return [f"azure:{subscription}/{group}" for group in profile.scope["resource_groups"]]
    if profile.provider == "gcp":
        project = str(profile.scope["project_id"])
        return [f"gcp:{project}/{zone}" for zone in profile.scope["zones"]]
    return [f"runpod:{profile.profile_ref}"]


def _azure_list_target(profile: CloudProfile, target: object) -> str:
    """Require a resource-group selector contained in the Azure profile."""

    if not isinstance(target, Mapping) or set(target) != {"resource_group"}:
        raise ProviderFailure("INVALID_ARGUMENTS")
    group = target.get("resource_group")
    if not isinstance(group, str) or group not in profile.scope["resource_groups"]:
        raise ProviderFailure("SCOPE_DENIED")
    return group


def _azure_resource_target(profile: CloudProfile, target: object) -> str:
    """Resolve one ARM resource ID constrained to the configured subscription/group."""

    if not isinstance(target, Mapping) or set(target) != {"resource_id"}:
        raise ProviderFailure("INVALID_ARGUMENTS")
    resource_id = target.get("resource_id")
    if not isinstance(resource_id, str) or not _AZURE_RESOURCE_ID.fullmatch(resource_id):
        raise ProviderFailure("INVALID_ARGUMENTS")
    match = _AZURE_RESOURCE_ID.fullmatch(resource_id)
    assert match is not None
    if match.group("subscription").casefold() != str(profile.scope["subscription_id"]).casefold():
        raise ProviderFailure("SCOPE_DENIED")
    if match.group("group").casefold() not in {
        str(value).casefold() for value in profile.scope["resource_groups"]
    }:
        raise ProviderFailure("SCOPE_DENIED")
    if any(segment in {".", ".."} for segment in resource_id.split("/")) or any(
        char in resource_id for char in ("%", "?", "#", "\\")
    ):
        raise ProviderFailure("INVALID_ARGUMENTS")
    return resource_id


def _azure_deployment_target(profile: CloudProfile, target: object) -> tuple[str, str]:
    """Resolve a template deployment name inside a bound resource group."""

    if not isinstance(target, Mapping) or set(target) != {"resource_group", "deployment_name"}:
        raise ProviderFailure("INVALID_ARGUMENTS")
    group, name = target.get("resource_group"), target.get("deployment_name")
    if not isinstance(group, str) or group not in profile.scope["resource_groups"]:
        raise ProviderFailure("SCOPE_DENIED")
    if not isinstance(name, str) or not _RESOURCE_NAME.fullmatch(name):
        raise ProviderFailure("INVALID_ARGUMENTS")
    return group, name


def _azure_deployment_body(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an ARM template and construct one resource-group deployment body."""

    if set(parameters) - {"template", "parameters", "mode"}:
        raise ProviderFailure("INVALID_ARGUMENTS")
    template = parameters.get("template")
    template_parameters = parameters.get("parameters", {})
    mode = parameters.get("mode", "Incremental")
    if not isinstance(template, Mapping) or not isinstance(template_parameters, Mapping):
        raise ProviderFailure("INVALID_ARGUMENTS")
    if mode not in {"Incremental", "Complete"}:
        raise ProviderFailure("INVALID_ARGUMENTS")
    return {
        "properties": {
            "mode": mode,
            "template": dict(template),
            "parameters": dict(template_parameters),
        }
    }


def _azure_path(resource_id: str) -> str:
    """Quote an already validated ARM ID without changing path separators."""

    return "/" + quote(resource_id.lstrip("/"), safe="/-_()")


def _azure_resource_api_version(profile: CloudProfile, resource_id: str) -> str:
    """Select a host-configured API version for the exact ARM resource type."""

    match = _AZURE_RESOURCE_ID.fullmatch(resource_id)
    if match is None:
        raise ProviderFailure("INVALID_ARGUMENTS")
    provider = match.group("provider")
    segments = match.group("types").split("/")
    resource_types = segments[0::2]
    type_name = provider + "/" + "/".join(resource_types)
    mapped = profile.scope["resource_api_versions"].get(type_name.casefold())
    if isinstance(mapped, str):
        return mapped
    if type_name.casefold() == "microsoft.compute/virtualmachines":
        return str(profile.scope["vm_api_version"])
    raise ProviderFailure("API_VERSION_NOT_CONFIGURED")


def _is_azure_deployment_resource_id(resource_id: str) -> bool:
    """Identify deployment resources that require the separately scoped deploy route."""

    match = _AZURE_RESOURCE_ID.fullmatch(resource_id)
    return (
        match is not None
        and match.group("provider").casefold() == "microsoft.resources"
        and match.group("types").split("/", 1)[0].casefold() == "deployments"
    )


def _require_azure_vm_id(resource_id: str) -> None:
    """Permit start/stop only for virtual machines in the fixed ARM shape."""

    match = _AZURE_RESOURCE_ID.fullmatch(resource_id)
    if match is None or match.group("provider").casefold() != "microsoft.compute":
        raise ProviderFailure("UNSUPPORTED_OPERATION")
    if match.group("types").casefold() != "virtualmachines":
        raise ProviderFailure("UNSUPPORTED_OPERATION")


def _azure_summary(value: object) -> dict[str, Any]:
    """Return only non-secret ARM resource fields used for operator decisions."""

    if not isinstance(value, Mapping):
        return {}
    properties = value.get("properties") if isinstance(value.get("properties"), Mapping) else {}
    return {
        key: value[key]
        for key in ("id", "name", "type", "location")
        if isinstance(value.get(key), (str, int, float, bool))
    } | {"provisioning_state": properties.get("provisioningState", "UNKNOWN")}


def _azure_provisioning_state(value: Mapping[str, Any]) -> str:
    """Translate an ARM provisioning state into provider-neutral operation state."""

    properties = value.get("properties") if isinstance(value.get("properties"), Mapping) else {}
    state = properties.get("provisioningState", value.get("status"))
    if not isinstance(state, str):
        return "UNKNOWN_RESULT"
    normalized = state.upper()
    if normalized in {"SUCCEEDED", "SUCCESSFUL", "COMPLETED"}:
        return "SUCCEEDED"
    if normalized in {"FAILED", "CANCELED", "CANCELLED"}:
        return "FAILED"
    return "PENDING"


def _azure_actual_state(value: Mapping[str, Any]) -> str:
    """Read the ARM provisioning state without asserting application readiness."""

    properties = value.get("properties") if isinstance(value.get("properties"), Mapping) else {}
    return str(properties.get("provisioningState", "UNKNOWN"))


def _azure_operation_state(value: object) -> str:
    """Normalize Azure-AsyncOperation response status."""

    if not isinstance(value, Mapping):
        return "UNKNOWN_RESULT"
    state = value.get("status")
    if not isinstance(state, str):
        return "UNKNOWN_RESULT"
    if state.casefold() in {"succeeded", "success"}:
        return "SUCCEEDED"
    if state.casefold() in {"failed", "canceled", "cancelled"}:
        return "FAILED"
    return "PENDING"


def _azure_activity_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only timestamp, operation, and status from Activity Log events."""

    operation_name = (
        value.get("operationName") if isinstance(value.get("operationName"), Mapping) else {}
    )
    status = value.get("status") if isinstance(value.get("status"), Mapping) else {}
    return {
        "event_timestamp": value.get("eventTimestamp"),
        "operation": operation_name.get("value"),
        "status": status.get("value"),
    }


def _validated_azure_operation_path(profile: CloudProfile, value: object) -> str:
    """Reject operation paths outside the configured Azure subscription."""

    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or any(ch in value for ch in "?#\\%")
    ):
        raise ProviderFailure("OPERATION_NOT_FOUND")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ProviderFailure("OPERATION_NOT_FOUND")
    prefix = f"/subscriptions/{profile.scope['subscription_id']}/"
    if not value.casefold().startswith(prefix.casefold()):
        raise ProviderFailure("OPERATION_NOT_FOUND")
    return value


def _looks_like_api_version(value: str) -> bool:
    """Validate an API version embedded in a vendor operation response."""

    return re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}(?:-preview)?", value) is not None


def _gcp_list_target(profile: CloudProfile, target: object) -> str:
    """Require one configured Compute Engine zone for instance enumeration."""

    if not isinstance(target, Mapping) or set(target) != {"zone"}:
        raise ProviderFailure("INVALID_ARGUMENTS")
    zone = target.get("zone")
    if not isinstance(zone, str) or zone not in profile.scope["zones"]:
        raise ProviderFailure("SCOPE_DENIED")
    return zone


def _gcp_instance_target(profile: CloudProfile, target: object) -> tuple[str, str]:
    """Validate one GCE instance name and zone against the profile scope."""

    if not isinstance(target, Mapping) or set(target) != {"zone", "instance_name"}:
        raise ProviderFailure("INVALID_ARGUMENTS")
    zone, name = target.get("zone"), target.get("instance_name")
    if not isinstance(zone, str) or zone not in profile.scope["zones"]:
        raise ProviderFailure("SCOPE_DENIED")
    if (
        not isinstance(name, str)
        or not _GCP_INSTANCE_NAME.fullmatch(name)
        or not _gcp_name_in_scope(profile, name)
    ):
        raise ProviderFailure("SCOPE_DENIED")
    return zone, name


def _gcp_name_in_scope(profile: CloudProfile, name: str) -> bool:
    """Check an instance name against the fixed prefix set."""

    return any(name.startswith(prefix) for prefix in profile.scope["instance_name_prefixes"])


def _gcp_page_params(cursor: object) -> dict[str, str]:
    """Forward only an opaque vendor page token, never a caller-supplied URL."""

    if cursor is None:
        return {}
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
        raise ProviderFailure("INVALID_ARGUMENTS")
    return {"pageToken": cursor}


def _ensure_gcp_create_body(profile: CloudProfile, name: str, body: Mapping[str, Any]) -> None:
    """Reject project/zone/name overrides and keep creation within allowed names."""

    if "name" in body or "project" in body or "zone" in body or not body:
        raise ProviderFailure("INVALID_ARGUMENTS")
    if not _gcp_name_in_scope(profile, name):
        raise ProviderFailure("SCOPE_DENIED")


def _gcp_summary(value: object) -> dict[str, Any]:
    """Return only safe Compute Engine state fields."""

    if not isinstance(value, Mapping):
        return {}
    return {
        key: value[key]
        for key in ("id", "name", "zone", "machineType", "status", "creationTimestamp")
        if isinstance(value.get(key), (str, int, float, bool))
    }


def _response_mapping_list(
    body: object, field_name: str, *, allow_missing: bool = False
) -> list[Mapping[str, Any]]:
    """Require a provider response collection instead of hiding malformed data as empty."""

    if not isinstance(body, Mapping) or "error" in body:
        raise ProviderFailure("INVALID_PROVIDER_RESPONSE")
    if field_name not in body and allow_missing:
        return []
    value = body.get(field_name)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ProviderFailure("INVALID_PROVIDER_RESPONSE")
    return value


def _gcp_operation_state(value: Mapping[str, Any]) -> str:
    """Translate a Compute Engine operation status to the common task state."""

    status = value.get("status")
    if status == "DONE":
        return "FAILED" if isinstance(value.get("error"), Mapping) else "SUCCEEDED"
    if status in {"PENDING", "RUNNING"}:
        return "PENDING"
    return "UNKNOWN_RESULT"


def _gcp_operation_diagnostics(value: object) -> list[dict[str, Any]]:
    """Return bounded, vendor-code-only operation diagnostics."""

    if not isinstance(value, Mapping):
        return []
    error = value.get("error")
    details = error.get("errors", []) if isinstance(error, Mapping) else []
    result = []
    for item in details[:10] if isinstance(details, list) else []:
        if isinstance(item, Mapping):
            result.append(
                {
                    "code": _bounded_scalar(item.get("code")),
                    "location": _bounded_scalar(item.get("location")),
                }
            )
    return result


def _runpod_list_target(target: object) -> None:
    """Require the single Runpod Pods collection scope for list/CLI calls."""

    if (
        not isinstance(target, Mapping)
        or set(target) != {"scope_ref"}
        or target.get("scope_ref") != "pods"
    ):
        raise ProviderFailure("INVALID_ARGUMENTS")


def _runpod_id_target(profile: CloudProfile, target: object) -> str:
    """Resolve one Pod ID present in the profile's explicit ID/prefix set."""

    if not isinstance(target, Mapping) or set(target) not in ({"pod_id"}, {"pod_id", "pod_name"}):
        raise ProviderFailure("INVALID_ARGUMENTS")
    pod_id = target.get("pod_id")
    if not isinstance(pod_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", pod_id):
        raise ProviderFailure("INVALID_ARGUMENTS")
    scope = profile.scope
    name = target.get("pod_name")
    if isinstance(name, str):
        _runpod_create_target(profile, {"pod_name": name})
    elif pod_id not in scope["pod_ids"] and not any(
        pod_id.startswith(prefix) for prefix in scope["pod_id_prefixes"]
    ):
        raise ProviderFailure("SCOPE_DENIED")
    return pod_id


def _runpod_create_target(profile: CloudProfile, target: object) -> str:
    """Validate a new Pod name under the profile's allowed name prefixes."""

    if not isinstance(target, Mapping) or set(target) != {"pod_name"}:
        raise ProviderFailure("INVALID_ARGUMENTS")
    name = target.get("pod_name")
    if not isinstance(name, str) or not _RESOURCE_NAME.fullmatch(name):
        raise ProviderFailure("INVALID_ARGUMENTS")
    if not any(name.startswith(prefix) for prefix in profile.scope["pod_name_prefixes"]):
        raise ProviderFailure("SCOPE_DENIED")
    return name


def _runpod_create_body(
    profile: CloudProfile, name: str, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    """Construct a strict Runpod Pods v2 body with explicit cost/resource bounds."""

    allowed = {
        "image",
        "gpu",
        "disk",
        "mounts",
        "ports",
        "cloud",
        "dataCenterIds",
        "templateId",
        "args",
        "startSsh",
    }
    if set(parameters) - allowed:
        raise ProviderFailure("INVALID_ARGUMENTS")
    image = parameters.get("image")
    template_id = parameters.get("templateId")
    if not isinstance(image, str) and template_id is None:
        raise ProviderFailure("INVALID_ARGUMENTS")
    if image is not None and (not isinstance(image, str) or not 1 <= len(image) <= 512):
        raise ProviderFailure("INVALID_ARGUMENTS")
    if template_id is not None and (
        not isinstance(template_id, str) or not _RESOURCE_NAME.fullmatch(template_id)
    ):
        raise ProviderFailure("INVALID_ARGUMENTS")
    gpu = parameters.get("gpu")
    if not isinstance(gpu, Mapping) or set(gpu) - {"id", "count"}:
        raise ProviderFailure("INVALID_ARGUMENTS")
    gpu_id, gpu_count = gpu.get("id"), gpu.get("count", 1)
    if not isinstance(gpu_id, str) or gpu_id not in profile.scope["gpu_type_ids"]:
        raise ProviderFailure("SCOPE_DENIED")
    if (
        isinstance(gpu_count, bool)
        or not isinstance(gpu_count, int)
        or not 1 <= gpu_count <= profile.scope["max_gpu_count"]
    ):
        raise ProviderFailure("SCOPE_DENIED")
    disk = parameters.get("disk", 20)
    if (
        isinstance(disk, bool)
        or not isinstance(disk, int)
        or not 1 <= disk <= profile.scope["max_container_disk_gb"]
    ):
        raise ProviderFailure("SCOPE_DENIED")
    data_centers = parameters.get("dataCenterIds", list(profile.scope["data_center_ids"]))
    if (
        not isinstance(data_centers, list)
        or not data_centers
        or any(
            not isinstance(item, str) or item not in profile.scope["data_center_ids"]
            for item in data_centers
        )
    ):
        raise ProviderFailure("SCOPE_DENIED")
    body: dict[str, Any] = {
        "name": name,
        "gpu": {"id": gpu_id, "count": gpu_count},
        "disk": disk,
        "dataCenterIds": data_centers,
    }
    if image is not None:
        body["image"] = image
    if template_id is not None:
        body["templateId"] = template_id
    ports = parameters.get("ports")
    if ports is not None:
        if (
            not isinstance(ports, list)
            or len(ports) > 32
            or any(not isinstance(port, str) or len(port) > 32 for port in ports)
        ):
            raise ProviderFailure("INVALID_ARGUMENTS")
        body["ports"] = ports
    mounts = parameters.get("mounts")
    if mounts is not None:
        if not isinstance(mounts, Mapping) or set(mounts) - {"persistent"}:
            raise ProviderFailure("INVALID_ARGUMENTS")
        persistent = mounts.get("persistent")
        if not isinstance(persistent, Mapping) or set(persistent) - {"size", "path"}:
            raise ProviderFailure("INVALID_ARGUMENTS")
        volume_size = persistent.get("size", 0)
        if (
            isinstance(volume_size, bool)
            or not isinstance(volume_size, int)
            or not 0 <= volume_size <= profile.scope["max_volume_disk_gb"]
        ):
            raise ProviderFailure("SCOPE_DENIED")
        path = persistent.get("path", "/workspace")
        if not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/"):
            raise ProviderFailure("INVALID_ARGUMENTS")
        body["mounts"] = {"persistent": {"size": volume_size, "path": path}}
    for key in ("cloud", "args", "startSsh"):
        if key in parameters:
            value = parameters[key]
            if key == "cloud" and value not in {"SECURE", "COMMUNITY", "ALL"}:
                raise ProviderFailure("INVALID_ARGUMENTS")
            if key == "args" and (not isinstance(value, str) or len(value) > 4096):
                raise ProviderFailure("INVALID_ARGUMENTS")
            if key == "startSsh" and not isinstance(value, bool):
                raise ProviderFailure("INVALID_ARGUMENTS")
            body[key] = value
    return body


def _runpod_update_body(profile: CloudProfile, parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Allow only bounded, independently verifiable Pod configuration updates."""

    allowed = {"name", "image", "disk", "mounts", "ports", "args"}
    if not parameters or set(parameters) - allowed:
        raise ProviderFailure("INVALID_ARGUMENTS")
    body = dict(parameters)
    if "name" in body:
        name = body["name"]
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 128
            or not any(name.startswith(prefix) for prefix in profile.scope["pod_name_prefixes"])
        ):
            raise ProviderFailure("SCOPE_DENIED")
    if "disk" in body:
        disk = body["disk"]
        if (
            isinstance(disk, bool)
            or not isinstance(disk, int)
            or not 1 <= disk <= profile.scope["max_container_disk_gb"]
        ):
            raise ProviderFailure("SCOPE_DENIED")
    if "image" in body and (
        not isinstance(body["image"], str) or not 1 <= len(body["image"]) <= 512
    ):
        raise ProviderFailure("INVALID_ARGUMENTS")
    if "args" in body and (not isinstance(body["args"], str) or len(body["args"]) > 4096):
        raise ProviderFailure("INVALID_ARGUMENTS")
    if "ports" in body and (
        not isinstance(body["ports"], list)
        or len(body["ports"]) > 32
        or any(
            not isinstance(port, str)
            or not 1 <= len(port) <= 32
            or any(ord(character) < 32 for character in port)
            for port in body["ports"]
        )
    ):
        raise ProviderFailure("INVALID_ARGUMENTS")
    if "mounts" in body:
        mounts = body["mounts"]
        if not isinstance(mounts, Mapping) or set(mounts) != {"persistent"}:
            raise ProviderFailure("INVALID_ARGUMENTS")
        persistent = mounts.get("persistent")
        if not isinstance(persistent, Mapping) or set(persistent) != {"size", "path"}:
            raise ProviderFailure("INVALID_ARGUMENTS")
        size = persistent["size"]
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= profile.scope["max_volume_disk_gb"]
        ):
            raise ProviderFailure("SCOPE_DENIED")
        path = persistent["path"]
        if (
            not isinstance(path, str)
            or not 1 <= len(path) <= 512
            or not path.startswith("/")
            or any(part in {".", ".."} for part in path.split("/"))
            or any(ord(character) < 32 for character in path)
        ):
            raise ProviderFailure("INVALID_ARGUMENTS")
    return body


def _runpod_update_receipt(configuration: Mapping[str, Any]) -> dict[str, Any]:
    """Build a durable comparison receipt without copying sensitive argument text."""

    return {
        "kind": "runpod_pod_update",
        "expected_configuration": {
            key: value for key, value in configuration.items() if key != "args"
        },
        "expected_fields": sorted(configuration),
        "expected_configuration_sha256": payload_sha256(configuration),
    }


def _runpod_update_receipt_valid(receipt: object) -> bool:
    """Validate the safe configuration projection and digest stored in a signed receipt."""

    if not isinstance(receipt, Mapping) or receipt.get("kind") != "runpod_pod_update":
        return False
    expected_configuration = receipt.get("expected_configuration")
    expected_fields = receipt.get("expected_fields")
    expected_digest = receipt.get("expected_configuration_sha256")
    return (
        isinstance(expected_configuration, Mapping)
        and isinstance(expected_fields, list)
        and bool(expected_fields)
        and all(
            isinstance(field, str) and field in {"name", "image", "disk", "mounts", "ports", "args"}
            for field in expected_fields
        )
        and len(set(expected_fields)) == len(expected_fields)
        and set(expected_configuration) == set(expected_fields) - {"args"}
        and isinstance(expected_digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", expected_digest) is not None
    )


def _runpod_update_matches(pod: Mapping[str, Any], receipt: object) -> bool:
    """Compare requested fields with a fresh Pod read without exposing sensitive args."""

    if not _runpod_update_receipt_valid(receipt):
        return False
    assert isinstance(receipt, Mapping)
    expected_fields = receipt["expected_fields"]
    expected_digest = receipt["expected_configuration_sha256"]
    observed = {field: pod.get(field) for field in expected_fields}
    return payload_sha256(observed) == expected_digest


def _runpod_configuration_snapshot(
    pod: Mapping[str, Any], expected_configuration: object
) -> dict[str, Any]:
    """Project safe mutable configuration fields from a Runpod Pod document."""

    if not isinstance(expected_configuration, Mapping):
        return {}
    return {key: pod.get(key) for key in expected_configuration if key != "args"}


def _runpod_log_records(payload: str, *, max_records: int) -> list[dict[str, Any]]:
    """Decode Runpod log event data while keeping only timestamp, source, and line."""

    try:
        decoded: Any = json.loads(payload)
    except json.JSONDecodeError:
        decoded = payload
    values = decoded if isinstance(decoded, list) else [decoded]
    records: list[dict[str, Any]] = []
    for value in values:
        if len(records) >= max_records:
            break
        if isinstance(value, Mapping):
            line = value.get("line")
            record: dict[str, Any] = {}
            timestamp = value.get("ts")
            source = value.get("source")
            if isinstance(timestamp, (str, int, float)) and not isinstance(timestamp, bool):
                record["ts"] = timestamp
            if isinstance(source, str):
                record["source"] = source
            record["line"] = line if isinstance(line, str) else ""
            records.append(record)
        elif isinstance(value, str):
            records.append({"line": value})
    return records


def _runpod_pod_in_scope(profile: CloudProfile, value: Mapping[str, Any]) -> bool:
    """Keep list and name-read results inside configured Pod IDs/name prefixes."""

    pod_id, name = value.get("id"), value.get("name")
    if isinstance(pod_id, str) and (
        pod_id in profile.scope["pod_ids"]
        or any(pod_id.startswith(prefix) for prefix in profile.scope["pod_id_prefixes"])
    ):
        return True
    return isinstance(name, str) and any(
        name.startswith(prefix) for prefix in profile.scope["pod_name_prefixes"]
    )


def _runpod_summary(value: object) -> dict[str, Any]:
    """Return safe Pod state and cost fields without environment variables or SSH data."""

    if not isinstance(value, Mapping):
        return {}
    keys = (
        "id",
        "name",
        "image",
        "status",
        "cost",
        "createdAt",
        "startedAt",
        "dataCenterId",
        "cloud",
    )
    result = {
        key: value[key] for key in keys if isinstance(value.get(key), (str, int, float, bool))
    }
    gpu = value.get("gpu")
    if isinstance(gpu, Mapping):
        result["gpu"] = {
            key: gpu[key]
            for key in ("id", "count", "vcpuCount", "memory")
            if isinstance(gpu.get(key), (str, int, float, bool))
        }
    return result


def _redacted_runpod_receipt(value: object) -> dict[str, Any]:
    """Reduce a Runpod mutation response to a non-secret Pod receipt."""

    return _runpod_summary(value)


def _vendor_state(profile: CloudProfile, body: object) -> str:
    """Normalize immediate vendor operation/resource state without guessing readiness."""

    if profile.provider == "azure" and isinstance(body, Mapping):
        return _azure_provisioning_state(body)
    if profile.provider == "gcp" and isinstance(body, Mapping) and "status" in body:
        return _gcp_operation_state(body)
    if profile.provider == "runpod" and isinstance(body, Mapping):
        return "SUCCEEDED" if body.get("id") else "UNKNOWN_RESULT"
    return "PENDING"


def _expected_state(provider: str, operation: str) -> str | None:
    """Return a vendor-neutral expected target state for supported operations."""

    states = {
        "azure": {
            "start": "RUNNING",
            "stop": "STOPPED",
            "delete": "ABSENT",
            "create": "SUCCEEDED",
            "deploy": "SUCCEEDED",
        },
        "gcp": {
            "start": "RUNNING",
            "stop": "TERMINATED",
            "delete": "ABSENT",
            "create": "RUNNING",
            "deploy": "RUNNING",
        },
        "runpod": {
            "start": "RUNNING",
            "stop": "EXITED",
            "restart": "RUNNING",
            "delete": "TERMINATED",
            "create": "RUNNING",
            "deploy": "RUNNING",
        },
    }
    return states.get(provider, {}).get(operation)


def _actual_state(provider: str, operation: str, body: object) -> str:
    """Return a bounded vendor state from an immediate mutation response."""

    if provider == "runpod" and isinstance(body, Mapping):
        return str(body.get("status", "UNKNOWN"))
    if provider == "gcp" and isinstance(body, Mapping):
        return str(body.get("status", "PENDING"))
    if provider == "azure" and isinstance(body, Mapping):
        return _azure_actual_state(body)
    return "UNKNOWN"


def _readiness(provider: str, operation: str, body: object) -> str:
    """Keep service readiness distinct from provider resource lifecycle state."""

    actual = _actual_state(provider, operation, body)
    return "NOT_READY" if actual in {"STOPPED", "TERMINATED", "ABSENT", "EXITED"} else "UNKNOWN"


def _resource_ref(profile: CloudProfile, target: Mapping[str, Any]) -> str:
    """Return a compact resource reference without credentials or response bodies."""

    if isinstance(target.get("resource_id"), str):
        return str(target["resource_id"])
    if isinstance(target.get("instance_name"), str):
        return f"{profile.scope['project_id']}/{target.get('zone')}/{target['instance_name']}"
    if isinstance(target.get("pod_id"), str):
        return str(target["pod_id"])
    if isinstance(target.get("pod_name"), str):
        return str(target["pod_name"])
    if isinstance(target.get("deployment_name"), str):
        return f"{target.get('resource_group')}/{target['deployment_name']}"
    return profile.profile_ref


def _bounded_scalar(value: object) -> str | int | float | bool | None:
    """Return only small JSON scalar diagnostics."""

    if (
        isinstance(value, (str, int, float, bool))
        and not isinstance(value, str)
        or isinstance(value, str)
        and len(value) <= 256
    ):
        return value
    return None


def _exception_caused_by(error: BaseException, exception_type: type[BaseException]) -> bool:
    """Check wrapped SDK causes without exposing provider exception text."""

    visited: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited:
        if isinstance(current, exception_type):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _exception_status_code(error: BaseException) -> int | None:
    """Read only a numeric HTTP status from a wrapped SDK exception."""

    visited: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited:
        status_code = getattr(current, "status_code", None)
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            return status_code
        response = getattr(current, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            return status_code
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _google_error_indicates_permission_denied(error: BaseException) -> bool:
    """Recognize safe structured IAM denial fields without inspecting error text."""

    def is_denial(value: object, depth: int = 0) -> bool:
        if depth > 3 or not isinstance(value, Mapping):
            return False
        for key in ("code", "status", "error"):
            item = value.get(key)
            if isinstance(item, int) and not isinstance(item, bool) and item == 403:
                return True
            if isinstance(item, str) and item.upper() in {
                "403",
                "ACCESS_DENIED",
                "FORBIDDEN",
                "INSUFFICIENT_PERMISSIONS",
                "PERMISSION_DENIED",
            }:
                return True
            if is_denial(item, depth + 1):
                return True
        return False

    return any(is_denial(argument) for argument in error.args)


def _now() -> str:
    """Return the current UTC timestamp in canonical RFC 3339 form."""

    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _verification_key(settings: ProviderSettings) -> bytes:
    """Return the binding's configured Exchange verification key for ref signing."""

    caller = next(iter(settings.trusted_callers.values()), None)
    if caller is None:
        raise ConfigurationError("trusted Exchange caller is unavailable")
    return caller.verification_key
