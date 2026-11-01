"""Production Account Center capability entrypoint.

This package owns external identity links and short credential leases. It never
acts as Platform identity authority or exposes token material to user methods.
中文：本插件只负责外部账号绑定与短时凭据租约。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cyrene_plugin_runtime.configuration import read_environment_settings

from .github import (
    GitHubCredentialPersistenceError,
    GitHubCredentialPolicyError,
    GitHubIdentityVerificationError,
    GitHubOAuthClient,
    pkce_s256_challenge,
)
from .github_app import GitHubAppClient, GitHubAppError
from .huggingface_auth import HuggingFaceAuthError, HuggingFaceIdentityClient
from .huggingface_oauth import (
    ALLOWED_OAUTH_SCOPES,
    HuggingFaceOAuthClient,
    HuggingFaceOAuthError,
    parse_device_oauth_seed,
)
from .microsoft_graph_oauth import (
    ALLOWED_GRAPH_SCOPES,
    AZURE_DEVOPS_RESOURCE_SCOPE,
    INITIAL_GRAPH_SCOPES,
    AzureDevOpsOAuthClient,
    MicrosoftGraphOAuthClient,
    MicrosoftGraphOAuthError,
)
from .models import AccountCenterError, Anchor, OAuthCredential, require_object
from .security import (
    canonical_json,
    decode_key,
    decrypt,
    encrypt,
    payload_digest,
    verify_context,
)
from .store import AccountStore

CAPABILITY_ID = "account.center.v1"
INTERFACE_VERSION = "1"
METHODS = (
    "begin_link",
    "complete_github_link",
    "observe_wecom_challenge",
    "confirm_wecom_link",
    "list_links",
    "get_authorization",
    "unlink",
    "create_credential_lease",
    "redeem_credential_lease",
    "resolve_linked_sender",
    "link_github_app_installation",
    "link_huggingface_profile",
    "rotate_huggingface_profile",
    "begin_huggingface_renewal",
    "poll_huggingface_renewal",
    "register_host_identity_profile",
    "list_provider_profiles",
    "unlink_provider_profile",
    "create_provider_credential_lease",
    "redeem_provider_credential_lease",
    "begin_microsoft_graph_link",
    "complete_microsoft_graph_link",
    "begin_microsoft_graph_scope_upgrade",
    "complete_microsoft_graph_scope_upgrade",
    "update_microsoft_graph_v2_scope",
    "begin_azure_devops_link",
    "complete_azure_devops_link",
)
PROVIDER_OPERATIONS = {
    "github.issue.get",
    "github.issue.comment.create",
    "github.issue.state.update",
}
PROVIDER_OPERATIONS_BY_PROVIDER = {
    "github": {
        "github.repository.list",
        "github.repository.get",
        "github.branches.list",
        "github.pull_requests.list",
        "github.pull_request.get",
        "github.commits.compare",
        "github.actions.runs.list",
        "github.ci.status",
        "github.issues.search",
        "github.branch.get",
        "github.file.get",
        "github.actions.jobs.list",
        "github.actions.job.logs.read",
        "github.branch.create",
        "github.branch.delete",
        "github.file.commit",
        "github.pull_request.draft.create",
        "github.pull_request.draft.find",
        "github.actions.rerun_failed",
        "github.vendor.cli.execute",
        *PROVIDER_OPERATIONS,
    },
    "huggingface": {
        "huggingface.models.list",
        "huggingface.datasets.list",
        "huggingface.spaces.list",
        "huggingface.repo.get",
        "huggingface.file.download",
        "huggingface.file.upload",
        "huggingface.vendor.cli.execute",
    },
    "microsoft_graph": {
        "microsoft_graph.planner.plan.get",
        "microsoft_graph.planner.plans.list",
        "microsoft_graph.planner.tasks.list",
        "microsoft_graph.planner.task.get",
        "microsoft_graph.planner.task.create",
        "microsoft_graph.planner.task.update",
        "microsoft_graph.sharepoint.site.get",
        "microsoft_graph.sharepoint.list.get",
        "microsoft_graph.sharepoint.list.items.delta",
        "microsoft_graph.todo.lists.list",
        "microsoft_graph.todo.tasks.list",
        "microsoft_graph.todo.task.get",
        "microsoft_graph.todo.task.create",
        "microsoft_graph.todo.task.update",
        "microsoft_graph.todo.task.delete",
        "microsoft_graph.calendar.calendars.list",
        "microsoft_graph.calendar.events.list",
        "microsoft_graph.calendar.event.get",
        "microsoft_graph.calendar.event.create",
        "microsoft_graph.calendar.event.update",
        "microsoft_graph.calendar.event.delete",
        "microsoft_graph.calendar.freebusy.get",
        "microsoft_graph.mail.folders.list",
        "microsoft_graph.mail.messages.list",
        "microsoft_graph.mail.message.get",
        "microsoft_graph.mail.drafts.list",
        "microsoft_graph.mail.draft.create",
        "microsoft_graph.mail.draft.update",
        "microsoft_graph.mail.draft.delete",
        "microsoft_graph.mail.message.send",
        "microsoft_graph.sharepoint.drives.list",
        "microsoft_graph.sharepoint.drive.items.list",
    },
    "azure_devops": {
        "azure_devops.observations.poll",
        "azure_devops.pipelines.list",
        "azure_devops.pipeline_runs.list",
        "azure_devops.pipeline_run.get",
        "azure_devops.deployments.list",
        "azure_devops.release_environment.get",
    },
}
CALLBACK_METHODS = {
    "complete_github_link",
    "complete_microsoft_graph_link",
    "complete_microsoft_graph_scope_upgrade",
    "complete_azure_devops_link",
    "observe_wecom_challenge",
}
EXCHANGE_ONLY_METHODS = {
    "begin_link",
    "confirm_wecom_link",
    "list_links",
    "get_authorization",
    "unlink",
    "create_credential_lease",
    "resolve_linked_sender",
    "link_github_app_installation",
    "link_huggingface_profile",
    "rotate_huggingface_profile",
    "begin_huggingface_renewal",
    "poll_huggingface_renewal",
    "register_host_identity_profile",
    "list_provider_profiles",
    "unlink_provider_profile",
    "create_provider_credential_lease",
    "begin_microsoft_graph_link",
    "begin_microsoft_graph_scope_upgrade",
    "update_microsoft_graph_v2_scope",
    "begin_azure_devops_link",
}
PUBLIC_METHODS = {
    "begin_link",
    "confirm_wecom_link",
    "list_links",
    "get_authorization",
    "unlink",
    "create_credential_lease",
    "link_github_app_installation",
    "link_huggingface_profile",
    "rotate_huggingface_profile",
    "register_host_identity_profile",
    "unlink_provider_profile",
    "begin_microsoft_graph_link",
    "begin_microsoft_graph_scope_upgrade",
    "update_microsoft_graph_v2_scope",
    "begin_azure_devops_link",
}
REQUEST_TYPE_PREFIX = "type.googleapis.com/cyrene.account.center.v1."
MICROSOFT_GRAPH_PROVIDER_CALLER_ID = "microsoft-graph-provider"
MAX_REQUEST_BYTES = 64 * 1024
MAX_IDEMPOTENCY_RESPONSE_BYTES = 64 * 1024
SESSION_TTL_SECONDS = 600
LEASE_TTL_SECONDS = 45
IDEMPOTENT_METHODS = {
    "begin_link",
    "confirm_wecom_link",
    "unlink",
    "link_github_app_installation",
    "link_huggingface_profile",
    "rotate_huggingface_profile",
    "register_host_identity_profile",
    "unlink_provider_profile",
    "begin_microsoft_graph_link",
    "begin_microsoft_graph_scope_upgrade",
    "update_microsoft_graph_v2_scope",
    "begin_azure_devops_link",
}


@dataclass(frozen=True, slots=True)
class DirectTypedPayload:
    """Small adapter matching the Python DirectPluginRuntime payload shape."""

    type_url: str
    value: bytes


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys to keep signed payload meaning unambiguous."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _action_type_name(action: str, suffix: str) -> str:
    """Map one fixed snake-case action to its contract message name."""

    names = {
        "begin_link": "BeginLink",
        "complete_github_link": "CompleteGitHubLink",
        "observe_wecom_challenge": "ObserveWeComChallenge",
        "confirm_wecom_link": "ConfirmWeComLink",
        "list_links": "ListLinks",
        "get_authorization": "GetAuthorization",
        "unlink": "Unlink",
        "create_credential_lease": "CreateCredentialLease",
        "redeem_credential_lease": "RedeemCredentialLease",
        "resolve_linked_sender": "ResolveLinkedSender",
        "link_github_app_installation": "LinkGitHubAppInstallation",
        "link_huggingface_profile": "LinkHuggingFaceProfile",
        "rotate_huggingface_profile": "RotateHuggingFaceProfile",
        "begin_huggingface_renewal": "BeginHuggingFaceRenewal",
        "poll_huggingface_renewal": "PollHuggingFaceRenewal",
        "register_host_identity_profile": "RegisterHostIdentityProfile",
        "list_provider_profiles": "ListProviderProfiles",
        "unlink_provider_profile": "UnlinkProviderProfile",
        "create_provider_credential_lease": "CreateProviderCredentialLease",
        "redeem_provider_credential_lease": "RedeemProviderCredentialLease",
        "begin_microsoft_graph_link": "BeginMicrosoftGraphLink",
        "complete_microsoft_graph_link": "CompleteMicrosoftGraphLink",
        "begin_microsoft_graph_scope_upgrade": "BeginMicrosoftGraphScopeUpgrade",
        "complete_microsoft_graph_scope_upgrade": "CompleteMicrosoftGraphScopeUpgrade",
        "update_microsoft_graph_v2_scope": "UpdateMicrosoftGraphV2Scope",
        "begin_azure_devops_link": "BeginAzureDevOpsLink",
        "complete_azure_devops_link": "CompleteAzureDevOpsLink",
    }
    return names[action] + suffix


def _required_text(payload: Mapping[str, Any], field: str, *, maximum: int = 4096) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AccountCenterError("invalid_request")
    return value


def _require_fields(
    payload: Mapping[str, Any], required: set[str], optional: set[str] = frozenset()
) -> None:
    """Enforce each method's closed JSON shape before business logic."""

    if not required.issubset(payload) or set(payload) - required - optional:
        raise AccountCenterError("invalid_request")


def _safe_resource(value: Any) -> dict[str, Any]:
    """Validate the fixed GitHub resource descriptor without accepting URLs."""

    resource = require_object(value)
    if set(resource) != {"repository_id", "repository", "issue_number"}:
        raise AccountCenterError("invalid_request")
    if (
        not isinstance(resource["repository_id"], str)
        or not resource["repository_id"]
        or len(resource["repository_id"]) > 100
        or not isinstance(resource["repository"], str)
        or "/" not in resource["repository"]
        or len(resource["repository"]) > 200
        or not all(
            part and part.replace("-", "").replace("_", "").replace(".", "").isalnum()
            for part in resource["repository"].split("/")
        )
        or not isinstance(resource["issue_number"], int)
        or isinstance(resource["issue_number"], bool)
        or resource["issue_number"] < 1
    ):
        raise AccountCenterError("invalid_request")
    return resource


def _resource_json(resource: Mapping[str, Any]) -> str:
    """Serialize the exact resource tuple used by lease matching."""

    return canonical_json(resource).decode("utf-8")


class AccountCenterPlugin:
    """Implement account.center.v1 using private storage and fixed providers."""

    plugin_id = "cyrene.accounts.account-center"
    version = "0.1.0"
    capabilities = (CAPABILITY_ID,)

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        store: AccountStore | None = None,
        github_client: Any | None = None,
        github_app_client: Any | None = None,
        huggingface_client: Any | None = None,
        huggingface_oauth_client: Any | None = None,
        microsoft_graph_oauth_client: Any | None = None,
        azure_devops_oauth_client: Any | None = None,
        now: Any = time.time,
    ) -> None:
        selected = dict(config) if config is not None else _environment_config()
        encryption = selected.get("encryption_key")
        if isinstance(encryption, str):
            encryption = decode_key(encryption, length=32)
        if not isinstance(encryption, bytes) or len(encryption) != 32:
            raise AccountCenterError("configuration_unavailable")
        self._encryption_key = encryption
        self._binding_id = _required_config_text(selected, "binding_id")
        self._store = store or AccountStore(_required_config_text(selected, "storage_path"))
        self._now = now
        callback_uri = selected.get("github_callback_uri")
        self._callback_uri = (
            _validated_callback_uri(callback_uri) if callback_uri is not None else None
        )
        graph_callback_uri = selected.get("microsoft_graph_callback_uri")
        self._microsoft_graph_callback_uri = (
            _validated_microsoft_graph_callback_uri(graph_callback_uri)
            if graph_callback_uri is not None
            else None
        )
        self._caller_keys = _load_caller_keys(selected)
        self._wecom_sources = _mapping_config(selected, "wecom_sources")
        self._exchange_caller = _required_config_text(selected, "exchange_caller_id")
        callback_caller = selected.get("github_callback_caller_id")
        if callback_caller is not None and (
            not isinstance(callback_caller, str) or not callback_caller
        ):
            raise AccountCenterError("configuration_unavailable")
        self._github_callback_caller = callback_caller
        graph_callback_caller = selected.get("microsoft_graph_callback_caller_id")
        if graph_callback_caller is not None and (
            not isinstance(graph_callback_caller, str) or not graph_callback_caller
        ):
            raise AccountCenterError("configuration_unavailable")
        self._microsoft_graph_callback_caller = graph_callback_caller
        self._github_provider_caller = _required_config_text(selected, "github_provider_caller_id")
        huggingface_provider_caller = selected.get("huggingface_provider_caller_id")
        if huggingface_provider_caller is not None and (
            not isinstance(huggingface_provider_caller, str) or not huggingface_provider_caller
        ):
            raise AccountCenterError("configuration_unavailable")
        self._provider_caller_ids = {"github": self._github_provider_caller}
        if huggingface_provider_caller:
            self._provider_caller_ids["huggingface"] = huggingface_provider_caller
        microsoft_graph_provider_caller = selected.get("microsoft_graph_provider_caller_id")
        if microsoft_graph_provider_caller is not None and (
            not isinstance(microsoft_graph_provider_caller, str)
            or not microsoft_graph_provider_caller
            or microsoft_graph_provider_caller != MICROSOFT_GRAPH_PROVIDER_CALLER_ID
        ):
            raise AccountCenterError("configuration_unavailable")
        if microsoft_graph_provider_caller:
            self._provider_caller_ids["microsoft_graph"] = microsoft_graph_provider_caller
        azure_devops_provider_caller = selected.get("azure_devops_provider_caller_id")
        if azure_devops_provider_caller is not None and (
            not isinstance(azure_devops_provider_caller, str) or not azure_devops_provider_caller
        ):
            raise AccountCenterError("configuration_unavailable")
        if azure_devops_provider_caller:
            self._provider_caller_ids["azure_devops"] = azure_devops_provider_caller
        provider_secrets = _mapping_config(selected, "provider_secrets")
        self._provider_secrets = {
            str(reference): str(secret)
            for reference, secret in provider_secrets.items()
            if isinstance(reference, str) and reference and isinstance(secret, str) and secret
        }
        if len(self._provider_secrets) != len(provider_secrets):
            raise AccountCenterError("configuration_unavailable")
        workspace_operator_profile_refs = selected.get("workspace_operator_profile_refs", [])
        if (
            not isinstance(workspace_operator_profile_refs, list)
            or any(not isinstance(reference, str) for reference in workspace_operator_profile_refs)
            or len(set(workspace_operator_profile_refs)) != len(workspace_operator_profile_refs)
        ):
            raise AccountCenterError("configuration_unavailable")
        self._workspace_operator_profile_refs = {
            _opaque_profile_ref(reference) for reference in workspace_operator_profile_refs
        }
        self._method_callers = _load_method_callers(
            selected,
            exchange_caller=self._exchange_caller,
            callback_caller=self._github_callback_caller,
            microsoft_graph_callback_caller=self._microsoft_graph_callback_caller,
            microsoft_graph_provider_caller=microsoft_graph_provider_caller,
            provider_caller=self._github_provider_caller,
            provider_callers=set(self._provider_caller_ids.values()),
            wecom_callers=set(self._wecom_sources),
        )
        expected_callers = {
            self._exchange_caller,
            self._github_provider_caller,
            *self._provider_caller_ids.values(),
            *self._wecom_sources,
        }
        if self._github_callback_caller:
            expected_callers.add(self._github_callback_caller)
        if self._microsoft_graph_callback_caller:
            expected_callers.add(self._microsoft_graph_callback_caller)
        caller_roles = [
            self._exchange_caller,
            *self._provider_caller_ids.values(),
            *self._wecom_sources,
        ]
        if self._github_callback_caller:
            caller_roles.append(self._github_callback_caller)
        if self._microsoft_graph_callback_caller:
            caller_roles.append(self._microsoft_graph_callback_caller)
        if (
            len(caller_roles) != len(set(caller_roles))
            or set(self._caller_keys) != expected_callers
        ):
            raise AccountCenterError("configuration_unavailable")
        self._refresh_lock = threading.Lock()
        self._account_refresh_locks: dict[str, threading.Lock] = {}
        if github_client is not None:
            self._github = github_client
        elif selected.get("github_client_id") and selected.get("github_client_secret"):
            self._github = GitHubOAuthClient(
                client_id=_required_config_text(selected, "github_client_id"),
                client_secret=_required_config_text(selected, "github_client_secret"),
            )
        else:
            self._github = None
        self._github_app = github_app_client
        if self._github_app is None:
            app_id = selected.get("github_app_id")
            private_key = selected.get("github_app_private_key")
            if app_id is not None or private_key is not None:
                if not isinstance(app_id, str) or not isinstance(private_key, str):
                    raise AccountCenterError("configuration_unavailable")
                try:
                    self._github_app = GitHubAppClient(app_id, private_key, now=now)
                except GitHubAppError:
                    raise AccountCenterError("configuration_unavailable") from None
        self._huggingface = huggingface_client or HuggingFaceIdentityClient()
        self._huggingface_oauth = huggingface_oauth_client or HuggingFaceOAuthClient()
        graph_client_id = selected.get("microsoft_graph_client_id")
        graph_client_secret = selected.get("microsoft_graph_client_secret")
        if (graph_client_id is None) != (graph_client_secret is None):
            raise AccountCenterError("configuration_unavailable")
        if microsoft_graph_oauth_client is not None:
            self._microsoft_graph_oauth = microsoft_graph_oauth_client
        elif graph_client_id and graph_client_secret:
            self._microsoft_graph_oauth = MicrosoftGraphOAuthClient(
                client_id=_required_config_text(selected, "microsoft_graph_client_id"),
                client_secret=_required_config_text(selected, "microsoft_graph_client_secret"),
                office_v2_enabled=True,
                now=now,
            )
        else:
            self._microsoft_graph_oauth = None
        self._microsoft_graph_client_id = graph_client_id or getattr(
            self._microsoft_graph_oauth, "client_id", None
        )
        self._azure_devops_oauth = azure_devops_oauth_client
        if self._azure_devops_oauth is None and graph_client_id and graph_client_secret:
            self._azure_devops_oauth = AzureDevOpsOAuthClient(
                client_id=_required_config_text(selected, "microsoft_graph_client_id"),
                client_secret=_required_config_text(selected, "microsoft_graph_client_secret"),
                now=now,
            )
        self._azure_devops_client_id = graph_client_id or getattr(
            self._azure_devops_oauth, "client_id", None
        )
        entra_enabled = any(
            value is not None
            for value in (
                graph_callback_uri,
                graph_callback_caller,
                microsoft_graph_provider_caller,
                azure_devops_provider_caller,
                graph_client_id,
                graph_client_secret,
                microsoft_graph_oauth_client,
                azure_devops_oauth_client,
            )
        )
        if entra_enabled and (
            self._microsoft_graph_callback_uri is None
            or self._microsoft_graph_callback_caller is None
            or not isinstance(self._microsoft_graph_client_id, str)
            or not self._microsoft_graph_client_id
            or (microsoft_graph_provider_caller and self._microsoft_graph_oauth is None)
            or (azure_devops_provider_caller and self._azure_devops_oauth is None)
        ):
            raise AccountCenterError("configuration_unavailable")
        self._provider_refresh_locks: dict[str, threading.Lock] = {}
        self._provider_refresh_lock = threading.Lock()
        self._recover_interrupted_callback_credentials()
        self._recover_interrupted_unlink_results()
        self._recover_pending_idempotency_results()

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        request_id: str | None = None,
        **_: Any,
    ) -> tuple[bool, DirectTypedPayload | str]:
        """Route a fixed method over DirectPluginRuntime with a signed envelope."""

        if capability != CAPABILITY_ID or action not in METHODS:
            return False, "METHOD_NOT_FOUND: capability method unavailable"
        expected_type = REQUEST_TYPE_PREFIX + _action_type_name(action, "Request")
        if request_type_url is not None and request_type_url != expected_type:
            return False, "INVALID_REQUEST: request contract mismatch"
        if len(payload) > MAX_REQUEST_BYTES:
            return False, "INVALID_REQUEST: request too large"
        try:
            envelope = json.loads(
                payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs
            )
            if not isinstance(envelope, dict) or set(envelope) != {
                "payload",
                "trusted_context",
            }:
                raise AccountCenterError("invalid_request")
            request = require_object(envelope["payload"])
            context = require_object(envelope["trusted_context"], code="untrusted_caller")
            result = self.call(action, request, context)
            response_name = _action_type_name(action, "Response")
            return True, DirectTypedPayload(
                type_url=REQUEST_TYPE_PREFIX + response_name,
                value=canonical_json(result),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return False, "INVALID_REQUEST: request rejected"
        except AccountCenterError as error:
            return False, f"PERMISSION_DENIED: {error.code}"
        except Exception:  # noqa: BLE001 - capability boundary must sanitize every internal failure.
            return False, "EXECUTION_FAILED: account operation failed"

    def call(
        self, method: str, payload: Mapping[str, Any], context: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Verify caller authorization then invoke one capability method."""

        if method not in METHODS:
            raise AccountCenterError("method_not_found")
        request = require_object(dict(payload))
        trusted = require_object(dict(context), code="untrusted_caller")
        anchor = verify_context(method, request, trusted, self._caller_keys, now=int(self._now()))
        caller_id = trusted["caller_id"]
        if trusted["binding_id"] != self._binding_id:
            raise AccountCenterError("untrusted_caller")
        permitted = self._method_callers.get(method, set())
        if caller_id not in permitted:
            raise AccountCenterError("untrusted_caller")
        if method == "resolve_linked_sender" and caller_id != self._exchange_caller:
            raise AccountCenterError("untrusted_caller")
        self._store.consume_request_id(trusted["request_id"], trusted["expires_at"])
        claimed_operation = trusted["operation_id"]
        if claimed_operation != method:
            raise AccountCenterError("untrusted_caller")
        if method in IDEMPOTENT_METHODS:
            key = _idempotency_key(request)
            body = dict(request)
            body.pop("idempotency_key")
            key_hash = _idempotency_scope_hash(method, anchor, key)
            response_blob = self._store.begin_idempotent_command(
                key_hash=key_hash,
                method=method,
                payload_sha256=payload_digest(body),
                now=int(self._now()),
            )
            if response_blob is not None:
                return self._decode_idempotency_response(key_hash, response_blob)
            try:
                result = self._dispatch(
                    method,
                    body,
                    anchor,
                    caller_id,
                    trusted,
                    idempotency_key_hash=key_hash,
                )
            except AccountCenterError as error:
                self._store.complete_idempotent_command(
                    key_hash,
                    self._encode_idempotency_response(key_hash, {"error": error.code}),
                )
                raise
            except Exception:  # noqa: BLE001 - persist only a fixed sanitized result.
                self._store.complete_idempotent_command(
                    key_hash,
                    self._encode_idempotency_response(key_hash, {"error": "execution_failed"}),
                )
                raise AccountCenterError("execution_failed") from None
            return result
        return self._dispatch(method, request, anchor, caller_id, trusted)

    def _dispatch(
        self,
        method: str,
        request: Mapping[str, Any],
        anchor: Anchor,
        caller_id: str,
        trusted: Mapping[str, Any],
        *,
        idempotency_key_hash: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch one already-authenticated fixed capability method."""

        if method == "begin_link":
            return self._begin_link(request, anchor, idempotency_key_hash)
        if method == "complete_github_link":
            return self._complete_github_link(request)
        if method == "observe_wecom_challenge":
            return self._observe_wecom_challenge(request, trusted)
        if method == "confirm_wecom_link":
            return self._confirm_wecom_link(request, anchor, idempotency_key_hash)
        if method == "list_links":
            return self._list_links(request, anchor)
        if method == "get_authorization":
            return self._get_authorization(request, anchor)
        if method == "unlink":
            return self._unlink(request, anchor, idempotency_key_hash)
        if method == "create_credential_lease":
            return self._create_credential_lease(request, anchor, trusted)
        if method == "redeem_credential_lease":
            return self._redeem_credential_lease(request, anchor, caller_id, trusted)
        if method == "resolve_linked_sender":
            return self._resolve_linked_sender(request, trusted)
        if method == "link_github_app_installation":
            return self._link_github_app_installation(request, anchor)
        if method == "link_huggingface_profile":
            return self._link_huggingface_profile(request, anchor)
        if method == "rotate_huggingface_profile":
            return self._rotate_huggingface_profile(request, anchor)
        if method == "begin_huggingface_renewal":
            return self._begin_huggingface_renewal(request, anchor)
        if method == "poll_huggingface_renewal":
            return self._poll_huggingface_renewal(request, anchor)
        if method == "register_host_identity_profile":
            return self._register_host_identity_profile(request, anchor)
        if method == "list_provider_profiles":
            return self._list_provider_profiles(request, anchor, caller_id)
        if method == "unlink_provider_profile":
            return self._unlink_provider_profile(request, anchor)
        if method == "create_provider_credential_lease":
            return self._create_provider_credential_lease(request, anchor, trusted)
        if method == "redeem_provider_credential_lease":
            return self._redeem_provider_credential_lease(request, anchor, caller_id, trusted)
        if method == "begin_microsoft_graph_link":
            return self._begin_microsoft_graph_link(request, anchor, idempotency_key_hash)
        if method == "complete_microsoft_graph_link":
            return self._complete_microsoft_graph_link(request)
        if method == "begin_microsoft_graph_scope_upgrade":
            return self._begin_microsoft_graph_scope_upgrade(request, anchor, idempotency_key_hash)
        if method == "update_microsoft_graph_v2_scope":
            return self._update_microsoft_graph_v2_scope(request, anchor)
        if method == "complete_microsoft_graph_scope_upgrade":
            return self._complete_microsoft_graph_scope_upgrade(request)
        if method == "begin_azure_devops_link":
            return self._begin_azure_devops_link(request, anchor, idempotency_key_hash)
        if method == "complete_azure_devops_link":
            return self._complete_azure_devops_link(request)
        raise AccountCenterError("method_not_found")

    def _encode_idempotency_response(self, key_hash: str, value: Mapping[str, Any]) -> bytes:
        """Encrypt one bounded replay response before storing it privately."""

        clear = canonical_json(dict(value))
        if len(clear) > MAX_IDEMPOTENCY_RESPONSE_BYTES:
            raise AccountCenterError("response_too_large")
        return encrypt(
            self._encryption_key,
            clear,
            f"command-idempotency:{key_hash}".encode(),
        )

    def _decode_idempotency_response(self, key_hash: str, blob: bytes) -> dict[str, Any]:
        """Replay the exact cached public result or its stable sanitized error."""

        clear = decrypt(
            self._encryption_key,
            blob,
            f"command-idempotency:{key_hash}".encode(),
        )
        if len(clear) > MAX_IDEMPOTENCY_RESPONSE_BYTES:
            raise AccountCenterError("idempotency_result_unavailable")
        try:
            value = json.loads(clear, object_pairs_hook=_reject_duplicate_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise AccountCenterError("idempotency_result_unavailable") from None
        if not isinstance(value, dict) or len(value) != 1:
            raise AccountCenterError("idempotency_result_unavailable")
        if "error" in value and isinstance(value["error"], str):
            raise AccountCenterError(value["error"])
        if "value" in value and isinstance(value["value"], dict):
            return value["value"]
        raise AccountCenterError("idempotency_result_unavailable")

    def _begin_link(
        self,
        request: Mapping[str, Any],
        anchor: Anchor,
        idempotency_key_hash: str | None = None,
    ) -> dict[str, Any]:
        _require_fields(request, {"provider"}, {"workspace_id"})
        provider = request.get("provider")
        if provider not in {"github", "wecom"}:
            raise AccountCenterError("invalid_request")
        workspace_id = request.get("workspace_id", anchor.workspace_id)
        if workspace_id != anchor.workspace_id:
            raise AccountCenterError("scope_mismatch")
        now = int(self._now())
        state = _secret_text()
        nonce = _secret_text()
        session_ref = str(uuid.uuid4())
        code_verifier = _secret_text() if provider == "github" else None
        verifier_blob = (
            self._encrypt_session_secret(session_ref, code_verifier)
            if code_verifier is not None
            else None
        )
        response: dict[str, Any] = {
            "link_session_ref": session_ref,
            "expires_at": now + SESSION_TTL_SECONDS,
        }
        if provider == "github":
            if self._github is None or self._callback_uri is None:
                raise AccountCenterError("provider_not_configured")
            response["authorization_url"] = self._github.authorization_url(
                redirect_uri=self._callback_uri,
                state=state,
                code_challenge=pkce_s256_challenge(code_verifier),
            )
        else:
            response["callback_nonce"] = nonce
        command_result = (
            (
                idempotency_key_hash,
                self._encode_idempotency_response(idempotency_key_hash, {"value": response}),
            )
            if idempotency_key_hash is not None
            else None
        )
        session_ref = self._store.create_session(
            anchor,
            provider,
            _digest_text(state),
            _digest_text(nonce),
            self._callback_uri if provider == "github" else None,
            verifier_blob,
            now + SESSION_TTL_SECONDS,
            now=now,
            session_ref=session_ref,
            command_result=command_result,
        )
        return response

    def _complete_github_link(self, request: Mapping[str, Any]) -> dict[str, Any]:
        _require_fields(request, {"state", "code"})
        if self._github is None or self._callback_uri is None:
            raise AccountCenterError("provider_not_configured")
        state = _required_text(request, "state", maximum=256)
        code = _required_text(request, "code", maximum=4096)
        now = int(self._now())
        state_digest = _digest_text(state)
        session = self._store.claim_session_by_state(state_digest, self._callback_uri, now=now)
        session_ref = session["link_session_ref"]
        if (
            not hmac.compare_digest(session["state_digest"], state_digest)
            or session["callback_uri"] != self._callback_uri
        ):
            raise AccountCenterError("link_session_invalid")
        verifier_blob = session["code_verifier_blob"]
        if not isinstance(verifier_blob, bytes):
            self._store.fail_session(session_ref, "not_issued")
            raise AccountCenterError("link_session_invalid")
        try:
            verifier = self._decrypt_session_secret(session_ref, verifier_blob)
        except AccountCenterError:
            self._store.fail_session(session_ref, "not_issued")
            raise AccountCenterError("link_session_invalid") from None
        credential: OAuthCredential | None = None
        try:
            credential, stable_subject = self._github.exchange_code(
                code,
                redirect_uri=session["callback_uri"],
                code_verifier=verifier,
                on_credential=lambda value: self._persist_callback_credential(session_ref, value),
            )
            owner = _anchor_from_session(session)
            account_ref = str(uuid.uuid4())
            encrypted = self._encrypt_credential(account_ref=account_ref, credential=credential)
            account_ref = self._store.insert_account(
                account_ref=account_ref,
                anchor=owner,
                provider="github",
                source_authority="https://github.com",
                stable_subject=stable_subject,
                metadata_json="{}",
                credential_blob=encrypted,
                credential_expires_at=credential.expires_at,
                refresh_expires_at=credential.refresh_expires_at,
                now=now,
                complete_session_ref=session_ref,
            )
            return {
                "account_ref": account_ref,
                "provider": "github",
                "status": "active",
            }
        except GitHubIdentityVerificationError as error:
            outcome = self._revoke_callback_token(error.credential.access_token)
            self._store.fail_session(session_ref, outcome)
            raise AccountCenterError("identity_verification_failed") from None
        except GitHubCredentialPersistenceError as error:
            outcome = self._revoke_callback_token(error.credential.access_token)
            self._store.fail_session(session_ref, outcome)
            raise AccountCenterError("credential_persistence_failed") from None
        except GitHubCredentialPolicyError as error:
            outcome = self._revoke_callback_token(error.credential.access_token)
            self._store.fail_session(session_ref, outcome)
            raise AccountCenterError("provider_rejected") from None
        except AccountCenterError as error:
            outcome = (
                self._revoke_callback_token(credential.access_token)
                if credential is not None
                else "unknown"
                if error.code == "provider_unavailable"
                else "not_issued"
            )
            self._store.fail_session(session_ref, outcome)
            raise
        except Exception:  # noqa: BLE001 - never expose or retain a failed OAuth result.
            outcome = (
                self._revoke_callback_token(credential.access_token)
                if credential is not None
                else "unknown"
            )
            self._store.fail_session(session_ref, outcome)
            raise AccountCenterError("link_failed") from None

    def _observe_wecom_challenge(
        self, request: Mapping[str, Any], context: Mapping[str, Any]
    ) -> dict[str, Any]:
        _require_fields(
            request,
            {
                "bot_namespace",
                "source_authority",
                "stable_subject",
                "challenge_id",
                "challenge_value",
                "expires_at",
            },
        )
        caller_id = context["caller_id"]
        source = self._wecom_sources.get(caller_id)
        if not isinstance(source, Mapping):
            raise AccountCenterError("untrusted_caller")
        bot_namespace = _required_text(request, "bot_namespace", maximum=200)
        authority = _required_text(request, "source_authority", maximum=500)
        stable_subject = _required_text(request, "stable_subject", maximum=500)
        challenge_id = _uuid_text(request["challenge_id"])
        challenge_value = _required_text(request, "challenge_value", maximum=128)
        expires_at = request.get("expires_at")
        now = int(self._now())
        allowed_bots = source.get("bot_namespaces")
        if (
            authority != source.get("source_authority")
            or not isinstance(allowed_bots, list)
            or bot_namespace not in allowed_bots
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or not now < expires_at <= now + SESSION_TTL_SECONDS
        ):
            raise AccountCenterError("challenge_rejected")
        self._store.add_wecom_challenge(
            challenge_id=challenge_id,
            bot_namespace=bot_namespace,
            source_authority=authority,
            stable_subject=stable_subject,
            organization_id=context["organization_id"],
            value_digest=_digest_text(challenge_value),
            expires_at=expires_at,
        )
        return {
            "accepted": True,
            "challenge_id": challenge_id,
            "expires_at": expires_at,
        }

    def _confirm_wecom_link(
        self,
        request: Mapping[str, Any],
        anchor: Anchor,
        idempotency_key_hash: str | None = None,
    ) -> dict[str, Any]:
        _require_fields(request, {"link_session_ref", "challenge_id", "challenge_value"})
        session_ref = _uuid_text(request["link_session_ref"])
        challenge_id = _uuid_text(request["challenge_id"])
        challenge_value = _required_text(request, "challenge_value", maximum=128)
        now = int(self._now())
        session = self._store.session(session_ref)
        if session["provider"] != "wecom" or session["expires_at"] <= now:
            raise AccountCenterError("link_session_invalid")
        if not _session_matches_anchor(session, anchor):
            raise AccountCenterError("scope_mismatch")
        if not hmac.compare_digest(session["nonce_digest"], _digest_text(challenge_value)):
            raise AccountCenterError("challenge_rejected")
        account_ref = str(uuid.uuid4())
        response = {"account_ref": account_ref, "provider": "wecom", "status": "active"}
        command_result = (
            (
                idempotency_key_hash,
                self._encode_idempotency_response(idempotency_key_hash, {"value": response}),
            )
            if idempotency_key_hash is not None
            else None
        )
        self._store.complete_wecom_link(
            session_ref=session_ref,
            challenge_id=challenge_id,
            anchor=anchor,
            expected_value_digest=_digest_text(challenge_value),
            account_ref=account_ref,
            now=now,
            command_result=command_result,
        )
        return response

    def _list_links(self, request: Mapping[str, Any], anchor: Anchor) -> dict[str, Any]:
        _require_fields(request, set(), {"workspace_id"})
        workspace_id = request.get("workspace_id", anchor.workspace_id)
        if workspace_id != anchor.workspace_id:
            raise AccountCenterError("scope_mismatch")
        rows = self._store.list_accounts(anchor, workspace_id)
        return {
            "links": [
                {
                    "account_ref": row["account_ref"],
                    "provider": row["provider"],
                    "source_authority": row["source_authority"],
                    "bot_namespace": row["bot_namespace"],
                    "status": row["status"],
                    "linked_at": row["linked_at"],
                    "workspace_id": row["workspace_id"],
                }
                for row in rows
            ]
        }

    def _link_github_app_installation(
        self, request: Mapping[str, Any], anchor: Anchor
    ) -> dict[str, Any]:
        """Verify and persist one GitHub App installation profile."""

        _require_fields(
            request,
            {"profile_ref", "installation_id", "repositories"},
        )
        if self._github_app is None:
            raise AccountCenterError("provider_not_configured")
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        installation_id = _numeric_id(request["installation_id"])
        repositories = _safe_github_repositories(request["repositories"])
        try:
            identity = self._github_app.verify_installation(installation_id)
            token = self._github_app.create_installation_token(
                installation_id,
                [repository["repository_id"] for repository in repositories],
            )
            self._github_app.verify_repositories(token.access_token, repositories)
        except GitHubAppError as error:
            raise AccountCenterError(error.code) from None
        scope = {"repositories": repositories}
        self._store.create_provider_profile(
            profile_ref=profile_ref,
            anchor=anchor,
            provider="github",
            identity_ref=installation_id,
            target_scope_json=canonical_json(scope).decode("utf-8"),
            auth_status="healthy",
            external_subject=identity["login"],
            credential_blob=self._encrypt_provider_token(
                profile_ref, token.access_token, token.expires_at
            ),
            credential_expires_at=token.expires_at,
            credential_secret_ref=None,
            access_mode=self._configured_profile_access_mode(profile_ref),
            now=int(self._now()),
        )
        return {
            "profile_ref": profile_ref,
            "provider": "github",
            "access_mode": self._configured_profile_access_mode(profile_ref),
            "auth_status": "healthy",
            "external_subject": identity["login"],
            "expires_at": token.expires_at,
        }

    def _link_huggingface_profile(
        self, request: Mapping[str, Any], anchor: Anchor
    ) -> dict[str, Any]:
        """Verify a host credential source and persist encrypted profile material."""

        _require_fields(
            request,
            {"profile_ref", "credential_secret_ref", "scopes"},
            {"auth_mode"},
        )
        if "huggingface" not in self._provider_caller_ids:
            raise AccountCenterError("provider_not_configured")
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        secret_ref = _safe_secret_reference(request["credential_secret_ref"])
        auth_mode = _huggingface_auth_mode(request.get("auth_mode"))
        scopes = _safe_huggingface_scopes(request["scopes"])
        token: str
        refresh_blob: bytes | None = None
        refresh_expires_at: int | None = None
        oauth_client_id: str | None = None
        oauth_scopes_json: str | None = None
        if auth_mode == "device_oauth":
            seed = self._huggingface_oauth_seed(secret_ref, scopes)
            now = int(self._now())
            if seed.access_token_expires_at <= now or (
                seed.refresh_token_expires_at is not None and seed.refresh_token_expires_at <= now
            ):
                raise AccountCenterError("credential_rejected")
            token = seed.access_token
            refresh_blob = self._encrypt_provider_refresh_token(profile_ref, seed.refresh_token)
            refresh_expires_at = seed.refresh_token_expires_at
            oauth_client_id = seed.client_id
            oauth_scopes_json = (
                canonical_json(list(seed.oauth_scopes)).decode("utf-8")
                if seed.oauth_scopes is not None
                else None
            )
            credential_expires_at = seed.access_token_expires_at
        else:
            token = self._provider_secrets.get(secret_ref, "")
            if not token:
                raise AccountCenterError("credential_unavailable")
            credential_expires_at = None
        try:
            external_subject = self._huggingface.whoami(token)
        except HuggingFaceAuthError as error:
            raise AccountCenterError(error.code) from None
        self._store.create_provider_profile(
            profile_ref=profile_ref,
            anchor=anchor,
            provider="huggingface",
            identity_ref=external_subject,
            target_scope_json=canonical_json(scopes).decode("utf-8"),
            auth_status="healthy",
            external_subject=external_subject,
            credential_blob=self._encrypt_provider_token(profile_ref, token, credential_expires_at),
            credential_expires_at=credential_expires_at,
            credential_secret_ref=secret_ref,
            auth_mode=auth_mode,
            access_mode=self._configured_profile_access_mode(profile_ref),
            credential_refresh_blob=refresh_blob,
            credential_refresh_expires_at=refresh_expires_at,
            oauth_client_id=oauth_client_id,
            oauth_scopes_json=oauth_scopes_json,
            now=int(self._now()),
        )
        return {
            "profile_ref": profile_ref,
            "provider": "huggingface",
            "auth_mode": auth_mode,
            "access_mode": self._configured_profile_access_mode(profile_ref),
            "auth_status": "healthy",
            "external_subject": external_subject,
            "expires_at": credential_expires_at,
        }

    def _rotate_huggingface_profile(
        self, request: Mapping[str, Any], anchor: Anchor
    ) -> dict[str, Any]:
        """Rotate an HF credential through a configured host secret reference."""

        _require_fields(
            request,
            {"profile_ref", "credential_secret_ref"},
            {"auth_mode"},
        )
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        secret_ref = _safe_secret_reference(request["credential_secret_ref"])
        profile = self._store.get_provider_profile(profile_ref, anchor)
        if profile["provider"] != "huggingface":
            raise AccountCenterError("profile_provider_mismatch")
        auth_mode = _huggingface_auth_mode(request.get("auth_mode", profile["auth_mode"]))
        scopes = _json_object(profile["target_scope_json"])
        token: str
        refresh_blob: bytes | None = None
        refresh_expires_at: int | None = None
        oauth_client_id: str | None = None
        oauth_scopes_json: str | None = None
        if auth_mode == "device_oauth":
            seed = self._huggingface_oauth_seed(secret_ref, scopes)
            now = int(self._now())
            if seed.access_token_expires_at <= now or (
                seed.refresh_token_expires_at is not None and seed.refresh_token_expires_at <= now
            ):
                raise AccountCenterError("credential_rejected")
            token = seed.access_token
            refresh_blob = self._encrypt_provider_refresh_token(profile_ref, seed.refresh_token)
            refresh_expires_at = seed.refresh_token_expires_at
            oauth_client_id = seed.client_id
            oauth_scopes_json = (
                canonical_json(list(seed.oauth_scopes)).decode("utf-8")
                if seed.oauth_scopes is not None
                else None
            )
            credential_expires_at = seed.access_token_expires_at
        else:
            token = self._provider_secrets.get(secret_ref, "")
            if not token:
                raise AccountCenterError("credential_unavailable")
            credential_expires_at = None
        try:
            external_subject = self._huggingface.whoami(token)
        except HuggingFaceAuthError as error:
            raise AccountCenterError(error.code) from None
        if profile["external_subject"] and profile["external_subject"] != external_subject:
            raise AccountCenterError("external_identity_mismatch")
        self._store.update_huggingface_profile_credentials(
            profile_ref=profile_ref,
            anchor=anchor,
            credential_blob=self._encrypt_provider_token(profile_ref, token, credential_expires_at),
            credential_refresh_blob=refresh_blob,
            credential_expires_at=credential_expires_at,
            credential_refresh_expires_at=refresh_expires_at,
            oauth_client_id=oauth_client_id,
            oauth_scopes_json=oauth_scopes_json,
            auth_mode=auth_mode,
            credential_secret_ref=secret_ref,
            external_subject=external_subject,
            now=int(self._now()),
        )
        return {
            "profile_ref": profile_ref,
            "provider": "huggingface",
            "auth_mode": auth_mode,
            "access_mode": profile["access_mode"],
            "auth_status": "healthy",
            "external_subject": external_subject,
            "expires_at": credential_expires_at,
        }

    def _begin_huggingface_renewal(
        self, request: Mapping[str, Any], anchor: Anchor
    ) -> dict[str, Any]:
        """Start one owner-scoped HF device login without returning its private code."""

        _require_fields(request, {"profile_ref", "session_ref"})
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        session_ref = _opaque_profile_ref(request["session_ref"])
        profile = self._store.get_provider_profile(profile_ref, anchor)
        if (
            profile["provider"] != "huggingface"
            or profile["auth_mode"] != "device_oauth"
            or not profile["oauth_client_id"]
            or not profile["oauth_scopes_json"]
        ):
            raise AccountCenterError("profile_provider_mismatch")
        if profile["refresh_claim_id"] is not None:
            raise AccountCenterError("credential_refresh_in_progress")
        oauth_scopes = _stored_huggingface_oauth_scopes(profile["oauth_scopes_json"])
        now = int(self._now())
        session, created = self._store.begin_huggingface_device_renewal(
            profile_ref=profile_ref,
            session_ref=session_ref,
            anchor=anchor,
            now=now,
        )
        if not created:
            return _huggingface_renewal_response(session)
        if session["profile_revision"] != profile["credential_revision"]:
            session = self._store.fail_huggingface_device_renewal_start(
                session_ref,
                anchor,
                state="failed",
                reason_code="profile_changed_before_device_start",
                now=int(self._now()),
            )
            return _huggingface_renewal_response(session)
        try:
            device = self._huggingface_oauth.begin_device_code(
                client_id=str(profile["oauth_client_id"]),
                oauth_scopes=oauth_scopes,
            )
        except HuggingFaceOAuthError as error:
            session = self._store.fail_huggingface_device_renewal_start(
                session_ref,
                anchor,
                state="unknown" if error.uncertain else "failed",
                reason_code=error.code,
                now=int(self._now()),
            )
            return _huggingface_renewal_response(session)
        encrypted_device_code = encrypt(
            self._encryption_key,
            device.device_code.encode("utf-8"),
            f"huggingface-device-renewal:{session_ref}".encode(),
        )
        started_at = int(self._now())
        session = self._store.complete_huggingface_device_renewal_start(
            session_ref,
            anchor,
            encrypted_device_code=encrypted_device_code,
            user_code=device.user_code,
            verification_uri=device.verification_uri,
            expires_at=started_at + device.expires_in,
            interval_seconds=device.interval_seconds,
            now=started_at,
        )
        return _huggingface_renewal_response(session)

    def _poll_huggingface_renewal(
        self, request: Mapping[str, Any], anchor: Anchor
    ) -> dict[str, Any]:
        """Poll one fenced HF device session and atomically replace its owned profile."""

        _require_fields(request, {"profile_ref", "session_ref"})
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        session_ref = _opaque_profile_ref(request["session_ref"])
        session = self._store.get_huggingface_device_renewal(session_ref, anchor)
        if session["profile_ref"] != profile_ref:
            raise AccountCenterError("renewal_session_not_found")
        now = int(self._now())
        session, claim_id = self._store.claim_huggingface_device_renewal_poll(
            session_ref,
            anchor,
            now=now,
        )
        if claim_id is None:
            return _huggingface_renewal_response(session)
        profile = self._store.get_provider_profile(profile_ref, anchor)
        if (
            profile["provider"] != "huggingface"
            or profile["auth_mode"] != "device_oauth"
            or profile["credential_revision"] != session["profile_revision"]
            or profile["refresh_claim_id"] is not None
            or not isinstance(profile["oauth_client_id"], str)
            or not profile["oauth_client_id"]
        ):
            failed = self._store.fail_huggingface_device_renewal_poll(
                session_ref,
                anchor,
                claim_id,
                state="failed",
                reason_code="profile_changed_during_renewal",
                now=int(self._now()),
            )
            return _huggingface_renewal_response(failed)
        device_blob = session.get("device_code_blob")
        if not isinstance(device_blob, bytes):
            failed = self._store.fail_huggingface_device_renewal_poll(
                session_ref,
                anchor,
                claim_id,
                state="unknown",
                reason_code="device_code_unavailable",
                now=int(self._now()),
            )
            return _huggingface_renewal_response(failed)
        try:
            device_code = decrypt(
                self._encryption_key,
                device_blob,
                f"huggingface-device-renewal:{session_ref}".encode(),
            ).decode("utf-8")
            token = self._huggingface_oauth.poll_device_code(
                client_id=profile["oauth_client_id"],
                device_code=device_code,
                now=now,
            )
        except UnicodeDecodeError:
            token_error = HuggingFaceOAuthError("device_code_unavailable", uncertain=True)
        except HuggingFaceOAuthError as error:
            token_error = error
        else:
            token_error = None
        if token_error is not None:
            return self._record_huggingface_device_poll_error(
                session_ref,
                anchor,
                claim_id,
                session,
                token_error,
            )
        if token.refresh_token is None:
            failed = self._store.fail_huggingface_device_renewal_poll(
                session_ref,
                anchor,
                claim_id,
                state="failed",
                reason_code="refresh_token_missing",
                now=int(self._now()),
            )
            return _huggingface_renewal_response(failed)
        old_scopes = _stored_huggingface_oauth_scopes(profile["oauth_scopes_json"])
        if token.oauth_scopes is not None and set(token.oauth_scopes) != set(old_scopes):
            failed = self._store.fail_huggingface_device_renewal_poll(
                session_ref,
                anchor,
                claim_id,
                state="failed",
                reason_code="authorization_scope_mismatch",
                now=int(self._now()),
            )
            return _huggingface_renewal_response(failed)
        try:
            external_subject = self._huggingface.whoami(token.access_token)
        except HuggingFaceAuthError:
            failed = self._store.fail_huggingface_device_renewal_poll(
                session_ref,
                anchor,
                claim_id,
                state="failed",
                reason_code="provider_authentication_failed",
                now=int(self._now()),
            )
            return _huggingface_renewal_response(failed)
        if not profile["external_subject"] or external_subject != profile["external_subject"]:
            failed = self._store.fail_huggingface_device_renewal_poll(
                session_ref,
                anchor,
                claim_id,
                state="failed",
                reason_code="external_identity_mismatch",
                now=int(self._now()),
            )
            return _huggingface_renewal_response(failed)
        scopes_json = profile["oauth_scopes_json"]
        completed_at = int(self._now())
        completed = self._store.complete_huggingface_device_renewal(
            session_ref,
            anchor,
            claim_id,
            credential_blob=self._encrypt_provider_token(
                profile_ref,
                token.access_token,
                token.access_token_expires_at,
            ),
            credential_refresh_blob=self._encrypt_provider_refresh_token(
                profile_ref,
                token.refresh_token,
            ),
            credential_expires_at=token.access_token_expires_at,
            credential_refresh_expires_at=token.refresh_token_expires_at,
            oauth_scopes_json=scopes_json,
            external_subject=external_subject,
            now=completed_at,
        )
        return _huggingface_renewal_response(completed)

    def _record_huggingface_device_poll_error(
        self,
        session_ref: str,
        anchor: Anchor,
        claim_id: str,
        session: Mapping[str, Any],
        error: HuggingFaceOAuthError,
    ) -> dict[str, Any]:
        """Persist pending, throttled, final, and uncertain poll outcomes safely."""

        now = int(self._now())
        current_interval = session.get("interval_seconds")
        interval = (
            current_interval
            if isinstance(current_interval, int) and not isinstance(current_interval, bool)
            else 5
        )
        if error.code == "authorization_pending":
            updated = self._store.defer_huggingface_device_renewal_poll(
                session_ref,
                anchor,
                claim_id,
                interval_seconds=interval,
                reason_code=None,
                now=now,
            )
            return _huggingface_renewal_response(updated)
        if error.code in {"slow_down", "provider_rate_limited"}:
            updated = self._store.defer_huggingface_device_renewal_poll(
                session_ref,
                anchor,
                claim_id,
                interval_seconds=min(300, interval + 5),
                reason_code=error.code,
                now=now,
            )
            return _huggingface_renewal_response(updated)
        state = {
            "expired_token": "expired",
            "access_denied": "cancelled",
        }.get(error.code, "unknown" if error.uncertain else "failed")
        updated = self._store.fail_huggingface_device_renewal_poll(
            session_ref,
            anchor,
            claim_id,
            state=state,
            reason_code=error.code,
            now=now,
        )
        return _huggingface_renewal_response(updated)

    def _begin_microsoft_graph_link(
        self,
        request: Mapping[str, Any],
        anchor: Anchor,
        idempotency_key_hash: str | None,
    ) -> dict[str, Any]:
        """Start a v1 Planner link or one feature-scoped Office v2 link."""

        _require_fields(
            request,
            {"profile_ref"},
            {"planner_plan_ids", "office_v2_scope"},
        )
        if ("planner_plan_ids" in request) == ("office_v2_scope" in request):
            raise AccountCenterError("invalid_request")
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        external_subject = _trusted_entra_external_subject(anchor)
        if self._microsoft_graph_oauth is None or self._microsoft_graph_callback_uri is None:
            raise AccountCenterError("provider_not_configured")
        access_mode = self._configured_profile_access_mode(profile_ref)
        if access_mode != "actor_owned" or profile_ref in self._workspace_operator_profile_refs:
            raise AccountCenterError("profile_unavailable")
        if "office_v2_scope" in request:
            target_scope = _safe_microsoft_graph_profile_scope(request["office_v2_scope"])
            tenant_id, _ = _graph_subject_parts(external_subject)
            if (
                target_scope.get("schema_version") != 2
                or target_scope["tenant_id"] != tenant_id
                or not isinstance(anchor.workspace_id, str)
                or target_scope["workspace_id"] != anchor.workspace_id
            ):
                raise AccountCenterError("profile_scope_mismatch")
            selection = target_scope
            oauth_scopes = _expected_microsoft_graph_scopes(target_scope)
        else:
            plan_ids = _safe_graph_planner_ids(request["planner_plan_ids"])
            selection = {"planner_plan_ids": plan_ids}
            oauth_scopes = INITIAL_GRAPH_SCOPES
        return self._start_microsoft_graph_oauth_session(
            profile_ref=profile_ref,
            flow="link",
            anchor=anchor,
            target_scope_json=canonical_json(selection).decode("utf-8"),
            oauth_scopes=tuple(sorted(oauth_scopes)),
            access_mode=access_mode,
            profile_revision=None,
            idempotency_key_hash=idempotency_key_hash,
        )

    def _begin_microsoft_graph_scope_upgrade(
        self,
        request: Mapping[str, Any],
        anchor: Anchor,
        idempotency_key_hash: str | None,
    ) -> dict[str, Any]:
        """Request one closed permission upgrade for an existing owned profile."""

        _require_fields(
            request,
            {"profile_ref", "permission"},
            {"site_id", "list_ids", "mail_features"},
        )
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        permission = request["permission"]
        profile = self._store.get_provider_profile(profile_ref, anchor)
        if (
            profile["provider"] != "microsoft_graph"
            or profile["auth_mode"] != "delegated_oauth"
            or profile["auth_status"] != "healthy"
        ):
            raise AccountCenterError("profile_provider_mismatch")
        self._require_entra_profile_owner(profile, anchor)
        if self._microsoft_graph_oauth is None or self._microsoft_graph_callback_uri is None:
            raise AccountCenterError("provider_not_configured")
        target_scope = _safe_microsoft_graph_profile_scope(
            _json_object(profile["target_scope_json"])
        )
        current_scopes = _safe_microsoft_graph_oauth_scopes(
            _json_array(profile["oauth_scopes_json"])
        )
        if current_scopes != _expected_microsoft_graph_scopes(target_scope):
            raise AccountCenterError("profile_unavailable")
        next_scopes = set(current_scopes)
        if permission == "planner_tasks_write":
            if "site_id" in request or "list_ids" in request or "mail_features" in request:
                raise AccountCenterError("invalid_request")
            if target_scope.get("schema_version") == 2:
                raise AccountCenterError("office_v2_scope_upgrade_requires_relink")
            if target_scope["planner"]["tasks_write"]:
                raise AccountCenterError("scope_already_granted")
            target_scope["planner"]["tasks_write"] = True
            next_scopes.add("Tasks.ReadWrite")
        elif permission == "sharepoint_selected":
            if (
                "site_id" not in request
                or "list_ids" not in request
                or "mail_features" in request
            ):
                raise AccountCenterError("invalid_request")
            if target_scope.get("schema_version") == 2:
                raise AccountCenterError("office_v2_scope_upgrade_requires_relink")
            site_id = _safe_graph_site_id(request["site_id"])
            list_ids = _safe_graph_uuid_list(request["list_ids"])
            if target_scope["sharepoint"]["enabled"]:
                raise AccountCenterError("scope_already_granted")
            target_scope["sharepoint"] = {
                "enabled": True,
                "site_id": site_id,
                "list_ids": list_ids,
            }
            next_scopes.add("Sites.Selected")
        elif permission == "mail":
            if "site_id" in request or "list_ids" in request or "mail_features" not in request:
                raise AccountCenterError("invalid_request")
            if target_scope.get("schema_version") != 2:
                raise AccountCenterError("office_v2_scope_upgrade_requires_relink")
            mail_features = _safe_microsoft_graph_mail_feature_request(request["mail_features"])
            mail = target_scope["mail"]
            if not any(
                requested and not mail[target]
                for requested, target in (
                    (mail_features["metadata_read"], "metadata_read"),
                    (mail_features["drafts_write"], "drafts_write"),
                    (mail_features["send"], "send_enabled"),
                )
            ):
                raise AccountCenterError("scope_already_granted")
            mail["metadata_read"] = mail["metadata_read"] or mail_features["metadata_read"]
            mail["drafts_write"] = mail["drafts_write"] or mail_features["drafts_write"]
            mail["send_enabled"] = mail["send_enabled"] or mail_features["send"]
            mail["enabled"] = bool(
                mail["metadata_read"] or mail["drafts_write"] or mail["send_enabled"]
            )
            next_scopes = _expected_microsoft_graph_scopes(target_scope)
        else:
            raise AccountCenterError("invalid_request")
        return self._start_microsoft_graph_oauth_session(
            profile_ref=profile_ref,
            flow="scope_upgrade",
            anchor=anchor,
            target_scope_json=canonical_json(target_scope).decode("utf-8"),
            oauth_scopes=tuple(sorted(next_scopes)),
            access_mode=profile["access_mode"],
            profile_revision=int(profile["credential_revision"]),
            idempotency_key_hash=idempotency_key_hash,
        )

    def _update_microsoft_graph_v2_scope(
        self, request: Mapping[str, Any], anchor: Anchor
    ) -> dict[str, Any]:
        """CAS-update only the selected targets on one v2 actor-owned profile."""

        _require_fields(
            request,
            {
                "profile_ref",
                "workspace_id",
                "expected_credential_revision",
                "target_scope",
            },
        )
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        workspace_id = _safe_graph_uuid(request["workspace_id"])
        revision = request["expected_credential_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise AccountCenterError("invalid_request")
        target_scope = _safe_microsoft_graph_profile_scope(request["target_scope"])
        external_subject = _trusted_entra_external_subject(anchor)
        tenant_id, _ = _graph_subject_parts(external_subject)
        if (
            workspace_id != anchor.workspace_id
            or target_scope.get("schema_version") != 2
            or target_scope["workspace_id"] != workspace_id
            or target_scope["tenant_id"] != tenant_id
        ):
            raise AccountCenterError("profile_scope_mismatch")

        profile = self._store.get_provider_profile(profile_ref, anchor)
        if (
            profile["provider"] != "microsoft_graph"
            or profile["auth_mode"] != "delegated_oauth"
            or profile["auth_status"] != "healthy"
            or profile["access_mode"] != "actor_owned"
            or profile["external_subject"] != external_subject
            or profile["credential_revision"] != revision
        ):
            raise AccountCenterError("profile_revision_conflict")
        current_scope = _safe_microsoft_graph_profile_scope(
            _json_object(profile["target_scope_json"])
        )
        if current_scope.get("schema_version") != 2:
            raise AccountCenterError("profile_provider_mismatch")

        # Only resource selections may change; OAuth feature grants and fixed
        # privacy flags stay byte-for-byte equivalent after normalization.
        if (
            current_scope["tenant_id"] != target_scope["tenant_id"]
            or current_scope["workspace_id"] != target_scope["workspace_id"]
            or _microsoft_graph_scope_permissions(current_scope)
            != _microsoft_graph_scope_permissions(target_scope)
        ):
            raise AccountCenterError("scope_upgrade_required")
        oauth_scopes = _safe_microsoft_graph_oauth_scopes(_json_array(profile["oauth_scopes_json"]))
        if oauth_scopes != _expected_microsoft_graph_scopes(target_scope):
            raise AccountCenterError("scope_upgrade_required")
        next_revision = self._store.update_provider_profile_target_scope(
            profile_ref=profile_ref,
            anchor=anchor,
            expected_revision=revision,
            target_scope_json=canonical_json(target_scope).decode("utf-8"),
            now=int(self._now()),
        )
        return {
            "profile_ref": profile_ref,
            "provider": "microsoft_graph",
            "workspace_id": workspace_id,
            "scope_schema_version": 2,
            "target_scope": target_scope,
            "oauth_scopes": sorted(oauth_scopes),
            "auth_status": "healthy",
            "reauth_required": False,
            "credential_revision": next_revision,
            "expires_at": profile["credential_expires_at"],
            "refresh_expires_at": profile["credential_refresh_expires_at"],
        }

    def _start_microsoft_graph_oauth_session(
        self,
        *,
        profile_ref: str,
        flow: str,
        anchor: Anchor,
        target_scope_json: str,
        oauth_scopes: tuple[str, ...],
        access_mode: str,
        profile_revision: int | None,
        idempotency_key_hash: str | None,
    ) -> dict[str, Any]:
        """Persist one owner-scoped PKCE/state/nonce session before redirect."""

        client = self._microsoft_graph_oauth
        callback_uri = self._microsoft_graph_callback_uri
        client_id = self._microsoft_graph_client_id
        if client is None or callback_uri is None or not isinstance(client_id, str):
            raise AccountCenterError("provider_not_configured")
        state = _secret_text()
        nonce = _secret_text()
        verifier = _secret_text()
        session_ref = str(uuid.uuid4())
        now = int(self._now())
        expires_at = now + SESSION_TTL_SECONDS
        graph_scopes = tuple(sorted(_safe_microsoft_graph_oauth_scopes(list(oauth_scopes))))
        oauth_scopes_json = canonical_json(list(graph_scopes)).decode("utf-8")
        try:
            authorization_url = client.authorization_url(
                redirect_uri=callback_uri,
                state=state,
                nonce=nonce,
                code_challenge=pkce_s256_challenge(verifier),
                scopes=("openid", "profile", "offline_access", *graph_scopes),
                prompt="consent" if flow == "scope_upgrade" else None,
            )
        except MicrosoftGraphOAuthError as error:
            raise AccountCenterError(error.code) from None
        response = {
            "link_session_ref": session_ref,
            "authorization_url": authorization_url,
            "expires_at": expires_at,
        }
        command_result = (
            (
                idempotency_key_hash,
                self._encode_idempotency_response(idempotency_key_hash, {"value": response}),
            )
            if idempotency_key_hash is not None
            else None
        )
        self._store.create_microsoft_entra_oauth_session(
            session_ref=session_ref,
            profile_ref=profile_ref,
            provider="microsoft_graph",
            flow=flow,
            anchor=anchor,
            state_digest=_digest_text(state),
            nonce_digest=_digest_text(nonce),
            callback_uri=callback_uri,
            code_verifier_blob=self._encrypt_session_secret(session_ref, verifier),
            target_scope_json=target_scope_json,
            oauth_scopes_json=oauth_scopes_json,
            oauth_client_id=client_id,
            access_mode=access_mode,
            profile_revision=profile_revision,
            expires_at=expires_at,
            now=now,
            command_result=command_result,
        )
        return response

    def _complete_microsoft_graph_link(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Consume one Graph callback and create its encrypted provider profile."""

        return self._complete_microsoft_graph_oauth(request, expected_flow="link")

    def _complete_microsoft_graph_scope_upgrade(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Consume one consent upgrade and atomically replace the profile tokens."""

        return self._complete_microsoft_graph_oauth(request, expected_flow="scope_upgrade")

    def _complete_microsoft_graph_oauth(
        self, request: Mapping[str, Any], *, expected_flow: str
    ) -> dict[str, Any]:
        """Verify callback identity and commit token rotation before success."""

        _require_fields(request, {"state", "code"})
        client = self._microsoft_graph_oauth
        callback_uri = self._microsoft_graph_callback_uri
        if client is None or callback_uri is None:
            raise AccountCenterError("provider_not_configured")
        state = _required_text(request, "state", maximum=256)
        code = _required_text(request, "code", maximum=4096)
        session = self._store.claim_microsoft_entra_oauth_session(
            _digest_text(state), callback_uri, now=int(self._now())
        )
        session_ref = str(session["session_ref"])
        try:
            if (
                session["provider"] != "microsoft_graph"
                or session["flow"] != expected_flow
                or session["access_mode"] != "actor_owned"
            ):
                raise AccountCenterError("link_session_invalid")
            verifier_blob = session["code_verifier_blob"]
            if not isinstance(verifier_blob, bytes) or not verifier_blob:
                raise AccountCenterError("link_session_invalid")
            verifier = self._decrypt_session_secret(session_ref, verifier_blob)
            graph_scopes = _safe_microsoft_graph_oauth_scopes(
                _json_array(session["oauth_scopes_json"])
            )
            requested_scopes = (
                "openid",
                "profile",
                "offline_access",
                *sorted(graph_scopes),
            )
            token, identity = client.exchange_code(
                code=code,
                redirect_uri=callback_uri,
                code_verifier=verifier,
                nonce_digest=str(session["nonce_digest"]),
                requested_scopes=requested_scopes,
            )
            tenant_id, object_id = _graph_subject_parts(identity.external_subject)
            if identity.tenant_id != tenant_id or identity.object_id != object_id:
                raise AccountCenterError("external_identity_mismatch")
            session_anchor = _anchor_from_session(session)
            if identity.external_subject != _trusted_entra_external_subject(session_anchor):
                raise AccountCenterError("external_identity_mismatch")
            actual_scopes = _safe_microsoft_graph_oauth_scopes(list(token.scopes))
            if graph_scopes != actual_scopes:
                raise AccountCenterError("scope_rejected")
            if not token.refresh_token:
                raise AccountCenterError("credential_unavailable")
            if expected_flow == "scope_upgrade":
                target_scope_json = str(session["target_scope_json"])
            else:
                selection = _json_object(session["target_scope_json"])
                if selection.get("schema_version") == 2:
                    target_scope = _safe_microsoft_graph_profile_scope(selection)
                    if (
                        target_scope["tenant_id"] != identity.tenant_id
                        or target_scope["workspace_id"] != session_anchor.workspace_id
                    ):
                        raise AccountCenterError("external_identity_mismatch")
                    target_scope_json = canonical_json(target_scope).decode("utf-8")
                else:
                    if set(selection) != {"planner_plan_ids"}:
                        raise AccountCenterError("link_session_invalid")
                    plan_ids = _safe_graph_planner_ids(selection["planner_plan_ids"])
                    target_scope_json = canonical_json(
                        {
                            "tenant_id": identity.tenant_id,
                            "planner": {
                                "plan_ids": plan_ids,
                                "tasks_read": True,
                                "tasks_write": False,
                            },
                            "sharepoint": {
                                "enabled": False,
                                "site_id": None,
                                "list_ids": [],
                            },
                        }
                    ).decode("utf-8")
            target_scope = _safe_microsoft_graph_profile_scope(_json_object(target_scope_json))
            if actual_scopes != _expected_microsoft_graph_scopes(target_scope):
                raise AccountCenterError("scope_rejected")
            saved_scopes_json = canonical_json(sorted(actual_scopes)).decode("utf-8")
            profile = self._store.complete_microsoft_entra_oauth_session(
                session_ref=session_ref,
                provider="microsoft_graph",
                anchor=_anchor_from_session(session),
                identity_ref=identity.external_subject,
                external_subject=identity.external_subject,
                target_scope_json=target_scope_json,
                credential_blob=self._encrypt_provider_token(
                    str(session["profile_ref"]), token.access_token, token.expires_at
                ),
                credential_refresh_blob=self._encrypt_provider_refresh_token(
                    str(session["profile_ref"]), token.refresh_token
                ),
                credential_expires_at=token.expires_at,
                credential_refresh_expires_at=token.refresh_expires_at,
                oauth_scopes_json=saved_scopes_json,
                oauth_client_id=str(session["oauth_client_id"]),
                now=int(self._now()),
            )
            response = {
                "profile_ref": profile["profile_ref"],
                "provider": "microsoft_graph",
                "auth_mode": "delegated_oauth",
                "access_mode": profile["access_mode"],
                "auth_status": "healthy",
                "external_subject": profile["external_subject"],
                "expires_at": profile["credential_expires_at"],
                "refresh_expires_at": profile["credential_refresh_expires_at"],
                "scope_schema_version": target_scope.get("schema_version", 1),
                "oauth_scopes": sorted(actual_scopes),
                "credential_revision": profile["credential_revision"],
                "reauth_required": False,
            }
            if target_scope.get("schema_version") == 2:
                response["workspace_id"] = target_scope["workspace_id"]
            return response
        except MicrosoftGraphOAuthError as error:
            self._fail_microsoft_entra_oauth_session(session_ref)
            raise AccountCenterError(error.code) from None
        except AccountCenterError:
            self._fail_microsoft_entra_oauth_session(session_ref)
            raise
        except Exception:  # noqa: BLE001 - callbacks never expose provider details.
            self._fail_microsoft_entra_oauth_session(session_ref)
            raise AccountCenterError("provider_unavailable") from None

    def _fail_microsoft_entra_oauth_session(self, session_ref: str) -> None:
        try:
            self._store.fail_microsoft_entra_oauth_session(session_ref, now=int(self._now()))
        except AccountCenterError:
            pass

    def _begin_azure_devops_link(
        self,
        request: Mapping[str, Any],
        anchor: Anchor,
        idempotency_key_hash: str | None,
    ) -> dict[str, Any]:
        """Start Entra consent for one host-authorized Azure DevOps source."""

        _require_fields(
            request,
            {
                "profile_ref",
                "tenant_id",
                "organization",
                "project_id",
                "source_id",
                "source_kind",
            },
            {"pipeline_id", "definition_id", "definition_environment_id"},
        )
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        if (
            profile_ref in self._workspace_operator_profile_refs
            or "azure_devops" not in self._provider_caller_ids
        ):
            raise AccountCenterError("profile_unavailable")
        client = self._azure_devops_oauth
        callback_uri = self._microsoft_graph_callback_uri
        client_id = self._azure_devops_client_id
        if client is None or callback_uri is None or not isinstance(client_id, str):
            raise AccountCenterError("provider_not_configured")
        target = {
            field: request[field]
            for field in (
                "tenant_id",
                "organization",
                "project_id",
                "source_id",
                "source_kind",
                "pipeline_id",
                "definition_id",
                "definition_environment_id",
            )
            if field in request
        }
        target_scope = _safe_azure_devops_profile_scope(target)
        expected_subject = _trusted_entra_external_subject(anchor)
        if target_scope["tenant_id"] != expected_subject.split(":", 1)[0]:
            raise AccountCenterError("external_identity_mismatch")
        if self._configured_profile_access_mode(profile_ref) != "actor_owned":
            raise AccountCenterError("profile_unavailable")
        state = _secret_text()
        nonce = _secret_text()
        verifier = _secret_text()
        session_ref = str(uuid.uuid4())
        now = int(self._now())
        expires_at = now + SESSION_TTL_SECONDS
        scopes = ("openid", "profile", "offline_access", AZURE_DEVOPS_RESOURCE_SCOPE)
        try:
            authorization_url = client.authorization_url(
                redirect_uri=callback_uri,
                state=state,
                nonce=nonce,
                code_challenge=pkce_s256_challenge(verifier),
                scopes=scopes,
            )
        except MicrosoftGraphOAuthError as error:
            raise AccountCenterError(error.code) from None
        response = {
            "link_session_ref": session_ref,
            "authorization_url": authorization_url,
            "expires_at": expires_at,
        }
        command_result = (
            (
                idempotency_key_hash,
                self._encode_idempotency_response(idempotency_key_hash, {"value": response}),
            )
            if idempotency_key_hash is not None
            else None
        )
        self._store.create_microsoft_entra_oauth_session(
            session_ref=session_ref,
            profile_ref=profile_ref,
            provider="azure_devops",
            flow="link",
            anchor=anchor,
            state_digest=_digest_text(state),
            nonce_digest=_digest_text(nonce),
            callback_uri=callback_uri,
            code_verifier_blob=self._encrypt_session_secret(session_ref, verifier),
            target_scope_json=canonical_json(target_scope).decode("utf-8"),
            oauth_scopes_json=canonical_json([AZURE_DEVOPS_RESOURCE_SCOPE]).decode("utf-8"),
            oauth_client_id=client_id,
            access_mode="actor_owned",
            profile_revision=None,
            expires_at=expires_at,
            now=now,
            command_result=command_result,
        )
        return response

    def _complete_azure_devops_link(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Verify Entra identity and persist the opaque ADO bearer pair."""

        _require_fields(request, {"state", "code"})
        client = self._azure_devops_oauth
        callback_uri = self._microsoft_graph_callback_uri
        if client is None or callback_uri is None:
            raise AccountCenterError("provider_not_configured")
        state = _required_text(request, "state", maximum=256)
        code = _required_text(request, "code", maximum=4096)
        session = self._store.claim_microsoft_entra_oauth_session(
            _digest_text(state), callback_uri, now=int(self._now())
        )
        session_ref = str(session["session_ref"])
        try:
            if (
                session["provider"] != "azure_devops"
                or session["flow"] != "link"
                or session["access_mode"] != "actor_owned"
            ):
                raise AccountCenterError("link_session_invalid")
            verifier_blob = session["code_verifier_blob"]
            if not isinstance(verifier_blob, bytes) or not verifier_blob:
                raise AccountCenterError("link_session_invalid")
            verifier = self._decrypt_session_secret(session_ref, verifier_blob)
            scopes = (
                "openid",
                "profile",
                "offline_access",
                AZURE_DEVOPS_RESOURCE_SCOPE,
            )
            token, identity = client.exchange_code(
                code=code,
                redirect_uri=callback_uri,
                code_verifier=verifier,
                nonce_digest=str(session["nonce_digest"]),
                requested_scopes=scopes,
            )
            tenant_id, object_id = _graph_subject_parts(identity.external_subject)
            target_scope = _safe_azure_devops_profile_scope(
                _json_object(session["target_scope_json"])
            )
            if (
                identity.tenant_id != tenant_id
                or identity.object_id != object_id
                or identity.external_subject
                != _trusted_entra_external_subject(_anchor_from_session(session))
                or tenant_id != target_scope["tenant_id"]
                or set(token.scopes) != {AZURE_DEVOPS_RESOURCE_SCOPE}
                or not token.refresh_token
            ):
                raise AccountCenterError("external_identity_mismatch")
            oauth_scopes_json = canonical_json([AZURE_DEVOPS_RESOURCE_SCOPE]).decode("utf-8")
            profile = self._store.complete_microsoft_entra_oauth_session(
                session_ref=session_ref,
                provider="azure_devops",
                anchor=_anchor_from_session(session),
                identity_ref=identity.external_subject,
                external_subject=identity.external_subject,
                target_scope_json=canonical_json(target_scope).decode("utf-8"),
                credential_blob=self._encrypt_provider_token(
                    str(session["profile_ref"]), token.access_token, token.expires_at
                ),
                credential_refresh_blob=self._encrypt_provider_refresh_token(
                    str(session["profile_ref"]), token.refresh_token
                ),
                credential_expires_at=token.expires_at,
                credential_refresh_expires_at=token.refresh_expires_at,
                oauth_scopes_json=oauth_scopes_json,
                oauth_client_id=str(session["oauth_client_id"]),
                now=int(self._now()),
            )
            return {
                "profile_ref": profile["profile_ref"],
                "provider": "azure_devops",
                "auth_mode": "delegated_oauth",
                "access_mode": "actor_owned",
                "auth_status": "healthy",
                "external_subject": profile["external_subject"],
                "expires_at": profile["credential_expires_at"],
            }
        except MicrosoftGraphOAuthError as error:
            self._fail_microsoft_entra_oauth_session(session_ref)
            raise AccountCenterError(error.code) from None
        except AccountCenterError:
            self._fail_microsoft_entra_oauth_session(session_ref)
            raise
        except Exception:  # noqa: BLE001 - no callback detail or tokens may escape.
            self._fail_microsoft_entra_oauth_session(session_ref)
            raise AccountCenterError("provider_unavailable") from None

    def _huggingface_oauth_seed(self, secret_ref: str, scopes: Mapping[str, Any]) -> Any:
        """Read and validate one launcher-provided OAuth seed without logging it."""

        serialized = self._provider_secrets.get(secret_ref)
        if serialized is None:
            raise AccountCenterError("credential_unavailable")
        try:
            seed = parse_device_oauth_seed(
                serialized,
                expected_secret_ref=secret_ref,
            )
            seed_scope = _safe_huggingface_scopes(seed.target_scope)
        except HuggingFaceOAuthError as error:
            raise AccountCenterError(error.code) from None
        if canonical_json(seed_scope) != canonical_json(scopes):
            raise AccountCenterError("authorization_scope_mismatch")
        return seed

    def _register_host_identity_profile(
        self, request: Mapping[str, Any], anchor: Anchor
    ) -> dict[str, Any]:
        """Register only metadata for host-managed cloud credentials."""

        _require_fields(request, {"profile_ref", "provider", "identity_ref", "target_scope"})
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        provider = request["provider"]
        if provider not in {"azure", "gcp", "runpod"}:
            raise AccountCenterError("invalid_request")
        identity_ref = _safe_identity_ref(request["identity_ref"])
        target_scope = _safe_profile_scope(request["target_scope"])
        self._store.create_provider_profile(
            profile_ref=profile_ref,
            anchor=anchor,
            provider=provider,
            identity_ref=identity_ref,
            target_scope_json=canonical_json(target_scope).decode("utf-8"),
            auth_status="unknown",
            external_subject=None,
            credential_blob=None,
            credential_expires_at=None,
            credential_secret_ref=None,
            access_mode=self._configured_profile_access_mode(profile_ref),
            now=int(self._now()),
        )
        return {
            "profile_ref": profile_ref,
            "provider": provider,
            "identity_ref": identity_ref,
            "target_scope": target_scope,
            "access_mode": self._configured_profile_access_mode(profile_ref),
            "auth_status": "unknown",
            "external_subject": None,
            "expires_at": None,
        }

    def _list_provider_profiles(
        self, request: Mapping[str, Any], anchor: Anchor, caller_id: str
    ) -> dict[str, Any]:
        """Return profile references, scope, and health without secret material."""

        _require_fields(request, set())
        graph_provider_caller = self._provider_caller_ids.get("microsoft_graph")
        graph_provider_view = caller_id == graph_provider_caller
        if graph_provider_view:
            _trusted_entra_external_subject(anchor)
        rows = self._store.list_provider_profiles(anchor)
        if graph_provider_view:
            rows = [row for row in rows if row["provider"] == "microsoft_graph"]
        profiles = []
        for row in rows:
            if graph_provider_view:
                self._require_entra_profile_owner(row, anchor)
            try:
                scope = json.loads(row["target_scope_json"])
            except (TypeError, json.JSONDecodeError):
                raise AccountCenterError("profile_unavailable") from None
            profile = {
                "profile_ref": row["profile_ref"],
                "provider": row["provider"],
                "identity_ref": row["identity_ref"],
                "target_scope": scope,
                "auth_mode": row["auth_mode"],
                "access_mode": row["access_mode"],
                "auth_status": row["auth_status"],
                "external_subject": row["external_subject"],
                "expires_at": row["credential_expires_at"],
                "refresh_expires_at": row["credential_refresh_expires_at"],
                "credential_revision": row["credential_revision"],
            }
            if row["provider"] == "huggingface" and row["auth_mode"] == "device_oauth":
                if row["oauth_scopes_json"] is None:
                    oauth_scopes = None
                else:
                    try:
                        oauth_scopes = json.loads(row["oauth_scopes_json"])
                    except (TypeError, json.JSONDecodeError):
                        raise AccountCenterError("profile_unavailable") from None
                    if not isinstance(oauth_scopes, list):
                        raise AccountCenterError("profile_unavailable")
                profile["oauth_scopes"] = oauth_scopes
            if row["provider"] == "microsoft_graph":
                profile_scope = _safe_microsoft_graph_profile_scope(scope)
                oauth_scopes = _safe_microsoft_graph_oauth_scopes(
                    _json_array(row["oauth_scopes_json"])
                )
                if oauth_scopes != _expected_microsoft_graph_scopes(profile_scope):
                    raise AccountCenterError("profile_unavailable")
                profile["target_scope"] = profile_scope
                profile["oauth_scopes"] = sorted(oauth_scopes)
                profile["scope_schema_version"] = profile_scope.get("schema_version", 1)
                profile["reauth_required"] = row["auth_status"] == "reauthorization_required"
            if row["provider"] == "azure_devops":
                profile["target_scope"] = _safe_azure_devops_profile_scope(scope)
                profile["oauth_scopes"] = sorted(
                    _safe_azure_devops_oauth_scopes(_json_array(row["oauth_scopes_json"]))
                )
            profiles.append(profile)
        return {"profiles": profiles}

    def _configured_profile_access_mode(self, profile_ref: str) -> str:
        """Apply only the launcher-owned workspace sharing allowlist."""

        if profile_ref in self._workspace_operator_profile_refs:
            return "workspace_operator"
        return "actor_owned"

    def _unlink_provider_profile(
        self, request: Mapping[str, Any], anchor: Anchor
    ) -> dict[str, Any]:
        """Revoke one local profile binding and clear its encrypted credential."""

        _require_fields(request, {"profile_ref"})
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        self._store.unlink_provider_profile(profile_ref, anchor, now=int(self._now()))
        return {"unlinked": True, "profile_ref": profile_ref, "status": "unlinked"}

    def _create_provider_credential_lease(
        self, request: Mapping[str, Any], anchor: Anchor, context: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Issue one profile-scoped lease for an exact provider operation."""

        _require_fields(
            request,
            {"profile_ref", "operation_id", "target", "arguments_sha256"},
            {"connector_grant"},
        )
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        operation_id = _required_text(request, "operation_id", maximum=200)
        arguments_sha256 = _sha256_text(request["arguments_sha256"])
        profile = self._store.get_provider_profile(
            profile_ref, anchor, allow_workspace_operator=True
        )
        provider = profile["provider"]
        if provider in {"microsoft_graph", "azure_devops"}:
            self._require_entra_profile_owner(profile, anchor)
        if operation_id not in PROVIDER_OPERATIONS_BY_PROVIDER.get(provider, set()):
            raise AccountCenterError("authorization_denied")
        now = int(self._now())
        grant = request.get("connector_grant")
        if profile["access_mode"] == "workspace_operator" and grant is None:
            raise AccountCenterError("connector_grant_required")
        if grant is not None:
            grant = _validated_connector_grant(
                grant,
                anchor=anchor,
                profile_ref=profile_ref,
                provider=provider,
                operation_id=operation_id,
                arguments_sha256=arguments_sha256,
                now=now,
            )
        target = _safe_provider_target(provider, operation_id, request["target"])
        scope = _json_object(profile["target_scope_json"])
        if not _provider_target_allowed(provider, operation_id, target, scope):
            raise AccountCenterError("authorization_denied")
        if provider == "microsoft_graph" and not _microsoft_graph_operation_authorized(
            operation_id, profile
        ):
            raise AccountCenterError("authorization_denied")
        if provider == "azure_devops" and not _azure_devops_operation_authorized(
            operation_id, profile
        ):
            raise AccountCenterError("authorization_denied")
        provider_caller = self._provider_caller_ids.get(provider)
        if provider_caller is None or profile["auth_status"] != "healthy":
            raise AccountCenterError("profile_unavailable")
        expires_at = now + LEASE_TTL_SECONDS
        if grant is not None:
            expires_at = min(expires_at, grant["expires_at"])
        target_json = canonical_json(target).decode("utf-8")
        lease_ref = self._store.create_provider_lease(
            profile_ref=profile_ref,
            anchor=anchor,
            binding_id=_required_text(context, "binding_id", maximum=200),
            provider_caller_id=provider_caller,
            operation_id=operation_id,
            target_json=target_json,
            arguments_sha256=arguments_sha256,
            expires_at=expires_at,
            now=now,
            allow_workspace_operator=(profile["access_mode"] == "workspace_operator"),
            grant_ref=grant["grant_ref"] if grant is not None else None,
            connection_ref=grant["connection_ref"] if grant is not None else None,
            grant_provider_id=grant["provider_id"] if grant is not None else None,
            grant_operation_id=grant["operation_id"] if grant is not None else None,
            resource_ref=grant["resource_ref"] if grant is not None else None,
            grant_issued_at=grant["issued_at"] if grant is not None else None,
            grant_expires_at=grant["expires_at"] if grant is not None else None,
        )
        response = {
            "lease_ref": lease_ref,
            "profile_ref": profile_ref,
            "provider": provider,
            "operation_id": operation_id,
            "target": target,
            "arguments_sha256": arguments_sha256,
            "expires_at": expires_at,
        }
        if provider == "microsoft_graph":
            graph_scope = _safe_microsoft_graph_profile_scope(scope)
            response["profile_scope_sha256"] = hashlib.sha256(
                canonical_json(graph_scope)
            ).hexdigest()
        if provider == "azure_devops":
            ado_scope = _safe_azure_devops_profile_scope(scope)
            response["profile_scope_sha256"] = hashlib.sha256(canonical_json(ado_scope)).hexdigest()
        return response

    def _redeem_provider_credential_lease(
        self,
        request: Mapping[str, Any],
        anchor: Anchor,
        caller_id: str,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Consume a matching lease and return short-lived material privately."""

        _require_fields(
            request,
            {"lease_ref", "profile_ref", "operation_id", "target", "arguments_sha256"},
        )
        lease_ref = _uuid_text(request["lease_ref"])
        profile_ref = _opaque_profile_ref(request["profile_ref"])
        operation_id = _required_text(request, "operation_id", maximum=200)
        arguments_sha256 = _sha256_text(request["arguments_sha256"])
        profile = self._store.get_provider_profile(
            profile_ref, anchor, allow_workspace_operator=True
        )
        provider = profile["provider"]
        if provider in {"microsoft_graph", "azure_devops"}:
            self._require_entra_profile_owner(profile, anchor)
        if caller_id != self._provider_caller_ids.get(provider):
            raise AccountCenterError("untrusted_caller")
        if operation_id not in PROVIDER_OPERATIONS_BY_PROVIDER.get(provider, set()):
            raise AccountCenterError("lease_rejected")
        target = _safe_provider_target(provider, operation_id, request["target"])
        scope = _json_object(profile["target_scope_json"])
        if not _provider_target_allowed(provider, operation_id, target, scope):
            raise AccountCenterError("lease_rejected")
        if provider == "microsoft_graph" and not _microsoft_graph_operation_authorized(
            operation_id, profile
        ):
            raise AccountCenterError("lease_rejected")
        if provider == "azure_devops" and not _azure_devops_operation_authorized(
            operation_id, profile
        ):
            raise AccountCenterError("lease_rejected")
        profile = self._store.redeem_provider_lease(
            lease_ref=lease_ref,
            profile_ref=profile_ref,
            anchor=anchor,
            caller_id=caller_id,
            binding_id=_required_text(context, "binding_id", maximum=200),
            operation_id=operation_id,
            target_json=canonical_json(target).decode("utf-8"),
            arguments_sha256=arguments_sha256,
            now=int(self._now()),
        )
        credential, expires_at = self._fresh_provider_credential(profile, anchor)
        response = {
            "access_token": credential,
            "token_type": "Bearer",
            "external_subject": profile["external_subject"] or profile["identity_ref"],
            "expires_at": expires_at,
        }
        if provider == "huggingface" and operation_id == "huggingface.vendor.cli.execute":
            response["profile_scope_sha256"] = hashlib.sha256(
                canonical_json(_safe_huggingface_scopes(scope))
            ).hexdigest()
        if provider == "microsoft_graph":
            graph_scope = _safe_microsoft_graph_profile_scope(scope)
            response["profile_scope_sha256"] = hashlib.sha256(
                canonical_json(graph_scope)
            ).hexdigest()
        if provider == "azure_devops":
            ado_scope = _safe_azure_devops_profile_scope(scope)
            response["profile_scope_sha256"] = hashlib.sha256(canonical_json(ado_scope)).hexdigest()
        return response

    def _fresh_provider_credential(
        self, profile: Mapping[str, Any], anchor: Anchor
    ) -> tuple[str, int | None]:
        """Reuse a cached installation token or mint one once near expiry."""

        profile_ref = str(profile["profile_ref"])
        if profile["provider"] == "azure_devops":
            return self._fresh_azure_devops_oauth_credential(profile, anchor)
        if profile["provider"] == "microsoft_graph":
            return self._fresh_microsoft_graph_oauth_credential(profile, anchor)
        if profile["provider"] == "huggingface":
            if profile["auth_mode"] == "device_oauth":
                return self._fresh_huggingface_oauth_credential(profile, anchor)
            if profile["credential_blob"] is None:
                raise AccountCenterError("credential_unavailable")
            credential, expires_at = self._decrypt_provider_token(
                profile_ref, bytes(profile["credential_blob"])
            )
            if expires_at is not None and expires_at <= int(self._now()):
                self._store.update_provider_profile_auth_status(
                    profile_ref,
                    anchor,
                    "reauthorization_required",
                    now=int(self._now()),
                    allow_workspace_operator=True,
                )
                raise AccountCenterError("credential_reauthorization_required")
            return credential, expires_at
        if profile["provider"] != "github":
            raise AccountCenterError("profile_provider_mismatch")
        with self._provider_refresh_lock:
            lock = self._provider_refresh_locks.setdefault(profile_ref, threading.Lock())
        with lock:
            current = self._store.get_provider_profile(
                profile_ref, anchor, allow_workspace_operator=True
            )
            expires_at = current["credential_expires_at"]
            if (
                current["credential_blob"] is not None
                and isinstance(expires_at, int)
                and expires_at > int(self._now()) + 60
            ):
                return self._decrypt_provider_token(profile_ref, bytes(current["credential_blob"]))
            if self._github_app is None:
                self._store.update_provider_profile_auth_status(
                    profile_ref,
                    anchor,
                    "reauthorization_required",
                    now=int(self._now()),
                    allow_workspace_operator=True,
                )
                raise AccountCenterError("provider_not_configured")
            scope = _json_object(current["target_scope_json"])
            repositories = _safe_github_repositories(scope.get("repositories"))
            try:
                token = self._github_app.create_installation_token(
                    str(current["identity_ref"]),
                    [repository["repository_id"] for repository in repositories],
                )
            except GitHubAppError as error:
                auth_status = (
                    "reauthorization_required"
                    if error.code
                    in {
                        "provider_authentication_failed",
                        "installation_not_found",
                        "credential_refresh_unknown",
                    }
                    else "unavailable"
                )
                self._store.update_provider_profile_auth_status(
                    profile_ref,
                    anchor,
                    auth_status,
                    now=int(self._now()),
                    allow_workspace_operator=True,
                )
                raise AccountCenterError(error.code) from None
            encrypted = self._encrypt_provider_token(
                profile_ref, token.access_token, token.expires_at
            )
            self._store.update_provider_profile_credential(
                profile_ref=profile_ref,
                anchor=anchor,
                credential_blob=encrypted,
                credential_expires_at=token.expires_at,
                external_subject=str(current["external_subject"]),
                auth_status="healthy",
                now=int(self._now()),
                allow_workspace_operator=True,
            )
            return token.access_token, token.expires_at

    def _fresh_azure_devops_oauth_credential(
        self, profile: Mapping[str, Any], anchor: Anchor
    ) -> tuple[str, int]:
        """Refresh one opaque ADO resource token under the profile claim."""

        self._require_entra_profile_owner(profile, anchor)
        profile_ref = str(profile["profile_ref"])
        with self._provider_refresh_lock:
            lock = self._provider_refresh_locks.setdefault(profile_ref, threading.Lock())
        with lock:
            deadline = time.monotonic() + 18
            while True:
                state, current, claim_id = self._store.claim_provider_oauth_refresh(
                    profile_ref,
                    anchor,
                    provider="azure_devops",
                    auth_mode="delegated_oauth",
                    now=int(self._now()),
                )
                if state == "ready" and current is not None:
                    if current["credential_blob"] is None:
                        raise AccountCenterError("credential_unavailable")
                    token, expires_at = self._decrypt_provider_token(
                        profile_ref, bytes(current["credential_blob"])
                    )
                    if not isinstance(expires_at, int):
                        raise AccountCenterError("credential_unavailable")
                    return token, expires_at
                if state == "reauthorization_required":
                    raise AccountCenterError("credential_reauthorization_required")
                if state == "claimed" and current is not None and claim_id is not None:
                    break
                if state != "waiting" or time.monotonic() >= deadline:
                    raise AccountCenterError("credential_refresh_in_progress")
                time.sleep(0.05)

            refresh_blob = current["credential_refresh_blob"]
            if (
                refresh_blob is None
                or current["oauth_client_id"] != self._azure_devops_client_id
                or self._azure_devops_oauth is None
            ):
                self._release_azure_devops_refresh(
                    profile_ref, anchor, claim_id, clear_credentials=True
                )
                raise AccountCenterError("credential_reauthorization_required")
            try:
                refresh_token = self._decrypt_provider_refresh_token(
                    profile_ref, bytes(refresh_blob)
                )
                scopes = _safe_azure_devops_oauth_scopes(_json_array(current["oauth_scopes_json"]))
                tenant_id, _object_id = _graph_subject_parts(current["external_subject"])
                expected_subject = _trusted_entra_external_subject(anchor)
                scope = _safe_azure_devops_profile_scope(_json_object(current["target_scope_json"]))
                if (
                    current["identity_ref"] != current["external_subject"]
                    or current["external_subject"] != expected_subject
                    or scope["tenant_id"] != tenant_id
                ):
                    raise AccountCenterError("external_identity_mismatch")
                refreshed = self._azure_devops_oauth.refresh(
                    tenant_id=tenant_id,
                    refresh_token=refresh_token,
                    requested_scopes=tuple(sorted(scopes)),
                )
                if set(refreshed.scopes) != scopes:
                    raise AccountCenterError("scope_rejected")
                if (
                    refreshed.identity is None
                    or refreshed.identity.external_subject != expected_subject
                    or refreshed.identity.tenant_id != tenant_id
                ):
                    raise AccountCenterError("external_identity_mismatch")
                next_refresh_token = refreshed.refresh_token or refresh_token
                self._store.complete_provider_oauth_refresh(
                    profile_ref=profile_ref,
                    anchor=anchor,
                    claim_id=claim_id,
                    credential_blob=self._encrypt_provider_token(
                        profile_ref, refreshed.access_token, refreshed.expires_at
                    ),
                    credential_refresh_blob=self._encrypt_provider_refresh_token(
                        profile_ref, next_refresh_token
                    ),
                    credential_expires_at=refreshed.expires_at,
                    credential_refresh_expires_at=current["credential_refresh_expires_at"],
                    oauth_scopes_json=canonical_json(sorted(scopes)).decode("utf-8"),
                    provider="azure_devops",
                    auth_mode="delegated_oauth",
                    now=int(self._now()),
                )
                return refreshed.access_token, refreshed.expires_at
            except MicrosoftGraphOAuthError as error:
                self._release_azure_devops_refresh(
                    profile_ref, anchor, claim_id, clear_credentials=True
                )
                raise AccountCenterError(error.code) from None
            except AccountCenterError:
                self._release_azure_devops_refresh(
                    profile_ref, anchor, claim_id, clear_credentials=True
                )
                raise
            except Exception:  # noqa: BLE001 - rotated resource tokens are uncertain.
                self._release_azure_devops_refresh(
                    profile_ref, anchor, claim_id, clear_credentials=True
                )
                raise AccountCenterError("credential_refresh_unknown") from None

    def _release_azure_devops_refresh(
        self,
        profile_ref: str,
        anchor: Anchor,
        claim_id: str,
        *,
        clear_credentials: bool,
    ) -> None:
        """Invalidate an ambiguous ADO refresh and its outstanding leases."""

        try:
            self._store.release_provider_oauth_refresh(
                profile_ref=profile_ref,
                anchor=anchor,
                claim_id=claim_id,
                now=int(self._now()),
                provider="azure_devops",
                auth_mode="delegated_oauth",
                auth_status="reauthorization_required",
                clear_credentials=clear_credentials,
            )
        except AccountCenterError:
            return

    def _fresh_microsoft_graph_oauth_credential(
        self, profile: Mapping[str, Any], anchor: Anchor
    ) -> tuple[str, int]:
        """Refresh Graph credentials under the shared durable profile claim."""

        self._require_entra_profile_owner(profile, anchor)
        profile_ref = str(profile["profile_ref"])
        with self._provider_refresh_lock:
            lock = self._provider_refresh_locks.setdefault(profile_ref, threading.Lock())
        with lock:
            deadline = time.monotonic() + 18
            while True:
                state, current, claim_id = self._store.claim_provider_oauth_refresh(
                    profile_ref,
                    anchor,
                    provider="microsoft_graph",
                    auth_mode="delegated_oauth",
                    now=int(self._now()),
                )
                if state == "ready" and current is not None:
                    if current["credential_blob"] is None:
                        raise AccountCenterError("credential_unavailable")
                    access_token, expires_at = self._decrypt_provider_token(
                        profile_ref, bytes(current["credential_blob"])
                    )
                    if not isinstance(expires_at, int):
                        raise AccountCenterError("credential_unavailable")
                    return access_token, expires_at
                if state == "reauthorization_required":
                    raise AccountCenterError("credential_reauthorization_required")
                if state == "claimed" and current is not None and claim_id is not None:
                    break
                if state != "waiting" or time.monotonic() >= deadline:
                    raise AccountCenterError("credential_refresh_in_progress")
                time.sleep(0.05)

            refresh_blob = current["credential_refresh_blob"]
            stored_client_id = current["oauth_client_id"]
            if (
                refresh_blob is None
                or not isinstance(stored_client_id, str)
                or stored_client_id != self._microsoft_graph_client_id
                or self._microsoft_graph_oauth is None
            ):
                self._release_microsoft_graph_refresh(
                    profile_ref, anchor, claim_id, clear_credentials=True
                )
                raise AccountCenterError("credential_reauthorization_required")
            try:
                refresh_token = self._decrypt_provider_refresh_token(
                    profile_ref, bytes(refresh_blob)
                )
                scopes = _safe_microsoft_graph_oauth_scopes(
                    _json_array(current["oauth_scopes_json"])
                )
                current_scope = _safe_microsoft_graph_profile_scope(
                    _json_object(current["target_scope_json"])
                )
                if scopes != _expected_microsoft_graph_scopes(current_scope):
                    raise AccountCenterError("credential_reauthorization_required")
                tenant_id, expected_object_id = _graph_subject_parts(current["external_subject"])
                expected_subject = _trusted_entra_external_subject(anchor)
                refreshed = self._microsoft_graph_oauth.refresh(
                    tenant_id=tenant_id,
                    refresh_token=refresh_token,
                    requested_scopes=tuple(sorted(scopes)),
                )
                refreshed_scopes = set(refreshed.scopes)
                if refreshed_scopes != scopes:
                    raise AccountCenterError("scope_rejected")
                if (
                    refreshed.identity is None
                    or refreshed.identity.external_subject != expected_subject
                    or refreshed.identity.tenant_id != tenant_id
                    or refreshed.identity.object_id != expected_object_id
                ):
                    raise AccountCenterError("external_identity_mismatch")
                identity = self._microsoft_graph_oauth.verify_identity(
                    refreshed.access_token, tenant_id
                )
                if (
                    identity.tenant_id != tenant_id
                    or identity.object_id != expected_object_id
                    or identity.external_subject != current["external_subject"]
                    or identity.external_subject != expected_subject
                ):
                    raise AccountCenterError("external_identity_mismatch")
                next_refresh_token = refreshed.refresh_token or refresh_token
                self._store.complete_provider_oauth_refresh(
                    profile_ref=profile_ref,
                    anchor=anchor,
                    claim_id=claim_id,
                    credential_blob=self._encrypt_provider_token(
                        profile_ref, refreshed.access_token, refreshed.expires_at
                    ),
                    credential_refresh_blob=self._encrypt_provider_refresh_token(
                        profile_ref, next_refresh_token
                    ),
                    credential_expires_at=refreshed.expires_at,
                    credential_refresh_expires_at=current["credential_refresh_expires_at"],
                    oauth_scopes_json=canonical_json(sorted(refreshed_scopes)).decode("utf-8"),
                    provider="microsoft_graph",
                    auth_mode="delegated_oauth",
                    now=int(self._now()),
                )
                return refreshed.access_token, refreshed.expires_at
            except MicrosoftGraphOAuthError as error:
                self._release_microsoft_graph_refresh(
                    profile_ref, anchor, claim_id, clear_credentials=True
                )
                raise AccountCenterError(error.code) from None
            except AccountCenterError:
                self._release_microsoft_graph_refresh(
                    profile_ref, anchor, claim_id, clear_credentials=True
                )
                raise
            except Exception:  # noqa: BLE001 - rotated refresh outcomes are uncertain.
                self._release_microsoft_graph_refresh(
                    profile_ref, anchor, claim_id, clear_credentials=True
                )
                raise AccountCenterError("credential_refresh_unknown") from None

    def _require_entra_profile_owner(self, profile: Mapping[str, Any], anchor: Anchor) -> str:
        """Require a Microsoft profile to match its trusted Entra actor anchor."""

        expected_subject = _trusted_entra_external_subject(anchor)
        profile_ref = str(profile["profile_ref"])
        if (
            profile["provider"] not in {"microsoft_graph", "azure_devops"}
            or profile["auth_mode"] != "delegated_oauth"
            or profile["access_mode"] != "actor_owned"
            or profile["external_subject"] != expected_subject
            or profile["identity_ref"] != expected_subject
        ):
            try:
                self._store.require_provider_reauthorization(
                    profile_ref, anchor, now=int(self._now())
                )
            except AccountCenterError:
                pass
            raise AccountCenterError("external_identity_mismatch")
        try:
            tenant_id, object_id = _graph_subject_parts(profile["external_subject"])
        except AccountCenterError:
            try:
                self._store.require_provider_reauthorization(
                    profile_ref, anchor, now=int(self._now())
                )
            except AccountCenterError:
                pass
            raise AccountCenterError("external_identity_mismatch") from None
        if f"{tenant_id}:{object_id}" != expected_subject:
            raise AccountCenterError("external_identity_mismatch")
        return expected_subject

    def _release_microsoft_graph_refresh(
        self,
        profile_ref: str,
        anchor: Anchor,
        claim_id: str,
        *,
        clear_credentials: bool,
    ) -> None:
        """Clear ambiguous Graph refresh material and invalidate outstanding leases."""

        try:
            self._store.release_provider_oauth_refresh(
                profile_ref=profile_ref,
                anchor=anchor,
                claim_id=claim_id,
                now=int(self._now()),
                provider="microsoft_graph",
                auth_mode="delegated_oauth",
                auth_status="reauthorization_required",
                clear_credentials=clear_credentials,
            )
        except AccountCenterError:
            return

    def _fresh_huggingface_oauth_credential(
        self, profile: Mapping[str, Any], anchor: Anchor
    ) -> tuple[str, int | None]:
        """Refresh an HF OAuth grant under a persisted cross-process claim."""

        profile_ref = str(profile["profile_ref"])
        with self._provider_refresh_lock:
            lock = self._provider_refresh_locks.setdefault(profile_ref, threading.Lock())
        with lock:
            deadline = time.monotonic() + 18
            while True:
                state, current, claim_id = self._store.claim_provider_oauth_refresh(
                    profile_ref,
                    anchor,
                    provider="huggingface",
                    auth_mode="device_oauth",
                    now=int(self._now()),
                )
                if state == "ready" and current is not None:
                    if current["credential_blob"] is None:
                        raise AccountCenterError("credential_unavailable")
                    return self._decrypt_provider_token(
                        profile_ref, bytes(current["credential_blob"])
                    )
                if state == "reauthorization_required":
                    raise AccountCenterError("credential_reauthorization_required")
                if state == "claimed" and current is not None and claim_id is not None:
                    break
                if state != "waiting" or time.monotonic() >= deadline:
                    raise AccountCenterError("credential_refresh_in_progress")
                time.sleep(0.05)

            refresh_blob = current["credential_refresh_blob"]
            client_id = current["oauth_client_id"]
            if refresh_blob is None or not isinstance(client_id, str) or not client_id:
                self._release_huggingface_refresh(
                    profile_ref,
                    anchor,
                    claim_id,
                    auth_status="reauthorization_required",
                    clear_credentials=True,
                )
                raise AccountCenterError("credential_reauthorization_required")
            try:
                refresh_token = self._decrypt_provider_refresh_token(
                    profile_ref, bytes(refresh_blob)
                )
                now = int(self._now())
                refreshed = self._huggingface_oauth.refresh(
                    client_id=client_id,
                    refresh_token=refresh_token,
                    now=now,
                )
                external_subject = self._huggingface.whoami(refreshed.access_token)
                expected_subject = current["external_subject"]
                if not expected_subject or external_subject != expected_subject:
                    self._release_huggingface_refresh(
                        profile_ref,
                        anchor,
                        claim_id,
                        auth_status="reauthorization_required",
                        clear_credentials=True,
                    )
                    raise AccountCenterError("external_identity_mismatch")
                next_refresh_token = refreshed.refresh_token or refresh_token
                next_refresh_expires_at = (
                    refreshed.refresh_token_expires_at or current["credential_refresh_expires_at"]
                )
                next_oauth_scopes_json = (
                    canonical_json(list(refreshed.oauth_scopes)).decode("utf-8")
                    if refreshed.oauth_scopes is not None
                    else current["oauth_scopes_json"]
                )
                self._store.complete_provider_oauth_refresh(
                    profile_ref=profile_ref,
                    anchor=anchor,
                    claim_id=claim_id,
                    credential_blob=self._encrypt_provider_token(
                        profile_ref,
                        refreshed.access_token,
                        refreshed.access_token_expires_at,
                    ),
                    credential_refresh_blob=self._encrypt_provider_refresh_token(
                        profile_ref, next_refresh_token
                    ),
                    credential_expires_at=refreshed.access_token_expires_at,
                    credential_refresh_expires_at=next_refresh_expires_at,
                    oauth_scopes_json=next_oauth_scopes_json,
                    provider="huggingface",
                    auth_mode="device_oauth",
                    now=int(self._now()),
                )
                return refreshed.access_token, refreshed.access_token_expires_at
            except HuggingFaceOAuthError as error:
                needs_reauthorization = error.uncertain or error.code == "credential_rejected"
                auth_status = (
                    "reauthorization_required"
                    if needs_reauthorization
                    else "unavailable"
                    if error.code == "oauth_client_rejected"
                    else "healthy"
                )
                self._release_huggingface_refresh(
                    profile_ref,
                    anchor,
                    claim_id,
                    auth_status=auth_status,
                    clear_credentials=needs_reauthorization,
                )
                raise AccountCenterError(error.code) from None
            except HuggingFaceAuthError as error:
                self._release_huggingface_refresh(
                    profile_ref,
                    anchor,
                    claim_id,
                    auth_status="reauthorization_required",
                    clear_credentials=True,
                )
                raise AccountCenterError(error.code) from None
            except AccountCenterError as error:
                if error.code != "external_identity_mismatch":
                    self._release_huggingface_refresh(
                        profile_ref,
                        anchor,
                        claim_id,
                        auth_status="reauthorization_required",
                        clear_credentials=True,
                    )
                raise
            except Exception:  # noqa: BLE001 - refresh outcome must never leak provider errors.
                self._release_huggingface_refresh(
                    profile_ref,
                    anchor,
                    claim_id,
                    auth_status="reauthorization_required",
                    clear_credentials=True,
                )
                raise AccountCenterError("credential_refresh_unknown") from None

    def _release_huggingface_refresh(
        self,
        profile_ref: str,
        anchor: Anchor,
        claim_id: str,
        *,
        auth_status: str,
        clear_credentials: bool,
    ) -> None:
        """Best-effort release after a provider outcome leaves no safe retry."""

        try:
            self._store.release_provider_oauth_refresh(
                profile_ref=profile_ref,
                anchor=anchor,
                claim_id=claim_id,
                now=int(self._now()),
                provider="huggingface",
                auth_mode="device_oauth",
                auth_status=auth_status,
                clear_credentials=clear_credentials,
            )
        except AccountCenterError:
            # A persisted claim is recovered as reauthorization-required after
            # its bounded stale window if private storage is temporarily down.
            return

    def _encrypt_provider_token(
        self, profile_ref: str, token: str, expires_at: int | None
    ) -> bytes:
        """Encrypt an access token with profile-specific authenticated data."""

        if not token or len(token) > 8192:
            raise AccountCenterError("credential_unavailable")
        return encrypt(
            self._encryption_key,
            canonical_json({"access_token": token, "expires_at": expires_at}),
            f"provider-profile:{profile_ref}".encode(),
        )

    def _decrypt_provider_token(self, profile_ref: str, blob: bytes) -> tuple[str, int | None]:
        """Decrypt one provider token into short-lived process memory."""

        clear = decrypt(
            self._encryption_key,
            blob,
            f"provider-profile:{profile_ref}".encode(),
        )
        try:
            value = json.loads(clear)
            token = value["access_token"]
            expires_at = value.get("expires_at")
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            raise AccountCenterError("credential_unavailable") from None
        if (
            not isinstance(token, str)
            or not token
            or (
                expires_at is not None
                and (isinstance(expires_at, bool) or not isinstance(expires_at, int))
            )
        ):
            raise AccountCenterError("credential_unavailable")
        return token, expires_at

    def _encrypt_provider_refresh_token(self, profile_ref: str, token: str) -> bytes:
        """Encrypt an OAuth refresh token under a purpose-specific profile key."""

        if not token or len(token) > 8192 or "\r" in token or "\n" in token:
            raise AccountCenterError("credential_unavailable")
        return encrypt(
            self._encryption_key,
            token.encode("utf-8"),
            f"provider-refresh:{profile_ref}".encode(),
        )

    def _decrypt_provider_refresh_token(self, profile_ref: str, blob: bytes) -> str:
        """Decrypt one OAuth refresh token into short-lived process memory."""

        clear = decrypt(
            self._encryption_key,
            blob,
            f"provider-refresh:{profile_ref}".encode(),
        )
        try:
            token = clear.decode("utf-8")
        except UnicodeDecodeError:
            raise AccountCenterError("credential_unavailable") from None
        if not token or len(token) > 8192 or "\r" in token or "\n" in token:
            raise AccountCenterError("credential_unavailable")
        return token

    def _get_authorization(self, request: Mapping[str, Any], anchor: Anchor) -> dict[str, Any]:
        account_ref, provider, operation_id, resource, _args_digest = _authorization_request(
            request
        )
        row = self._store.get_account(account_ref, anchor)
        authorized = (
            row["status"] == "active"
            and row["credential_status"] == "ready"
            and row["provider"] == provider == "github"
            and operation_id in PROVIDER_OPERATIONS
        )
        if authorized:
            authorized = self._resource_scope_allowed(resource, operation_id)
        return {
            "authorized": bool(authorized),
            "account_ref": account_ref,
            "provider": provider,
        }

    def _create_credential_lease(
        self, request: Mapping[str, Any], anchor: Anchor, context: Mapping[str, Any]
    ) -> dict[str, Any]:
        account_ref, provider, operation_id, resource, args_digest = _authorization_request(request)
        if (
            provider != "github"
            or operation_id not in PROVIDER_OPERATIONS
            or not self._resource_scope_allowed(resource, operation_id)
        ):
            raise AccountCenterError("authorization_denied")
        row = self._store.get_account(account_ref, anchor)
        if row["status"] != "active" or row["provider"] != "github":
            raise AccountCenterError("authorization_denied")
        if row["credential_status"] != "ready":
            raise AccountCenterError("credential_reauthentication_required")
        now = int(self._now())
        expires_at = now + LEASE_TTL_SECONDS
        lease_ref = self._store.create_lease(
            account_ref=account_ref,
            anchor=anchor,
            binding_id=_required_text(context, "binding_id", maximum=200),
            provider_caller_id=self._github_provider_caller,
            operation_id=operation_id,
            resource_json=_resource_json(resource),
            arguments_sha256=args_digest,
            expires_at=expires_at,
            now=now,
        )
        return {
            "lease_ref": lease_ref,
            "provider": "github",
            "operation_id": operation_id,
            "resource": resource,
            "arguments_sha256": args_digest,
            "expires_at": expires_at,
        }

    def _redeem_credential_lease(
        self,
        request: Mapping[str, Any],
        anchor: Anchor,
        caller_id: str,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        _require_fields(request, {"lease_ref", "operation_id", "resource", "arguments_sha256"})
        if caller_id != self._github_provider_caller:
            raise AccountCenterError("untrusted_caller")
        lease_ref = _uuid_text(request["lease_ref"])
        operation_id = _required_text(request, "operation_id", maximum=200)
        if operation_id not in PROVIDER_OPERATIONS:
            raise AccountCenterError("lease_rejected")
        resource = _safe_resource(request["resource"])
        args_digest = _sha256_text(request["arguments_sha256"])
        account, _blob = self._store.redeem_lease(
            lease_ref=lease_ref,
            anchor=anchor,
            caller_id=caller_id,
            binding_id=_required_text(context, "binding_id", maximum=200),
            operation_id=operation_id,
            resource_json=_resource_json(resource),
            arguments_sha256=args_digest,
            now=int(self._now()),
        )
        credential = self._fresh_credential(account)
        # This action is only callable by the fixed GitHub provider caller. Its
        # response travels on the private capability channel, never public REST.
        return {
            "access_token": credential.access_token,
            "token_type": "Bearer",
            "external_subject": account["stable_subject"],
            "expires_at": credential.expires_at,
        }

    def _resolve_linked_sender(
        self, request: Mapping[str, Any], context: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Resolve trusted WeCom sender identity to an owner anchor without grants."""

        _require_fields(request, {"source_authority", "bot_namespace", "stable_subject"})
        source_authority = _required_text(request, "source_authority", maximum=500)
        bot_namespace = _required_text(request, "bot_namespace", maximum=200)
        stable_subject = _required_text(request, "stable_subject", maximum=500)
        configured = {
            (source.get("source_authority"), bot)
            for source in self._wecom_sources.values()
            if isinstance(source, Mapping)
            for bot in source.get("bot_namespaces", [])
            if isinstance(bot, str)
        }
        if (source_authority, bot_namespace) not in configured:
            raise AccountCenterError("source_not_found")
        row = self._store.resolve_linked_sender(
            source_authority=source_authority,
            bot_namespace=bot_namespace,
            stable_subject=stable_subject,
            organization_id=context["organization_id"],
        )
        if row is None:
            return {"linked": False}
        return {
            "linked": True,
            "account_ref": row["account_ref"],
            "provider": "wecom",
            "issuer": row["anchor_issuer"],
            "subject": row["anchor_subject"],
            "organization_id": row["organization_id"],
            "workspace_id": row["workspace_id"],
        }

    def _fresh_credential(self, account: Mapping[str, Any]) -> OAuthCredential:
        """Refresh once under a durable claim; never retry an uncertain rotation."""

        account_ref = account["account_ref"]
        with self._refresh_lock:
            account_lock = self._account_refresh_locks.setdefault(account_ref, threading.Lock())
        with account_lock:
            owner = Anchor(
                account["anchor_issuer"],
                account["anchor_subject"],
                account["organization_id"],
                account["workspace_id"],
            )
            current = self._store.get_account(account_ref, owner)
            if current["status"] != "active" or current["credential_status"] != "ready":
                raise AccountCenterError("credential_reauthentication_required")
            now = int(self._now())
            if (
                current["credential_expires_at"] is None
                or current["credential_expires_at"] > now + 30
            ):
                return self._decrypt_credential(account_ref, bytes(current["credential_blob"]))
            current_credential = self._decrypt_credential(
                account_ref, bytes(current["credential_blob"])
            )
            if not current_credential.refresh_token or (
                current["refresh_expires_at"] is not None and current["refresh_expires_at"] <= now
            ):
                claim_id = str(uuid.uuid4())
                self._store.claim_refresh(account_ref, owner, claim_id, now=now)
                self._store.fail_refresh(account_ref, claim_id)
                raise AccountCenterError("credential_reauthentication_required")

            claim_id = str(uuid.uuid4())
            revision = self._store.claim_refresh(account_ref, owner, claim_id, now=now)
            try:
                refreshed = self._github.refresh(current_credential.refresh_token)
            except AccountCenterError as error:
                self._store.fail_refresh(account_ref, claim_id)
                code = (
                    "credential_refresh_unknown"
                    if error.code == "provider_unavailable"
                    else "credential_refresh_rejected"
                )
                raise AccountCenterError(code) from None
            except Exception:  # noqa: BLE001 - unknown rotation outcomes fail closed.
                self._store.fail_refresh(account_ref, claim_id)
                raise AccountCenterError("credential_refresh_unknown") from None
            encrypted = self._encrypt_credential(account_ref=account_ref, credential=refreshed)
            self._store.finish_refresh(
                account_ref,
                owner,
                claim_id,
                revision,
                encrypted,
                refreshed.expires_at,
                refreshed.refresh_expires_at,
            )
            return refreshed

    def _revoke_callback_token(self, access_token: str) -> str:
        """Best-effort revoke a minted token and retain only its sanitized result."""

        try:
            self._github.revoke(access_token)
            return "revoked"
        except AccountCenterError as error:
            return "unknown" if error.code == "provider_unavailable" else "failed"
        except Exception:  # noqa: BLE001 - outcome is remote and cannot be inferred.
            return "unknown"

    def _persist_callback_credential(self, session_ref: str, credential: OAuthCredential) -> None:
        """Durably encrypt a minted token before GitHub identity verification."""

        encrypted = self._encrypt_credential(
            session_ref=f"oauth-callback:{session_ref}", credential=credential
        )
        try:
            self._store.save_pending_callback_credential(session_ref, encrypted)
        except AccountCenterError:
            raise GitHubCredentialPersistenceError(credential) from None

    def _recover_interrupted_callback_credentials(self) -> None:
        """Revoke tokens persisted by callbacks interrupted before verification."""

        for row in self._store.interrupted_callback_credentials():
            session_ref = row["link_session_ref"]
            try:
                credential = self._decrypt_credential(
                    f"oauth-callback:{session_ref}",
                    bytes(row["pending_credential_blob"]),
                )
                outcome = (
                    self._revoke_callback_token(credential.access_token)
                    if self._github is not None
                    else "unknown"
                )
            except AccountCenterError:
                outcome = "unknown"
            self._store.fail_session(session_ref, outcome)

    def _recover_interrupted_unlink_results(self) -> None:
        """Finalize local unlinks whose remote revocation was interrupted."""

        for row in self._store.interrupted_unlink_commands():
            key_hash = row["remote_revocation_command_hash"]
            response = {"unlinked": True, "status": "revocation_unknown"}
            encrypted = self._encode_idempotency_response(key_hash, {"value": response})
            self._store.finalize_interrupted_unlink(row["account_ref"], key_hash, encrypted)

    def _recover_pending_idempotency_results(self) -> None:
        """Turn crashed command reservations into stable terminal errors."""

        for key_hash in self._store.pending_idempotent_commands():
            encrypted = self._encode_idempotency_response(
                key_hash, {"error": "idempotency_result_unknown"}
            )
            self._store.complete_idempotent_command(key_hash, encrypted)

    def _unlink(
        self,
        request: Mapping[str, Any],
        anchor: Anchor,
        idempotency_key_hash: str | None = None,
    ) -> dict[str, Any]:
        _require_fields(request, {"account_ref"})
        account_ref = _uuid_text(request["account_ref"])
        row = self._store.get_account(account_ref, anchor)
        credential: OAuthCredential | None = None
        credential_unavailable = False
        if row["provider"] == "github" and row["credential_blob"] is not None:
            try:
                credential = self._decrypt_credential(account_ref, bytes(row["credential_blob"]))
            except AccountCenterError as error:
                if error.code != "credential_unavailable":
                    raise
                credential_unavailable = True
        status = "unlinked"
        if (
            row["provider"] == "github"
            and credential is None
            and (row["credential_status"] != "ready" or credential_unavailable)
        ):
            status = "revocation_unknown"
        if credential is None:
            response = {"unlinked": True, "status": status}
            command_result = (
                (
                    idempotency_key_hash,
                    self._encode_idempotency_response(idempotency_key_hash, {"value": response}),
                )
                if idempotency_key_hash is not None
                else None
            )
            self._store.unlink(
                account_ref,
                anchor,
                now=int(self._now()),
                command_result=command_result,
                command_key_hash=idempotency_key_hash,
                force_remote_unknown=credential_unavailable,
            )
            return response

        self._store.unlink(
            account_ref,
            anchor,
            now=int(self._now()),
            command_key_hash=idempotency_key_hash,
        )

        try:
            self._github.revoke(credential.access_token)
            remote_status = "revoked"
            status = "unlinked"
        except AccountCenterError as error:
            remote_status = "unknown" if error.code == "provider_unavailable" else "failed"
            status = "revocation_unknown" if remote_status == "unknown" else "revocation_failed"
        except Exception:  # noqa: BLE001 - network outcome is not knowable.
            remote_status = "unknown"
            status = "revocation_unknown"
        response = {"unlinked": True, "status": status}
        command_result = (
            (
                idempotency_key_hash,
                self._encode_idempotency_response(idempotency_key_hash, {"value": response}),
            )
            if idempotency_key_hash is not None
            else None
        )
        self._store.set_revocation_status(account_ref, remote_status, command_result=command_result)
        return response

    def _resource_scope_allowed(self, resource: Mapping[str, Any], operation_id: str) -> bool:
        """Fail closed unless a fixed configured repository allowlist permits use."""

        # A host can constrain repositories per operation; absent policy grants none.
        allowed = getattr(self, "_repository_allowlist", None)
        if allowed is None:
            return True
        return resource["repository_id"] in allowed.get(operation_id, set())

    def _encrypt_credential(
        self,
        *,
        credential: OAuthCredential,
        account_ref: str | None = None,
        session_ref: str | None = None,
    ) -> bytes:
        """Encrypt the token bundle with an opaque account-associated key."""

        associated = (account_ref or session_ref or "pending-account").encode("utf-8")
        clear = canonical_json(
            {
                "access_token": credential.access_token,
                "refresh_token": credential.refresh_token,
                "expires_at": credential.expires_at,
                "refresh_expires_at": credential.refresh_expires_at,
            }
        )
        return encrypt(self._encryption_key, clear, associated)

    def _encrypt_session_secret(self, session_ref: str, secret: str) -> bytes:
        """Encrypt short-lived PKCE data under its owning OAuth session."""

        return encrypt(
            self._encryption_key,
            secret.encode("ascii"),
            f"oauth-pkce:{session_ref}".encode(),
        )

    def _decrypt_session_secret(self, session_ref: str, blob: bytes) -> str:
        """Recover one PKCE verifier only for the callback that owns the session."""

        try:
            return decrypt(
                self._encryption_key,
                blob,
                f"oauth-pkce:{session_ref}".encode(),
            ).decode("ascii")
        except UnicodeDecodeError:
            raise AccountCenterError("link_session_invalid") from None

    def _decrypt_credential(self, account_ref: str, blob: bytes) -> OAuthCredential:
        """Decrypt one private token bundle into short-lived process memory."""

        clear = decrypt(self._encryption_key, blob, account_ref.encode("utf-8"))
        try:
            value = json.loads(clear)
            access_token = value["access_token"]
            if not isinstance(access_token, str) or not access_token:
                raise ValueError
            refresh_token = value.get("refresh_token")
            return OAuthCredential(
                access_token,
                refresh_token,
                value.get("expires_at"),
                value.get("refresh_expires_at"),
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise AccountCenterError("credential_unavailable") from None


def _opaque_profile_ref(value: Any) -> str:
    """Require a canonical opaque UUID supplied by Exchange profile storage."""

    return _uuid_text(value)


def _numeric_id(value: Any) -> str:
    """Validate a positive provider-side numeric identity without coercion."""

    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise AccountCenterError("invalid_request")
    text = str(value)
    if not text.isdecimal() or int(text) < 1 or len(text) > 32:
        raise AccountCenterError("invalid_request")
    return text


def _safe_github_repositories(value: Any) -> list[dict[str, str]]:
    """Validate the stable repository allowlist used by GitHub App leases."""

    if isinstance(value, Mapping):
        value = value.get("repositories")
    if not isinstance(value, list) or not value or len(value) > 100:
        raise AccountCenterError("invalid_request")
    repositories: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "repository_id",
            "owner",
            "name",
        }:
            raise AccountCenterError("invalid_request")
        repository_id = _numeric_id(item["repository_id"])
        owner = item["owner"]
        name = item["name"]
        if (
            not isinstance(owner, str)
            or not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
            or repository_id in seen_ids
        ):
            raise AccountCenterError("invalid_request")
        seen_ids.add(repository_id)
        repositories.append({"repository_id": repository_id, "owner": owner, "name": name})
    return repositories


def _safe_huggingface_scopes(value: Any) -> dict[str, Any]:
    """Validate a closed fine-grained Hub repository allowlist."""

    scope = require_object(value)
    if set(scope) != {"repositories"}:
        raise AccountCenterError("invalid_request")
    raw_repositories = scope["repositories"]
    if (
        not isinstance(raw_repositories, list)
        or not raw_repositories
        or len(raw_repositories) > 100
    ):
        raise AccountCenterError("invalid_request")
    repositories: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_repositories:
        if not isinstance(item, Mapping) or set(item) != {
            "repo_type",
            "repo_id",
            "permissions",
        }:
            raise AccountCenterError("invalid_request")
        repo_type = item["repo_type"]
        repo_id = item["repo_id"]
        permissions = item["permissions"]
        if (
            repo_type not in {"model", "dataset", "space"}
            or not isinstance(repo_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo_id)
            or not isinstance(permissions, list)
            or not permissions
            or any(not isinstance(permission, str) for permission in permissions)
            or set(permissions) - {"read", "write"}
            or len(permissions) != len(set(permissions))
            or (repo_type, repo_id) in seen
        ):
            raise AccountCenterError("invalid_request")
        seen.add((repo_type, repo_id))
        repositories.append(
            {
                "repo_type": repo_type,
                "repo_id": repo_id,
                "permissions": sorted(permissions),
            }
        )
    repositories.sort(key=lambda repository: (repository["repo_type"], repository["repo_id"]))
    return {"repositories": repositories}


def _huggingface_auth_mode(value: Any) -> str:
    """Resolve an explicit HF credential mode, preserving legacy token callers."""

    mode = "fine_grained_token" if value is None else value
    if mode not in {"fine_grained_token", "device_oauth"}:
        raise AccountCenterError("invalid_request")
    return mode


def _safe_identity_ref(value: Any) -> str:
    """Accept only a bounded opaque host identity or secret reference."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not re.fullmatch(r"[A-Za-z0-9._:/@-]+", value)
        or any(fragment in value.lower() for fragment in ("token", "secret", "password", "key="))
    ):
        raise AccountCenterError("invalid_request")
    return value


def _safe_profile_scope(value: Any) -> dict[str, Any]:
    """Keep cloud scope metadata bounded and prevent credential-shaped fields."""

    scope = require_object(value)
    encoded = canonical_json(scope)
    if len(encoded) > 16_384:
        raise AccountCenterError("invalid_request")

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str) or any(
                    marker in key.lower()
                    for marker in (
                        "token",
                        "secret",
                        "password",
                        "private_key",
                        "credential",
                    )
                ):
                    raise AccountCenterError("invalid_request")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            if len(item) > 1000 or "\x00" in item:
                raise AccountCenterError("invalid_request")
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise AccountCenterError("invalid_request")

    visit(scope)
    return dict(scope)


def _safe_graph_uuid_list(value: Any) -> list[str]:
    """Normalize one nonempty list of unique Microsoft Graph object IDs."""

    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise AccountCenterError("invalid_request")
    normalized = [_safe_graph_uuid(item) for item in value]
    if len(set(normalized)) != len(normalized):
        raise AccountCenterError("invalid_request")
    return normalized


def _safe_graph_uuid_list_optional(value: Any) -> list[str]:
    """Validate an optionally empty list of canonical Graph object IDs."""

    if not isinstance(value, list) or len(value) > 100:
        raise AccountCenterError("invalid_request")
    normalized = [_safe_graph_uuid(item) for item in value]
    if len(set(normalized)) != len(normalized):
        raise AccountCenterError("invalid_request")
    return normalized


def _safe_graph_planner_ids_allow_empty(value: Any) -> list[str]:
    """Validate a bounded Planner allowlist that may be empty for discovery."""

    if not isinstance(value, list) or len(value) > 100:
        raise AccountCenterError("invalid_request")
    if any(
        not isinstance(item, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item) is None
        for item in value
    ) or len(set(value)) != len(value):
        raise AccountCenterError("invalid_request")
    return list(value)


def _safe_graph_opaque_id(value: Any) -> str:
    """Validate an opaque Graph resource ID without permitting URL syntax."""

    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or re.fullmatch(r"[A-Za-z0-9._!~+=-]{1,512}", value) is None
    ):
        raise AccountCenterError("invalid_request")
    return value


def _safe_graph_opaque_id_list(value: Any) -> list[str]:
    """Validate a bounded optionally empty allowlist of Graph opaque IDs."""

    if not isinstance(value, list) or len(value) > 100:
        raise AccountCenterError("invalid_request")
    normalized = [_safe_graph_opaque_id(item) for item in value]
    if len(set(normalized)) != len(normalized):
        raise AccountCenterError("invalid_request")
    return normalized


def _safe_graph_schedule_ids(value: Any) -> list[str]:
    """Validate a bounded set of exact mailbox/room identifiers for free/busy."""

    if not isinstance(value, list) or len(value) > 100:
        raise AccountCenterError("invalid_request")
    normalized: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not 3 <= len(item) <= 320
            or any(char in item for char in "\r\n\x00")
            or re.fullmatch(r"[^\s@]+@[^\s@]+", item) is None
        ):
            raise AccountCenterError("invalid_request")
        normalized.append(item.casefold())
    if len(set(normalized)) != len(normalized):
        raise AccountCenterError("invalid_request")
    return normalized


def _safe_graph_planner_ids(value: Any) -> list[str]:
    """Validate the opaque case-sensitive identifiers used by Planner."""

    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise AccountCenterError("invalid_request")
    if any(
        not isinstance(item, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item) is None
        for item in value
    ) or len(set(value)) != len(value):
        raise AccountCenterError("invalid_request")
    return list(value)


def _safe_graph_planner_id(value: Any) -> str:
    """Validate one opaque case-sensitive Planner resource identifier."""

    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise AccountCenterError("invalid_request")
    return value


def _safe_graph_uuid(value: Any) -> str:
    """Require a canonical lowercase Microsoft tenant or object UUID."""

    try:
        if not isinstance(value, str):
            raise TypeError("invalid UUID")
        parsed = uuid.UUID(value)
        normalized = str(parsed)
        if normalized != value.lower():
            raise ValueError("noncanonical UUID")
        return normalized
    except (TypeError, ValueError):
        raise AccountCenterError("invalid_request") from None


def _graph_subject_parts(value: Any) -> tuple[str, str]:
    """Parse the immutable canonical tenant/object subject stored by Graph."""

    if not isinstance(value, str) or value.count(":") != 1:
        raise AccountCenterError("profile_unavailable")
    tenant_id, object_id = value.split(":", 1)
    try:
        return _safe_graph_uuid(tenant_id), _safe_graph_uuid(object_id)
    except AccountCenterError:
        raise AccountCenterError("profile_unavailable") from None


def _trusted_entra_external_subject(anchor: Anchor) -> str:
    """Map a signed Entra issuer/subject pair to immutable tenant/object IDs."""

    match = re.fullmatch(
        r"https://login\.microsoftonline\.com/([0-9a-fA-F-]{36})/v2\.0",
        anchor.issuer,
    )
    if match is None:
        raise AccountCenterError("external_identity_mismatch")
    try:
        tenant_id = _safe_graph_uuid(match.group(1))
        object_id = _safe_graph_uuid(anchor.subject)
    except AccountCenterError:
        raise AccountCenterError("external_identity_mismatch") from None
    if anchor.issuer != f"https://login.microsoftonline.com/{tenant_id}/v2.0":
        raise AccountCenterError("external_identity_mismatch")
    if anchor.subject != object_id:
        raise AccountCenterError("external_identity_mismatch")
    return f"{tenant_id}:{object_id}"


def _safe_graph_site_id(value: Any) -> str:
    """Accept one host/collection/web SharePoint ID, never a path or URL."""

    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise AccountCenterError("invalid_request")
    parts = value.split(",")
    if (
        len(parts) != 3
        or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", parts[0]) is None
        or any(label.startswith("-") or label.endswith("-") for label in parts[0].split("."))
    ):
        raise AccountCenterError("invalid_request")
    _safe_graph_uuid(parts[1])
    _safe_graph_uuid(parts[2])
    return value


def _safe_microsoft_graph_profile_scope(value: Any) -> dict[str, Any]:
    """Validate one exact v1 or v2 actor-owned Graph target scope."""

    scope = require_object(value, code="profile_unavailable")
    if scope.get("schema_version") == 2:
        return _safe_microsoft_graph_profile_scope_v2(scope)
    if set(scope) != {"tenant_id", "planner", "sharepoint"}:
        raise AccountCenterError("profile_unavailable")
    try:
        tenant_id = _safe_graph_uuid(scope["tenant_id"])
        planner = require_object(scope["planner"], code="profile_unavailable")
        if set(planner) != {"plan_ids", "tasks_read", "tasks_write"}:
            raise AccountCenterError("profile_unavailable")
        plan_ids = _safe_graph_planner_ids(planner["plan_ids"])
        if planner["tasks_read"] is not True or not isinstance(planner["tasks_write"], bool):
            raise AccountCenterError("profile_unavailable")
        sharepoint = require_object(scope["sharepoint"], code="profile_unavailable")
        if set(sharepoint) != {"enabled", "site_id", "list_ids"}:
            raise AccountCenterError("profile_unavailable")
        enabled = sharepoint["enabled"]
        if not isinstance(enabled, bool):
            raise AccountCenterError("profile_unavailable")
        if enabled:
            site_id = _safe_graph_site_id(sharepoint["site_id"])
            list_ids = _safe_graph_uuid_list(sharepoint["list_ids"])
        elif sharepoint["site_id"] is None and sharepoint["list_ids"] == []:
            site_id = None
            list_ids = []
        else:
            raise AccountCenterError("profile_unavailable")
    except AccountCenterError:
        raise AccountCenterError("profile_unavailable") from None
    return {
        "tenant_id": tenant_id,
        "planner": {
            "plan_ids": plan_ids,
            "tasks_read": True,
            "tasks_write": planner["tasks_write"],
        },
        "sharepoint": {
            "enabled": enabled,
            "site_id": site_id,
            "list_ids": list_ids,
        },
    }


def _safe_microsoft_graph_profile_scope_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed Office v2 scope while keeping message bodies and file content off."""

    required = {
        "schema_version",
        "tenant_id",
        "workspace_id",
        "planner",
        "sharepoint",
        "todo",
        "calendar",
        "mail",
    }
    if set(value) != required:
        raise AccountCenterError("profile_unavailable")
    try:
        tenant_id = _safe_graph_uuid(value["tenant_id"])
        workspace_id = _safe_graph_uuid(value["workspace_id"])
        if value["schema_version"] != 2:
            raise AccountCenterError("profile_unavailable")

        planner = require_object(value["planner"], code="profile_unavailable")
        if set(planner) != {"plan_ids", "tasks_read", "tasks_write"}:
            raise AccountCenterError("profile_unavailable")
        plan_ids = _safe_graph_planner_ids_allow_empty(planner["plan_ids"])
        planner_read = planner["tasks_read"]
        planner_write = planner["tasks_write"]
        if (
            not isinstance(planner_read, bool)
            or not isinstance(planner_write, bool)
            or (planner_write and not planner_read)
            or (not planner_read and (plan_ids or planner_write))
        ):
            raise AccountCenterError("profile_unavailable")

        sharepoint = require_object(value["sharepoint"], code="profile_unavailable")
        if set(sharepoint) != {
            "enabled",
            "site_id",
            "list_ids",
            "drive_ids",
            "metadata_read",
            "content_read",
        }:
            raise AccountCenterError("profile_unavailable")
        sp_enabled = sharepoint["enabled"]
        sp_metadata = sharepoint["metadata_read"]
        if not isinstance(sp_enabled, bool) or not isinstance(sp_metadata, bool):
            raise AccountCenterError("profile_unavailable")
        if sharepoint["content_read"] is not False:
            raise AccountCenterError("profile_unavailable")
        if sp_enabled:
            site_id = _safe_graph_site_id(sharepoint["site_id"])
            list_ids = _safe_graph_uuid_list_optional(sharepoint["list_ids"])
            drive_ids = _safe_graph_opaque_id_list(sharepoint["drive_ids"])
            if not sp_metadata:
                raise AccountCenterError("profile_unavailable")
        elif (
            sharepoint["site_id"] is None
            and sharepoint["list_ids"] == []
            and sharepoint["drive_ids"] == []
            and sp_metadata is False
        ):
            site_id, list_ids, drive_ids = None, [], []
        else:
            raise AccountCenterError("profile_unavailable")

        todo = require_object(value["todo"], code="profile_unavailable")
        if set(todo) != {"enabled", "task_list_ids", "tasks_read", "tasks_write"}:
            raise AccountCenterError("profile_unavailable")
        todo_enabled = todo["enabled"]
        todo_read = todo["tasks_read"]
        todo_write = todo["tasks_write"]
        if not all(isinstance(item, bool) for item in (todo_enabled, todo_read, todo_write)):
            raise AccountCenterError("profile_unavailable")
        task_list_ids = _safe_graph_opaque_id_list(todo["task_list_ids"])
        if (
            (todo_write and not todo_read)
            or (todo_enabled and not todo_read)
            or (not todo_enabled and (task_list_ids or todo_read or todo_write))
        ):
            raise AccountCenterError("profile_unavailable")

        calendar = require_object(value["calendar"], code="profile_unavailable")
        if set(calendar) != {
            "enabled",
            "calendar_ids",
            "events_read",
            "events_write",
            "freebusy_read",
            "freebusy_subjects",
            "attendees_write",
        }:
            raise AccountCenterError("profile_unavailable")
        calendar_enabled = calendar["enabled"]
        events_read = calendar["events_read"]
        events_write = calendar["events_write"]
        freebusy_read = calendar["freebusy_read"]
        attendees_write = calendar["attendees_write"]
        if not all(
            isinstance(item, bool)
            for item in (
                calendar_enabled,
                events_read,
                events_write,
                freebusy_read,
                attendees_write,
            )
        ):
            raise AccountCenterError("profile_unavailable")
        calendar_ids = _safe_graph_opaque_id_list(calendar["calendar_ids"])
        freebusy_subjects = _safe_graph_schedule_ids(calendar["freebusy_subjects"])
        if (
            attendees_write
            or (events_write and not events_read)
            or (calendar_enabled and not (events_read or freebusy_read))
            or (
                not calendar_enabled
                and (
                    calendar_ids
                    or events_read
                    or events_write
                    or freebusy_read
                    or freebusy_subjects
                )
            )
            or (not events_read and calendar_ids)
            or (not freebusy_read and freebusy_subjects)
        ):
            raise AccountCenterError("profile_unavailable")

        mail = require_object(value["mail"], code="profile_unavailable")
        if set(mail) != {
            "enabled",
            "folder_ids",
            "metadata_read",
            "body_read",
            "drafts_write",
            "send_enabled",
        }:
            raise AccountCenterError("profile_unavailable")
        metadata_read = mail["metadata_read"]
        body_read = mail["body_read"]
        drafts_write = mail["drafts_write"]
        send_enabled = mail["send_enabled"]
        if not all(
            isinstance(item, bool)
            for item in (mail["enabled"], metadata_read, body_read, drafts_write, send_enabled)
        ):
            raise AccountCenterError("profile_unavailable")
        folder_ids = _safe_graph_opaque_id_list(mail["folder_ids"])
        if (
            body_read is not False
            or (not metadata_read and folder_ids)
            or mail["enabled"] != (metadata_read or drafts_write or send_enabled)
        ):
            raise AccountCenterError("profile_unavailable")
    except AccountCenterError:
        raise AccountCenterError("profile_unavailable") from None
    return {
        "schema_version": 2,
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "planner": {
            "plan_ids": plan_ids,
            "tasks_read": planner_read,
            "tasks_write": planner_write,
        },
        "sharepoint": {
            "enabled": sp_enabled,
            "site_id": site_id,
            "list_ids": list_ids,
            "drive_ids": drive_ids,
            "metadata_read": sp_metadata,
            "content_read": False,
        },
        "todo": {
            "enabled": todo_enabled,
            "task_list_ids": task_list_ids,
            "tasks_read": todo_read,
            "tasks_write": todo_write,
        },
        "calendar": {
            "enabled": calendar_enabled,
            "calendar_ids": calendar_ids,
            "events_read": events_read,
            "events_write": events_write,
            "freebusy_read": freebusy_read,
            "freebusy_subjects": freebusy_subjects,
            "attendees_write": False,
        },
        "mail": {
            "enabled": mail["enabled"],
            "folder_ids": folder_ids,
            "metadata_read": metadata_read,
            "body_read": False,
            "drafts_write": drafts_write,
            "send_enabled": send_enabled,
        },
    }


def _safe_microsoft_graph_mail_feature_request(value: Any) -> dict[str, bool]:
    """Validate one explicit, additive Mail permission selection."""

    features = require_object(value, code="invalid_request")
    if set(features) != {"metadata_read", "drafts_write", "send"}:
        raise AccountCenterError("invalid_request")
    if any(not isinstance(features[key], bool) for key in features):
        raise AccountCenterError("invalid_request")
    if not any(features.values()):
        raise AccountCenterError("invalid_request")
    return dict(features)


def _expected_microsoft_graph_scopes(scope: Mapping[str, Any]) -> set[str]:
    """Derive the minimum exact Graph OAuth set from one validated profile."""

    if scope.get("schema_version") != 2:
        return {
            "User.Read",
            "Tasks.Read",
            *({"Tasks.ReadWrite"} if scope["planner"]["tasks_write"] else set()),
            *({"Sites.Selected"} if scope["sharepoint"]["enabled"] else set()),
        }
    required = {"User.Read"}
    if scope["planner"]["tasks_write"] or scope["todo"]["tasks_write"]:
        required.add("Tasks.ReadWrite")
    elif scope["planner"]["tasks_read"] or scope["todo"]["tasks_read"]:
        required.add("Tasks.Read")
    if scope["sharepoint"]["enabled"]:
        required.add("Sites.Selected")
    if scope["calendar"]["events_write"]:
        required.add("Calendars.ReadWrite")
    elif scope["calendar"]["events_read"] or scope["calendar"]["freebusy_read"]:
        required.add("Calendars.ReadBasic")
    mail = scope["mail"]
    if mail["drafts_write"]:
        required.add("Mail.ReadWrite")
    elif mail["metadata_read"] or mail["send_enabled"]:
        required.add("Mail.ReadBasic")
    if mail["send_enabled"]:
        required.add("Mail.Send")
    return required


def _microsoft_graph_scope_permissions(scope: Mapping[str, Any]) -> dict[str, Any]:
    """Return only immutable v2 feature grants, excluding selected resource IDs."""

    return {
        "planner": {
            "tasks_read": scope["planner"]["tasks_read"],
            "tasks_write": scope["planner"]["tasks_write"],
        },
        "sharepoint": {
            "enabled": scope["sharepoint"]["enabled"],
            "site_id": scope["sharepoint"]["site_id"],
            "metadata_read": scope["sharepoint"]["metadata_read"],
            "content_read": scope["sharepoint"]["content_read"],
        },
        "todo": {
            "enabled": scope["todo"]["enabled"],
            "tasks_read": scope["todo"]["tasks_read"],
            "tasks_write": scope["todo"]["tasks_write"],
        },
        "calendar": {
            "enabled": scope["calendar"]["enabled"],
            "events_read": scope["calendar"]["events_read"],
            "events_write": scope["calendar"]["events_write"],
            "freebusy_read": scope["calendar"]["freebusy_read"],
            "attendees_write": scope["calendar"]["attendees_write"],
        },
        "mail": {
            "enabled": scope["mail"]["enabled"],
            "metadata_read": scope["mail"]["metadata_read"],
            "body_read": scope["mail"]["body_read"],
            "drafts_write": scope["mail"]["drafts_write"],
            "send_enabled": scope["mail"]["send_enabled"],
        },
    }


def _safe_microsoft_graph_oauth_scopes(value: Any) -> set[str]:
    """Require the Graph delegated-scope set stored with one profile."""

    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(scope, str) for scope in value)
    ):
        raise AccountCenterError("profile_unavailable")
    scopes = set(value)
    if len(scopes) != len(value) or not scopes <= ALLOWED_GRAPH_SCOPES or "User.Read" not in scopes:
        raise AccountCenterError("profile_unavailable")
    return scopes


def _safe_azure_devops_numeric_id(value: Any) -> int:
    """Validate one bounded, positive Azure DevOps numeric locator."""

    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_147_483_647:
        raise AccountCenterError("invalid_request")
    return value


def _safe_azure_devops_profile_scope(value: Any) -> dict[str, Any]:
    """Validate one host-selected Azure DevOps project source tuple."""

    scope = require_object(value, code="profile_unavailable")
    allowed = {
        "tenant_id",
        "organization",
        "project_id",
        "source_id",
        "source_kind",
        "pipeline_id",
        "definition_id",
        "definition_environment_id",
    }
    if set(scope) - allowed or not {
        "tenant_id",
        "organization",
        "project_id",
        "source_id",
        "source_kind",
    }.issubset(scope):
        raise AccountCenterError("profile_unavailable")
    try:
        tenant_id = _safe_graph_uuid(scope["tenant_id"])
        project_id = _safe_graph_uuid(scope["project_id"])
        organization = scope["organization"]
        source_id = scope["source_id"]
        source_kind = scope["source_kind"]
        if (
            not isinstance(organization, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", organization) is None
            or not isinstance(source_id, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", source_id) is None
            or source_kind not in {"project_pipelines", "pipeline_runs", "release_deployments"}
        ):
            raise AccountCenterError("profile_unavailable")
        normalized: dict[str, Any] = {
            "tenant_id": tenant_id,
            "organization": organization,
            "project_id": project_id,
            "source_id": source_id,
            "source_kind": source_kind,
        }
        if source_kind == "project_pipelines":
            if (
                "pipeline_id" in scope
                or "definition_id" in scope
                or "definition_environment_id" in scope
            ):
                raise AccountCenterError("profile_unavailable")
        elif source_kind == "pipeline_runs":
            if (
                "pipeline_id" not in scope
                or "definition_id" in scope
                or "definition_environment_id" in scope
            ):
                raise AccountCenterError("profile_unavailable")
            normalized["pipeline_id"] = _safe_azure_devops_numeric_id(scope["pipeline_id"])
        else:
            if "pipeline_id" in scope:
                raise AccountCenterError("profile_unavailable")
            if "definition_id" in scope:
                normalized["definition_id"] = _safe_azure_devops_numeric_id(scope["definition_id"])
            if "definition_environment_id" in scope:
                if "definition_id" not in normalized:
                    raise AccountCenterError("profile_unavailable")
                normalized["definition_environment_id"] = _safe_azure_devops_numeric_id(
                    scope["definition_environment_id"]
                )
        return normalized
    except AccountCenterError:
        raise AccountCenterError("profile_unavailable") from None


def _safe_azure_devops_oauth_scopes(value: Any) -> set[str]:
    """Accept only the separate Entra Azure DevOps `.default` resource grant."""

    if value != [AZURE_DEVOPS_RESOURCE_SCOPE]:
        raise AccountCenterError("profile_unavailable")
    return {AZURE_DEVOPS_RESOURCE_SCOPE}


def _safe_secret_reference(value: Any) -> str:
    """Require one allowlisted host secret reference, never a token value."""

    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9._/-]{0,127}", value):
        raise AccountCenterError("invalid_request")
    return value


def _validated_connector_grant(
    value: Any,
    *,
    anchor: Anchor,
    profile_ref: str,
    provider: str,
    operation_id: str,
    arguments_sha256: str,
    now: int,
) -> dict[str, Any]:
    """Check the closed Exchange-HMAC-covered Directory proof against this lease."""

    grant = require_object(value, code="connector_grant_required")
    fields = {
        "grant_ref",
        "issued_at",
        "expires_at",
        "issuer",
        "subject",
        "organization_id",
        "workspace_id",
        "connection_ref",
        "provider_id",
        "profile_ref",
        "operation_id",
        "resource_ref",
        "arguments_sha256",
    }
    if set(grant) != fields:
        raise AccountCenterError("connector_grant_required")
    try:
        grant_ref = grant["grant_ref"]
        if not isinstance(grant_ref, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", grant_ref):
            raise AccountCenterError("connector_grant_required")
        connection_ref = _uuid_text(grant["connection_ref"])
        granted_profile_ref = _opaque_profile_ref(grant["profile_ref"])
        issuer = _required_text(grant, "issuer", maximum=500)
        subject = _required_text(grant, "subject", maximum=500)
        organization_id = _required_text(grant, "organization_id", maximum=200)
        workspace_id = _required_text(grant, "workspace_id", maximum=200)
        granted_operation = _required_text(grant, "operation_id", maximum=200)
        resource_ref = _required_text(grant, "resource_ref", maximum=512)
        digest = _sha256_text(grant["arguments_sha256"])
    except AccountCenterError:
        raise AccountCenterError("connector_grant_required") from None
    issued_at = grant["issued_at"]
    expires_at = grant["expires_at"]
    if (
        not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or issued_at > now
        or now - issued_at > 15
        or expires_at <= now
        or expires_at <= issued_at
        or expires_at - issued_at > 15
        or issuer != anchor.issuer
        or subject != anchor.subject
        or organization_id != anchor.organization_id
        or anchor.workspace_id is None
        or workspace_id != anchor.workspace_id
        or grant["provider_id"] != provider
        or granted_profile_ref != profile_ref
        or granted_operation != operation_id
        or digest != arguments_sha256
    ):
        raise AccountCenterError("connector_grant_required")
    return {
        "grant_ref": grant_ref,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "connection_ref": connection_ref,
        "provider_id": provider,
        "profile_ref": profile_ref,
        "operation_id": operation_id,
        "resource_ref": resource_ref,
        "arguments_sha256": digest,
    }


def _safe_provider_target(provider: str, operation_id: str, value: Any) -> dict[str, Any]:
    """Validate a provider-specific, URL-free target for lease binding."""

    target = require_object(value)
    if len(canonical_json(target)) > 8192:
        raise AccountCenterError("invalid_request")
    if provider == "github":
        if operation_id == "github.vendor.cli.execute":
            if target:
                raise AccountCenterError("invalid_request")
            return {}
        if operation_id == "github.repository.list":
            if target:
                raise AccountCenterError("invalid_request")
            return {}
        allowed = {"repository_id", "repository"}
        required = {"repository_id", "repository"}
        if operation_id in {
            "github.issue.get",
            "github.issue.comment.create",
            "github.issue.state.update",
        }:
            allowed.add("issue_number")
            required.add("issue_number")
        elif operation_id == "github.pull_request.get":
            allowed.add("pull_number")
            required.add("pull_number")
        elif operation_id == "github.commits.compare":
            allowed.update({"base_sha", "head_sha"})
            required.update({"base_sha", "head_sha"})
        elif operation_id == "github.actions.runs.list":
            allowed.update({"branch", "commit_sha"})
        elif operation_id == "github.ci.status":
            allowed.add("commit_sha")
            required.add("commit_sha")
        elif operation_id == "github.issues.search":
            pass  # Search filters are argument-digest-bound, not resource locators.
        elif operation_id in {
            "github.branch.get",
            "github.branch.create",
            "github.branch.delete",
        }:
            allowed.add("branch")
            required.add("branch")
        elif operation_id in {"github.file.get", "github.file.commit"}:
            allowed.update({"branch", "path"})
            required.update({"branch", "path"})
        elif operation_id == "github.actions.jobs.list":
            allowed.add("run_id")
            required.add("run_id")
        elif operation_id == "github.actions.job.logs.read":
            allowed.update({"run_id", "job_id"})
            required.update({"run_id", "job_id"})
        elif operation_id in {
            "github.pull_request.draft.create",
            "github.pull_request.draft.find",
        }:
            allowed.update({"head_branch", "base_branch"})
            required.update({"head_branch", "base_branch"})
        elif operation_id == "github.actions.rerun_failed":
            allowed.add("run_id")
            required.add("run_id")
        if set(target) - allowed or not required.issubset(target):
            raise AccountCenterError("invalid_request")
        repository_id = _numeric_id(target["repository_id"])
        repository = target["repository"]
        if not isinstance(repository, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repository
        ):
            raise AccountCenterError("invalid_request")
        normalized = {"repository_id": repository_id, "repository": repository}
        for field in ("issue_number", "pull_number"):
            if field in target:
                number = target[field]
                if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                    raise AccountCenterError("invalid_request")
                normalized[field] = number
        for field in ("branch", "head_branch", "base_branch"):
            if field in target:
                text = target[field]
                if not _github_branch_locator(text):
                    raise AccountCenterError("invalid_request")
                normalized[field] = text
        if "path" in target:
            path = target["path"]
            if not _github_path_locator(path):
                raise AccountCenterError("invalid_request")
            normalized["path"] = path
        for field in ("run_id", "job_id"):
            if field in target:
                identifier = target[field]
                if not isinstance(identifier, str) or not re.fullmatch(
                    r"[1-9][0-9]{0,19}", identifier
                ):
                    raise AccountCenterError("invalid_request")
                normalized[field] = identifier
        if "commit_sha" in target:
            commit_sha = target["commit_sha"]
            if not isinstance(commit_sha, str) or not re.fullmatch(
                r"[0-9a-fA-F]{7,64}", commit_sha
            ):
                raise AccountCenterError("invalid_request")
            normalized["commit_sha"] = commit_sha
        for field in ("base_sha", "head_sha"):
            if field in target:
                commit_sha = target[field]
                if not isinstance(commit_sha, str) or not re.fullmatch(
                    r"[0-9a-fA-F]{40}", commit_sha
                ):
                    raise AccountCenterError("invalid_request")
                normalized[field] = commit_sha.lower()
        return normalized
    if provider == "huggingface":
        if operation_id == "huggingface.vendor.cli.execute":
            if target:
                raise AccountCenterError("invalid_request")
            return {}
        allowed = {"repo_type", "repo_id", "revision", "path"}
        if set(target) - allowed or "repo_type" not in target:
            raise AccountCenterError("invalid_request")
        repo_type = target["repo_type"]
        if repo_type not in {"model", "dataset", "space"}:
            raise AccountCenterError("invalid_request")
        normalized = {"repo_type": repo_type}
        if operation_id in {
            "huggingface.models.list",
            "huggingface.datasets.list",
            "huggingface.spaces.list",
        }:
            expected = operation_id.split(".")[1][:-1]
            if repo_type != expected or set(target) != {"repo_type"}:
                raise AccountCenterError("invalid_request")
            return normalized
        repo_id = target.get("repo_id")
        if not isinstance(repo_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo_id
        ):
            raise AccountCenterError("invalid_request")
        normalized["repo_id"] = repo_id
        if "revision" in target:
            revision = target["revision"]
            if (
                not isinstance(revision, str)
                or not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", revision)
                or ".." in revision.split("/")
            ):
                raise AccountCenterError("invalid_request")
            normalized["revision"] = revision
        if "path" in target:
            path = target["path"]
            if not isinstance(path, str) or not path or len(path) > 1024:
                raise AccountCenterError("invalid_request")
            if any(segment in {"", ".", ".."} for segment in path.split("/")):
                raise AccountCenterError("invalid_request")
            normalized["path"] = path
        required = {
            "huggingface.repo.get": {"repo_type", "repo_id"},
            "huggingface.file.download": {"repo_type", "repo_id", "path"},
            "huggingface.file.upload": {"repo_type", "repo_id", "path"},
        }
        if operation_id in required and set(normalized) < required[operation_id]:
            raise AccountCenterError("invalid_request")
        return normalized
    if provider == "microsoft_graph":
        target_fields = {
            "microsoft_graph.planner.plan.get": {"tenant_id", "plan_id"},
            "microsoft_graph.planner.plans.list": {"tenant_id"},
            "microsoft_graph.planner.tasks.list": {"tenant_id", "plan_id"},
            "microsoft_graph.planner.task.get": {
                "tenant_id",
                "plan_id",
                "task_id",
            },
            "microsoft_graph.planner.task.create": {
                "tenant_id",
                "plan_id",
                "bucket_id",
            },
            "microsoft_graph.planner.task.update": {
                "tenant_id",
                "plan_id",
                "task_id",
            },
            "microsoft_graph.sharepoint.site.get": {"tenant_id", "site_id"},
            "microsoft_graph.sharepoint.list.get": {
                "tenant_id",
                "site_id",
                "list_id",
            },
            "microsoft_graph.sharepoint.list.items.delta": {
                "tenant_id",
                "site_id",
                "list_id",
            },
            "microsoft_graph.todo.lists.list": {"tenant_id"},
            "microsoft_graph.todo.tasks.list": {"tenant_id", "task_list_id"},
            "microsoft_graph.todo.task.get": {
                "tenant_id",
                "task_list_id",
                "task_id",
            },
            "microsoft_graph.todo.task.create": {"tenant_id", "task_list_id"},
            "microsoft_graph.todo.task.update": {
                "tenant_id",
                "task_list_id",
                "task_id",
            },
            "microsoft_graph.todo.task.delete": {
                "tenant_id",
                "task_list_id",
                "task_id",
            },
            "microsoft_graph.calendar.calendars.list": {"tenant_id"},
            "microsoft_graph.calendar.events.list": {"tenant_id", "calendar_id"},
            "microsoft_graph.calendar.event.get": {
                "tenant_id",
                "calendar_id",
                "event_id",
            },
            "microsoft_graph.calendar.event.create": {
                "tenant_id",
                "calendar_id",
                "transaction_id",
            },
            "microsoft_graph.calendar.event.update": {
                "tenant_id",
                "calendar_id",
                "event_id",
            },
            "microsoft_graph.calendar.event.delete": {
                "tenant_id",
                "calendar_id",
                "event_id",
            },
            "microsoft_graph.calendar.freebusy.get": {
                "tenant_id",
                "schedule_ids",
            },
            "microsoft_graph.mail.folders.list": {"tenant_id"},
            "microsoft_graph.mail.messages.list": {"tenant_id", "folder_id"},
            "microsoft_graph.mail.message.get": {
                "tenant_id",
                "folder_id",
                "message_id",
            },
            "microsoft_graph.mail.drafts.list": {"tenant_id", "folder_id"},
            "microsoft_graph.mail.draft.create": {"tenant_id", "folder_id"},
            "microsoft_graph.mail.draft.update": {
                "tenant_id",
                "folder_id",
                "message_id",
            },
            "microsoft_graph.mail.draft.delete": {
                "tenant_id",
                "folder_id",
                "message_id",
            },
            "microsoft_graph.mail.message.send": {
                "tenant_id",
                "folder_id",
                "message_id",
            },
            "microsoft_graph.sharepoint.drives.list": {"tenant_id", "site_id"},
            "microsoft_graph.sharepoint.drive.items.list": {
                "tenant_id",
                "site_id",
                "drive_id",
            },
        }
        expected = target_fields.get(operation_id)
        if expected is None:
            raise AccountCenterError("authorization_denied")
        if set(target) != expected:
            raise AccountCenterError("invalid_request")
        normalized = {"tenant_id": _safe_graph_uuid(target["tenant_id"])}
        if "plan_id" in expected:
            normalized["plan_id"] = _safe_graph_planner_id(target["plan_id"])
        if "task_id" in expected:
            task_id_validator = (
                _safe_graph_opaque_id
                if operation_id.startswith("microsoft_graph.todo.")
                else _safe_graph_planner_id
            )
            normalized["task_id"] = task_id_validator(target["task_id"])
        if "bucket_id" in expected:
            normalized["bucket_id"] = _safe_graph_planner_id(target["bucket_id"])
        if "site_id" in expected:
            normalized["site_id"] = _safe_graph_site_id(target["site_id"])
        if "list_id" in expected:
            normalized["list_id"] = _safe_graph_uuid(target["list_id"])
        if "task_list_id" in expected:
            normalized["task_list_id"] = _safe_graph_opaque_id(target["task_list_id"])
        if "calendar_id" in expected:
            normalized["calendar_id"] = _safe_graph_opaque_id(target["calendar_id"])
        if "event_id" in expected:
            normalized["event_id"] = _safe_graph_opaque_id(target["event_id"])
        if "transaction_id" in expected:
            normalized["transaction_id"] = _safe_graph_uuid(target["transaction_id"])
        if "schedule_ids" in expected:
            normalized["schedule_ids"] = sorted(_safe_graph_schedule_ids(target["schedule_ids"]))
        if "folder_id" in expected:
            if operation_id in {
                "microsoft_graph.mail.drafts.list",
                "microsoft_graph.mail.draft.create",
                "microsoft_graph.mail.draft.update",
                "microsoft_graph.mail.draft.delete",
                "microsoft_graph.mail.message.send",
            }:
                if target["folder_id"] != "drafts":
                    raise AccountCenterError("invalid_request")
                normalized["folder_id"] = "drafts"
            else:
                normalized["folder_id"] = _safe_graph_opaque_id(target["folder_id"])
        if "message_id" in expected:
            normalized["message_id"] = _safe_graph_opaque_id(target["message_id"])
        if "drive_id" in expected:
            normalized["drive_id"] = _safe_graph_opaque_id(target["drive_id"])
        return normalized
    if provider == "azure_devops":
        base = {"tenant_id", "organization", "project_id", "source_id", "source_kind"}
        allowed_fields = {
            "azure_devops.observations.poll": base
            | {"pipeline_id", "definition_id", "definition_environment_id"},
            "azure_devops.pipelines.list": base,
            "azure_devops.pipeline_runs.list": base | {"pipeline_id"},
            "azure_devops.pipeline_run.get": base | {"pipeline_id", "run_id"},
            "azure_devops.deployments.list": base | {"definition_id", "definition_environment_id"},
            "azure_devops.release_environment.get": base
            | {
                "definition_id",
                "definition_environment_id",
                "release_id",
                "environment_id",
            },
        }
        required_fields = {
            "azure_devops.observations.poll": base,
            "azure_devops.pipelines.list": base,
            "azure_devops.pipeline_runs.list": base | {"pipeline_id"},
            "azure_devops.pipeline_run.get": base | {"pipeline_id", "run_id"},
            "azure_devops.deployments.list": base,
            "azure_devops.release_environment.get": base | {"release_id", "environment_id"},
        }
        allowed = allowed_fields.get(operation_id)
        required = required_fields.get(operation_id)
        if allowed is None or required is None:
            raise AccountCenterError("authorization_denied")
        if set(target) - allowed or not required.issubset(target):
            raise AccountCenterError("invalid_request")
        tenant_id = _safe_graph_uuid(target["tenant_id"])
        project_id = _safe_graph_uuid(target["project_id"])
        organization = target["organization"]
        source_id = target["source_id"]
        source_kind = target["source_kind"]
        if (
            not isinstance(organization, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", organization) is None
            or not isinstance(source_id, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", source_id) is None
            or source_kind not in {"project_pipelines", "pipeline_runs", "release_deployments"}
        ):
            raise AccountCenterError("invalid_request")
        normalized = {
            "tenant_id": tenant_id,
            "organization": organization,
            "project_id": project_id,
            "source_id": source_id,
            "source_kind": source_kind,
        }
        for field in (
            "pipeline_id",
            "definition_id",
            "definition_environment_id",
            "run_id",
            "release_id",
            "environment_id",
        ):
            if field in target:
                normalized[field] = _safe_azure_devops_numeric_id(target[field])
        return normalized
    raise AccountCenterError("authorization_denied")


def _github_branch_locator(value: Any) -> bool:
    """Validate a bounded Git reference locator without normalizing its identity."""

    return not (
        not isinstance(value, str)
        or not 1 <= len(value) <= 255
        or re.fullmatch(r"[A-Za-z0-9._/-]+", value) is None
        or value.startswith("/")
        or value.endswith(("/", "."))
        or "//" in value
        or "@{" in value
        or any(
            part in {"", ".", ".."} or part.startswith(".") or part.endswith(".lock")
            for part in value.split("/")
        )
    )


def _github_path_locator(value: Any) -> bool:
    """Validate a bounded repository-relative file locator."""

    return not (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value) is None
        or any(part in {"", ".", ".."} or part.startswith(".") for part in value.split("/"))
    )


def _provider_target_allowed(
    provider: str,
    operation_id: str,
    target: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> bool:
    """Check one target against the linked profile's explicit resource scope."""

    if provider == "github":
        repositories = _safe_github_repositories(scope.get("repositories"))
        if operation_id == "github.vendor.cli.execute":
            return bool(repositories)
        if operation_id == "github.repository.list":
            return bool(repositories)
        return any(
            repository["repository_id"] == target.get("repository_id")
            and f"{repository['owner']}/{repository['name']}" == target.get("repository")
            for repository in repositories
        )
    if provider == "huggingface":
        repositories = _safe_huggingface_scopes(scope).get("repositories", [])
        if operation_id == "huggingface.vendor.cli.execute":
            return bool(repositories)
        repo_type = target.get("repo_type")
        required_permission = "write" if operation_id == "huggingface.file.upload" else "read"
        matching = [item for item in repositories if item["repo_type"] == repo_type]
        if operation_id.endswith(".list"):
            return any("read" in item["permissions"] for item in matching)
        return any(
            item["repo_id"] == target.get("repo_id") and required_permission in item["permissions"]
            for item in matching
        )
    if provider == "microsoft_graph":
        try:
            graph_scope = _safe_microsoft_graph_profile_scope(scope)
        except AccountCenterError:
            return False
        if target.get("tenant_id") != graph_scope["tenant_id"]:
            return False
        if operation_id.startswith("microsoft_graph.planner."):
            if operation_id == "microsoft_graph.planner.plans.list":
                return graph_scope["planner"]["tasks_read"]
            return (
                graph_scope["planner"]["tasks_read"]
                and target.get("plan_id") in graph_scope["planner"]["plan_ids"]
            )
        if graph_scope.get("schema_version") == 2:
            mail = graph_scope["mail"]
            if operation_id == "microsoft_graph.mail.folders.list":
                return mail["metadata_read"]
            if operation_id in {
                "microsoft_graph.mail.messages.list",
                "microsoft_graph.mail.message.get",
            }:
                return mail["metadata_read"] and target.get("folder_id") in mail["folder_ids"]
            if operation_id in {
                "microsoft_graph.mail.drafts.list",
                "microsoft_graph.mail.draft.create",
                "microsoft_graph.mail.draft.update",
                "microsoft_graph.mail.draft.delete",
            }:
                return mail["drafts_write"]
            if operation_id == "microsoft_graph.mail.message.send":
                return mail["send_enabled"]
            if operation_id == "microsoft_graph.todo.lists.list":
                return graph_scope["todo"]["enabled"] and graph_scope["todo"]["tasks_read"]
            if operation_id.startswith("microsoft_graph.todo."):
                todo = graph_scope["todo"]
                if (
                    not todo["enabled"]
                    or not todo["tasks_read"]
                    or target.get("task_list_id") not in todo["task_list_ids"]
                ):
                    return False
                return (
                    not operation_id.rsplit(".", 1)[-1]
                    in {
                        "create",
                        "update",
                        "delete",
                    }
                    or todo["tasks_write"]
                )
            if operation_id == "microsoft_graph.calendar.calendars.list":
                calendar = graph_scope["calendar"]
                return calendar["enabled"] and (
                    calendar["events_read"] or calendar["freebusy_read"]
                )
            if operation_id.startswith("microsoft_graph.calendar."):
                calendar = graph_scope["calendar"]
                if not calendar["enabled"]:
                    return False
                if operation_id == "microsoft_graph.calendar.freebusy.get":
                    schedule_ids = target.get("schedule_ids")
                    return (
                        calendar["freebusy_read"]
                        and isinstance(schedule_ids, list)
                        and bool(schedule_ids)
                        and set(schedule_ids) <= set(calendar["freebusy_subjects"])
                    )
                if target.get("calendar_id") not in calendar["calendar_ids"]:
                    return False
                if operation_id in {
                    "microsoft_graph.calendar.events.list",
                    "microsoft_graph.calendar.event.get",
                }:
                    return calendar["events_read"]
                return calendar["events_write"]
            if operation_id.startswith("microsoft_graph.sharepoint."):
                sharepoint = graph_scope["sharepoint"]
                if (
                    not sharepoint["enabled"]
                    or not sharepoint["metadata_read"]
                    or target.get("site_id") != sharepoint["site_id"]
                ):
                    return False
                if "drive_id" in target:
                    return target.get("drive_id") in sharepoint["drive_ids"]
                if "list_id" in target:
                    return target.get("list_id") in sharepoint["list_ids"]
                return True
            return False
        if operation_id.startswith("microsoft_graph.sharepoint."):
            if (
                not graph_scope["sharepoint"]["enabled"]
                or target.get("site_id") != graph_scope["sharepoint"]["site_id"]
            ):
                return False
            return (
                target.get("list_id") in graph_scope["sharepoint"]["list_ids"]
                if "list_id" in target
                else True
            )
    if provider == "azure_devops":
        try:
            ado_scope = _safe_azure_devops_profile_scope(scope)
        except AccountCenterError:
            return False
        exact_fields = (
            "tenant_id",
            "organization",
            "project_id",
            "source_id",
            "source_kind",
            "pipeline_id",
            "definition_id",
            "definition_environment_id",
        )
        if any(target.get(field) != ado_scope.get(field) for field in exact_fields):
            return False
        required_kind = {
            "azure_devops.pipelines.list": "project_pipelines",
            "azure_devops.pipeline_runs.list": "pipeline_runs",
            "azure_devops.pipeline_run.get": "pipeline_runs",
            "azure_devops.deployments.list": "release_deployments",
            "azure_devops.release_environment.get": "release_deployments",
        }.get(operation_id)
        if required_kind is not None and ado_scope["source_kind"] != required_kind:
            return False
        if operation_id == "azure_devops.observations.poll" and ado_scope["source_kind"] not in {
            "pipeline_runs",
            "release_deployments",
        }:
            return False
        return not (
            operation_id == "azure_devops.release_environment.get"
            and "definition_environment_id" in ado_scope
            and target.get("environment_id") != ado_scope["definition_environment_id"]
        )
    return False


def _microsoft_graph_operation_authorized(operation_id: str, profile: Mapping[str, Any]) -> bool:
    """Require recorded delegated consent for the exact Graph operation class."""

    try:
        scopes = _safe_microsoft_graph_oauth_scopes(_json_array(profile["oauth_scopes_json"]))
        profile_scope = _safe_microsoft_graph_profile_scope(
            _json_object(profile["target_scope_json"])
        )
    except (AccountCenterError, KeyError, TypeError):
        return False
    if scopes != _expected_microsoft_graph_scopes(profile_scope):
        return False
    if operation_id in {
        "microsoft_graph.planner.task.create",
        "microsoft_graph.planner.task.update",
    }:
        return profile_scope["planner"]["tasks_write"] and "Tasks.ReadWrite" in scopes
    if operation_id.startswith("microsoft_graph.planner."):
        return bool({"Tasks.Read", "Tasks.ReadWrite"} & scopes)
    if operation_id.startswith("microsoft_graph.sharepoint."):
        return profile_scope["sharepoint"]["enabled"] and "Sites.Selected" in scopes
    if profile_scope.get("schema_version") == 2:
        mail = profile_scope["mail"]
        if operation_id in {
            "microsoft_graph.mail.folders.list",
            "microsoft_graph.mail.messages.list",
            "microsoft_graph.mail.message.get",
        }:
            return mail["metadata_read"] and bool({"Mail.ReadBasic", "Mail.ReadWrite"} & scopes)
        if operation_id in {
            "microsoft_graph.mail.drafts.list",
            "microsoft_graph.mail.draft.create",
            "microsoft_graph.mail.draft.update",
            "microsoft_graph.mail.draft.delete",
        }:
            return mail["drafts_write"] and "Mail.ReadWrite" in scopes
        if operation_id == "microsoft_graph.mail.message.send":
            return (
                mail["send_enabled"]
                and "Mail.Send" in scopes
                and bool({"Mail.ReadBasic", "Mail.ReadWrite"} & scopes)
            )
        if operation_id.startswith("microsoft_graph.todo."):
            return profile_scope["todo"]["enabled"] and profile_scope["todo"]["tasks_read"]
        if operation_id.startswith("microsoft_graph.calendar."):
            calendar = profile_scope["calendar"]
            if operation_id.endswith((".create", ".update", ".delete")):
                return calendar["events_write"] and "Calendars.ReadWrite" in scopes
            if operation_id == "microsoft_graph.calendar.freebusy.get":
                return calendar["freebusy_read"] and bool(
                    {"Calendars.ReadBasic", "Calendars.ReadWrite"} & scopes
                )
            return calendar["enabled"] and bool(
                {"Calendars.ReadBasic", "Calendars.ReadWrite"} & scopes
            )
    return False


def _azure_devops_operation_authorized(operation_id: str, profile: Mapping[str, Any]) -> bool:
    """Require the fixed Entra Azure DevOps resource on one actor-owned profile."""

    try:
        scopes = _safe_azure_devops_oauth_scopes(_json_array(profile["oauth_scopes_json"]))
        scope = _safe_azure_devops_profile_scope(_json_object(profile["target_scope_json"]))
    except (AccountCenterError, KeyError, TypeError):
        return False
    if (
        profile["provider"] != "azure_devops"
        or profile["auth_mode"] != "delegated_oauth"
        or profile["access_mode"] != "actor_owned"
        or AZURE_DEVOPS_RESOURCE_SCOPE not in scopes
    ):
        return False
    allowed_by_source = {
        "project_pipelines": {"azure_devops.pipelines.list"},
        "pipeline_runs": {
            "azure_devops.observations.poll",
            "azure_devops.pipeline_runs.list",
            "azure_devops.pipeline_run.get",
        },
        "release_deployments": {
            "azure_devops.observations.poll",
            "azure_devops.deployments.list",
            "azure_devops.release_environment.get",
        },
    }
    return operation_id in allowed_by_source[scope["source_kind"]]


def _json_object(value: Any) -> dict[str, Any]:
    """Decode one stored JSON object or fail closed on corrupt profile metadata."""

    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise AccountCenterError("profile_unavailable") from None
    if not isinstance(result, dict):
        raise AccountCenterError("profile_unavailable")
    return result


def _json_array(value: Any) -> list[Any]:
    """Decode one stored JSON list or fail closed on corrupt profile metadata."""

    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise AccountCenterError("profile_unavailable") from None
    if not isinstance(result, list):
        raise AccountCenterError("profile_unavailable")
    return result


def _authorization_request(
    request: Mapping[str, Any],
) -> tuple[str, str, str, dict[str, Any], str]:
    """Validate common authorization and lease request fields."""

    _require_fields(request, {"account_ref", "operation_id", "resource", "arguments_sha256"})
    account_ref = _uuid_text(request["account_ref"])
    operation_id = _required_text(request, "operation_id", maximum=200)
    resource = _safe_resource(request["resource"])
    args_digest = _sha256_text(request["arguments_sha256"])
    return account_ref, "github", operation_id, resource, args_digest


def _anchor_from_session(session: Mapping[str, Any]) -> Anchor:
    """Rebuild the original verified owner from an expiring link session."""

    return Anchor(
        session["anchor_issuer"],
        session["anchor_subject"],
        session["organization_id"],
        session["workspace_id"],
    )


def _session_matches_anchor(session: Mapping[str, Any], anchor: Anchor) -> bool:
    return (
        session["anchor_issuer"] == anchor.issuer
        and session["anchor_subject"] == anchor.subject
        and session["organization_id"] == anchor.organization_id
        and session["workspace_id"] == anchor.workspace_id
    )


def _idempotency_key(request: Mapping[str, Any]) -> str:
    """Validate the opaque per-command key required for public mutations."""

    value = _required_text(request, "idempotency_key", maximum=200)
    if value != value.strip() or any(
        ord(character) < 0x21 or ord(character) == 0x7F for character in value
    ):
        raise AccountCenterError("invalid_request")
    return value


def _idempotency_scope_hash(method: str, anchor: Anchor, key: str) -> str:
    """Hash the exact verified owner, method, and caller-supplied idempotency key."""

    return hashlib.sha256(
        canonical_json(
            {
                "method": method,
                "issuer": anchor.issuer,
                "subject": anchor.subject,
                "organization_id": anchor.organization_id,
                "workspace_id": anchor.workspace_id,
                "idempotency_key": key,
            }
        )
    ).hexdigest()


def _uuid_text(value: Any) -> str:
    """Require one canonical opaque UUID string."""

    if not isinstance(value, str):
        raise AccountCenterError("invalid_request")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise AccountCenterError("invalid_request") from None
    if str(parsed) != value.lower():
        raise AccountCenterError("invalid_request")
    return str(parsed)


def _secret_text() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_text(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise AccountCenterError("invalid_request")
    return value


def _required_config_text(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AccountCenterError("configuration_unavailable")
    return value


def _validated_callback_uri(value: str) -> str:
    """Require the fixed HTTPS callback with no additional OAuth query fields."""

    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AccountCenterError("configuration_unavailable")
    return value


def _validated_microsoft_graph_callback_uri(value: str) -> str:
    """Require a private localhost OAuth UI callback with a fixed path."""

    if not isinstance(value, str):
        raise AccountCenterError("configuration_unavailable")
    match = re.fullmatch(r"http://localhost:([0-9]{1,5})/oauth/callback", value)
    if match is None or not 1 <= int(match.group(1)) <= 65_535:
        raise AccountCenterError("configuration_unavailable")
    return value


def _mapping_config(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = config.get(key, {})
    if not isinstance(value, Mapping):
        raise AccountCenterError("configuration_unavailable")
    return value


def _load_caller_keys(config: Mapping[str, Any]) -> dict[str, bytes]:
    """Load per-caller HMAC keys through injected environment secret names."""

    raw = _mapping_config(config, "caller_keys")
    result: dict[str, bytes] = {}
    for caller_id, value in raw.items():
        if isinstance(value, bytes):
            key = value
        elif isinstance(value, str):
            key = decode_key(value, length=32)
        else:
            raise AccountCenterError("configuration_unavailable")
        if not isinstance(caller_id, str) or len(key) != 32:
            raise AccountCenterError("configuration_unavailable")
        result[caller_id] = key
    if not result:
        raise AccountCenterError("configuration_unavailable")
    if len(set(result.values())) != len(result):
        raise AccountCenterError("configuration_unavailable")
    return result


def _load_method_callers(
    config: Mapping[str, Any],
    *,
    exchange_caller: str,
    callback_caller: str | None,
    microsoft_graph_callback_caller: str | None,
    microsoft_graph_provider_caller: str | None,
    provider_caller: str,
    provider_callers: set[str],
    wecom_callers: set[str],
) -> dict[str, set[str]]:
    """Apply fixed method roles; activation settings may only narrow them."""

    configured = _mapping_config(config, "method_callers")
    if set(configured) - set(METHODS):
        raise AccountCenterError("configuration_unavailable")
    role_callers = {
        **{method: {exchange_caller} for method in EXCHANGE_ONLY_METHODS},
        "list_provider_profiles": {exchange_caller}
        | (
            {microsoft_graph_provider_caller}
            if microsoft_graph_provider_caller is not None
            else set()
        ),
        "complete_github_link": {callback_caller} if callback_caller else set(),
        "complete_microsoft_graph_link": (
            {microsoft_graph_callback_caller} if microsoft_graph_callback_caller else set()
        ),
        "complete_microsoft_graph_scope_upgrade": (
            {microsoft_graph_callback_caller} if microsoft_graph_callback_caller else set()
        ),
        "complete_azure_devops_link": (
            {microsoft_graph_callback_caller} if microsoft_graph_callback_caller else set()
        ),
        "observe_wecom_challenge": wecom_callers,
        "redeem_credential_lease": {provider_caller},
        "redeem_provider_credential_lease": provider_callers,
    }
    result: dict[str, set[str]] = {}
    for method in METHODS:
        values = configured.get(method, sorted(role_callers[method]))
        if not isinstance(values, list) or any(
            not isinstance(item, str) or not item for item in values
        ):
            raise AccountCenterError("configuration_unavailable")
        callers = set(values)
        if len(callers) != len(values) or not callers <= role_callers[method]:
            raise AccountCenterError("configuration_unavailable")
        result[method] = callers
    return result


def _load_caller_ids(config: Mapping[str, Any]) -> set[str]:
    raw = _mapping_config(config, "caller_keys")
    return {key for key in raw if isinstance(key, str)}


def _environment_config() -> dict[str, Any]:
    """Resolve the standard binding JSON and stable host secret references.

    Secret values stay outside the activation JSON. ``hostSecretRefs`` maps each
    stable reference to the environment variable name injected by the host.
    """

    try:
        settings = read_environment_settings()
        if settings is None:
            raise AccountCenterError("configuration_unavailable")
        supplied = json.loads(settings["config"], object_pairs_hook=_reject_duplicate_pairs)
        if not isinstance(supplied, dict):
            raise AccountCenterError("configuration_unavailable")
        config = dict(supplied)
        allowed_fields = {
            "storage_path",
            "github_callback_uri",
            "microsoft_graph_callback_uri",
            "microsoft_graph_client_id",
            "microsoft_graph_client_secret_ref",
            "microsoft_graph_callback_caller_id",
            "microsoft_graph_provider_caller_id",
            "azure_devops_provider_caller_id",
            "github_client_id",
            "github_client_secret_ref",
            "encryption_key_ref",
            "caller_key_refs",
            "hostSecretRefs",
            "exchange_caller_id",
            "github_callback_caller_id",
            "github_provider_caller_id",
            "huggingface_provider_caller_id",
            "github_app_id",
            "github_app_private_key_ref",
            "provider_secret_refs",
            "provider_secret_file_refs",
            "hostSecretFileRefs",
            "workspace_operator_profile_refs",
            "method_callers",
            "wecom_sources",
        }
        optional_fields = {
            "wecom_sources",
            "storage_path",
            "github_callback_uri",
            "microsoft_graph_callback_uri",
            "microsoft_graph_client_id",
            "microsoft_graph_client_secret_ref",
            "microsoft_graph_callback_caller_id",
            "microsoft_graph_provider_caller_id",
            "azure_devops_provider_caller_id",
            "github_client_id",
            "github_client_secret_ref",
            "github_callback_caller_id",
            "huggingface_provider_caller_id",
            "github_app_id",
            "github_app_private_key_ref",
            "provider_secret_refs",
            "provider_secret_file_refs",
            "hostSecretFileRefs",
            "workspace_operator_profile_refs",
        }
        required_fields = allowed_fields - optional_fields
        if set(config) - allowed_fields or not required_fields <= set(config):
            raise AccountCenterError("configuration_unavailable")
        storage_root = _validated_private_storage_root(os.environ.get("CYRENE_PLUGIN_STORAGE_PATH"))
        config["storage_path"] = str(
            _resolved_account_database_path(config.get("storage_path"), storage_root)
        )
        config.setdefault("workspace_operator_profile_refs", [])
        config["binding_id"] = settings["binding_id"]
        refs = config.get("hostSecretRefs")
        if not isinstance(refs, dict) or any(
            not isinstance(ref, str) or not ref or not isinstance(env_name, str) or not env_name
            for ref, env_name in refs.items()
        ):
            raise AccountCenterError("configuration_unavailable")
        file_refs = config.get("hostSecretFileRefs", {})
        if not isinstance(file_refs, dict) or any(
            not isinstance(reference, str)
            or not re.fullmatch(r"[a-z][a-z0-9._/-]{0,127}", reference)
            or not isinstance(path, str)
            or not path.startswith("/run/secrets/")
            or ".." in Path(path).parts
            for reference, path in file_refs.items()
        ):
            raise AccountCenterError("configuration_unavailable")
        if set(file_refs) & set(refs):
            raise AccountCenterError("configuration_unavailable")

        oauth_fields = (
            "github_callback_uri",
            "github_client_id",
            "github_client_secret_ref",
            "github_callback_caller_id",
        )
        if any(config.get(field) is not None for field in oauth_fields) and not all(
            config.get(field) for field in oauth_fields
        ):
            raise AccountCenterError("configuration_unavailable")
        graph_oauth_fields = (
            "microsoft_graph_callback_uri",
            "microsoft_graph_client_id",
            "microsoft_graph_client_secret_ref",
            "microsoft_graph_callback_caller_id",
        )
        if any(config.get(field) is not None for field in graph_oauth_fields) and not all(
            config.get(field) for field in graph_oauth_fields
        ):
            raise AccountCenterError("configuration_unavailable")

        def resolve(reference: Any) -> str:
            if not isinstance(reference, str) or reference not in refs:
                raise AccountCenterError("configuration_unavailable")
            env_name = refs[reference]
            if not env_name.isidentifier() or not env_name.isupper():
                raise AccountCenterError("configuration_unavailable")
            value = os.environ.get(env_name)
            if not value:
                raise AccountCenterError("configuration_unavailable")
            return value

        encryption_ref = config.pop("encryption_key_ref", None)
        caller_key_refs = config.pop("caller_key_refs", None)
        github_secret_ref = config.pop("github_client_secret_ref", None)
        graph_secret_ref = config.pop("microsoft_graph_client_secret_ref", None)
        app_private_key_ref = config.pop("github_app_private_key_ref", None)
        provider_secret_refs = config.pop("provider_secret_refs", [])
        provider_secret_file_refs = config.pop("provider_secret_file_refs", [])
        if not isinstance(caller_key_refs, dict) or not caller_key_refs:
            raise AccountCenterError("configuration_unavailable")
        if (
            not isinstance(provider_secret_refs, list)
            or any(not isinstance(reference, str) for reference in provider_secret_refs)
            or len(set(provider_secret_refs)) != len(provider_secret_refs)
        ):
            raise AccountCenterError("configuration_unavailable")
        if (
            not isinstance(provider_secret_file_refs, list)
            or any(not isinstance(reference, str) for reference in provider_secret_file_refs)
            or len(set(provider_secret_file_refs)) != len(provider_secret_file_refs)
            or set(provider_secret_file_refs) - set(file_refs)
            or set(provider_secret_file_refs) & set(provider_secret_refs)
        ):
            raise AccountCenterError("configuration_unavailable")
        if (config.get("github_app_id") is None) != (app_private_key_ref is None):
            raise AccountCenterError("configuration_unavailable")
        config["encryption_key"] = resolve(encryption_ref)
        config["caller_keys"] = {
            caller: resolve(reference) for caller, reference in caller_key_refs.items()
        }
        if github_secret_ref is not None:
            config["github_client_secret"] = resolve(github_secret_ref)
        if graph_secret_ref is not None:
            config["microsoft_graph_client_secret"] = resolve(graph_secret_ref)
        if app_private_key_ref is not None:
            config["github_app_private_key"] = resolve(app_private_key_ref)
        config["provider_secrets"] = {
            reference: resolve(reference) for reference in provider_secret_refs
        }
        config["provider_secrets"].update(
            {
                reference: _read_private_secret_file(file_refs[reference])
                for reference in provider_secret_file_refs
            }
        )
        # Reject unused references so the host binding has one auditable meaning.
        used = {
            encryption_ref,
            github_secret_ref,
            graph_secret_ref,
            app_private_key_ref,
            *caller_key_refs.values(),
            *provider_secret_refs,
        } - {None}
        if used != set(refs) or set(provider_secret_file_refs) != set(file_refs):
            raise AccountCenterError("configuration_unavailable")
        return config
    except AccountCenterError:
        raise
    except (KeyError, ValueError, TypeError):
        raise AccountCenterError("configuration_unavailable") from None


class Plugin(AccountCenterPlugin):
    """Manifest-compatible short entrypoint alias."""


def _stored_huggingface_oauth_scopes(value: Any) -> tuple[str, ...]:
    """Load an exact persisted OAuth scope set without adding privileges."""

    try:
        scopes = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise AccountCenterError("profile_unavailable") from None
    if (
        not isinstance(scopes, list)
        or not scopes
        or any(not isinstance(scope, str) or scope not in ALLOWED_OAUTH_SCOPES for scope in scopes)
        or len(scopes) != len(set(scopes))
    ):
        raise AccountCenterError("profile_unavailable")
    return tuple(sorted(scopes))


def _huggingface_renewal_response(session: Mapping[str, Any]) -> dict[str, Any]:
    """Project only user-action metadata from a private device renewal row."""

    state = session.get("status")
    if state not in {
        "starting",
        "waiting_user",
        "succeeded",
        "failed",
        "expired",
        "cancelled",
        "unknown",
    }:
        raise AccountCenterError("renewal_session_unavailable")
    profile_revision = session.get("profile_revision")
    if isinstance(profile_revision, bool) or not isinstance(profile_revision, int):
        raise AccountCenterError("renewal_session_unavailable")
    active = state == "waiting_user"
    user_code = session.get("user_code") if active else None
    verification_uri = session.get("verification_uri") if active else None
    if active and (
        not isinstance(user_code, str)
        or re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", user_code) is None
        or verification_uri
        not in {
            "https://hf.co/oauth/device",
            "https://huggingface.co/oauth/device",
        }
    ):
        raise AccountCenterError("renewal_session_unavailable")
    expires_at = session.get("expires_at") if active else None
    interval = session.get("interval_seconds") if active else None
    return {
        "profile_ref": session.get("profile_ref"),
        "session_ref": session.get("session_ref"),
        "state": state,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "expires_at": expires_at if isinstance(expires_at, int) else None,
        "interval_seconds": interval if isinstance(interval, int) else None,
        "generation": profile_revision + (1 if state == "succeeded" else 0),
        "reason_code": session.get("reason_code"),
    }


def _read_private_secret_file(path: str) -> str:
    """Read one bounded, non-symlink secret mounted by the service launcher."""

    if not path.startswith("/run/secrets/") or ".." in Path(path).parts:
        raise AccountCenterError("configuration_unavailable")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size < 1
                or metadata.st_size > 64_000
                or metadata.st_mode & 0o022
            ):
                raise AccountCenterError("configuration_unavailable")
            with os.fdopen(descriptor, "rb", closefd=False) as secret_file:
                value = secret_file.read(64_001)
        finally:
            os.close(descriptor)
    except AccountCenterError:
        raise
    except OSError:
        raise AccountCenterError("configuration_unavailable") from None
    if len(value) > 64_000:
        raise AccountCenterError("configuration_unavailable")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise AccountCenterError("configuration_unavailable") from None


def _validated_private_storage_root(value: str | None) -> Path:
    """Require the host-provided, existing private directory for this binding."""

    if sys.platform != "linux" or not value:
        raise AccountCenterError("configuration_unavailable")
    path = Path(value)
    if not path.is_absolute():
        raise AccountCenterError("configuration_unavailable")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        raise AccountCenterError("configuration_unavailable") from None
    if not resolved.is_dir():
        raise AccountCenterError("configuration_unavailable")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise AccountCenterError("configuration_unavailable")
    if metadata.st_uid != os.geteuid():
        raise AccountCenterError("configuration_unavailable")
    return resolved


def _resolved_account_database_path(value: Any, storage_root: Path) -> Path:
    """Use the managed default and constrain any explicit file to that root."""

    if value is None:
        return storage_root / "accounts.sqlite3"
    if not isinstance(value, str) or not value:
        raise AccountCenterError("configuration_unavailable")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise AccountCenterError("configuration_unavailable")
    try:
        parent = candidate.parent.resolve(strict=True)
        resolved = parent / candidate.name
        try:
            metadata = resolved.lstat()
        except FileNotFoundError:
            metadata = None
        parent_metadata = parent.stat()
    except OSError:
        raise AccountCenterError("configuration_unavailable") from None
    if (
        not parent.is_relative_to(storage_root)
        or resolved == storage_root
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        or parent_metadata.st_uid != os.geteuid()
    ):
        raise AccountCenterError("configuration_unavailable")
    if metadata is not None and (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise AccountCenterError("configuration_unavailable")
    return resolved
