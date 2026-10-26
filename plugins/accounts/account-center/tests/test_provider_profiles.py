"""Owner-scoped provider identity and exact lease regression coverage.

The fakes keep all authorization tests local and deterministic. 中文：仅用本地桩验证身份边界。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

import pytest
from account_center import plugin as account_center_plugin_module
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
from account_center.models import AccountCenterError, Anchor
from account_center.plugin import AccountCenterPlugin
from cyrene_account_center_contracts import canonical_json, sign_context

EXCHANGE = "exchange-service"
GITHUB_PROVIDER = "github-provider"
HF_PROVIDER = "huggingface-provider"
ACTOR = {
    "issuer": "https://platform.example",
    "subject": "user-derek",
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

    def create_installation_token(
        self, installation_id: str, repository_ids: list[str]
    ) -> Any:
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


class ProfileHarness:
    """Build a production Account Center with deterministic provider clients."""

    def __init__(
        self,
        path: Path,
        *,
        oauth_seed: dict[str, Any] | None = None,
        workspace_operator_profile_refs: list[str] | None = None,
        oauth_client: FakeHuggingFaceOAuth | None = None,
    ) -> None:
        self.now = 10_000
        self.sequence = 0
        self.keys = {
            caller: (caller.encode("ascii") * 32)[:32]
            for caller in (EXCHANGE, GITHUB_PROVIDER, HF_PROVIDER)
        }
        self.github_app = FakeGitHubApp(lambda: self.now)
        self.huggingface = FakeHuggingFace()
        self.huggingface_oauth = oauth_client or FakeHuggingFaceOAuth()
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
        if workspace_operator_profile_refs is not None:
            self.config["workspace_operator_profile_refs"] = (
                workspace_operator_profile_refs
            )
        self.plugin = self._new_plugin()

    def _new_plugin(self) -> AccountCenterPlugin:
        return AccountCenterPlugin(
            self.config,
            github_app_client=self.github_app,
            huggingface_client=self.huggingface,
            huggingface_oauth_client=self.huggingface_oauth,
            now=lambda: self.now,
        )

    def call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        caller: str = EXCHANGE,
        actor: Mapping[str, str | None] | None = None,
    ) -> dict[str, Any]:
        request = dict(payload)
        if method in {
            "link_github_app_installation",
            "link_huggingface_profile",
            "rotate_huggingface_profile",
            "register_host_identity_profile",
            "unlink_provider_profile",
        }:
            request.setdefault("idempotency_key", f"idem-{method}-{self.sequence + 1}")
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
            binding_id="account-center-binding",
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
            binding_id="account-center-binding",
            request_id=f"00000000-0000-4000-8000-{self.sequence:012d}",
            operation_id=method,
            key=self.keys[caller],
            issued_at=self.now,
            expires_at=self.now + 120,
        )
        envelope = json.dumps(
            {"payload": request, "trusted_context": context.as_dict()}
        ).encode("utf-8")
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
    return {
        "repositories": [
            {"repo_type": "model", "repo_id": repo_id, "permissions": ["read"]}
        ]
    }


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
    material = harness.call(
        "redeem_provider_credential_lease", redeem, caller=GITHUB_PROVIDER
    )
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


@pytest.mark.parametrize(
    ("operation", "target", "arguments"), GITHUB_TYPED_TARGET_CASES
)
def test_typed_github_app_leases_bind_exact_locator_digest_and_one_use(
    tmp_path: Path,
    operation: str,
    target: dict[str, Any],
    arguments: dict[str, Any],
) -> None:
    harness = ProfileHarness(tmp_path / f"{operation.replace('.', '-')}.sqlite3")
    profile_ref = _github_profile(harness)
    allowed_operations = account_center_plugin_module.PROVIDER_OPERATIONS_BY_PROVIDER[
        "github"
    ]
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

    material = harness.call(
        "redeem_provider_credential_lease", redemption, caller=GITHUB_PROVIDER
    )
    assert material["access_token"].startswith("installation-token-")
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call(
            "redeem_provider_credential_lease", redemption, caller=GITHUB_PROVIDER
        )


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
    result = harness.call(
        "redeem_provider_credential_lease", redemption, caller=HF_PROVIDER
    )
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
    lease = harness.invoke_runtime(
        "create_provider_credential_lease", authorized, actor=operator
    )
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
    wire_grant_ref = (
        "sha256:1d7617748443c2d6dab3bb08835c49659a875aeac010314dddc24308aa3d60f1"
    )
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
        elif reference.endswith(
            ("/exchange", "/github-provider", "/huggingface-provider")
        ):
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
    assert resolved["provider_secrets"] == {
        "huggingface/fine-grained-token": "host-secret-value"
    }


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
        if reference.endswith(
            ("/exchange", "/github-provider", "/huggingface-provider")
        ):
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

    assert resolved["provider_secrets"] == {
        "huggingface/connector-token": oauth_seed_text
    }
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


def test_huggingface_oauth_refresh_uses_fixed_endpoint_and_preserves_actual_scope() -> (
    None
):
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


def test_huggingface_device_oauth_uses_official_fixed_endpoints_and_poll_grant() -> (
    None
):
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
        "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code"
        in captured[1]["body"]
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
