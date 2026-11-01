"""Owner-scoped provider identity and exact lease regression coverage.

The fakes keep all authorization tests local and deterministic. 中文：仅用本地桩验证身份边界。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from account_center import plugin as account_center_plugin_module
from account_center.github import pkce_s256_challenge
from account_center.github_app import GitHubAppError
from account_center.huggingface_auth import (
    HuggingFaceAuthError,
    HuggingFaceIdentityClient,
)
from account_center.huggingface_oauth import (
    HuggingFaceDeviceCode,
    HuggingFaceOAuthClient,
    HuggingFaceOAuthError,
    HuggingFaceOAuthToken,
    parse_device_oauth_seed,
)
from account_center.microsoft_graph_oauth import (
    AZURE_DEVOPS_RESOURCE_SCOPE,
    MicrosoftGraphIdentity,
    MicrosoftGraphOAuthError,
    MicrosoftGraphToken,
)
from account_center.models import AccountCenterError, Anchor
from account_center.plugin import (
    AccountCenterPlugin,
    _validated_microsoft_graph_callback_uri,
)
from cyrene_account_center_contracts import canonical_json, sign_context

EXCHANGE = "exchange-service"
GITHUB_PROVIDER = "github-provider"
HF_PROVIDER = "huggingface-provider"
GRAPH_PROVIDER = "microsoft-graph-provider"
GRAPH_CALLBACK = "microsoft-entra-callback"
ADO_PROVIDER = "azure-devops-observation-provider"
BILIBILI_PROVIDER = "cyrene.connectors.bilibili"
GRAPH_TENANT_ID = "11111111-2222-4333-8444-555555555555"
GRAPH_OBJECT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_GRAPH_OBJECT_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
GRAPH_PROFILE_REF = "00000000-0000-4000-8000-000000000401"
GRAPH_V2_WORKSPACE_ID = "dddddddd-eeee-4fff-8000-111111111112"
GRAPH_LOOPBACK_CALLBACK = "http://localhost:52191/oauth/callback"
GRAPH_PLAN_ID = "plan_A2b_935s-FIFm2QAFkHM"
GRAPH_TASK_ID = "task_yQk3-opaque_ID"
GRAPH_BUCKET_ID = "bucket_936w-A2b"
GRAPH_SITE_ID = (
    "contoso.sharepoint.com,66666666-7777-4888-8999-aaaaaaaaaaaa,"
    "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
)
GRAPH_LIST_ID = "cccccccc-dddd-4eee-8fff-111111111111"
ADO_PROFILE_REF = "00000000-0000-4000-8000-000000000402"
ACTOR = {
    "issuer": "https://platform.example",
    "subject": "user-derek",
    "organization_id": "org-a",
    "workspace_id": "workspace-a",
}
ENTRA_ACTOR = {
    "issuer": f"https://login.microsoftonline.com/{GRAPH_TENANT_ID}/v2.0",
    "subject": GRAPH_OBJECT_ID,
    "organization_id": "org-a",
    "workspace_id": "workspace-a",
}
GITHUB_REPOSITORIES = [{"repository_id": "123", "owner": "acme", "name": "project"}]
GITHUB_TARGET = {"repository_id": "123", "repository": "acme/project"}
GITHUB_TYPED_TARGET_CASES = [
    (
        "github.issues.search",
        GITHUB_TARGET,
        {
            **GITHUB_TARGET,
            "state": "OPEN",
            "limit": 20,
            "updated_since": "2026-10-01T00:00:00Z",
        },
    ),
    (
        "github.commits.compare",
        {
            **GITHUB_TARGET,
            "base_sha": "A" * 40,
            "head_sha": "b" * 40,
        },
        {
            **GITHUB_TARGET,
            "base_sha": "A" * 40,
            "head_sha": "b" * 40,
        },
    ),
    (
        "github.branch.get",
        {**GITHUB_TARGET, "branch": "cyrene/assistant/task-1"},
        {**GITHUB_TARGET, "branch": "cyrene/assistant/task-1"},
    ),
    (
        "github.file.get",
        {
            **GITHUB_TARGET,
            "branch": "cyrene/assistant/task-1",
            "path": "src/main.py",
        },
        {
            **GITHUB_TARGET,
            "branch": "cyrene/assistant/task-1",
            "path": "src/main.py",
        },
    ),
    (
        "github.actions.jobs.list",
        {**GITHUB_TARGET, "run_id": "987654321"},
        {**GITHUB_TARGET, "run_id": "987654321", "limit": 50},
    ),
    (
        "github.actions.job.logs.read",
        {**GITHUB_TARGET, "run_id": "987654321", "job_id": "987654322"},
        {
            **GITHUB_TARGET,
            "run_id": "987654321",
            "job_id": "987654322",
            "max_lines": 100,
        },
    ),
    (
        "github.branch.create",
        {**GITHUB_TARGET, "branch": "cyrene/assistant/task-1"},
        {
            **GITHUB_TARGET,
            "branch": "cyrene/assistant/task-1",
            "from_sha": "a" * 40,
        },
    ),
    (
        "github.branch.delete",
        {**GITHUB_TARGET, "branch": "cyrene/assistant/task-1"},
        {
            **GITHUB_TARGET,
            "branch": "cyrene/assistant/task-1",
            "expected_sha": "a" * 40,
        },
    ),
    (
        "github.file.commit",
        {
            **GITHUB_TARGET,
            "branch": "cyrene/assistant/task-1",
            "path": "src/main.py",
        },
        {
            **GITHUB_TARGET,
            "branch": "cyrene/assistant/task-1",
            "path": "src/main.py",
            "message": "Update the task file",
            "content_base64": "YQ==",
            "expected_sha": "b" * 40,
        },
    ),
    (
        "github.pull_request.draft.create",
        {
            **GITHUB_TARGET,
            "head_branch": "cyrene/assistant/task-1",
            "base_branch": "develop",
        },
        {
            **GITHUB_TARGET,
            "head_branch": "cyrene/assistant/task-1",
            "base_branch": "develop",
            "title": "Task change",
            "body": "Bounded task work.",
        },
    ),
    (
        "github.pull_request.draft.find",
        {
            **GITHUB_TARGET,
            "head_branch": "cyrene/assistant/task-1",
            "base_branch": "develop",
        },
        {
            **GITHUB_TARGET,
            "head_branch": "cyrene/assistant/task-1",
            "base_branch": "develop",
        },
    ),
    (
        "github.actions.rerun_failed",
        {**GITHUB_TARGET, "run_id": "987654321"},
        {
            **GITHUB_TARGET,
            "run_id": "987654321",
            "expected_workflow_id": "987654323",
            "expected_head_branch": "cyrene/assistant/task-1",
            "expected_head_sha": "c" * 40,
            "expected_attempt": 2,
        },
    ),
]


class FakeGitHubApp:
    """Return predictable installation details and short-lived token values."""

    def __init__(self, now: Any) -> None:
        self.now = now
        self.verify_calls: list[str] = []
        self.token_calls: list[tuple[str, list[str]]] = []
        self.repository_calls: list[list[dict[str, str]]] = []
        self.token_ttl = 10
        self.token_error: GitHubAppError | None = None

    def verify_installation(self, installation_id: str) -> dict[str, str]:
        self.verify_calls.append(installation_id)
        return {"account_id": "88", "login": "acme"}

    def create_installation_token(self, installation_id: str, repository_ids: list[str]) -> Any:
        from account_center.github_app import InstallationToken

        self.token_calls.append((installation_id, repository_ids))
        if self.token_error is not None:
            raise self.token_error
        token = f"installation-token-{len(self.token_calls)}"
        ttl = self.token_ttl if len(self.token_calls) == 1 else 3600
        return InstallationToken(token, int(self.now()) + ttl)

    def verify_repositories(
        self,
        installation_token: str,
        repositories: list[dict[str, str]],
    ) -> None:
        assert installation_token.startswith("installation-token-")
        self.repository_calls.append(repositories)


class FakeHuggingFace:
    """Resolve test tokens to stable Hub account names without a network call."""

    identities: ClassVar[dict[str, str]] = {
        "hf_token_original": "derek-li",
        "hf_token_rotated": "derek-li",
        "hf_token_other": "other-user",
        "oauth_access_initial": "derek-li",
        "oauth_access_refreshed": "derek-li",
        "oauth_access_device_renewed": "derek-li",
        "oauth_access_wrong": "other-user",
    }

    def whoami(self, token: str) -> str:
        if token not in self.identities:
            raise HuggingFaceAuthError("credential_rejected")
        return self.identities[token]


class FakeHuggingFaceOAuth:
    """Simulate HF OAuth rotation, including rotating refresh credentials."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error: HuggingFaceOAuthError | None = None
        self.access_token = "oauth_access_refreshed"
        self.next_refresh_token = "oauth_refresh_rotated"
        self.delay = 0.05
        self._lock = threading.Lock()

    def refresh(
        self,
        *,
        client_id: str,
        refresh_token: str,
        now: int,
    ) -> HuggingFaceOAuthToken:
        with self._lock:
            self.calls.append((client_id, refresh_token))
        if self.error is not None:
            raise self.error
        if self.delay:
            time.sleep(self.delay)
        return HuggingFaceOAuthToken(
            self.access_token,
            self.next_refresh_token,
            now + 3600,
            now + 7200,
            None,
        )


class FakeHuggingFaceDeviceOAuth(FakeHuggingFaceOAuth):
    """Drive owner-initiated HF device renewal without any external request."""

    def __init__(self) -> None:
        super().__init__()
        self.start_calls: list[tuple[str, tuple[str, ...]]] = []
        self.poll_calls: list[tuple[str, str]] = []
        self.start_error: HuggingFaceOAuthError | None = None
        self.poll_error: HuggingFaceOAuthError | None = None
        self.poll_started: threading.Event | None = None
        self.poll_release: threading.Event | None = None
        self.device_token = HuggingFaceOAuthToken(
            "oauth_access_device_renewed",
            "oauth_refresh_device_rotated",
            13_600,
            17_200,
            ("openid", "profile", "read-repos"),
        )

    def begin_device_code(
        self,
        *,
        client_id: str,
        oauth_scopes: tuple[str, ...],
    ) -> HuggingFaceDeviceCode:
        self.start_calls.append((client_id, oauth_scopes))
        if self.start_error is not None:
            raise self.start_error
        return HuggingFaceDeviceCode(
            "private-device-code",
            "ABCD-EFGH",
            "https://hf.co/oauth/device",
            600,
            5,
        )

    def poll_device_code(
        self,
        *,
        client_id: str,
        device_code: str,
        now: int,
    ) -> HuggingFaceOAuthToken:
        self.poll_calls.append((client_id, device_code))
        if self.poll_started is not None:
            self.poll_started.set()
        if self.poll_release is not None and not self.poll_release.wait(timeout=2):
            raise RuntimeError("test device poll was not released")
        if self.poll_error is not None:
            raise self.poll_error
        return self.device_token


class FakeMicrosoftEntraOAuth:
    """Check PKCE and OIDC inputs while returning synthetic resource tokens."""

    client_id = "synthetic-entra-client"

    def __init__(self, now: Any) -> None:
        self.now = now
        self.authorizations: list[dict[str, Any]] = []
        self._authorization_cursor = 0
        self.refresh_calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.identities: dict[str, MicrosoftGraphIdentity] = {}
        self.refresh_identity = MicrosoftGraphIdentity(GRAPH_TENANT_ID, GRAPH_OBJECT_ID)
        self.access_ttl = 3600
        self.refresh_delay = 0.0
        self._lock = threading.Lock()
        self._counter = 0

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
        values = {
            "client_id": self.client_id,
            "response_type": "code",
            "response_mode": "query",
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": " ".join(scopes),
        }
        if prompt is not None:
            values["prompt"] = prompt
        self.authorizations.append(values)
        return "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?" + urlencode(
            values
        )

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        nonce_digest: str,
        requested_scopes: tuple[str, ...],
    ) -> tuple[MicrosoftGraphToken, MicrosoftGraphIdentity]:
        if self._authorization_cursor >= len(self.authorizations):
            raise MicrosoftGraphOAuthError("link_session_invalid")
        authorization = self.authorizations[self._authorization_cursor]
        self._authorization_cursor += 1
        if (
            authorization["redirect_uri"] != redirect_uri
            or not hmac.compare_digest(
                hashlib.sha256(authorization["nonce"].encode()).hexdigest(),
                nonce_digest,
            )
            or pkce_s256_challenge(code_verifier) != authorization["code_challenge"]
            or tuple(authorization["scope"].split()) != requested_scopes
            or not code
        ):
            raise MicrosoftGraphOAuthError("identity_verification_failed")
        resource_scopes = tuple(
            scope
            for scope in requested_scopes
            if scope
            in {
                "User.Read",
                "Tasks.Read",
                "Tasks.ReadWrite",
                "Sites.Selected",
                "Calendars.ReadBasic",
                "Calendars.ReadWrite",
                "Mail.ReadBasic",
                "Mail.ReadWrite",
                "Mail.Send",
                AZURE_DEVOPS_RESOURCE_SCOPE,
            }
        )
        with self._lock:
            self._counter += 1
            number = self._counter
        access_token = f"entra-access-{number}"
        identity = MicrosoftGraphIdentity(GRAPH_TENANT_ID, GRAPH_OBJECT_ID)
        self.identities[access_token] = identity
        if AZURE_DEVOPS_RESOURCE_SCOPE not in resource_scopes:
            self.verify_identity(access_token, identity.tenant_id)
        return (
            MicrosoftGraphToken(
                access_token=access_token,
                refresh_token=f"entra-refresh-{number}",
                expires_at=int(self.now()) + self.access_ttl,
                refresh_expires_at=None,
                scopes=tuple(sorted(resource_scopes)),
            ),
            identity,
        )

    def refresh(
        self,
        *,
        tenant_id: str,
        refresh_token: str,
        requested_scopes: tuple[str, ...],
    ) -> MicrosoftGraphToken:
        with self._lock:
            self.refresh_calls.append((tenant_id, refresh_token, requested_scopes))
            number = len(self.refresh_calls)
        if self.refresh_delay:
            time.sleep(self.refresh_delay)
        access_token = f"entra-refreshed-access-{number}"
        self.identities[access_token] = MicrosoftGraphIdentity(GRAPH_TENANT_ID, GRAPH_OBJECT_ID)
        return MicrosoftGraphToken(
            access_token=access_token,
            refresh_token=f"entra-rotated-refresh-{number}",
            expires_at=int(self.now()) + 3600,
            refresh_expires_at=None,
            scopes=tuple(sorted(requested_scopes)),
            identity=self.refresh_identity,
        )

    def verify_identity(self, access_token: str, tenant_id: str) -> MicrosoftGraphIdentity:
        identity = self.identities.get(access_token)
        if identity is None or identity.tenant_id != tenant_id:
            raise MicrosoftGraphOAuthError("credential_rejected")
        return identity


class ProfileHarness:
    """Build a production Account Center with deterministic provider clients."""

    def __init__(
        self,
        path: Path,
        *,
        oauth_seed: dict[str, Any] | None = None,
        workspace_operator_profile_refs: list[str] | None = None,
        oauth_client: FakeHuggingFaceOAuth | None = None,
        entra_oauth_client: FakeMicrosoftEntraOAuth | None = None,
        default_actor: Mapping[str, str | None] = ACTOR,
    ) -> None:
        self.now = 10_000
        self.sequence = 0
        self.default_actor = default_actor
        self.keys = {
            caller: (caller.encode("ascii") * 32)[:32]
            for caller in (EXCHANGE, GITHUB_PROVIDER, HF_PROVIDER)
        }
        self.github_app = FakeGitHubApp(lambda: self.now)
        self.huggingface = FakeHuggingFace()
        self.huggingface_oauth = oauth_client or FakeHuggingFaceOAuth()
        self.entra_oauth = entra_oauth_client or FakeMicrosoftEntraOAuth(lambda: self.now)
        provider_secrets = {
            "huggingface/fine-grained-token": "hf_token_original",
            "huggingface/rotated-token": "hf_token_rotated",
            "huggingface/wrong-identity-token": "hf_token_other",
        }
        if oauth_seed is not None:
            provider_secrets["huggingface/connector-token"] = json.dumps(oauth_seed)
        self.config = {
            "binding_id": "account-center-binding",
            "storage_path": str(path),
            "encryption_key": b"e" * 32,
            "exchange_caller_id": EXCHANGE,
            "github_provider_caller_id": GITHUB_PROVIDER,
            "huggingface_provider_caller_id": HF_PROVIDER,
            "caller_keys": self.keys,
            "provider_secrets": provider_secrets,
            "wecom_sources": {},
        }
        if entra_oauth_client is not None:
            self.keys.update(
                {
                    GRAPH_CALLBACK: b"g" * 32,
                    GRAPH_PROVIDER: b"p" * 32,
                    ADO_PROVIDER: b"a" * 32,
                }
            )
            self.config.update(
                {
                    "caller_keys": self.keys,
                    "microsoft_graph_callback_uri": GRAPH_LOOPBACK_CALLBACK,
                    "microsoft_graph_callback_caller_id": GRAPH_CALLBACK,
                    "microsoft_graph_client_id": self.entra_oauth.client_id,
                    "microsoft_graph_client_secret": "synthetic-client-secret",
                    "microsoft_graph_provider_caller_id": GRAPH_PROVIDER,
                    "azure_devops_provider_caller_id": ADO_PROVIDER,
                }
            )
        if workspace_operator_profile_refs is not None:
            self.config["workspace_operator_profile_refs"] = workspace_operator_profile_refs
        self.plugin = self._new_plugin()

    def _new_plugin(self) -> AccountCenterPlugin:
        return AccountCenterPlugin(
            self.config,
            github_app_client=self.github_app,
            huggingface_client=self.huggingface,
            huggingface_oauth_client=self.huggingface_oauth,
            microsoft_graph_oauth_client=(
                self.entra_oauth if "microsoft_graph_client_id" in self.config else None
            ),
            azure_devops_oauth_client=(
                self.entra_oauth if "microsoft_graph_client_id" in self.config else None
            ),
            now=lambda: self.now,
        )

    def call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        caller: str = EXCHANGE,
        actor: Mapping[str, str | None] | None = None,
        binding_id: str = "account-center-binding",
    ) -> dict[str, Any]:
        request = dict(payload)
        if method in {
            "link_github_app_installation",
            "link_huggingface_profile",
            "rotate_huggingface_profile",
            "register_host_identity_profile",
            "unlink_provider_profile",
            "begin_microsoft_graph_link",
            "begin_microsoft_graph_scope_upgrade",
            "update_microsoft_graph_v2_scope",
            "begin_azure_devops_link",
            "begin_bilibili_link",
            "complete_bilibili_link",
        }:
            request.setdefault("idempotency_key", f"idem-{method}-{self.sequence + 1}")
        self.sequence += 1
        identity = self.default_actor if actor is None else actor
        context = sign_context(
            method=method,
            payload=request,
            caller_id=caller,
            issuer=str(identity["issuer"]),
            subject=str(identity["subject"]),
            organization_id=str(identity["organization_id"]),
            workspace_id=identity["workspace_id"],
            binding_id=binding_id,
            request_id=f"00000000-0000-4000-8000-{self.sequence:012d}",
            operation_id=method,
            key=self.keys[caller],
            issued_at=self.now,
            expires_at=self.now + 120,
        )
        return self.plugin.call(method, request, context.as_dict())

    def invoke_runtime(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        caller: str = EXCHANGE,
        actor: Mapping[str, str | None] | None = None,
        binding_id: str = "account-center-binding",
    ) -> dict[str, Any]:
        request = dict(payload)
        self.sequence += 1
        identity = ACTOR if actor is None else actor
        context = sign_context(
            method=method,
            payload=request,
            caller_id=caller,
            issuer=str(identity["issuer"]),
            subject=str(identity["subject"]),
            organization_id=str(identity["organization_id"]),
            workspace_id=identity["workspace_id"],
            binding_id=binding_id,
            request_id=f"00000000-0000-4000-8000-{self.sequence:012d}",
            operation_id=method,
            key=self.keys[caller],
            issued_at=self.now,
            expires_at=self.now + 120,
        )
        envelope = json.dumps({"payload": request, "trusted_context": context.as_dict()}).encode(
            "utf-8"
        )
        succeeded, response = self.plugin.on_invoke(
            account_center_plugin_module.CAPABILITY_ID,
            method,
            envelope,
            request_type_url=(
                account_center_plugin_module.REQUEST_TYPE_PREFIX
                + account_center_plugin_module._action_type_name(method, "Request")
            ),
        )
        assert succeeded
        return json.loads(response.value)


def _digest(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(arguments)).hexdigest()


def _huggingface_scope(repo_id: str = "derek/oauth-model") -> dict[str, Any]:
    return {"repositories": [{"repo_type": "model", "repo_id": repo_id, "permissions": ["read"]}]}


def _oauth_seed(
    scope: dict[str, Any],
    *,
    access_expires_at: int = 10_020,
    refresh_expires_at: int | None = 20_000,
    oauth_scopes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "provider": "huggingface",
        "auth_mode": "device_oauth",
        "client_id": "first-party-public-client-id",
        "oauth_scopes": oauth_scopes,
        "access_token": "oauth_access_initial",
        "refresh_token": "oauth_refresh_initial",
        "access_token_expires_at": access_expires_at,
        "refresh_token_expires_at": refresh_expires_at,
        "target_scope": scope,
        "credential_secret_ref": "huggingface/connector-token",
    }


def _huggingface_oauth_lease(
    harness: ProfileHarness, profile_ref: str, scope: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    target = scope["repositories"][0]
    arguments = {
        "repo_type": target["repo_type"],
        "repo_id": target["repo_id"],
    }
    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": profile_ref,
            "operation_id": "huggingface.repo.get",
            "target": arguments,
            "arguments_sha256": _digest(arguments),
        },
    )
    redemption = {
        "lease_ref": lease["lease_ref"],
        "profile_ref": profile_ref,
        "operation_id": "huggingface.repo.get",
        "target": arguments,
        "arguments_sha256": _digest(arguments),
    }
    return lease, redemption


def _connector_grant(
    harness: ProfileHarness,
    *,
    actor: Mapping[str, str | None],
    profile_ref: str,
    operation_id: str,
    arguments_sha256: str,
    grant_ref: str,
) -> dict[str, Any]:
    return {
        "grant_ref": grant_ref,
        "issued_at": harness.now,
        "expires_at": harness.now + 15,
        "issuer": str(actor["issuer"]),
        "subject": str(actor["subject"]),
        "organization_id": str(actor["organization_id"]),
        "workspace_id": str(actor["workspace_id"]),
        "connection_ref": "00000000-0000-4000-8000-000000000901",
        "provider_id": "huggingface",
        "profile_ref": profile_ref,
        "operation_id": operation_id,
        "resource_ref": "sha256:scope-bound-resource",
        "arguments_sha256": arguments_sha256,
    }


def _github_profile(harness: ProfileHarness) -> str:
    result = harness.call(
        "link_github_app_installation",
        {
            "profile_ref": "00000000-0000-4000-8000-000000000101",
            "installation_id": "9001",
            "repositories": GITHUB_REPOSITORIES,
        },
    )
    assert result["auth_status"] == "healthy"
    return result["profile_ref"]


def test_github_app_profile_mints_refreshes_caches_and_scope_binds_leases(
    tmp_path: Path,
) -> None:
    harness = ProfileHarness(tmp_path / "accounts.sqlite3")
    profile_ref = _github_profile(harness)
    assert harness.github_app.verify_calls == ["9001"]
    assert harness.github_app.token_calls == [("9001", ["123"])]
    assert harness.github_app.repository_calls == [GITHUB_REPOSITORIES]

    operation = "github.repository.get"
    arguments = {**GITHUB_TARGET}
    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": profile_ref,
            "operation_id": operation,
            "target": GITHUB_TARGET,
            "arguments_sha256": _digest(arguments),
        },
    )
    redeem = {
        "lease_ref": lease["lease_ref"],
        "profile_ref": profile_ref,
        "operation_id": operation,
        "target": GITHUB_TARGET,
        "arguments_sha256": _digest(arguments),
    }
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call(
            "redeem_provider_credential_lease",
            {**redeem, "arguments_sha256": "0" * 64},
            caller=GITHUB_PROVIDER,
        )
    material = harness.call("redeem_provider_credential_lease", redeem, caller=GITHUB_PROVIDER)
    assert material["access_token"] == "installation-token-2"
    assert material["expires_at"] == harness.now + 3600
    assert len(harness.github_app.token_calls) == 2

    second_lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": profile_ref,
            "operation_id": operation,
            "target": GITHUB_TARGET,
            "arguments_sha256": _digest(arguments),
        },
    )
    second = harness.call(
        "redeem_provider_credential_lease",
        {**redeem, "lease_ref": second_lease["lease_ref"]},
        caller=GITHUB_PROVIDER,
    )
    assert second["access_token"] == material["access_token"]
    assert len(harness.github_app.token_calls) == 2

    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call("redeem_provider_credential_lease", redeem, caller=GITHUB_PROVIDER)

    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": profile_ref,
                "operation_id": operation,
                "target": {"repository_id": "124", "repository": "acme/other"},
                "arguments_sha256": _digest(arguments),
            },
        )


def test_github_profile_operation_catalog_is_closed_for_typed_tools(
    tmp_path: Path,
) -> None:
    harness = ProfileHarness(tmp_path / "accounts.sqlite3")
    profile_ref = _github_profile(harness)
    allowed = account_center_plugin_module.PROVIDER_OPERATIONS_BY_PROVIDER["github"]
    expected_typed = {case[0] for case in GITHUB_TYPED_TARGET_CASES}
    assert expected_typed.issubset(allowed)
    assert "github.commits.compare" in allowed
    assert "github.actions.run.get" not in allowed

    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": profile_ref,
                "operation_id": "github.actions.run.get",
                "target": {**GITHUB_TARGET, "run_id": "987654321"},
                "arguments_sha256": _digest({"run_id": "987654321"}),
            },
        )


@pytest.mark.parametrize(("operation", "target", "arguments"), GITHUB_TYPED_TARGET_CASES)
def test_typed_github_app_leases_bind_exact_locator_digest_and_one_use(
    tmp_path: Path,
    operation: str,
    target: dict[str, Any],
    arguments: dict[str, Any],
) -> None:
    harness = ProfileHarness(tmp_path / f"{operation.replace('.', '-')}.sqlite3")
    profile_ref = _github_profile(harness)
    allowed_operations = account_center_plugin_module.PROVIDER_OPERATIONS_BY_PROVIDER["github"]
    assert operation in allowed_operations

    with pytest.raises(AccountCenterError, match="invalid_request"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": profile_ref,
                "operation_id": operation,
                "target": {**target, "unexpected": "locator"},
                "arguments_sha256": _digest(arguments),
            },
        )
    missing_locator = dict(target)
    if operation != "github.issues.search":
        locator_fields = set(target) - set(GITHUB_TARGET)
        missing_locator.pop(next(iter(locator_fields)))
        with pytest.raises(AccountCenterError, match="invalid_request"):
            harness.call(
                "create_provider_credential_lease",
                {
                    "profile_ref": profile_ref,
                    "operation_id": operation,
                    "target": missing_locator,
                    "arguments_sha256": _digest(arguments),
                },
            )
    out_of_scope_target = {**target, "repository_id": "124", "repository": "acme/other"}
    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": profile_ref,
                "operation_id": operation,
                "target": out_of_scope_target,
                "arguments_sha256": _digest(arguments),
            },
        )
    invalid_locator = dict(target)
    if "path" in target:
        invalid_locator["path"] = "../secret.txt"
    elif "branch" in target:
        invalid_locator["branch"] = "cyrene/assistant/../main"
    elif "run_id" in target:
        invalid_locator["run_id"] = "0"
    elif "head_branch" in target:
        invalid_locator["head_branch"] = "../develop"
    elif operation == "github.commits.compare":
        invalid_locator["base_sha"] = "a" * 39
    else:
        invalid_locator = {}
    if invalid_locator:
        with pytest.raises(AccountCenterError, match="invalid_request"):
            harness.call(
                "create_provider_credential_lease",
                {
                    "profile_ref": profile_ref,
                    "operation_id": operation,
                    "target": invalid_locator,
                    "arguments_sha256": _digest(arguments),
                },
            )

    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": profile_ref,
            "operation_id": operation,
            "target": target,
            "arguments_sha256": _digest(arguments),
        },
    )
    if operation == "github.commits.compare":
        assert lease["target"]["base_sha"] == "a" * 40
    redemption = {
        "lease_ref": lease["lease_ref"],
        "profile_ref": profile_ref,
        "operation_id": operation,
        "target": target,
        "arguments_sha256": _digest(arguments),
    }
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call(
            "redeem_provider_credential_lease",
            {**redemption, "arguments_sha256": _digest({**arguments, "changed": True})},
            caller=GITHUB_PROVIDER,
        )

    material = harness.call("redeem_provider_credential_lease", redemption, caller=GITHUB_PROVIDER)
    assert material["access_token"].startswith("installation-token-")
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call("redeem_provider_credential_lease", redemption, caller=GITHUB_PROVIDER)


def test_github_vendor_cli_lease_uses_profile_scope_and_empty_target(
    tmp_path: Path,
) -> None:
    harness = ProfileHarness(tmp_path / "accounts.sqlite3")
    profile_ref = _github_profile(harness)
    operation = "github.vendor.cli.execute"
    arguments = {"argv": ["pr", "list", "--repo", "acme/project"]}
    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": profile_ref,
            "operation_id": operation,
            "target": {},
            "arguments_sha256": _digest(arguments),
        },
    )

    assert lease["target"] == {}
    material = harness.call(
        "redeem_provider_credential_lease",
        {
            "lease_ref": lease["lease_ref"],
            "profile_ref": profile_ref,
            "operation_id": operation,
            "target": {},
            "arguments_sha256": _digest(arguments),
        },
        caller=GITHUB_PROVIDER,
    )
    assert material["access_token"].startswith("installation-token-")
    assert "profile_scope_sha256" not in material

    with pytest.raises(AccountCenterError, match="invalid_request"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": profile_ref,
                "operation_id": operation,
                "target": {"repository": "acme/project"},
                "arguments_sha256": _digest(arguments),
            },
        )


def test_find_people_merges_profile_links_with_declared_handles_without_secrets(
    tmp_path: Path,
) -> None:
    harness = ProfileHarness(tmp_path / "accounts.sqlite3")
    _github_profile(harness)
    other_actor = {**ACTOR, "subject": "user-other"}
    elsewhere = {**ACTOR, "subject": "user-elsewhere", "workspace_id": "workspace-b"}
    harness.github_app.verify_installation = lambda installation_id: {  # type: ignore[method-assign]
        "account_id": "89",
        "login": "globex",
    }
    for index, actor in enumerate((other_actor, elsewhere), start=1):
        harness.call(
            "link_github_app_installation",
            {
                "profile_ref": f"00000000-0000-4000-8000-00000000020{index}",
                "installation_id": f"900{index + 1}",
                "repositories": GITHUB_REPOSITORIES,
            },
            actor=actor,
        )
        harness.github_app.verify_installation = lambda installation_id, n=index: {  # type: ignore[method-assign]
            "account_id": str(90 + n),
            "login": f"globex-{n}",
        }
    declared = harness.call(
        "declare_account_handle",
        {
            "provider": "huggingface",
            "handle": "other-hf",
            "subject": "user-other",
            "display_name": "Other Owner",
            "idempotency_key": "declare-other",
        },
    )

    listed = harness.call("find_people", {})

    # People sort by display name, then mail, then subject: "Other Owner" < "user-derek".
    assert [person["subject"] for person in listed["people"]] == ["user-other", "user-derek"]
    people = {person["subject"]: person for person in listed["people"]}
    assert people["user-derek"]["person_ref"] is None
    assert people["user-derek"]["accounts"] == [
        {
            "source": "verified",
            "kind": "profile",
            "provider": "github",
            "handle": None,
            "external_subject": "acme",
            "status": "healthy",
            "updated_at": 10_000,
        }
    ]
    assert people["user-other"]["person_ref"] == declared["person_ref"]
    assert [
        (a["source"], a["kind"], a["provider"], a["handle"], a["external_subject"])
        for a in people["user-other"]["accounts"]
    ] == [
        ("declared", "handle", "huggingface", "other-hf", None),
        ("verified", "profile", "github", None, "globex"),
    ]
    rendered = json.dumps(listed)
    for forbidden in ("profile_ref", "installation", "token", "target_scope", "credential"):
        assert forbidden not in rendered
    with pytest.raises(AccountCenterError, match="untrusted_caller"):
        harness.call("find_people", {}, caller=GITHUB_PROVIDER)
    harness.call(
        "unlink_provider_profile",
        {"profile_ref": "00000000-0000-4000-8000-000000000101"},
    )
    assert [p["subject"] for p in harness.call("find_people", {})["people"]] == ["user-other"]


def test_revoke_person_links_dissolves_owned_profiles_and_skips_shared_operator_profiles(
    tmp_path: Path,
) -> None:
    shared_ref = "00000000-0000-4000-8000-000000000236"
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3", workspace_operator_profile_refs=[shared_ref]
    )
    _github_profile(harness)
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": shared_ref,
            "credential_secret_ref": "huggingface/fine-grained-token",
            "scopes": _huggingface_scope("derek/shared-model"),
        },
    )
    admin = {**ACTOR, "subject": "admin-1"}

    github = harness.call(
        "revoke_person_links",
        {"subject": "user-derek", "provider": "github", "idempotency_key": "rev-gh"},
        actor=admin,
    )
    assert github == {
        "subject": "user-derek",
        "provider": "github",
        "unlinked": 1,
        "skipped_shared": 0,
        "results": [{"kind": "profile", "status": "unlinked"}],
    }

    # A profile shared by the whole workspace is never dissolved on a person's behalf.
    shared = harness.call(
        "revoke_person_links",
        {"subject": "user-derek", "provider": "huggingface", "idempotency_key": "rev-hf"},
        actor=admin,
    )
    assert (shared["unlinked"], shared["skipped_shared"], shared["results"]) == (0, 1, [])

    people = harness.call("find_people", {})["people"]
    assert [(p["subject"], [a["provider"] for a in p["accounts"]]) for p in people] == [
        ("user-derek", ["huggingface"])
    ]


def test_github_installation_cannot_be_claimed_by_a_second_owner(
    tmp_path: Path,
) -> None:
    harness = ProfileHarness(tmp_path / "accounts.sqlite3")
    _github_profile(harness)
    other_actor = {
        "issuer": ACTOR["issuer"],
        "subject": "other-owner",
        "organization_id": ACTOR["organization_id"],
        "workspace_id": ACTOR["workspace_id"],
    }
    with pytest.raises(AccountCenterError, match="external_identity_owned"):
        harness.call(
            "link_github_app_installation",
            {
                "profile_ref": "00000000-0000-4000-8000-000000000111",
                "installation_id": "9001",
                "repositories": GITHUB_REPOSITORIES,
            },
            actor=other_actor,
        )


def test_uncertain_installation_token_mint_requires_relink_without_auto_retry(
    tmp_path: Path,
) -> None:
    harness = ProfileHarness(tmp_path / "accounts.sqlite3")
    profile_ref = _github_profile(harness)
    harness.now += 20
    harness.github_app.token_error = GitHubAppError("credential_refresh_unknown")
    operation = "github.repository.get"
    arguments = dict(GITHUB_TARGET)
    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": profile_ref,
            "operation_id": operation,
            "target": GITHUB_TARGET,
            "arguments_sha256": _digest(arguments),
        },
    )
    redemption = {
        "lease_ref": lease["lease_ref"],
        "profile_ref": profile_ref,
        "operation_id": operation,
        "target": GITHUB_TARGET,
        "arguments_sha256": _digest(arguments),
    }
    with pytest.raises(AccountCenterError, match="credential_refresh_unknown"):
        harness.call(
            "redeem_provider_credential_lease",
            redemption,
            caller=GITHUB_PROVIDER,
        )
    assert len(harness.github_app.token_calls) == 2
    profile = harness.call("list_provider_profiles", {})["profiles"][0]
    assert profile["auth_status"] == "reauthorization_required"
    with pytest.raises(AccountCenterError, match="profile_unavailable"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": profile_ref,
                "operation_id": operation,
                "target": GITHUB_TARGET,
                "arguments_sha256": _digest(arguments),
            },
        )
    assert len(harness.github_app.token_calls) == 2


def test_huggingface_link_rotation_is_host_secret_only_and_identity_pinned(
    tmp_path: Path,
) -> None:
    harness = ProfileHarness(tmp_path / "accounts.sqlite3")
    profile_ref = "00000000-0000-4000-8000-000000000202"
    scope = {
        "repositories": [
            {
                "repo_type": "model",
                "repo_id": "derek/model-a",
                "permissions": ["read", "write"],
            }
        ]
    }
    link_payload = {
        "profile_ref": profile_ref,
        "credential_secret_ref": "huggingface/fine-grained-token",
        "scopes": scope,
    }
    linked = harness.call("link_huggingface_profile", link_payload)
    assert linked["external_subject"] == "derek-li"
    assert "hf_token_original" not in str(link_payload)

    listed = harness.call("list_provider_profiles", {})
    assert listed["profiles"][0]["profile_ref"] == profile_ref
    assert "credential_secret_ref" not in listed["profiles"][0]
    assert "credential_blob" not in listed["profiles"][0]
    assert listed["profiles"][0]["credential_revision"] == 0

    with pytest.raises(AccountCenterError, match="external_identity_mismatch"):
        harness.call(
            "rotate_huggingface_profile",
            {
                "profile_ref": profile_ref,
                "credential_secret_ref": "huggingface/wrong-identity-token",
            },
        )

    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": profile_ref,
                "operation_id": "huggingface.file.upload",
                "target": {
                    "repo_type": "model",
                    "repo_id": "derek/other-model",
                    "path": "weights.bin",
                },
                "arguments_sha256": _digest({"path": "weights.bin"}),
            },
        )

    rotation = harness.call(
        "rotate_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/rotated-token",
        },
    )
    assert rotation["auth_status"] == "healthy"
    arguments = {"repo_type": "model", "repo_id": "derek/model-a", "path": "README.md"}
    target = {"repo_type": "model", "repo_id": "derek/model-a", "path": "README.md"}
    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": profile_ref,
            "operation_id": "huggingface.file.download",
            "target": target,
            "arguments_sha256": _digest(arguments),
        },
    )
    material = harness.call(
        "redeem_provider_credential_lease",
        {
            "lease_ref": lease["lease_ref"],
            "profile_ref": profile_ref,
            "operation_id": "huggingface.file.download",
            "target": target,
            "arguments_sha256": _digest(arguments),
        },
        caller=HF_PROVIDER,
    )
    assert material["access_token"] == "hf_token_rotated"
    assert material["external_subject"] == "derek-li"
    assert "profile_scope_sha256" not in material


def test_huggingface_device_oauth_refresh_persists_rotation_and_survives_restart(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000232"
    scope = _huggingface_scope()
    oauth = FakeHuggingFaceOAuth()
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(scope, oauth_scopes=None),
        oauth_client=oauth,
    )
    linked = harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/connector-token",
            "auth_mode": "device_oauth",
            "scopes": scope,
        },
    )
    assert linked["auth_mode"] == "device_oauth"
    assert linked["access_mode"] == "actor_owned"
    assert linked["expires_at"] == 10_020
    assert "oauth_access_initial" not in json.dumps(linked)
    listed = harness.call("list_provider_profiles", {})["profiles"][0]
    assert listed["oauth_scopes"] is None
    assert listed["auth_mode"] == "device_oauth"
    initial_revision = listed["credential_revision"]

    _, redemption = _huggingface_oauth_lease(harness, profile_ref, scope)
    result = harness.call("redeem_provider_credential_lease", redemption, caller=HF_PROVIDER)
    assert result["access_token"] == "oauth_access_refreshed"
    assert oauth.calls == [("first-party-public-client-id", "oauth_refresh_initial")]
    row = harness.plugin._store.get_provider_profile(profile_ref, Anchor(**ACTOR))
    stored_access, stored_expiry = harness.plugin._decrypt_provider_token(
        profile_ref, bytes(row["credential_blob"])
    )
    stored_refresh = harness.plugin._decrypt_provider_refresh_token(
        profile_ref, bytes(row["credential_refresh_blob"])
    )
    assert stored_access == result["access_token"]
    assert stored_expiry == result["expires_at"]
    assert stored_refresh == "oauth_refresh_rotated"
    assert b"oauth_refresh_rotated" not in bytes(row["credential_refresh_blob"])
    assert row["credential_refresh_expires_at"] == 17_200
    assert (
        harness.call("list_provider_profiles", {})["profiles"][0]["credential_revision"]
        > initial_revision
    )

    harness.plugin = harness._new_plugin()
    _, resumed_redemption = _huggingface_oauth_lease(harness, profile_ref, scope)
    resumed = harness.call(
        "redeem_provider_credential_lease",
        resumed_redemption,
        caller=HF_PROVIDER,
    )
    assert resumed["access_token"] == "oauth_access_refreshed"
    assert len(oauth.calls) == 1


def test_huggingface_oauth_refresh_is_serialized_across_account_center_instances(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000233"
    scope = _huggingface_scope("derek/concurrent-model")
    oauth = FakeHuggingFaceOAuth()
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(scope, access_expires_at=10_001),
        oauth_client=oauth,
    )
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/connector-token",
            "auth_mode": "device_oauth",
            "scopes": scope,
        },
    )
    second_plugin = harness._new_plugin()
    profile = harness.plugin._store.get_provider_profile(profile_ref, Anchor(**ACTOR))
    rendezvous = threading.Barrier(2)

    def refresh(plugin: AccountCenterPlugin) -> tuple[str, int | None]:
        rendezvous.wait(timeout=2)
        return plugin._fresh_provider_credential(profile, Anchor(**ACTOR))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(refresh, (harness.plugin, second_plugin)))
    assert results == [("oauth_access_refreshed", 13_600)] * 2
    assert len(oauth.calls) == 1
    final = harness.plugin._store.get_provider_profile(profile_ref, Anchor(**ACTOR))
    assert (
        harness.plugin._decrypt_provider_refresh_token(
            profile_ref, bytes(final["credential_refresh_blob"])
        )
        == "oauth_refresh_rotated"
    )


def test_huggingface_oauth_revocation_marks_profile_for_reauthorization(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000234"
    scope = _huggingface_scope("derek/revoked-model")
    oauth = FakeHuggingFaceOAuth()
    oauth.error = HuggingFaceOAuthError("credential_rejected")
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(scope, access_expires_at=10_001),
        oauth_client=oauth,
    )
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/connector-token",
            "auth_mode": "device_oauth",
            "scopes": scope,
        },
    )
    _, redemption = _huggingface_oauth_lease(harness, profile_ref, scope)
    with pytest.raises(AccountCenterError, match="credential_rejected"):
        harness.call("redeem_provider_credential_lease", redemption, caller=HF_PROVIDER)
    listed = harness.call("list_provider_profiles", {})["profiles"][0]
    assert listed["auth_status"] == "reauthorization_required"
    stored = harness.plugin._store.get_provider_profile(profile_ref, Anchor(**ACTOR))
    assert stored["credential_blob"] is None
    assert stored["credential_refresh_blob"] is None
    assert len(oauth.calls) == 1


def test_huggingface_oauth_refresh_wrong_subject_clears_rotated_material(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000235"
    scope = _huggingface_scope("derek/wrong-subject-model")
    oauth = FakeHuggingFaceOAuth()
    oauth.access_token = "oauth_access_wrong"
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(scope, access_expires_at=10_001),
        oauth_client=oauth,
    )
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/connector-token",
            "auth_mode": "device_oauth",
            "scopes": scope,
        },
    )
    _, redemption = _huggingface_oauth_lease(harness, profile_ref, scope)
    with pytest.raises(AccountCenterError, match="external_identity_mismatch"):
        harness.call("redeem_provider_credential_lease", redemption, caller=HF_PROVIDER)
    listed = harness.call("list_provider_profiles", {})["profiles"][0]
    assert listed["auth_status"] == "reauthorization_required"
    stored = harness.plugin._store.get_provider_profile(profile_ref, Anchor(**ACTOR))
    assert stored["credential_blob"] is None
    assert stored["credential_refresh_blob"] is None


def test_huggingface_oauth_seed_scope_must_match_host_selected_repositories(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000239"
    selected = _huggingface_scope("derek/selected-model")
    unselected = _huggingface_scope("derek/other-model")
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(selected),
    )
    with pytest.raises(AccountCenterError, match="authorization_scope_mismatch"):
        harness.call(
            "link_huggingface_profile",
            {
                "profile_ref": profile_ref,
                "credential_secret_ref": "huggingface/connector-token",
                "auth_mode": "device_oauth",
                "scopes": unselected,
            },
        )
    assert harness.call("list_provider_profiles", {})["profiles"] == []


def test_workspace_operator_profile_requires_fresh_one_use_exchange_grant(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000236"
    scope = _huggingface_scope("derek/shared-model")
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        workspace_operator_profile_refs=[profile_ref],
    )
    with pytest.raises(AccountCenterError, match="invalid_request"):
        harness.call(
            "link_huggingface_profile",
            {
                "profile_ref": "00000000-0000-4000-8000-000000000240",
                "credential_secret_ref": "huggingface/fine-grained-token",
                "scopes": scope,
                "access_mode": "workspace_operator",
            },
        )
    linked = harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/fine-grained-token",
            "scopes": scope,
        },
    )
    assert linked["access_mode"] == "workspace_operator"
    operator = {
        "issuer": ACTOR["issuer"],
        "subject": "maintainer-2",
        "organization_id": ACTOR["organization_id"],
        "workspace_id": ACTOR["workspace_id"],
    }
    arguments = {"repo_type": "model", "repo_id": "derek/shared-model"}
    digest = _digest(arguments)
    request = {
        "profile_ref": profile_ref,
        "operation_id": "huggingface.repo.get",
        "target": arguments,
        "arguments_sha256": digest,
    }
    with pytest.raises(AccountCenterError, match="connector_grant_required"):
        harness.call("create_provider_credential_lease", request, actor=operator)
    outsider = {
        **operator,
        "subject": "different-workspace-user",
        "workspace_id": "workspace-b",
    }
    with pytest.raises(AccountCenterError, match="profile_not_found"):
        harness.call("create_provider_credential_lease", request, actor=outsider)

    grant = _connector_grant(
        harness,
        actor=operator,
        profile_ref=profile_ref,
        operation_id="huggingface.repo.get",
        arguments_sha256=digest,
        grant_ref="sha256:" + "2" * 64,
    )
    authorized = {**request, "connector_grant": grant}
    lease = harness.invoke_runtime("create_provider_credential_lease", authorized, actor=operator)
    assert lease["expires_at"] == grant["expires_at"]
    harness.plugin._store.prune_expired(now=harness.now + 5)
    with harness.plugin._store._connect() as db:
        row = db.execute(
            "SELECT anchor_subject,connection_ref FROM provider_credential_leases "
            "WHERE lease_ref=?",
            (lease["lease_ref"],),
        ).fetchone()
        assert row["anchor_subject"] == "maintainer-2"
        assert row["connection_ref"] == grant["connection_ref"]
        consumed = db.execute(
            "SELECT anchor_subject FROM consumed_connector_grants WHERE grant_ref=?",
            (grant["grant_ref"],),
        ).fetchone()
        assert consumed["anchor_subject"] == "maintainer-2"
        assert (
            db.execute(
                "SELECT 1 FROM consumed_connector_grants WHERE grant_ref=?",
                (grant["grant_ref"],),
            ).fetchone()
            is not None
        )
        assert (
            db.execute(
                "SELECT 1 FROM provider_credential_leases WHERE lease_ref=?",
                (lease["lease_ref"],),
            ).fetchone()
            is not None
        )
    with pytest.raises(AccountCenterError, match="connector_grant_replayed"):
        harness.call("create_provider_credential_lease", authorized, actor=operator)
    result = harness.call(
        "redeem_provider_credential_lease",
        {
            "lease_ref": lease["lease_ref"],
            "profile_ref": profile_ref,
            "operation_id": "huggingface.repo.get",
            "target": arguments,
            "arguments_sha256": digest,
        },
        caller=HF_PROVIDER,
        actor=operator,
    )
    assert result["access_token"] == "hf_token_original"
    harness.now += 16
    fresh_grant = _connector_grant(
        harness,
        actor=operator,
        profile_ref=profile_ref,
        operation_id="huggingface.repo.get",
        arguments_sha256=digest,
        grant_ref="sha256:" + "4" * 64,
    )
    fresh_lease = harness.call(
        "create_provider_credential_lease",
        {**request, "connector_grant": fresh_grant},
        actor=operator,
    )
    with harness.plugin._store._connect() as db:
        assert (
            db.execute(
                "SELECT 1 FROM consumed_connector_grants WHERE grant_ref=?",
                (grant["grant_ref"],),
            ).fetchone()
            is None
        )
        assert (
            db.execute(
                "SELECT 1 FROM provider_credential_leases WHERE lease_ref=?",
                (lease["lease_ref"],),
            ).fetchone()
            is None
        )
        assert (
            db.execute(
                "SELECT 1 FROM consumed_connector_grants WHERE grant_ref=?",
                (fresh_grant["grant_ref"],),
            ).fetchone()
            is not None
        )
        assert (
            db.execute(
                "SELECT 1 FROM provider_credential_leases WHERE lease_ref=?",
                (fresh_lease["lease_ref"],),
            ).fetchone()
            is not None
        )
    with pytest.raises(AccountCenterError, match="connector_grant_required"):
        harness.call("create_provider_credential_lease", authorized, actor=operator)


def test_workspace_operator_grant_rejects_actor_target_and_age_mismatches(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000237"
    scope = _huggingface_scope("derek/guarded-model")
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        workspace_operator_profile_refs=[profile_ref],
    )
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/fine-grained-token",
            "scopes": scope,
        },
    )
    operator = {
        "issuer": ACTOR["issuer"],
        "subject": "maintainer-3",
        "organization_id": ACTOR["organization_id"],
        "workspace_id": ACTOR["workspace_id"],
    }
    arguments = {"repo_type": "model", "repo_id": "derek/guarded-model"}
    digest = _digest(arguments)
    request = {
        "profile_ref": profile_ref,
        "operation_id": "huggingface.repo.get",
        "target": arguments,
        "arguments_sha256": digest,
    }
    proof = _connector_grant(
        harness,
        actor=operator,
        profile_ref=profile_ref,
        operation_id="huggingface.repo.get",
        arguments_sha256=digest,
        grant_ref="sha256:" + "3" * 64,
    )
    altered_actor = {**proof, "subject": "somebody-else"}
    with pytest.raises(AccountCenterError, match="connector_grant_required"):
        harness.call(
            "create_provider_credential_lease",
            {**request, "connector_grant": altered_actor},
            actor=operator,
        )
    stale = {**proof, "issued_at": harness.now - 16}
    with pytest.raises(AccountCenterError, match="connector_grant_required"):
        harness.call(
            "create_provider_credential_lease",
            {**request, "connector_grant": stale},
            actor=operator,
        )
    mismatch = {**proof, "arguments_sha256": "f" * 64}
    with pytest.raises(AccountCenterError, match="connector_grant_required"):
        harness.call(
            "create_provider_credential_lease",
            {**request, "connector_grant": mismatch},
            actor=operator,
        )


def test_platform_rust_wire_grant_ref_survives_exchange_signed_runtime_envelope(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000241"
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        workspace_operator_profile_refs=[profile_ref],
    )
    linked = harness.call(
        "link_github_app_installation",
        {
            "profile_ref": profile_ref,
            "installation_id": "9001",
            "repositories": GITHUB_REPOSITORIES,
        },
    )
    assert linked["access_mode"] == "workspace_operator"
    operation = "github.repository.list"
    arguments_sha256 = _digest({})
    wire_grant_ref = "sha256:1d7617748443c2d6dab3bb08835c49659a875aeac010314dddc24308aa3d60f1"
    proof = {
        "grant_ref": wire_grant_ref,
        "issued_at": harness.now,
        "expires_at": harness.now + 15,
        "issuer": ACTOR["issuer"],
        "subject": ACTOR["subject"],
        "organization_id": ACTOR["organization_id"],
        "workspace_id": ACTOR["workspace_id"],
        "connection_ref": "00000000-0000-4000-8000-000000000901",
        "provider_id": "github",
        "profile_ref": profile_ref,
        "operation_id": operation,
        "resource_ref": "repositories",
        "arguments_sha256": arguments_sha256,
    }
    lease = harness.invoke_runtime(
        "create_provider_credential_lease",
        {
            "profile_ref": profile_ref,
            "operation_id": operation,
            "target": {},
            "arguments_sha256": arguments_sha256,
            "connector_grant": proof,
        },
    )
    assert lease["expires_at"] == proof["expires_at"]
    with harness.plugin._store._connect() as db:
        consumed = db.execute(
            "SELECT grant_ref,connection_ref,provider_id FROM consumed_connector_grants "
            "WHERE grant_ref=?",
            (wire_grant_ref,),
        ).fetchone()
    assert consumed["grant_ref"] == wire_grant_ref
    assert consumed["connection_ref"] == proof["connection_ref"]
    assert consumed["provider_id"] == "github"
    with pytest.raises(AccountCenterError, match="connector_grant_required"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": profile_ref,
                "operation_id": operation,
                "target": {},
                "arguments_sha256": arguments_sha256,
                "connector_grant": {
                    **proof,
                    "grant_ref": "00000000-0000-4000-8000-000000000902",
                },
            },
        )


def test_huggingface_vendor_cli_lease_uses_profile_scope_and_empty_target(
    tmp_path: Path,
) -> None:
    harness = ProfileHarness(tmp_path / "accounts.sqlite3")
    profile_ref = "00000000-0000-4000-8000-000000000204"
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/fine-grained-token",
            "scopes": {
                "repositories": [
                    {
                        "repo_type": "space",
                        "repo_id": "derek/demo-space",
                        "permissions": ["read"],
                    },
                    {
                        "repo_type": "model",
                        "repo_id": "derek/model-a",
                        "permissions": ["read", "write"],
                    },
                ]
            },
        },
    )
    operation = "huggingface.vendor.cli.execute"
    arguments = {"argv": ["models", "info", "derek/model-a"]}
    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": profile_ref,
            "operation_id": operation,
            "target": {},
            "arguments_sha256": _digest(arguments),
        },
    )

    assert lease["target"] == {}
    material = harness.call(
        "redeem_provider_credential_lease",
        {
            "lease_ref": lease["lease_ref"],
            "profile_ref": profile_ref,
            "operation_id": operation,
            "target": {},
            "arguments_sha256": _digest(arguments),
        },
        caller=HF_PROVIDER,
    )
    assert material["access_token"] == "hf_token_original"
    assert material["profile_scope_sha256"] == _digest(
        {
            "repositories": [
                {
                    "repo_type": "model",
                    "repo_id": "derek/model-a",
                    "permissions": ["read", "write"],
                },
                {
                    "repo_type": "space",
                    "repo_id": "derek/demo-space",
                    "permissions": ["read"],
                },
            ]
        }
    )

    with pytest.raises(AccountCenterError, match="invalid_request"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": profile_ref,
                "operation_id": operation,
                "target": {"repo_type": "model"},
                "arguments_sha256": _digest(arguments),
            },
        )


def test_cloud_profile_registry_stores_only_host_identity_metadata(
    tmp_path: Path,
) -> None:
    harness = ProfileHarness(tmp_path / "accounts.sqlite3")
    result = harness.call(
        "register_host_identity_profile",
        {
            "profile_ref": "00000000-0000-4000-8000-000000000303",
            "provider": "azure",
            "identity_ref": "managed-identity://navigator",
            "target_scope": {
                "subscription_id": "sub-1",
                "resource_groups": ["rg-cyrene"],
            },
        },
    )
    assert result["auth_status"] == "unknown"
    listed = harness.call("list_provider_profiles", {})["profiles"]
    assert listed[0]["provider"] == "azure"
    assert "credential_blob" not in listed[0]


def test_provider_callers_and_hmac_keys_must_be_distinct(tmp_path: Path) -> None:
    harness = ProfileHarness(tmp_path / "accounts.sqlite3")
    shared_key_config = dict(harness.config)
    shared_key_config["caller_keys"] = dict(harness.keys)
    shared_key_config["caller_keys"][HF_PROVIDER] = harness.keys[EXCHANGE]
    with pytest.raises(AccountCenterError, match="configuration_unavailable"):
        AccountCenterPlugin(
            shared_key_config,
            github_app_client=harness.github_app,
            huggingface_client=harness.huggingface,
            now=lambda: harness.now,
        )

    shared_identity_config = dict(harness.config)
    shared_identity_config["huggingface_provider_caller_id"] = EXCHANGE
    with pytest.raises(AccountCenterError, match="configuration_unavailable"):
        AccountCenterPlugin(
            shared_identity_config,
            github_app_client=harness.github_app,
            huggingface_client=harness.huggingface,
            now=lambda: harness.now,
        )


def test_app_only_runtime_config_omits_legacy_personal_oauth_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir(mode=0o700)
    os.chmod(storage, 0o700)
    environment_names = {
        "account-center/encryption-key": "TEST_ENCRYPTION_KEY",
        "account-center/caller-hmac-keys/exchange": "TEST_EXCHANGE_KEY",
        "account-center/caller-hmac-keys/github-provider": "TEST_GITHUB_PROVIDER_KEY",
        "account-center/caller-hmac-keys/huggingface-provider": "TEST_HF_PROVIDER_KEY",
        "github-app/private-key": "TEST_GITHUB_APP_PRIVATE_KEY",
        "huggingface/fine-grained-token": "TEST_HF_TOKEN",
    }
    for reference, environment_name in environment_names.items():
        secret = "host-secret-value"
        if reference == "account-center/encryption-key":
            secret = (b"e" * 32).hex()
        elif reference.endswith(("/exchange", "/github-provider", "/huggingface-provider")):
            secret = (reference.encode("ascii") * 32)[:32].hex()
        monkeypatch.setenv(environment_name, secret)
    config = {
        "encryption_key_ref": "account-center/encryption-key",
        "caller_key_refs": {
            EXCHANGE: "account-center/caller-hmac-keys/exchange",
            GITHUB_PROVIDER: "account-center/caller-hmac-keys/github-provider",
            HF_PROVIDER: "account-center/caller-hmac-keys/huggingface-provider",
        },
        "hostSecretRefs": environment_names,
        "exchange_caller_id": EXCHANGE,
        "github_provider_caller_id": GITHUB_PROVIDER,
        "huggingface_provider_caller_id": HF_PROVIDER,
        "github_app_id": "12345",
        "github_app_private_key_ref": "github-app/private-key",
        "provider_secret_refs": ["huggingface/fine-grained-token"],
        "method_callers": {},
    }
    monkeypatch.setenv("CYRENE_PLUGIN_STORAGE_PATH", str(storage))
    monkeypatch.setattr(
        account_center_plugin_module,
        "read_environment_settings",
        lambda: {"config": json.dumps(config), "binding_id": "account-center-binding"},
    )

    resolved = account_center_plugin_module._environment_config()

    assert "github_client_secret" not in resolved
    assert "github_callback_uri" not in resolved
    assert resolved["github_app_id"] == "12345"
    assert resolved["provider_secrets"] == {"huggingface/fine-grained-token": "host-secret-value"}


def test_device_oauth_runtime_config_reads_only_launcher_mounted_secret_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir(mode=0o700)
    os.chmod(storage, 0o700)
    environment_names = {
        "account-center/encryption-key": "TEST_ENCRYPTION_KEY",
        "account-center/caller-hmac-keys/exchange": "TEST_EXCHANGE_KEY",
        "account-center/caller-hmac-keys/github-provider": "TEST_GITHUB_PROVIDER_KEY",
        "account-center/caller-hmac-keys/huggingface-provider": "TEST_HF_PROVIDER_KEY",
    }
    for reference, environment_name in environment_names.items():
        secret = (b"e" * 32).hex()
        if reference.endswith(("/exchange", "/github-provider", "/huggingface-provider")):
            secret = (reference.encode("ascii") * 32)[:32].hex()
        monkeypatch.setenv(environment_name, secret)
    oauth_seed_text = json.dumps(_oauth_seed(_huggingface_scope()))
    config = {
        "encryption_key_ref": "account-center/encryption-key",
        "caller_key_refs": {
            EXCHANGE: "account-center/caller-hmac-keys/exchange",
            GITHUB_PROVIDER: "account-center/caller-hmac-keys/github-provider",
            HF_PROVIDER: "account-center/caller-hmac-keys/huggingface-provider",
        },
        "hostSecretRefs": environment_names,
        "hostSecretFileRefs": {
            "huggingface/connector-token": "/run/secrets/huggingface-oauth.json"
        },
        "provider_secret_file_refs": ["huggingface/connector-token"],
        "exchange_caller_id": EXCHANGE,
        "github_provider_caller_id": GITHUB_PROVIDER,
        "huggingface_provider_caller_id": HF_PROVIDER,
        "method_callers": {},
    }
    monkeypatch.setenv("CYRENE_PLUGIN_STORAGE_PATH", str(storage))
    monkeypatch.setattr(
        account_center_plugin_module,
        "read_environment_settings",
        lambda: {"config": json.dumps(config), "binding_id": "account-center-binding"},
    )
    monkeypatch.setattr(
        account_center_plugin_module,
        "_read_private_secret_file",
        lambda path: (
            oauth_seed_text
            if path == "/run/secrets/huggingface-oauth.json"
            else pytest.fail("unexpected secret file path")
        ),
    )

    resolved = account_center_plugin_module._environment_config()

    assert resolved["provider_secrets"] == {"huggingface/connector-token": oauth_seed_text}
    assert "huggingface/connector-token" not in resolved["hostSecretRefs"]
    assert resolved["workspace_operator_profile_refs"] == []


def test_schema_v5_huggingface_credential_does_not_migrate_to_oauth(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000238"
    scope = _huggingface_scope("derek/legacy-model")
    database = tmp_path / "accounts.sqlite3"
    harness = ProfileHarness(database)
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/fine-grained-token",
            "scopes": scope,
        },
    )
    with sqlite3.connect(database) as db:
        for column in (
            "access_mode",
            "auth_mode",
            "credential_refresh_blob",
            "credential_refresh_expires_at",
            "oauth_client_id",
            "oauth_scopes_json",
            "refresh_claim_id",
            "refresh_claim_owner",
            "refresh_claimed_at",
            "credential_revision",
        ):
            db.execute(f"ALTER TABLE provider_profiles DROP COLUMN {column}")
        db.execute("ALTER TABLE provider_credential_leases DROP COLUMN connection_ref")
        db.execute("DROP TABLE consumed_connector_grants")
        db.execute("PRAGMA user_version=5")

    store = account_center_plugin_module.AccountStore(database)
    migrated = store.get_provider_profile(profile_ref, Anchor(**ACTOR))
    assert migrated["auth_mode"] == "fine_grained_token"
    assert migrated["access_mode"] == "actor_owned"
    assert migrated["credential_refresh_blob"] is None
    assert migrated["oauth_client_id"] is None
    plugin = AccountCenterPlugin(
        harness.config,
        store=store,
        github_app_client=harness.github_app,
        huggingface_client=harness.huggingface,
        huggingface_oauth_client=harness.huggingface_oauth,
        now=lambda: harness.now,
    )
    credential, _ = plugin._fresh_provider_credential(migrated, Anchor(**ACTOR))
    assert credential == "hf_token_original"
    assert harness.huggingface_oauth.calls == []


def test_manifest_keeps_disabled_legacy_and_provider_secrets_optional() -> None:
    """Do not require OAuth callback or WeCom keys for App/HF-only activation."""

    manifest_path = Path(__file__).resolve().parents[1] / "plugin.manifest.json"
    secrets = json.loads(manifest_path.read_text(encoding="utf-8"))["secrets"]
    required = set(secrets["requiredSecretRefs"])
    optional = set(secrets["optionalSecretRefs"])

    assert required == {
        "account-center/encryption-key",
        "account-center/caller-hmac-keys/exchange",
        "account-center/caller-hmac-keys/github-provider",
    }
    assert {
        "account-center/caller-hmac-keys/callback",
        "account-center/caller-hmac-keys/wecom",
        "account-center/caller-hmac-keys/huggingface-provider",
        "github-app/client-secret",
        "github-app/private-key",
        "huggingface/fine-grained-token",
        "huggingface/connector-token",
    } <= optional


def test_schema_v8_migration_preserves_provider_rows_with_foreign_key_children(
    tmp_path: Path,
) -> None:
    """Rebuild provider profiles atomically without losing referenced child rows."""

    db_path = tmp_path / "accounts-v8-with-references.sqlite"
    ProfileHarness(db_path)
    now = int(time.time())
    profile_ref = "00000000-0000-4000-8000-000000000901"
    lease_ref = "00000000-0000-4000-8000-000000000902"
    session_ref = "00000000-0000-4000-8000-000000000903"
    profile_values = (
        profile_ref,
        ACTOR["issuer"],
        ACTOR["subject"],
        ACTOR["organization_id"],
        ACTOR["workspace_id"],
        "huggingface",
        "hf-user-1",
        '{"repositories":["owner/model"]}',
        "healthy",
        "hf-user-1",
        b"access-ciphertext-v8",
        now + 3600,
        None,
        "active",
        now - 20,
        now - 10,
        "device_oauth",
        b"refresh-ciphertext-v8",
        now + 7200,
        "oauth-client-v8",
        '["read-repos"]',
        "refresh-claim-v8",
        "other-account-center-instance",
        now,
        7,
        "actor_owned",
    )
    lease_values = (
        lease_ref,
        profile_ref,
        ACTOR["issuer"],
        ACTOR["subject"],
        ACTOR["organization_id"],
        ACTOR["workspace_id"],
        "account-center-binding",
        HF_PROVIDER,
        "huggingface.repo.get",
        '{"repo_type":"model","repo_id":"owner/model"}',
        "a" * 64,
        now + 600,
        None,
        "provider-connection-v8",
    )
    renewal_values = (
        session_ref,
        profile_ref,
        ACTOR["issuer"],
        ACTOR["subject"],
        ACTOR["organization_id"],
        ACTOR["workspace_id"],
        "waiting_user",
        b"device-code-ciphertext-v8",
        "ABCD-EFGH",
        "https://hf.co/oauth/device",
        now + 600,
        5,
        now + 10,
        7,
        "poll-claim-v8",
        "other-account-center-instance",
        now,
        "poll-pending-v8",
        now - 20,
        now - 10,
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO provider_profiles(profile_ref,anchor_issuer,anchor_subject,"
            "organization_id,workspace_id,provider,identity_ref,target_scope_json,"
            "auth_status,external_subject,credential_blob,credential_expires_at,"
            "credential_secret_ref,status,created_at,updated_at,auth_mode,"
            "credential_refresh_blob,credential_refresh_expires_at,oauth_client_id,"
            "oauth_scopes_json,refresh_claim_id,refresh_claim_owner,refresh_claimed_at,"
            "credential_revision,access_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            profile_values,
        )
        db.execute(
            "INSERT INTO provider_credential_leases(lease_ref,profile_ref,anchor_issuer,"
            "anchor_subject,organization_id,workspace_id,binding_id,provider_caller_id,"
            "operation_id,target_json,arguments_sha256,expires_at,redeemed_at,connection_ref) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            lease_values,
        )
        db.execute(
            "INSERT INTO huggingface_device_renewal_sessions(session_ref,profile_ref,"
            "anchor_issuer,anchor_subject,organization_id,workspace_id,status,device_code_blob,"
            "user_code,verification_uri,expires_at,interval_seconds,next_poll_at,"
            "profile_revision,poll_claim_id,poll_claim_owner,poll_claimed_at,reason_code,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            renewal_values,
        )

    def snapshot() -> dict[str, list[tuple[Any, ...]]]:
        with sqlite3.connect(db_path) as db:
            tables = (
                "provider_profiles",
                "provider_credential_leases",
                "huggingface_device_renewal_sessions",
            )
            return {
                table: [
                    tuple(row)
                    for row in db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                ]
                for table in tables
            }

    before = snapshot()
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 8")

    store = account_center_plugin_module.AccountStore(db_path)

    assert snapshot() == before
    with store._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 12
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "credential_rejected"),
        (403, "provider_permission_denied"),
        (429, "provider_rate_limited"),
    ],
)
def test_huggingface_identity_failures_are_structured_and_redacted(
    status: int, expected: str
) -> None:
    def transport(method: str, host: str, path: str, headers: dict[str, str]) -> Any:
        assert method == "GET"
        assert host == "huggingface.co"
        assert path == "/api/whoami-v2"
        assert headers["Authorization"] == "Bearer hf_private_value"
        return type(
            "Response",
            (),
            {"status": status, "headers": {}, "body": b"private upstream body"},
        )()

    client = HuggingFaceIdentityClient(transport=transport)
    with pytest.raises(HuggingFaceAuthError, match=expected) as caught:
        client.whoami("hf_private_value")
    assert "private upstream body" not in str(caught.value)
    assert "hf_private_value" not in str(caught.value)


def test_huggingface_oauth_refresh_uses_fixed_endpoint_and_preserves_actual_scope() -> None:
    captured: dict[str, Any] = {}

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Any:
        captured.update(
            {
                "method": method,
                "host": host,
                "path": path,
                "headers": dict(headers),
                "body": body.decode("ascii"),
            }
        )
        return type(
            "Response",
            (),
            {
                "status": 200,
                "headers": {},
                "body": json.dumps(
                    {
                        "access_token": "oauth_access_refreshed",
                        "refresh_token": "oauth_refresh_rotated",
                        "expires_in": 3600,
                        "refresh_expires_in": 7200,
                        "scope": "openid profile read-repos",
                    }
                ).encode(),
            },
        )()

    token = HuggingFaceOAuthClient(transport=transport).refresh(
        client_id="first-party-client",
        refresh_token="oauth_refresh_initial",
        now=10_000,
    )
    assert captured["method"] == "POST"
    assert captured["host"] == "huggingface.co"
    assert captured["path"] == "/oauth/token"
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert "grant_type=refresh_token" in captured["body"]
    assert "refresh_token=oauth_refresh_initial" in captured["body"]
    assert token == HuggingFaceOAuthToken(
        "oauth_access_refreshed",
        "oauth_refresh_rotated",
        13_600,
        17_200,
        ("openid", "profile", "read-repos"),
    )


def test_huggingface_oauth_invalid_grant_is_sanitized() -> None:
    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Any:
        return type(
            "Response",
            (),
            {
                "status": 400,
                "headers": {},
                "body": json.dumps(
                    {
                        "error": "invalid_grant",
                        "error_description": "oauth_refresh_initial is revoked",
                    }
                ).encode(),
            },
        )()

    client = HuggingFaceOAuthClient(transport=transport)
    with pytest.raises(HuggingFaceOAuthError, match="credential_rejected") as caught:
        client.refresh(
            client_id="first-party-client",
            refresh_token="oauth_refresh_initial",
            now=10_000,
        )
    assert "oauth_refresh_initial" not in str(caught.value)
    assert "revoked" not in str(caught.value)


def test_huggingface_device_oauth_uses_official_fixed_endpoints_and_poll_grant() -> None:
    captured: list[dict[str, Any]] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Any:
        captured.append(
            {
                "method": method,
                "host": host,
                "path": path,
                "headers": dict(headers),
                "body": body.decode("ascii"),
            }
        )
        response = (
            {
                "device_code": "device-secret-code",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://hf.co/oauth/device",
                "expires_in": 600,
                "interval": 5,
            }
            if path == "/oauth/device"
            else {
                "access_token": "hf_access_device",
                "refresh_token": "hf_refresh_rotated",
                "expires_in": 3600,
                "refresh_expires_in": 7200,
                "scope": "openid profile read-repos",
            }
        )
        return type(
            "Response",
            (),
            {"status": 200, "headers": {}, "body": json.dumps(response).encode()},
        )()

    client = HuggingFaceOAuthClient(transport=transport)
    device = client.begin_device_code(
        client_id="first-party-client",
        oauth_scopes=("openid", "profile", "read-repos"),
    )
    token = client.poll_device_code(
        client_id="first-party-client",
        device_code=device.device_code,
        now=10_000,
    )

    assert device.user_code == "ABCD-EFGH"
    assert device.verification_uri == "https://hf.co/oauth/device"
    assert token.access_token == "hf_access_device"
    assert token.refresh_token == "hf_refresh_rotated"
    assert [call["path"] for call in captured] == ["/oauth/device", "/oauth/token"]
    assert all(call["method"] == "POST" for call in captured)
    assert all(call["host"] == "huggingface.co" for call in captured)
    assert "scope=openid+profile+read-repos" in captured[0]["body"]
    assert (
        "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code" in captured[1]["body"]
    )
    assert "device-secret-code" not in str(captured[0])


def test_huggingface_device_oauth_pending_error_is_sanitized() -> None:
    def transport(
        _method: str,
        _host: str,
        _path: str,
        _headers: Mapping[str, str],
        _body: bytes,
    ) -> Any:
        return type(
            "Response",
            (),
            {
                "status": 400,
                "headers": {},
                "body": json.dumps(
                    {
                        "error": "authorization_pending",
                        "error_description": "wait for user",
                    }
                ).encode(),
            },
        )()

    client = HuggingFaceOAuthClient(transport=transport)
    with pytest.raises(HuggingFaceOAuthError, match="authorization_pending") as caught:
        client.poll_device_code(
            client_id="first-party-client",
            device_code="private-device-code",
            now=10_000,
        )
    assert "private-device-code" not in str(caught.value)
    assert "wait for user" not in str(caught.value)


@pytest.mark.parametrize(
    "device_facts",
    [
        {
            "device_code": "private-device-code",
            "user_code": "L94P-HPL8",
            "verification_uri": "https://hf.co/oauth/device?next=https://evil.example",
            "expires_in": 600,
            "interval": 5,
        },
        {
            "device_code": "private-device-code",
            "user_code": "l94p-hpl8",
            "verification_uri": "https://hf.co/oauth/device",
            "expires_in": 600,
            "interval": 5,
        },
    ],
)
def test_huggingface_device_oauth_rejects_noncanonical_user_action_metadata(
    device_facts: dict[str, Any],
) -> None:
    def transport(
        _method: str,
        _host: str,
        _path: str,
        _headers: Mapping[str, str],
        _body: bytes,
    ) -> Any:
        return type(
            "Response",
            (),
            {"status": 200, "headers": {}, "body": json.dumps(device_facts).encode()},
        )()

    client = HuggingFaceOAuthClient(transport=transport)
    with pytest.raises(HuggingFaceOAuthError, match="invalid_provider_response"):
        client.begin_device_code(
            client_id="first-party-client",
            oauth_scopes=("openid", "profile", "read-repos"),
        )


def test_huggingface_oauth_seed_rejects_pending_or_unselected_material() -> None:
    seed = _oauth_seed(_huggingface_scope(), oauth_scopes=None)
    parsed = parse_device_oauth_seed(
        json.dumps(seed), expected_secret_ref="huggingface/connector-token"
    )
    assert parsed.oauth_scopes is None
    assert parsed.target_scope["repositories"]
    pending = {**seed, "target_scope": {"repositories": []}}
    with pytest.raises(HuggingFaceOAuthError, match="credential_unavailable"):
        parse_device_oauth_seed(
            json.dumps(pending), expected_secret_ref="huggingface/connector-token"
        )


def test_huggingface_device_renewal_is_owner_scoped_encrypted_and_rotates_revision(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000240"
    session_ref = "00000000-0000-4000-8000-000000000241"
    scope = _huggingface_scope("derek/renewal-model")
    oauth = FakeHuggingFaceDeviceOAuth()
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(
            scope,
            oauth_scopes=["openid", "profile", "read-repos"],
        ),
        oauth_client=oauth,
    )
    linked = harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/connector-token",
            "auth_mode": "device_oauth",
            "scopes": scope,
        },
    )
    initial = harness.call("list_provider_profiles", {})["profiles"][0]
    start_payload = {"profile_ref": profile_ref, "session_ref": session_ref}
    started = harness.call("begin_huggingface_renewal", start_payload)
    replayed_start = harness.call("begin_huggingface_renewal", start_payload)

    assert started["state"] == "waiting_user"
    assert started["user_code"] == "ABCD-EFGH"
    assert started["verification_uri"] == "https://hf.co/oauth/device"
    assert started["expires_at"] == harness.now + 600
    assert started["generation"] == initial["credential_revision"]
    assert replayed_start == started
    assert oauth.start_calls == [
        ("first-party-public-client-id", ("openid", "profile", "read-repos"))
    ]
    assert "private-device-code" not in json.dumps(started)
    assert "oauth_refresh_initial" not in json.dumps(started)
    with harness.plugin._store._connect() as db:
        session = db.execute(
            "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
            (session_ref,),
        ).fetchone()
    assert session is not None
    assert b"private-device-code" not in bytes(session["device_code_blob"])

    harness.plugin = harness._new_plugin()
    other_actor = {**ACTOR, "subject": "another-owner"}
    with pytest.raises(AccountCenterError, match="renewal_session_not_found"):
        harness.call(
            "poll_huggingface_renewal",
            start_payload,
            actor=other_actor,
        )
    harness.now += 5
    completed = harness.call("poll_huggingface_renewal", start_payload)
    replayed_poll = harness.call("poll_huggingface_renewal", start_payload)

    assert completed["state"] == "succeeded"
    assert completed["user_code"] is None
    assert completed["verification_uri"] is None
    assert completed["generation"] == initial["credential_revision"] + 1
    assert replayed_poll == completed
    assert oauth.poll_calls == [("first-party-public-client-id", "private-device-code")]
    assert "oauth_access_device_renewed" not in json.dumps(completed)
    profile = harness.plugin._store.get_provider_profile(profile_ref, Anchor(**ACTOR))
    assert json.loads(profile["target_scope_json"]) == scope
    assert profile["external_subject"] == linked["external_subject"] == "derek-li"
    assert profile["credential_revision"] == initial["credential_revision"] + 1
    assert profile["auth_status"] == "healthy"
    assert harness.plugin._decrypt_provider_token(
        profile_ref, bytes(profile["credential_blob"])
    ) == ("oauth_access_device_renewed", 13_600)
    assert (
        harness.plugin._decrypt_provider_refresh_token(
            profile_ref, bytes(profile["credential_refresh_blob"])
        )
        == "oauth_refresh_device_rotated"
    )


def test_huggingface_device_renewal_rejects_a_different_hub_subject(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000242"
    session_ref = "00000000-0000-4000-8000-000000000243"
    scope = _huggingface_scope("derek/renewal-subject-model")
    oauth = FakeHuggingFaceDeviceOAuth()
    oauth.device_token = HuggingFaceOAuthToken(
        "oauth_access_wrong",
        "oauth_refresh_wrong_subject",
        13_600,
        17_200,
        ("openid", "profile", "read-repos"),
    )
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(
            scope,
            oauth_scopes=["openid", "profile", "read-repos"],
        ),
        oauth_client=oauth,
    )
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/connector-token",
            "auth_mode": "device_oauth",
            "scopes": scope,
        },
    )
    initial = harness.call("list_provider_profiles", {})["profiles"][0]
    request = {"profile_ref": profile_ref, "session_ref": session_ref}
    harness.call("begin_huggingface_renewal", request)
    harness.now += 5
    rejected = harness.call("poll_huggingface_renewal", request)

    assert rejected["state"] == "failed"
    assert rejected["reason_code"] == "external_identity_mismatch"
    current = harness.plugin._store.get_provider_profile(profile_ref, Anchor(**ACTOR))
    assert current["credential_revision"] == initial["credential_revision"]
    assert harness.plugin._decrypt_provider_token(
        profile_ref, bytes(current["credential_blob"])
    ) == ("oauth_access_initial", 10_020)
    assert (
        harness.plugin._decrypt_provider_refresh_token(
            profile_ref, bytes(current["credential_refresh_blob"])
        )
        == "oauth_refresh_initial"
    )


def test_huggingface_device_renewal_pending_and_slow_down_are_deferred(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000246"
    session_ref = "00000000-0000-4000-8000-000000000247"
    oauth = FakeHuggingFaceDeviceOAuth()
    scope = _huggingface_scope("derek/pending-renewal-model")
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(
            scope,
            oauth_scopes=["openid", "profile", "read-repos"],
        ),
        oauth_client=oauth,
    )
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/connector-token",
            "auth_mode": "device_oauth",
            "scopes": scope,
        },
    )
    request = {"profile_ref": profile_ref, "session_ref": session_ref}
    harness.call("begin_huggingface_renewal", request)

    oauth.poll_error = HuggingFaceOAuthError("authorization_pending")
    harness.now += 5
    pending = harness.call("poll_huggingface_renewal", request)
    repeated_before_deadline = harness.call("poll_huggingface_renewal", request)

    assert pending["state"] == "waiting_user"
    assert pending["interval_seconds"] == 5
    assert pending["reason_code"] is None
    assert repeated_before_deadline == pending
    assert len(oauth.poll_calls) == 1

    oauth.poll_error = HuggingFaceOAuthError("slow_down")
    harness.now += 5
    slowed = harness.call("poll_huggingface_renewal", request)
    repeated_before_next_deadline = harness.call("poll_huggingface_renewal", request)

    assert slowed["state"] == "waiting_user"
    assert slowed["interval_seconds"] == 10
    assert slowed["reason_code"] == "slow_down"
    assert repeated_before_next_deadline == slowed
    assert len(oauth.poll_calls) == 2


def test_huggingface_device_renewal_expires_without_an_upstream_poll(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000248"
    session_ref = "00000000-0000-4000-8000-000000000249"
    oauth = FakeHuggingFaceDeviceOAuth()
    scope = _huggingface_scope("derek/expired-renewal-model")
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(
            scope,
            oauth_scopes=["openid", "profile", "read-repos"],
        ),
        oauth_client=oauth,
    )
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/connector-token",
            "auth_mode": "device_oauth",
            "scopes": scope,
        },
    )
    request = {"profile_ref": profile_ref, "session_ref": session_ref}
    started = harness.call("begin_huggingface_renewal", request)
    harness.now = started["expires_at"]

    expired = harness.call("poll_huggingface_renewal", request)

    assert expired["state"] == "expired"
    assert expired["reason_code"] == "device_code_expired"
    assert expired["user_code"] is None
    assert expired["verification_uri"] is None
    assert oauth.poll_calls == []


def test_huggingface_device_renewal_uncertain_outcomes_are_not_replayed(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000250"
    start_session_ref = "00000000-0000-4000-8000-000000000251"
    poll_session_ref = "00000000-0000-4000-8000-000000000252"
    oauth = FakeHuggingFaceDeviceOAuth()
    scope = _huggingface_scope("derek/uncertain-renewal-model")
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(
            scope,
            oauth_scopes=["openid", "profile", "read-repos"],
        ),
        oauth_client=oauth,
    )
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/connector-token",
            "auth_mode": "device_oauth",
            "scopes": scope,
        },
    )
    start_request = {"profile_ref": profile_ref, "session_ref": start_session_ref}
    oauth.start_error = HuggingFaceOAuthError("provider_timeout", uncertain=True)

    unknown_start = harness.call("begin_huggingface_renewal", start_request)
    replayed_start = harness.call("begin_huggingface_renewal", start_request)

    assert unknown_start["state"] == "unknown"
    assert replayed_start == unknown_start
    assert len(oauth.start_calls) == 1

    oauth.start_error = None
    poll_request = {"profile_ref": profile_ref, "session_ref": poll_session_ref}
    harness.call("begin_huggingface_renewal", poll_request)
    oauth.poll_error = HuggingFaceOAuthError("provider_timeout", uncertain=True)
    harness.now += 5
    unknown_poll = harness.call("poll_huggingface_renewal", poll_request)
    replayed_poll = harness.call("poll_huggingface_renewal", poll_request)

    assert unknown_poll["state"] == "unknown"
    assert unknown_poll["user_code"] is None
    assert unknown_poll["verification_uri"] is None
    assert replayed_poll == unknown_poll
    assert len(oauth.poll_calls) == 1


def test_huggingface_device_renewal_fences_concurrent_polls(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000253"
    session_ref = "00000000-0000-4000-8000-000000000254"
    oauth = FakeHuggingFaceDeviceOAuth()
    oauth.poll_started = threading.Event()
    oauth.poll_release = threading.Event()
    scope = _huggingface_scope("derek/concurrent-renewal-model")
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(
            scope,
            oauth_scopes=["openid", "profile", "read-repos"],
        ),
        oauth_client=oauth,
    )
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/connector-token",
            "auth_mode": "device_oauth",
            "scopes": scope,
        },
    )
    request = {"profile_ref": profile_ref, "session_ref": session_ref}
    harness.call("begin_huggingface_renewal", request)
    harness.now += 5

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_poll = executor.submit(harness.call, "poll_huggingface_renewal", request)
        assert oauth.poll_started.wait(timeout=2)
        concurrent_poll = harness.call("poll_huggingface_renewal", request)
        assert concurrent_poll["state"] == "waiting_user"
        assert len(oauth.poll_calls) == 1
        oauth.poll_release.set()
        completed = first_poll.result(timeout=2)

    assert completed["state"] == "succeeded"
    assert len(oauth.poll_calls) == 1


def test_huggingface_workspace_operator_cannot_start_owner_renewal(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000255"
    session_ref = "00000000-0000-4000-8000-000000000256"
    scope = _huggingface_scope("derek/operator-renewal-model")
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(
            scope,
            oauth_scopes=["openid", "profile", "read-repos"],
        ),
        oauth_client=FakeHuggingFaceDeviceOAuth(),
        workspace_operator_profile_refs=[profile_ref],
    )
    linked = harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/connector-token",
            "auth_mode": "device_oauth",
            "scopes": scope,
        },
    )
    assert linked["access_mode"] == "workspace_operator"
    operator_actor = {**ACTOR, "subject": "workspace-maintainer"}

    with pytest.raises(AccountCenterError, match="profile_not_found"):
        harness.call(
            "begin_huggingface_renewal",
            {"profile_ref": profile_ref, "session_ref": session_ref},
            actor=operator_actor,
        )


def test_huggingface_profile_unlink_cancels_device_session_and_clears_code(
    tmp_path: Path,
) -> None:
    profile_ref = "00000000-0000-4000-8000-000000000244"
    session_ref = "00000000-0000-4000-8000-000000000245"
    scope = _huggingface_scope("derek/unlink-renewal-model")
    oauth = FakeHuggingFaceDeviceOAuth()
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        oauth_seed=_oauth_seed(
            scope,
            oauth_scopes=["openid", "profile", "read-repos"],
        ),
        oauth_client=oauth,
    )
    harness.call(
        "link_huggingface_profile",
        {
            "profile_ref": profile_ref,
            "credential_secret_ref": "huggingface/connector-token",
            "auth_mode": "device_oauth",
            "scopes": scope,
        },
    )
    request = {"profile_ref": profile_ref, "session_ref": session_ref}
    harness.call("begin_huggingface_renewal", request)
    harness.call("unlink_provider_profile", {"profile_ref": profile_ref})
    polled = harness.call("poll_huggingface_renewal", request)

    assert polled["state"] == "cancelled"
    assert polled["user_code"] is None
    assert polled["verification_uri"] is None
    assert oauth.poll_calls == []
    with harness.plugin._store._connect() as db:
        row = db.execute(
            "SELECT status,device_code_blob,user_code FROM huggingface_device_renewal_sessions "
            "WHERE session_ref=?",
            (session_ref,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["device_code_blob"] is None
    assert row["user_code"] is None


def _callback_state(session: Mapping[str, Any]) -> str:
    query = parse_qs(urlparse(str(session["authorization_url"])).query)
    return query["state"][0]


def _link_graph_profile(harness: ProfileHarness) -> dict[str, Any]:
    session = harness.call(
        "begin_microsoft_graph_link",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "planner_plan_ids": [GRAPH_PLAN_ID],
        },
    )
    return harness.call(
        "complete_microsoft_graph_link",
        {"state": _callback_state(session), "code": "synthetic-graph-code"},
        caller=GRAPH_CALLBACK,
    )


def _graph_office_v2_scope(workspace_id: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "tenant_id": GRAPH_TENANT_ID,
        "workspace_id": workspace_id,
        "planner": {"plan_ids": [], "tasks_read": True, "tasks_write": False},
        "sharepoint": {
            "enabled": True,
            "site_id": GRAPH_SITE_ID,
            "list_ids": [],
            "drive_ids": [],
            "metadata_read": True,
            "content_read": False,
        },
        "todo": {
            "enabled": True,
            "task_list_ids": [],
            "tasks_read": True,
            "tasks_write": True,
        },
        "calendar": {
            "enabled": True,
            "calendar_ids": [],
            "events_read": True,
            "events_write": True,
            "freebusy_read": False,
            "freebusy_subjects": [],
            "attendees_write": False,
        },
        "mail": {
            "enabled": False,
            "folder_ids": [],
            "metadata_read": False,
            "body_read": False,
            "drafts_write": False,
            "send_enabled": False,
        },
    }


def _link_azure_devops_profile(
    harness: ProfileHarness,
    *,
    profile_ref: str = ADO_PROFILE_REF,
    source_id: str = "pipeline-source-1",
    source_kind: str = "pipeline_runs",
    **scope: Any,
) -> dict[str, Any]:
    session = harness.call(
        "begin_azure_devops_link",
        {
            "profile_ref": profile_ref,
            "tenant_id": GRAPH_TENANT_ID,
            "organization": "cyrene-devops",
            "project_id": "12345678-1234-4234-8234-123456789abc",
            "source_id": source_id,
            "source_kind": source_kind,
            **scope,
        },
    )
    return harness.call(
        "complete_azure_devops_link",
        {"state": _callback_state(session), "code": f"synthetic-{profile_ref}"},
        caller=GRAPH_CALLBACK,
    )


def test_graph_pkce_profile_and_provider_lease_are_owner_scope_and_target_bound(
    tmp_path: Path,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=ENTRA_ACTOR,
    )
    linked = _link_graph_profile(harness)

    assert linked["provider"] == "microsoft_graph"
    assert linked["auth_mode"] == "delegated_oauth"
    assert linked["auth_status"] == "healthy"
    assert linked["external_subject"] == f"{GRAPH_TENANT_ID}:{GRAPH_OBJECT_ID}"
    assert isinstance(linked["expires_at"], int)
    assert "access_token" not in linked
    authorization_scope = oauth.authorizations[0]["scope"].split()
    assert set(authorization_scope) == {
        "openid",
        "profile",
        "offline_access",
        "User.Read",
        "Tasks.Read",
    }
    assert not any(
        "mail" in scope.lower() or "calendar" in scope.lower() for scope in authorization_scope
    )

    profile = harness.call("list_provider_profiles", {})["profiles"][0]
    assert profile["profile_ref"] == GRAPH_PROFILE_REF
    assert profile["external_subject"] == linked["external_subject"]
    assert profile["target_scope"] == {
        "tenant_id": GRAPH_TENANT_ID,
        "planner": {
            "plan_ids": [GRAPH_PLAN_ID],
            "tasks_read": True,
            "tasks_write": False,
        },
        "sharepoint": {"enabled": False, "site_id": None, "list_ids": []},
    }
    assert profile["oauth_scopes"] == ["Tasks.Read", "User.Read"]
    assert "credential_blob" not in profile
    assert "credential_refresh_blob" not in profile

    arguments = {"page_size": 20}
    arguments_sha256 = _digest(arguments)
    target = {"tenant_id": GRAPH_TENANT_ID, "plan_id": GRAPH_PLAN_ID}
    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.planner.tasks.list",
            "target": target,
            "arguments_sha256": arguments_sha256,
        },
    )
    other_actor = {**ENTRA_ACTOR, "subject": OTHER_GRAPH_OBJECT_ID}
    with pytest.raises(AccountCenterError, match="profile_not_found"):
        harness.call(
            "redeem_provider_credential_lease",
            {
                "lease_ref": lease["lease_ref"],
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.planner.tasks.list",
                "target": target,
                "arguments_sha256": arguments_sha256,
            },
            caller=GRAPH_PROVIDER,
            actor=other_actor,
        )
    expected_scope_digest = hashlib.sha256(canonical_json(profile["target_scope"])).hexdigest()
    assert lease["profile_scope_sha256"] == expected_scope_digest
    redemption = harness.call(
        "redeem_provider_credential_lease",
        {
            "lease_ref": lease["lease_ref"],
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.planner.tasks.list",
            "target": target,
            "arguments_sha256": arguments_sha256,
        },
        caller=GRAPH_PROVIDER,
    )
    assert redemption["external_subject"] == linked["external_subject"]
    assert redemption["profile_scope_sha256"] == expected_scope_digest
    assert isinstance(redemption["expires_at"], int)
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call(
            "redeem_provider_credential_lease",
            {
                "lease_ref": lease["lease_ref"],
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.planner.tasks.list",
                "target": target,
                "arguments_sha256": arguments_sha256,
            },
            caller=GRAPH_PROVIDER,
        )
    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.planner.task.create",
                "target": {
                    **target,
                    "bucket_id": GRAPH_BUCKET_ID,
                },
                "arguments_sha256": _digest({"title": "Synthetic task"}),
            },
        )
    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.planner.tasks.list",
                "target": {
                    "tenant_id": GRAPH_TENANT_ID,
                    "plan_id": "other_plan_id",
                },
                "arguments_sha256": arguments_sha256,
            },
        )
    with pytest.raises(AccountCenterError, match="profile_not_found"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.planner.tasks.list",
                "target": target,
                "arguments_sha256": arguments_sha256,
            },
            actor=other_actor,
        )


def test_graph_link_rejects_a_different_signed_entra_actor(
    tmp_path: Path,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=ENTRA_ACTOR,
    )
    other_actor = {**ENTRA_ACTOR, "subject": OTHER_GRAPH_OBJECT_ID}
    session = harness.call(
        "begin_microsoft_graph_link",
        {"profile_ref": GRAPH_PROFILE_REF, "planner_plan_ids": [GRAPH_PLAN_ID]},
        actor=other_actor,
    )
    with pytest.raises(AccountCenterError, match="external_identity_mismatch"):
        harness.call(
            "complete_microsoft_graph_link",
            {"state": _callback_state(session), "code": "synthetic-account-a"},
            caller=GRAPH_CALLBACK,
            actor=other_actor,
        )
    assert harness.call("list_provider_profiles", {})["profiles"] == []


def test_graph_scope_upgrades_are_explicit_and_sharepoint_is_selected(
    tmp_path: Path,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=ENTRA_ACTOR,
    )
    _link_graph_profile(harness)
    site_target = {"tenant_id": GRAPH_TENANT_ID, "site_id": GRAPH_SITE_ID}
    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.sharepoint.site.get",
                "target": site_target,
                "arguments_sha256": _digest({}),
            },
        )

    task_session = harness.call(
        "begin_microsoft_graph_scope_upgrade",
        {"profile_ref": GRAPH_PROFILE_REF, "permission": "planner_tasks_write"},
    )
    assert oauth.authorizations[-1]["prompt"] == "consent"
    task_upgrade = harness.call(
        "complete_microsoft_graph_scope_upgrade",
        {"state": _callback_state(task_session), "code": "synthetic-write-code"},
        caller=GRAPH_CALLBACK,
    )
    assert task_upgrade["auth_status"] == "healthy"
    profile = harness.call("list_provider_profiles", {})["profiles"][0]
    assert profile["target_scope"]["planner"]["tasks_write"] is True
    assert profile["oauth_scopes"] == [
        "Tasks.Read",
        "Tasks.ReadWrite",
        "User.Read",
    ]
    task_target = {
        "tenant_id": GRAPH_TENANT_ID,
        "plan_id": GRAPH_PLAN_ID,
        "bucket_id": GRAPH_BUCKET_ID,
    }
    task_lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.planner.task.create",
            "target": task_target,
            "arguments_sha256": _digest({"title": "Synthetic task"}),
        },
    )
    assert (
        task_lease["profile_scope_sha256"]
        == hashlib.sha256(canonical_json(profile["target_scope"])).hexdigest()
    )

    sharepoint_session = harness.call(
        "begin_microsoft_graph_scope_upgrade",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "permission": "sharepoint_selected",
            "site_id": GRAPH_SITE_ID,
            "list_ids": [GRAPH_LIST_ID],
        },
    )
    assert oauth.authorizations[-1]["prompt"] == "consent"
    harness.call(
        "complete_microsoft_graph_scope_upgrade",
        {
            "state": _callback_state(sharepoint_session),
            "code": "synthetic-sharepoint-code",
        },
        caller=GRAPH_CALLBACK,
    )
    profile = harness.call("list_provider_profiles", {})["profiles"][0]
    assert profile["target_scope"]["sharepoint"] == {
        "enabled": True,
        "site_id": GRAPH_SITE_ID,
        "list_ids": [GRAPH_LIST_ID],
    }
    assert "Sites.Selected" in profile["oauth_scopes"]
    list_target = {
        "tenant_id": GRAPH_TENANT_ID,
        "site_id": GRAPH_SITE_ID,
        "list_id": GRAPH_LIST_ID,
    }
    list_lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.sharepoint.list.items.delta",
            "target": list_target,
            "arguments_sha256": _digest({"cursor": None}),
        },
    )
    assert (
        list_lease["profile_scope_sha256"]
        == hashlib.sha256(canonical_json(profile["target_scope"])).hexdigest()
    )
    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.sharepoint.list.items.delta",
                "target": {
                    **list_target,
                    "list_id": "dddddddd-eeee-4fff-8000-111111111111",
                },
                "arguments_sha256": _digest({"cursor": None}),
            },
        )


def test_graph_office_v2_uses_minimum_scopes_discovery_and_revision_checked_selection(
    tmp_path: Path,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    actor = {**ENTRA_ACTOR, "workspace_id": GRAPH_V2_WORKSPACE_ID}
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=actor,
    )
    scope = _graph_office_v2_scope(GRAPH_V2_WORKSPACE_ID)
    session = harness.call(
        "begin_microsoft_graph_link",
        {"profile_ref": GRAPH_PROFILE_REF, "office_v2_scope": scope},
    )
    requested = set(oauth.authorizations[-1]["scope"].split())
    assert requested == {
        "openid",
        "profile",
        "offline_access",
        "User.Read",
        "Tasks.ReadWrite",
        "Sites.Selected",
        "Calendars.ReadWrite",
    }
    assert not any(scope_name.startswith("Mail.") for scope_name in requested)
    assert "Tasks.Read" not in requested
    assert "Calendars.ReadBasic" not in requested

    linked = harness.call(
        "complete_microsoft_graph_link",
        {"state": _callback_state(session), "code": "synthetic-office-v2-code"},
        caller=GRAPH_CALLBACK,
        actor=actor,
    )
    assert linked["scope_schema_version"] == 2
    assert linked["workspace_id"] == GRAPH_V2_WORKSPACE_ID
    assert linked["refresh_expires_at"] is None
    profile = harness.call("list_provider_profiles", {}, actor=actor)["profiles"][0]
    assert profile["target_scope"] == scope
    assert profile["scope_schema_version"] == 2
    assert profile["reauth_required"] is False
    assert profile["refresh_expires_at"] is None
    assert "credential_blob" not in profile

    discovery = (
        ("microsoft_graph.planner.plans.list", {"tenant_id": GRAPH_TENANT_ID}),
        ("microsoft_graph.todo.lists.list", {"tenant_id": GRAPH_TENANT_ID}),
        ("microsoft_graph.calendar.calendars.list", {"tenant_id": GRAPH_TENANT_ID}),
        (
            "microsoft_graph.sharepoint.drives.list",
            {"tenant_id": GRAPH_TENANT_ID, "site_id": GRAPH_SITE_ID},
        ),
    )
    for operation_id, target in discovery:
        lease = harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": operation_id,
                "target": target,
                "arguments_sha256": _digest({"page_size": 10}),
            },
            actor=actor,
        )
        assert lease["operation_id"] == operation_id

    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.todo.tasks.list",
                "target": {"tenant_id": GRAPH_TENANT_ID, "task_list_id": "unselected"},
                "arguments_sha256": _digest({}),
            },
            actor=actor,
        )

    selected = json.loads(json.dumps(scope))
    selected["planner"]["plan_ids"] = [GRAPH_PLAN_ID]
    selected["sharepoint"]["list_ids"] = [GRAPH_LIST_ID]
    selected["sharepoint"]["drive_ids"] = ["drive!1"]
    selected["todo"]["task_list_ids"] = ["AAMkAGI2...=="]
    selected["calendar"]["calendar_ids"] = ["calendar!1"]
    old_revision = profile["credential_revision"]
    updated = harness.call(
        "update_microsoft_graph_v2_scope",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "workspace_id": GRAPH_V2_WORKSPACE_ID,
            "expected_credential_revision": old_revision,
            "target_scope": selected,
        },
        actor=actor,
    )
    assert updated["credential_revision"] == old_revision + 1
    assert updated["target_scope"] == selected
    assert updated["oauth_scopes"] == [
        "Calendars.ReadWrite",
        "Sites.Selected",
        "Tasks.ReadWrite",
        "User.Read",
    ]

    task_lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.todo.tasks.list",
            "target": {"tenant_id": GRAPH_TENANT_ID, "task_list_id": "AAMkAGI2...=="},
            "arguments_sha256": _digest({"page_size": 10}),
        },
        actor=actor,
    )
    assert task_lease["operation_id"] == "microsoft_graph.todo.tasks.list"

    opaque_task_id = "AAMkTaskID...=="
    for operation_id in (
        "microsoft_graph.todo.task.get",
        "microsoft_graph.todo.task.update",
        "microsoft_graph.todo.task.delete",
    ):
        task_target = {
            "tenant_id": GRAPH_TENANT_ID,
            "task_list_id": "AAMkAGI2...==",
            "task_id": opaque_task_id,
        }
        task_operation_lease = harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": operation_id,
                "target": task_target,
                "arguments_sha256": _digest({}),
            },
            actor=actor,
        )
        assert task_operation_lease["operation_id"] == operation_id

    with pytest.raises(AccountCenterError, match="invalid_request"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.planner.task.get",
                "target": {
                    "tenant_id": GRAPH_TENANT_ID,
                    "plan_id": GRAPH_PLAN_ID,
                    "task_id": opaque_task_id,
                },
                "arguments_sha256": _digest({}),
            },
            actor=actor,
        )

    with pytest.raises(AccountCenterError, match="profile_revision_conflict"):
        harness.call(
            "update_microsoft_graph_v2_scope",
            {
                "profile_ref": GRAPH_PROFILE_REF,
                "workspace_id": GRAPH_V2_WORKSPACE_ID,
                "expected_credential_revision": old_revision,
                "target_scope": selected,
            },
            actor=actor,
        )


def test_graph_mail_scope_upgrade_is_one_consent_and_preserves_other_office_access(
    tmp_path: Path,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    actor = {**ENTRA_ACTOR, "workspace_id": GRAPH_V2_WORKSPACE_ID}
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=actor,
    )
    scope = _graph_office_v2_scope(GRAPH_V2_WORKSPACE_ID)
    initial = harness.call(
        "begin_microsoft_graph_link",
        {"profile_ref": GRAPH_PROFILE_REF, "office_v2_scope": scope},
    )
    harness.call(
        "complete_microsoft_graph_link",
        {"state": _callback_state(initial), "code": "synthetic-office-v2-code"},
        caller=GRAPH_CALLBACK,
        actor=actor,
    )
    old_profile = harness.call("list_provider_profiles", {}, actor=actor)["profiles"][0]
    original_revision = old_profile["credential_revision"]
    for invalid_features in (
        {"metadata_read": False, "drafts_write": False, "send": False},
        {"metadata_read": True, "drafts_write": False},
        {
            "metadata_read": True,
            "drafts_write": False,
            "send": False,
            "scopes": ["Mail.Read"],
        },
    ):
        with pytest.raises(AccountCenterError, match="invalid_request"):
            harness.call(
                "begin_microsoft_graph_scope_upgrade",
                {
                    "profile_ref": GRAPH_PROFILE_REF,
                    "permission": "mail",
                    "mail_features": invalid_features,
                },
                actor=actor,
            )
    assert len(oauth.authorizations) == 1

    task_lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.todo.lists.list",
            "target": {"tenant_id": GRAPH_TENANT_ID},
            "arguments_sha256": _digest({"page_size": 10}),
        },
        actor=actor,
    )
    assert task_lease["operation_id"] == "microsoft_graph.todo.lists.list"

    upgrade = harness.call(
        "begin_microsoft_graph_scope_upgrade",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "permission": "mail",
            "mail_features": {"metadata_read": True, "drafts_write": True, "send": True},
        },
        actor=actor,
    )
    upgrade_scopes = set(oauth.authorizations[-1]["scope"].split())
    assert oauth.authorizations[-1]["prompt"] == "consent"
    assert {"Mail.ReadWrite", "Mail.Send"} <= upgrade_scopes
    assert "Mail.ReadBasic" not in upgrade_scopes
    assert "Mail.Read" not in upgrade_scopes

    pending_profile = harness.call("list_provider_profiles", {}, actor=actor)["profiles"][0]
    assert pending_profile["credential_revision"] == original_revision
    assert pending_profile["target_scope"]["mail"] == scope["mail"]
    pending_task_lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.todo.lists.list",
            "target": {"tenant_id": GRAPH_TENANT_ID},
            "arguments_sha256": _digest({"page_size": 5}),
        },
        actor=actor,
    )
    assert pending_task_lease["profile_scope_sha256"] == task_lease["profile_scope_sha256"]

    completed = harness.call(
        "complete_microsoft_graph_scope_upgrade",
        {"state": _callback_state(upgrade), "code": "synthetic-mail-consent"},
        caller=GRAPH_CALLBACK,
        actor=actor,
    )
    assert completed["credential_revision"] == original_revision + 1
    profile = harness.call("list_provider_profiles", {}, actor=actor)["profiles"][0]
    assert profile["oauth_scopes"] == [
        "Calendars.ReadWrite",
        "Mail.ReadWrite",
        "Mail.Send",
        "Sites.Selected",
        "Tasks.ReadWrite",
        "User.Read",
    ]
    assert profile["target_scope"]["mail"] == {
        "enabled": True,
        "folder_ids": [],
        "metadata_read": True,
        "body_read": False,
        "drafts_write": True,
        "send_enabled": True,
    }
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call(
            "redeem_provider_credential_lease",
            {
                "lease_ref": task_lease["lease_ref"],
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.todo.lists.list",
                "target": {"tenant_id": GRAPH_TENANT_ID},
                "arguments_sha256": _digest({"page_size": 10}),
            },
            caller=GRAPH_PROVIDER,
            actor=actor,
        )

    folder_lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.mail.folders.list",
            "target": {"tenant_id": GRAPH_TENANT_ID},
            "arguments_sha256": _digest({"page_size": 10}),
        },
        actor=actor,
    )
    assert folder_lease["operation_id"] == "microsoft_graph.mail.folders.list"
    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.mail.messages.list",
                "target": {"tenant_id": GRAPH_TENANT_ID, "folder_id": "unselected-folder"},
                "arguments_sha256": _digest({"page_size": 10}),
            },
            actor=actor,
        )

    selected_scope = json.loads(json.dumps(profile["target_scope"]))
    selected_scope["mail"]["folder_ids"] = ["AAMkFolder_opaque=="]
    selected = harness.call(
        "update_microsoft_graph_v2_scope",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "workspace_id": GRAPH_V2_WORKSPACE_ID,
            "expected_credential_revision": profile["credential_revision"],
            "target_scope": selected_scope,
        },
        actor=actor,
    )
    assert selected["credential_revision"] == original_revision + 2
    assert selected["oauth_scopes"] == profile["oauth_scopes"]
    message_lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.mail.messages.list",
            "target": {"tenant_id": GRAPH_TENANT_ID, "folder_id": "AAMkFolder_opaque=="},
            "arguments_sha256": _digest({"page_size": 10}),
        },
        actor=actor,
    )
    assert message_lease["operation_id"] == "microsoft_graph.mail.messages.list"
    draft_lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.mail.draft.create",
            "target": {"tenant_id": GRAPH_TENANT_ID, "folder_id": "drafts"},
            "arguments_sha256": _digest({"subject": "Synthetic", "body_text": "local"}),
        },
        actor=actor,
    )
    assert draft_lease["operation_id"] == "microsoft_graph.mail.draft.create"
    send_lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.mail.message.send",
            "target": {
                "tenant_id": GRAPH_TENANT_ID,
                "folder_id": "drafts",
                "message_id": "AAMkDraft_opaque==",
            },
            "arguments_sha256": _digest({}),
        },
        actor=actor,
    )
    assert send_lease["operation_id"] == "microsoft_graph.mail.message.send"


def test_graph_mail_upgrade_failure_keeps_the_existing_v2_profile_healthy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    actor = {**ENTRA_ACTOR, "workspace_id": GRAPH_V2_WORKSPACE_ID}
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=actor,
    )
    scope = _graph_office_v2_scope(GRAPH_V2_WORKSPACE_ID)
    initial = harness.call(
        "begin_microsoft_graph_link",
        {"profile_ref": GRAPH_PROFILE_REF, "office_v2_scope": scope},
    )
    harness.call(
        "complete_microsoft_graph_link",
        {"state": _callback_state(initial), "code": "synthetic-office-v2-code"},
        caller=GRAPH_CALLBACK,
        actor=actor,
    )
    before = harness.call("list_provider_profiles", {}, actor=actor)["profiles"][0]
    upgrade = harness.call(
        "begin_microsoft_graph_scope_upgrade",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "permission": "mail",
            "mail_features": {"metadata_read": False, "drafts_write": False, "send": True},
        },
        actor=actor,
    )

    def fail_upgrade(**_: Any) -> tuple[MicrosoftGraphToken, MicrosoftGraphIdentity]:
        raise MicrosoftGraphOAuthError("identity_verification_failed")

    monkeypatch.setattr(oauth, "exchange_code", fail_upgrade)
    with pytest.raises(AccountCenterError, match="identity_verification_failed"):
        harness.call(
            "complete_microsoft_graph_scope_upgrade",
            {"state": _callback_state(upgrade), "code": "synthetic-denied-consent"},
            caller=GRAPH_CALLBACK,
            actor=actor,
        )

    after = harness.call("list_provider_profiles", {}, actor=actor)["profiles"][0]
    assert after["auth_status"] == "healthy"
    assert after["credential_revision"] == before["credential_revision"]
    assert after["oauth_scopes"] == before["oauth_scopes"]
    assert after["target_scope"] == before["target_scope"]
    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.mail.message.send",
                "target": {
                    "tenant_id": GRAPH_TENANT_ID,
                    "folder_id": "drafts",
                    "message_id": "AAMkDraft_opaque==",
                },
                "arguments_sha256": _digest({}),
            },
            actor=actor,
        )
    legacy_lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.todo.lists.list",
            "target": {"tenant_id": GRAPH_TENANT_ID},
            "arguments_sha256": _digest({"page_size": 10}),
        },
        actor=actor,
    )
    assert legacy_lease["operation_id"] == "microsoft_graph.todo.lists.list"


def test_graph_send_only_uses_readbasic_for_preflight_without_enabling_draft_tools(
    tmp_path: Path,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    actor = {**ENTRA_ACTOR, "workspace_id": GRAPH_V2_WORKSPACE_ID}
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=actor,
    )
    scope = _graph_office_v2_scope(GRAPH_V2_WORKSPACE_ID)
    initial = harness.call(
        "begin_microsoft_graph_link",
        {"profile_ref": GRAPH_PROFILE_REF, "office_v2_scope": scope},
    )
    harness.call(
        "complete_microsoft_graph_link",
        {"state": _callback_state(initial), "code": "synthetic-office-v2-code"},
        caller=GRAPH_CALLBACK,
        actor=actor,
    )
    upgrade = harness.call(
        "begin_microsoft_graph_scope_upgrade",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "permission": "mail",
            "mail_features": {"metadata_read": False, "drafts_write": False, "send": True},
        },
        actor=actor,
    )
    requested = set(oauth.authorizations[-1]["scope"].split())
    assert "Mail.ReadBasic" in requested
    assert "Mail.Send" in requested
    assert "Mail.ReadWrite" not in requested
    assert "Mail.Read" not in requested
    harness.call(
        "complete_microsoft_graph_scope_upgrade",
        {"state": _callback_state(upgrade), "code": "synthetic-send-only-code"},
        caller=GRAPH_CALLBACK,
        actor=actor,
    )

    send_target = {
        "tenant_id": GRAPH_TENANT_ID,
        "folder_id": "drafts",
        "message_id": "AAMkDraft_opaque==",
    }
    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.mail.message.send",
            "target": send_target,
            "arguments_sha256": _digest({}),
        },
        actor=actor,
    )
    assert lease["operation_id"] == "microsoft_graph.mail.message.send"
    for operation_id in (
        "microsoft_graph.mail.folders.list",
        "microsoft_graph.mail.drafts.list",
    ):
        target = (
            {"tenant_id": GRAPH_TENANT_ID}
            if operation_id.endswith("folders.list")
            else {"tenant_id": GRAPH_TENANT_ID, "folder_id": "drafts"}
        )
        with pytest.raises(AccountCenterError, match="authorization_denied"):
            harness.call(
                "create_provider_credential_lease",
                {
                    "profile_ref": GRAPH_PROFILE_REF,
                    "operation_id": operation_id,
                    "target": target,
                    "arguments_sha256": _digest({}),
                },
                actor=actor,
            )


def test_graph_callback_configuration_is_loopback_only_and_fixed_path() -> None:
    assert (
        _validated_microsoft_graph_callback_uri("http://localhost:52191/oauth/callback")
        == "http://localhost:52191/oauth/callback"
    )
    for callback in (
        "https://localhost:52191/oauth/callback",
        "http://127.0.0.1:52191/oauth/callback",
        "http://localhost:52191/api/v1/accounts/microsoft-graph/callback",
        "http://localhost:52191/oauth/callback?next=https://example.test",
        "http://example.test:52191/oauth/callback",
    ):
        with pytest.raises(AccountCenterError, match="configuration_unavailable"):
            _validated_microsoft_graph_callback_uri(callback)


def test_entra_oauth_state_replay_and_nonce_mismatch_fail_closed(
    tmp_path: Path,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=ENTRA_ACTOR,
    )
    session = harness.call(
        "begin_microsoft_graph_link",
        {"profile_ref": GRAPH_PROFILE_REF, "planner_plan_ids": [GRAPH_PLAN_ID]},
    )
    oauth.authorizations[-1]["nonce"] = "modified-nonce"
    with pytest.raises(AccountCenterError, match="identity_verification_failed"):
        harness.call(
            "complete_microsoft_graph_link",
            {"state": _callback_state(session), "code": "synthetic-code"},
            caller=GRAPH_CALLBACK,
        )
    with pytest.raises(AccountCenterError, match="link_session_invalid"):
        harness.call(
            "complete_microsoft_graph_link",
            {"state": _callback_state(session), "code": "synthetic-code"},
            caller=GRAPH_CALLBACK,
        )
    with harness.plugin._store._connect() as db:
        row = db.execute(
            "SELECT status,code_verifier_blob FROM microsoft_entra_oauth_sessions "
            "WHERE session_ref=?",
            (session["link_session_ref"],),
        ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert row["code_verifier_blob"] == b""


def test_azure_devops_profile_uses_separate_resource_and_source_bound_lease(
    tmp_path: Path,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    oauth.access_ttl = 10
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=ENTRA_ACTOR,
    )
    linked = _link_azure_devops_profile(harness, pipeline_id=42)
    assert linked["provider"] == "azure_devops"
    assert linked["auth_status"] == "healthy"
    assert linked["auth_mode"] == "delegated_oauth"
    assert linked["external_subject"] == f"{GRAPH_TENANT_ID}:{GRAPH_OBJECT_ID}"
    ado_authorization_scope = set(oauth.authorizations[0]["scope"].split())
    assert ado_authorization_scope == {
        "openid",
        "profile",
        "offline_access",
        AZURE_DEVOPS_RESOURCE_SCOPE,
    }
    assert not any(scope.startswith("Tasks.") for scope in ado_authorization_scope)
    assert not any(scope.startswith("vso.") for scope in ado_authorization_scope)

    profile = next(
        item
        for item in harness.call("list_provider_profiles", {})["profiles"]
        if item["provider"] == "azure_devops"
    )
    expected_scope = {
        "tenant_id": GRAPH_TENANT_ID,
        "organization": "cyrene-devops",
        "project_id": "12345678-1234-4234-8234-123456789abc",
        "source_id": "pipeline-source-1",
        "source_kind": "pipeline_runs",
        "pipeline_id": 42,
    }
    assert profile["target_scope"] == expected_scope
    assert profile["oauth_scopes"] == [AZURE_DEVOPS_RESOURCE_SCOPE]
    anchor = Anchor(
        ENTRA_ACTOR["issuer"],
        ENTRA_ACTOR["subject"],
        ENTRA_ACTOR["organization_id"],
        ENTRA_ACTOR["workspace_id"],
    )
    stored_profile = harness.plugin._store.get_provider_profile(ADO_PROFILE_REF, anchor)
    refreshed_token, expires_at = harness.plugin._fresh_provider_credential(stored_profile, anchor)
    assert refreshed_token == "entra-refreshed-access-1"
    assert expires_at == harness.now + 3600
    assert oauth.refresh_calls == [
        (GRAPH_TENANT_ID, "entra-refresh-1", (AZURE_DEVOPS_RESOURCE_SCOPE,))
    ]
    target = dict(expected_scope)
    arguments = {"pageSize": 20, "pageCursor": None}
    arguments_sha256 = _digest(arguments)
    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": ADO_PROFILE_REF,
            "operation_id": "azure_devops.pipeline_runs.list",
            "target": target,
            "arguments_sha256": arguments_sha256,
        },
    )
    scope_digest = hashlib.sha256(canonical_json(expected_scope)).hexdigest()
    assert lease["profile_scope_sha256"] == scope_digest
    redeemed = harness.call(
        "redeem_provider_credential_lease",
        {
            "lease_ref": lease["lease_ref"],
            "profile_ref": ADO_PROFILE_REF,
            "operation_id": "azure_devops.pipeline_runs.list",
            "target": target,
            "arguments_sha256": arguments_sha256,
        },
        caller=ADO_PROVIDER,
    )
    assert redeemed["external_subject"] == linked["external_subject"]
    assert redeemed["profile_scope_sha256"] == scope_digest

    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": ADO_PROFILE_REF,
                "operation_id": "azure_devops.pipeline_runs.list",
                "target": {**target, "source_id": "other-source"},
                "arguments_sha256": arguments_sha256,
            },
        )

    _link_azure_devops_profile(
        harness,
        profile_ref="00000000-0000-4000-8000-000000000403",
        source_id="release-source-1",
        source_kind="release_deployments",
        definition_id=12,
        definition_environment_id=5,
    )
    profiles = harness.call("list_provider_profiles", {})["profiles"]
    assert sum(item["provider"] == "azure_devops" for item in profiles) == 2

    other_actor = {**ENTRA_ACTOR, "subject": OTHER_GRAPH_OBJECT_ID}
    with pytest.raises(AccountCenterError, match="external_identity_mismatch"):
        # The callback is the point where the verified external identity is claimed.
        session = harness.call(
            "begin_azure_devops_link",
            {
                "profile_ref": "00000000-0000-4000-8000-000000000404",
                "tenant_id": GRAPH_TENANT_ID,
                "organization": "cyrene-devops",
                "project_id": "12345678-1234-4234-8234-123456789abc",
                "source_id": "other-owner-source",
                "source_kind": "pipeline_runs",
                "pipeline_id": 77,
            },
            actor=other_actor,
        )
        harness.call(
            "complete_azure_devops_link",
            {"state": _callback_state(session), "code": "other-owner-code"},
            caller=GRAPH_CALLBACK,
            actor=other_actor,
        )


def test_graph_provider_profile_read_is_owner_workspace_and_provider_filtered(
    tmp_path: Path,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=ENTRA_ACTOR,
    )
    _link_graph_profile(harness)
    _link_azure_devops_profile(harness, pipeline_id=42)

    graph_view = harness.call(
        "list_provider_profiles", {}, caller=GRAPH_PROVIDER, actor=ENTRA_ACTOR
    )["profiles"]
    assert [profile["provider"] for profile in graph_view] == ["microsoft_graph"]
    assert graph_view[0]["profile_ref"] == GRAPH_PROFILE_REF
    assert graph_view[0]["external_subject"] == f"{GRAPH_TENANT_ID}:{GRAPH_OBJECT_ID}"
    assert graph_view[0]["oauth_scopes"] == ["Tasks.Read", "User.Read"]
    assert not {"openid", "profile", "offline_access"} & set(graph_view[0]["oauth_scopes"])

    exchange_view = harness.call("list_provider_profiles", {}, actor=ENTRA_ACTOR)["profiles"]
    assert {profile["provider"] for profile in exchange_view} == {
        "microsoft_graph",
        "azure_devops",
    }

    other_actor = {**ENTRA_ACTOR, "subject": OTHER_GRAPH_OBJECT_ID}
    assert (
        harness.call("list_provider_profiles", {}, caller=GRAPH_PROVIDER, actor=other_actor)[
            "profiles"
        ]
        == []
    )
    other_workspace = {**ENTRA_ACTOR, "workspace_id": "different-workspace"}
    assert (
        harness.call("list_provider_profiles", {}, caller=GRAPH_PROVIDER, actor=other_workspace)[
            "profiles"
        ]
        == []
    )

    with pytest.raises(AccountCenterError, match="external_identity_mismatch"):
        harness.call("list_provider_profiles", {}, caller=GRAPH_PROVIDER, actor=ACTOR)
    with pytest.raises(AccountCenterError, match="untrusted_caller"):
        harness.call("list_provider_profiles", {}, caller=ADO_PROVIDER, actor=ENTRA_ACTOR)


def test_graph_refresh_identity_mismatch_revokes_cached_material_and_leases(
    tmp_path: Path,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    oauth.access_ttl = 1
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=ENTRA_ACTOR,
    )
    _link_graph_profile(harness)
    anchor = Anchor(
        ENTRA_ACTOR["issuer"],
        ENTRA_ACTOR["subject"],
        ENTRA_ACTOR["organization_id"],
        ENTRA_ACTOR["workspace_id"],
    )
    profile = harness.plugin._store.get_provider_profile(GRAPH_PROFILE_REF, anchor)
    target = {"tenant_id": GRAPH_TENANT_ID, "plan_id": GRAPH_PLAN_ID}
    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.planner.tasks.list",
            "target": target,
            "arguments_sha256": _digest({"page_size": 3}),
        },
    )
    oauth.refresh_identity = MicrosoftGraphIdentity(GRAPH_TENANT_ID, OTHER_GRAPH_OBJECT_ID)
    harness.now += 2
    with pytest.raises(AccountCenterError, match="external_identity_mismatch"):
        harness.plugin._fresh_provider_credential(profile, anchor)
    with harness.plugin._store._connect() as db:
        stored = db.execute(
            "SELECT auth_status,credential_blob,credential_refresh_blob "
            "FROM provider_profiles WHERE profile_ref=?",
            (GRAPH_PROFILE_REF,),
        ).fetchone()
        outstanding = db.execute(
            "SELECT COUNT(*) FROM provider_credential_leases WHERE lease_ref=?",
            (lease["lease_ref"],),
        ).fetchone()[0]
    assert stored is not None
    assert stored["auth_status"] == "reauthorization_required"
    assert stored["credential_blob"] is None
    assert stored["credential_refresh_blob"] is None
    assert outstanding == 0


def test_azure_devops_refresh_identity_mismatch_requires_reauthorization(
    tmp_path: Path,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    oauth.access_ttl = 1
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=ENTRA_ACTOR,
    )
    _link_azure_devops_profile(harness, pipeline_id=42)
    anchor = Anchor(
        ENTRA_ACTOR["issuer"],
        ENTRA_ACTOR["subject"],
        ENTRA_ACTOR["organization_id"],
        ENTRA_ACTOR["workspace_id"],
    )
    profile = harness.plugin._store.get_provider_profile(ADO_PROFILE_REF, anchor)
    oauth.refresh_identity = MicrosoftGraphIdentity(GRAPH_TENANT_ID, OTHER_GRAPH_OBJECT_ID)
    harness.now += 2
    with pytest.raises(AccountCenterError, match="external_identity_mismatch"):
        harness.plugin._fresh_provider_credential(profile, anchor)
    with harness.plugin._store._connect() as db:
        stored = db.execute(
            "SELECT auth_status,credential_blob,credential_refresh_blob "
            "FROM provider_profiles WHERE profile_ref=?",
            (ADO_PROFILE_REF,),
        ).fetchone()
    assert stored is not None
    assert stored["auth_status"] == "reauthorization_required"
    assert stored["credential_blob"] is None
    assert stored["credential_refresh_blob"] is None


def test_graph_refresh_is_serialized_and_unlink_revokes_local_material(
    tmp_path: Path,
) -> None:
    oauth = FakeMicrosoftEntraOAuth(lambda: 10_000)
    oauth.access_ttl = 10
    oauth.refresh_delay = 0.05
    harness = ProfileHarness(
        tmp_path / "accounts.sqlite3",
        entra_oauth_client=oauth,
        default_actor=ENTRA_ACTOR,
    )
    _link_graph_profile(harness)
    profile = harness.plugin._store.get_provider_profile(
        GRAPH_PROFILE_REF,
        Anchor(
            ENTRA_ACTOR["issuer"],
            ENTRA_ACTOR["subject"],
            ENTRA_ACTOR["organization_id"],
            ENTRA_ACTOR["workspace_id"],
        ),
    )
    second_plugin = harness._new_plugin()
    anchor = Anchor(
        ENTRA_ACTOR["issuer"],
        ENTRA_ACTOR["subject"],
        ENTRA_ACTOR["organization_id"],
        ENTRA_ACTOR["workspace_id"],
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(harness.plugin._fresh_provider_credential, profile, anchor),
            executor.submit(second_plugin._fresh_provider_credential, profile, anchor),
        ]
        refreshed = [future.result(timeout=3) for future in futures]
    assert refreshed[0] == refreshed[1]
    assert refreshed[0][0] == "entra-refreshed-access-1"
    assert len(oauth.refresh_calls) == 1
    stored = harness.plugin._store.get_provider_profile(GRAPH_PROFILE_REF, anchor)
    assert stored["credential_refresh_blob"] != b"entra-refresh-1"
    assert (
        harness.plugin._decrypt_provider_refresh_token(
            GRAPH_PROFILE_REF, bytes(stored["credential_refresh_blob"])
        )
        == "entra-rotated-refresh-1"
    )

    target = {"tenant_id": GRAPH_TENANT_ID, "plan_id": GRAPH_PLAN_ID}
    arguments_sha256 = _digest({"page_size": 5})
    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": GRAPH_PROFILE_REF,
            "operation_id": "microsoft_graph.planner.tasks.list",
            "target": target,
            "arguments_sha256": arguments_sha256,
        },
    )
    harness.call("unlink_provider_profile", {"profile_ref": GRAPH_PROFILE_REF})
    with pytest.raises(AccountCenterError, match="profile_not_found"):
        harness.call(
            "redeem_provider_credential_lease",
            {
                "lease_ref": lease["lease_ref"],
                "profile_ref": GRAPH_PROFILE_REF,
                "operation_id": "microsoft_graph.planner.tasks.list",
                "target": target,
                "arguments_sha256": arguments_sha256,
            },
            caller=GRAPH_PROVIDER,
        )
    with harness.plugin._store._connect() as db:
        stored_row = db.execute(
            "SELECT status,auth_status,credential_blob,credential_refresh_blob "
            "FROM provider_profiles WHERE profile_ref=?",
            (GRAPH_PROFILE_REF,),
        ).fetchone()
    assert stored_row is not None
    assert stored_row["status"] == "unlinked"
    assert stored_row["auth_status"] == "revoked"
    assert stored_row["credential_blob"] is None
    assert stored_row["credential_refresh_blob"] is None


def _enable_bilibili_provider(harness: ProfileHarness) -> None:
    """Register the fixed local provider identity and an isolated signing key."""

    harness.keys[BILIBILI_PROVIDER] = b"b" * 32
    harness.config["caller_keys"] = harness.keys
    harness.config["bilibili_provider_caller_id"] = BILIBILI_PROVIDER
    harness.plugin = harness._new_plugin()


def _bilibili_cookie_json() -> str:
    """Build a synthetic cookie jar for local Account Center boundary tests."""

    return json.dumps(
        {
            "schemaVersion": 1,
            "cookies": [
                {
                    "name": "SESSDATA",
                    "value": "synthetic-sessdata-secret",
                    "domain": ".bilibili.com",
                    "path": "/",
                    "expires_at": 20_000,
                },
                {
                    "name": "bili_jct",
                    "value": "synthetic-csrf-secret",
                    "domain": ".bilibili.com",
                    "path": "/",
                    "expires_at": 18_000,
                },
                {
                    "name": "DedeUserID",
                    "value": "12345",
                    "domain": ".bilibili.com",
                    "path": "/",
                    "expires_at": None,
                },
            ],
        }
    )


def test_bilibili_account_link_encrypts_cookie_and_issues_exact_one_use_lease(
    tmp_path: Path,
) -> None:
    """Bind a synthetic identity, then redeem one exact private cookie lease."""

    db_path = tmp_path / "accounts.sqlite3"
    harness = ProfileHarness(db_path)
    _enable_bilibili_provider(harness)
    profile_ref = "00000000-0000-4000-8000-000000000501"
    session = harness.call(
        "begin_bilibili_link",
        {"profile_ref": profile_ref},
        caller=BILIBILI_PROVIDER,
    )
    linked = harness.call(
        "complete_bilibili_link",
        {
            "profile_ref": profile_ref,
            "link_session_ref": session["link_session_ref"],
            "external_subject": "12345",
            "credential_json": _bilibili_cookie_json(),
            "credential_expires_at": 18_000,
        },
        caller=BILIBILI_PROVIDER,
    )

    assert linked["provider"] == "bilibili"
    assert linked["auth_mode"] == "session_cookie"
    assert linked["external_subject"] == "12345"
    assert linked["credential_revision"] == 1
    assert not {"credential_json", "access_token", "cookie"} & linked.keys()
    listed = harness.call("list_provider_profiles", {})["profiles"]
    assert len(listed) == 1
    assert listed[0]["target_scope"] == {"schema_version": 1, "account_id": "12345"}
    assert "synthetic-sessdata-secret" not in json.dumps(listed)

    with sqlite3.connect(db_path) as db:
        stored = db.execute(
            "SELECT auth_mode,credential_blob FROM provider_profiles WHERE profile_ref=?",
            (profile_ref,),
        ).fetchone()
    assert stored is not None
    assert stored[0] == "session_cookie"
    assert b"synthetic-sessdata-secret" not in stored[1]
    assert b"synthetic-csrf-secret" not in stored[1]

    arguments = {"user_id": "12345", "text": "local fixture only"}
    target = {"user_id": "12345"}
    with pytest.raises(AccountCenterError, match="authorization_denied"):
        harness.call(
            "create_provider_credential_lease",
            {
                "profile_ref": profile_ref,
                "operation_id": "bilibili.dynamic.publish",
                "target": {"user_id": "54321"},
                "arguments_sha256": _digest(arguments),
            },
        )
    lease = harness.call(
        "create_provider_credential_lease",
        {
            "profile_ref": profile_ref,
            "operation_id": "bilibili.dynamic.publish",
            "target": target,
            "arguments_sha256": _digest(arguments),
        },
    )
    redemption = {
        "lease_ref": lease["lease_ref"],
        "profile_ref": profile_ref,
        "operation_id": "bilibili.dynamic.publish",
        "target": target,
        "arguments_sha256": _digest(arguments),
    }
    with pytest.raises(AccountCenterError, match="untrusted_caller"):
        harness.call(
            "redeem_provider_credential_lease",
            redemption,
            caller=BILIBILI_PROVIDER,
            binding_id="different-binding",
        )
    material = harness.call(
        "redeem_provider_credential_lease",
        redemption,
        caller=BILIBILI_PROVIDER,
    )
    assert material["token_type"] == "BilibiliCookieJar"
    assert json.loads(material["access_token"])["cookies"]
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call(
            "redeem_provider_credential_lease",
            redemption,
            caller=BILIBILI_PROVIDER,
        )


def test_bilibili_link_session_is_bound_to_owner_binding_and_cookie_identity(
    tmp_path: Path,
) -> None:
    """Reject foreign or cross-binding completion before consuming a QR session."""

    harness = ProfileHarness(tmp_path / "accounts.sqlite3")
    _enable_bilibili_provider(harness)
    profile_ref = "00000000-0000-4000-8000-000000000502"
    session = harness.call(
        "begin_bilibili_link",
        {"profile_ref": profile_ref},
        caller=BILIBILI_PROVIDER,
    )
    completion = {
        "profile_ref": profile_ref,
        "link_session_ref": session["link_session_ref"],
        "external_subject": "12345",
        "credential_json": _bilibili_cookie_json(),
        "credential_expires_at": 18_000,
    }
    stranger = {**ACTOR, "subject": "some-other-user"}
    with pytest.raises(AccountCenterError, match="link_session_invalid"):
        harness.call(
            "complete_bilibili_link",
            completion,
            caller=BILIBILI_PROVIDER,
            actor=stranger,
        )
    with pytest.raises(AccountCenterError, match="untrusted_caller"):
        harness.call(
            "complete_bilibili_link",
            completion,
            caller=BILIBILI_PROVIDER,
            binding_id="another-binding",
        )

    wrong_jar = json.loads(_bilibili_cookie_json())
    next(cookie for cookie in wrong_jar["cookies"] if cookie["name"] == "DedeUserID")[
        "value"
    ] = "99999"
    wrong_identity = json.dumps(wrong_jar)
    with pytest.raises(AccountCenterError, match="external_identity_mismatch"):
        harness.call(
            "complete_bilibili_link",
            {
                **completion,
                "credential_json": wrong_identity,
                "idempotency_key": "wrong-identity-attempt",
            },
            caller=BILIBILI_PROVIDER,
        )

    success = harness.call(
        "complete_bilibili_link",
        {**completion, "idempotency_key": "first-completion"},
        caller=BILIBILI_PROVIDER,
    )
    with pytest.raises(AccountCenterError, match="link_session_invalid"):
        harness.call(
            "complete_bilibili_link",
            {**completion, "idempotency_key": "second-completion"},
            caller=BILIBILI_PROVIDER,
        )
    assert success["credential_revision"] == 1


@pytest.mark.parametrize(
    ("operation_id", "target"),
    [
        ("bilibili.search", {"search_type": "arbitrary"}),
        ("bilibili.user.find", {"user_id": "１２３"}),
        ("bilibili.user.find", {"user_id": "0"}),
        ("bilibili.user.find", {"user_id": "0123"}),
        ("bilibili.video.read", {"video_id": "https://www.bilibili.com/video/BV1234567890"}),
        ("bilibili.dm.read", {"peer_user_id": "123", "message_id": "0"}),
        (
            "bilibili.comment.create",
            {"video_id": "BV1234567890", "root_comment_id": "0", "parent_comment_id": "12"},
        ),
        ("bilibili.dm.send", {"peer_user_id": "123", "cookie": "model supplied"}),
    ],
)
def test_bilibili_provider_targets_are_closed_and_ascii_decimal(
    operation_id: str,
    target: dict[str, Any],
) -> None:
    """Reject URL, credential, Unicode-numeric, and inconsistent comment locators."""

    with pytest.raises(AccountCenterError, match="invalid_request"):
        account_center_plugin_module._safe_provider_target("bilibili", operation_id, target)


def test_bilibili_comment_target_allows_zero_only_for_top_level_comment_refs() -> None:
    """Permit the platform's top-level marker only in root and parent comment IDs."""

    assert account_center_plugin_module._safe_provider_target(
        "bilibili",
        "bilibili.comment.create",
        {
            "video_id": "BV1234567890",
            "root_comment_id": "0",
            "parent_comment_id": "0",
        },
    ) == {
        "video_id": "BV1234567890",
        "root_comment_id": "0",
        "parent_comment_id": "0",
    }


@pytest.mark.parametrize("external_subject", ["0", "0123", "１２３"])
def test_bilibili_external_subject_requires_positive_ascii_mid(
    external_subject: str,
) -> None:
    """Reject noncanonical, zero, and Unicode-digit identities at the service boundary."""

    with pytest.raises(AccountCenterError, match="invalid_request"):
        account_center_plugin_module._safe_bilibili_external_subject(external_subject)


def test_schema_v9_migration_enables_bilibili_without_orphaning_leases(tmp_path: Path) -> None:
    """Upgrade the prior profile constraint and preserve its child lease table."""

    db_path = tmp_path / "accounts.sqlite3"
    ProfileHarness(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version=9")
    store = account_center_plugin_module.AccountStore(db_path)
    with store._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 12
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='provider_profiles'"
        ).fetchone()[0]
        assert "'bilibili'" in sql
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bilibili_link_sessions'"
        ).fetchone() is not None
