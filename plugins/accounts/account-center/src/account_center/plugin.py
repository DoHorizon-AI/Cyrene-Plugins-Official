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
from .models import AccountCenterError, Anchor, OAuthCredential, require_object
from .security import canonical_json, decode_key, decrypt, encrypt, verify_context
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
)
PROVIDER_OPERATIONS = {
    "github.issue.get",
    "github.issue.comment.create",
    "github.issue.state.update",
}
CALLBACK_METHODS = {"complete_github_link", "observe_wecom_challenge"}
EXCHANGE_ONLY_METHODS = {
    "begin_link",
    "confirm_wecom_link",
    "list_links",
    "get_authorization",
    "unlink",
    "create_credential_lease",
    "resolve_linked_sender",
}
PUBLIC_METHODS = {
    "begin_link",
    "confirm_wecom_link",
    "list_links",
    "get_authorization",
    "unlink",
    "create_credential_lease",
}
REQUEST_TYPE_PREFIX = "type.googleapis.com/cyrene.account.center.v1."
MAX_REQUEST_BYTES = 64 * 1024
MAX_IDEMPOTENCY_RESPONSE_BYTES = 64 * 1024
SESSION_TTL_SECONDS = 600
LEASE_TTL_SECONDS = 45
IDEMPOTENT_METHODS = {"begin_link", "confirm_wecom_link", "unlink"}


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
    }
    return names[action] + suffix


def _required_text(
    payload: Mapping[str, Any], field: str, *, maximum: int = 4096
) -> str:
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
        self._store = store or AccountStore(
            _required_config_text(selected, "storage_path")
        )
        self._now = now
        self._callback_uri = _validated_callback_uri(
            _required_config_text(selected, "github_callback_uri")
        )
        self._caller_keys = _load_caller_keys(selected)
        self._wecom_sources = _mapping_config(selected, "wecom_sources")
        self._exchange_caller = _required_config_text(selected, "exchange_caller_id")
        self._github_callback_caller = _required_config_text(
            selected, "github_callback_caller_id"
        )
        self._github_provider_caller = _required_config_text(
            selected, "github_provider_caller_id"
        )
        self._method_callers = _load_method_callers(
            selected,
            exchange_caller=self._exchange_caller,
            callback_caller=self._github_callback_caller,
            provider_caller=self._github_provider_caller,
            wecom_callers=set(self._wecom_sources),
        )
        expected_callers = {
            self._exchange_caller,
            self._github_callback_caller,
            self._github_provider_caller,
            *self._wecom_sources,
        }
        if (
            len(expected_callers)
            != 3 + len(self._wecom_sources)
            or set(self._caller_keys) != expected_callers
        ):
            raise AccountCenterError("configuration_unavailable")
        self._refresh_lock = threading.Lock()
        self._account_refresh_locks: dict[str, threading.Lock] = {}
        if github_client is not None:
            self._github = github_client
        else:
            self._github = GitHubOAuthClient(
                client_id=_required_config_text(selected, "github_client_id"),
                client_secret=_required_config_text(selected, "github_client_secret"),
            )
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
            context = require_object(
                envelope["trusted_context"], code="untrusted_caller"
            )
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
        anchor = verify_context(
            method, request, trusted, self._caller_keys, now=int(self._now())
        )
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
                payload_sha256=hashlib.sha256(canonical_json(body)).hexdigest(),
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
                    self._encode_idempotency_response(
                        key_hash, {"error": error.code}
                    ),
                )
                raise
            except Exception:  # noqa: BLE001 - persist only a fixed sanitized result.
                self._store.complete_idempotent_command(
                    key_hash,
                    self._encode_idempotency_response(
                        key_hash, {"error": "execution_failed"}
                    ),
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
        raise AccountCenterError("method_not_found")

    def _encode_idempotency_response(
        self, key_hash: str, value: Mapping[str, Any]
    ) -> bytes:
        """Encrypt one bounded replay response before storing it privately."""

        clear = canonical_json(dict(value))
        if len(clear) > MAX_IDEMPOTENCY_RESPONSE_BYTES:
            raise AccountCenterError("response_too_large")
        return encrypt(
            self._encryption_key,
            clear,
            f"command-idempotency:{key_hash}".encode(),
        )

    def _decode_idempotency_response(
        self, key_hash: str, blob: bytes
    ) -> dict[str, Any]:
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
                self._encode_idempotency_response(
                    idempotency_key_hash, {"value": response}
                ),
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
        state = _required_text(request, "state", maximum=256)
        code = _required_text(request, "code", maximum=4096)
        now = int(self._now())
        state_digest = _digest_text(state)
        session = self._store.claim_session_by_state(
            state_digest, self._callback_uri, now=now
        )
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
                on_credential=lambda value: self._persist_callback_credential(
                    session_ref, value
                ),
            )
            owner = _anchor_from_session(session)
            account_ref = str(uuid.uuid4())
            encrypted = self._encrypt_credential(
                account_ref=account_ref, credential=credential
            )
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
            return {"account_ref": account_ref, "provider": "github", "status": "active"}
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
        _require_fields(
            request, {"link_session_ref", "challenge_id", "challenge_value"}
        )
        session_ref = _uuid_text(request["link_session_ref"])
        challenge_id = _uuid_text(request["challenge_id"])
        challenge_value = _required_text(request, "challenge_value", maximum=128)
        now = int(self._now())
        session = self._store.session(session_ref)
        if session["provider"] != "wecom" or session["expires_at"] <= now:
            raise AccountCenterError("link_session_invalid")
        if not _session_matches_anchor(session, anchor):
            raise AccountCenterError("scope_mismatch")
        if not hmac.compare_digest(
            session["nonce_digest"], _digest_text(challenge_value)
        ):
            raise AccountCenterError("challenge_rejected")
        account_ref = str(uuid.uuid4())
        response = {"account_ref": account_ref, "provider": "wecom", "status": "active"}
        command_result = (
            (
                idempotency_key_hash,
                self._encode_idempotency_response(
                    idempotency_key_hash, {"value": response}
                ),
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

    def _get_authorization(
        self, request: Mapping[str, Any], anchor: Anchor
    ) -> dict[str, Any]:
        account_ref, provider, operation_id, resource, _args_digest = (
            _authorization_request(request)
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
        account_ref, provider, operation_id, resource, args_digest = (
            _authorization_request(request)
        )
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
        _require_fields(
            request, {"lease_ref", "operation_id", "resource", "arguments_sha256"}
        )
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

    def _fresh_credential(
        self, account: Mapping[str, Any]
    ) -> OAuthCredential:
        """Refresh once under a durable claim; never retry an uncertain rotation."""

        account_ref = account["account_ref"]
        with self._refresh_lock:
            account_lock = self._account_refresh_locks.setdefault(
                account_ref, threading.Lock()
            )
        with account_lock:
            owner = Anchor(
                account["anchor_issuer"],
                account["anchor_subject"],
                account["organization_id"],
                account["workspace_id"],
            )
            current = self._store.get_account(account_ref, owner)
            if (
                current["status"] != "active"
                or current["credential_status"] != "ready"
            ):
                raise AccountCenterError("credential_reauthentication_required")
            now = int(self._now())
            if (
                current["credential_expires_at"] is None
                or current["credential_expires_at"] > now + 30
            ):
                return self._decrypt_credential(
                    account_ref, bytes(current["credential_blob"])
                )
            current_credential = self._decrypt_credential(
                account_ref, bytes(current["credential_blob"])
            )
            if not current_credential.refresh_token or (
                current["refresh_expires_at"] is not None
                and current["refresh_expires_at"] <= now
            ):
                claim_id = str(uuid.uuid4())
                self._store.claim_refresh(
                    account_ref, owner, claim_id, now=now
                )
                self._store.fail_refresh(account_ref, claim_id)
                raise AccountCenterError("credential_reauthentication_required")

            claim_id = str(uuid.uuid4())
            revision = self._store.claim_refresh(
                account_ref, owner, claim_id, now=now
            )
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
            encrypted = self._encrypt_credential(
                account_ref=account_ref, credential=refreshed
            )
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

    def _persist_callback_credential(
        self, session_ref: str, credential: OAuthCredential
    ) -> None:
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
                outcome = self._revoke_callback_token(credential.access_token)
            except AccountCenterError:
                outcome = "unknown"
            self._store.fail_session(session_ref, outcome)

    def _recover_interrupted_unlink_results(self) -> None:
        """Finalize local unlinks whose remote revocation was interrupted."""

        for row in self._store.interrupted_unlink_commands():
            key_hash = row["remote_revocation_command_hash"]
            response = {"unlinked": True, "status": "revocation_unknown"}
            encrypted = self._encode_idempotency_response(key_hash, {"value": response})
            self._store.finalize_interrupted_unlink(
                row["account_ref"], key_hash, encrypted
            )

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
                credential = self._decrypt_credential(
                    account_ref, bytes(row["credential_blob"])
                )
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
                    self._encode_idempotency_response(
                        idempotency_key_hash, {"value": response}
                    ),
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
            remote_status = (
                "unknown" if error.code == "provider_unavailable" else "failed"
            )
            status = (
                "revocation_unknown" if remote_status == "unknown" else "revocation_failed"
            )
        except Exception:  # noqa: BLE001 - network outcome is not knowable.
            remote_status = "unknown"
            status = "revocation_unknown"
        response = {"unlinked": True, "status": status}
        command_result = (
            (
                idempotency_key_hash,
                self._encode_idempotency_response(
                    idempotency_key_hash, {"value": response}
                ),
            )
            if idempotency_key_hash is not None
            else None
        )
        self._store.set_revocation_status(
            account_ref, remote_status, command_result=command_result
        )
        return response

    def _resource_scope_allowed(
        self, resource: Mapping[str, Any], operation_id: str
    ) -> bool:
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


def _authorization_request(
    request: Mapping[str, Any],
) -> tuple[str, str, str, dict[str, Any], str]:
    """Validate common authorization and lease request fields."""

    _require_fields(
        request, {"account_ref", "operation_id", "resource", "arguments_sha256"}
    )
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
    if value != value.strip() or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value):
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
    return result


def _load_method_callers(
    config: Mapping[str, Any],
    *,
    exchange_caller: str,
    callback_caller: str,
    provider_caller: str,
    wecom_callers: set[str],
) -> dict[str, set[str]]:
    """Apply fixed method roles; activation settings may only narrow them."""

    configured = _mapping_config(config, "method_callers")
    if set(configured) - set(METHODS):
        raise AccountCenterError("configuration_unavailable")
    role_callers = {
        **{method: {exchange_caller} for method in EXCHANGE_ONLY_METHODS},
        "complete_github_link": {callback_caller},
        "observe_wecom_challenge": wecom_callers,
        "redeem_credential_lease": {provider_caller},
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
        supplied = json.loads(
            settings["config"], object_pairs_hook=_reject_duplicate_pairs
        )
        if not isinstance(supplied, dict):
            raise AccountCenterError("configuration_unavailable")
        config = dict(supplied)
        allowed_fields = {
            "storage_path",
            "github_callback_uri",
            "github_client_id",
            "github_client_secret_ref",
            "encryption_key_ref",
            "caller_key_refs",
            "hostSecretRefs",
            "exchange_caller_id",
            "github_callback_caller_id",
            "github_provider_caller_id",
            "method_callers",
            "wecom_sources",
        }
        required_fields = allowed_fields - {"wecom_sources", "storage_path"}
        if set(config) - allowed_fields or not required_fields <= set(config):
            raise AccountCenterError("configuration_unavailable")
        storage_root = _validated_private_storage_root(
            os.environ.get("CYRENE_PLUGIN_STORAGE_PATH")
        )
        config["storage_path"] = str(
            _resolved_account_database_path(config.get("storage_path"), storage_root)
        )
        config["binding_id"] = settings["binding_id"]
        refs = config.get("hostSecretRefs")
        if not isinstance(refs, dict) or any(
            not isinstance(ref, str)
            or not ref
            or not isinstance(env_name, str)
            or not env_name
            for ref, env_name in refs.items()
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
        if not isinstance(caller_key_refs, dict) or not caller_key_refs:
            raise AccountCenterError("configuration_unavailable")
        config["encryption_key"] = resolve(encryption_ref)
        config["caller_keys"] = {
            caller: resolve(reference)
            for caller, reference in caller_key_refs.items()
        }
        config["github_client_secret"] = resolve(github_secret_ref)
        # Reject unused references so the host binding has one auditable meaning.
        used = {encryption_ref, github_secret_ref, *caller_key_refs.values()}
        if used != set(refs):
            raise AccountCenterError("configuration_unavailable")
        return config
    except AccountCenterError:
        raise
    except (KeyError, ValueError, TypeError):
        raise AccountCenterError("configuration_unavailable") from None


class Plugin(AccountCenterPlugin):
    """Manifest-compatible short entrypoint alias."""


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
