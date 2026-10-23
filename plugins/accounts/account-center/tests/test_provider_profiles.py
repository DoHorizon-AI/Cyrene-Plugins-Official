"""Owner-scoped provider identity and exact lease regression coverage.

The fakes keep all authorization tests local and deterministic. 中文：仅用本地桩验证身份边界。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest
from account_center import plugin as account_center_plugin_module
from account_center.github_app import GitHubAppError
from account_center.huggingface_auth import (
    HuggingFaceAuthError,
    HuggingFaceIdentityClient,
)
from account_center.models import AccountCenterError
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
GITHUB_REPOSITORIES = [
    {"repository_id": "123", "owner": "acme", "name": "project"}
]
GITHUB_TARGET = {"repository_id": "123", "repository": "acme/project"}


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
    }

    def whoami(self, token: str) -> str:
        if token not in self.identities:
            raise HuggingFaceAuthError("credential_rejected")
        return self.identities[token]


class ProfileHarness:
    """Build a production Account Center with deterministic provider clients."""

    def __init__(self, path: Path) -> None:
        self.now = 10_000
        self.sequence = 0
        self.keys = {
            caller: (caller.encode("ascii") * 32)[:32]
            for caller in (EXCHANGE, GITHUB_PROVIDER, HF_PROVIDER)
        }
        self.github_app = FakeGitHubApp(lambda: self.now)
        self.huggingface = FakeHuggingFace()
        self.config = {
            "binding_id": "account-center-binding",
            "storage_path": str(path),
            "encryption_key": b"e" * 32,
            "exchange_caller_id": EXCHANGE,
            "github_provider_caller_id": GITHUB_PROVIDER,
            "huggingface_provider_caller_id": HF_PROVIDER,
            "caller_keys": self.keys,
            "provider_secrets": {
                "huggingface/fine-grained-token": "hf_token_original",
                "huggingface/rotated-token": "hf_token_rotated",
                "huggingface/wrong-identity-token": "hf_token_other",
            },
            "wecom_sources": {},
        }
        self.plugin = AccountCenterPlugin(
            self.config,
            github_app_client=self.github_app,
            huggingface_client=self.huggingface,
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


def _digest(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(arguments)).hexdigest()


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


def test_github_installation_cannot_be_claimed_by_a_second_owner(tmp_path: Path) -> None:
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
            {"repo_type": "model", "repo_id": "derek/model-a", "permissions": ["read", "write"]}
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


def test_cloud_profile_registry_stores_only_host_identity_metadata(tmp_path: Path) -> None:
    harness = ProfileHarness(tmp_path / "accounts.sqlite3")
    result = harness.call(
        "register_host_identity_profile",
        {
            "profile_ref": "00000000-0000-4000-8000-000000000303",
            "provider": "azure",
            "identity_ref": "managed-identity://navigator",
            "target_scope": {"subscription_id": "sub-1", "resource_groups": ["rg-cyrene"]},
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
    assert resolved["provider_secrets"] == {
        "huggingface/fine-grained-token": "host-secret-value"
    }


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
    } <= optional


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "credential_rejected"), (403, "provider_permission_denied"), (429, "provider_rate_limited")],
)
def test_huggingface_identity_failures_are_structured_and_redacted(
    status: int, expected: str
) -> None:
    def transport(method: str, host: str, path: str, headers: dict[str, str]) -> Any:
        assert method == "GET"
        assert host == "huggingface.co"
        assert path == "/api/whoami-v2"
        assert headers["Authorization"] == "Bearer hf_private_value"
        return type("Response", (), {"status": status, "headers": {}, "body": b"private upstream body"})()

    client = HuggingFaceIdentityClient(transport=transport)
    with pytest.raises(HuggingFaceAuthError, match=expected) as caught:
        client.whoami("hf_private_value")
    assert "private upstream body" not in str(caught.value)
    assert "hf_private_value" not in str(caught.value)
