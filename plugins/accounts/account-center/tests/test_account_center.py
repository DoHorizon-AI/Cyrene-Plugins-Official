"""Lifecycle and trust-boundary tests for account.center.v1."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from account_center import store as account_store_module
from account_center.github import (
    GitHubIdentityVerificationError,
    OAuthCredential,
    pkce_s256_challenge,
)
from account_center.models import AccountCenterError
from account_center.plugin import (
    REQUEST_TYPE_PREFIX,
    AccountCenterPlugin,
    _idempotency_scope_hash,
)
from account_center.security import canonical_json
from account_center.store import AccountStore
from cyrene_account_center_contracts import sign_context
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, DirectPluginFailure
from cyrene_plugin_runtime.server import load_entrypoint, serve

EXCHANGE = "exchange-service"
CALLBACK = "oauth-callback-service"
WECOM = "wecom-source"
WECOM_SECOND = "wecom-source-secondary"
PROVIDER = "github-provider"
ACTOR = {
    "issuer": "https://platform.example",
    "subject": "user-42",
    "organization_id": "org-a",
    "workspace_id": "workspace-a",
}
RESOURCE = {"repository_id": "123", "repository": "acme/project", "issue_number": 17}
TOOL_OPERATION = "github.issue.comment.create"
TOKEN = "ghu_private_test_token_9b21"


class FakeGitHub:
    """Explicit test-only fake for the official OAuth transport boundary."""

    def __init__(self) -> None:
        self.revoked: list[str] = []
        self.refresh_count = 0
        self.authorization_requests: list[dict[str, str]] = []
        self.exchange_requests: list[dict[str, str]] = []
        self.revoke_error: AccountCenterError | None = None
        self.exchange_error: AccountCenterError | None = None
        self.refresh_error: AccountCenterError | None = None
        self.refresh_started: threading.Event | None = None
        self.refresh_continue: threading.Event | None = None

    def authorization_url(
        self, *, redirect_uri: str, state: str, code_challenge: str
    ) -> str:
        from urllib.parse import urlencode

        request = {
            "client_id": "test-client",
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": "offline_access",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "allow_signup": "false",
        }
        self.authorization_requests.append(request)
        return "https://github.com/login/oauth/authorize?" + urlencode(request)

    def exchange_code(
        self,
        code: str,
        *,
        redirect_uri: str,
        code_verifier: str,
        on_credential: Callable[[OAuthCredential], None] | None = None,
    ) -> tuple[OAuthCredential, str]:
        assert code == "one-time-code"
        if self.exchange_error is not None:
            if (
                isinstance(self.exchange_error, GitHubIdentityVerificationError)
                and on_credential is not None
            ):
                on_credential(self.exchange_error.credential)
            raise self.exchange_error
        request = {
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        self.exchange_requests.append(request)
        assert self.authorization_requests[-1]["code_challenge"] == pkce_s256_challenge(
            code_verifier
        )
        credential = OAuthCredential(TOKEN, "refresh_private_token", 20_000, 30_000)
        if on_credential is not None:
            on_credential(credential)
        return credential, "987654"

    def refresh(self, refresh_token: str) -> OAuthCredential:
        self.refresh_count += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        if self.refresh_started is not None:
            self.refresh_started.set()
        if self.refresh_continue is not None:
            assert self.refresh_continue.wait(timeout=5)
        assert refresh_token == "refresh_private_token"
        return OAuthCredential("ghu_refreshed_token", "refresh_rotated", 30_000, 40_000)

    def revoke(self, access_token: str) -> None:
        if self.revoke_error is not None:
            raise self.revoke_error
        self.revoked.append(access_token)


class Harness:
    """Build test principals and fixed method envelopes around a temp DB."""

    def __init__(self, path: Path, *, now: int = 10_000) -> None:
        self.now = now
        self.sequence = 0
        self.keys = {
            name: (name.encode("ascii") * 32)[:32]
            for name in (EXCHANGE, CALLBACK, WECOM, WECOM_SECOND, PROVIDER)
        }
        self.github = FakeGitHub()
        self.config = {
            "binding_id": "connection-a",
            "storage_path": str(path),
            "encryption_key": b"e" * 32,
            "github_callback_uri": "https://exchange.example/api/v1/accounts/github/callback",
            "github_client_id": "test-client",
            "github_client_secret": "test-secret",
            "exchange_caller_id": EXCHANGE,
            "github_callback_caller_id": CALLBACK,
            "github_provider_caller_id": PROVIDER,
            "caller_keys": self.keys,
            "method_callers": {
                "begin_link": [EXCHANGE],
                "complete_github_link": [CALLBACK],
                "observe_wecom_challenge": [WECOM, WECOM_SECOND],
                "confirm_wecom_link": [EXCHANGE],
                "list_links": [EXCHANGE],
                "get_authorization": [EXCHANGE],
                "unlink": [EXCHANGE],
                "create_credential_lease": [EXCHANGE],
                "redeem_credential_lease": [PROVIDER],
                "resolve_linked_sender": [EXCHANGE],
            },
            "wecom_sources": {
                WECOM: {
                    "source_authority": "wecom:tenant:9",
                    "bot_namespaces": ["bot-main", "bot/main"],
                },
                WECOM_SECOND: {
                    "source_authority": "wecom:tenant:9/bot",
                    "bot_namespaces": ["main"],
                },
            },
        }
        self.plugin = self.make_plugin()

    def make_plugin(self) -> AccountCenterPlugin:
        return AccountCenterPlugin(
            self.config, github_client=self.github, now=lambda: self.now
        )

    def context(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        caller: str = EXCHANGE,
        actor: dict[str, Any] | None = None,
        binding_id: str = "connection-a",
        request_id: str | None = None,
        operation_id: str | None = None,
        issued_at: int | None = None,
        expires_at: int | None = None,
    ) -> dict[str, Any]:
        identity = ACTOR if actor is None else actor
        if request_id is None:
            self.sequence += 1
            request_id = f"00000000-0000-4000-8000-{self.sequence:012d}"
        return sign_context(
            method=method,
            payload=payload,
            caller_id=caller,
            issuer=identity["issuer"],
            subject=identity["subject"],
            organization_id=identity["organization_id"],
            workspace_id=identity["workspace_id"],
            binding_id=binding_id,
            request_id=request_id,
            operation_id=operation_id or method,
            key=self.keys[caller],
            issued_at=self.now if issued_at is None else issued_at,
            expires_at=self.now + 120 if expires_at is None else expires_at,
        ).as_dict()

    def call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        caller: str = EXCHANGE,
        actor: dict[str, Any] | None = None,
        binding_id: str = "connection-a",
        request_id: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        call_payload = dict(payload)
        if method in {"begin_link", "confirm_wecom_link", "unlink"}:
            call_payload.setdefault(
                "idempotency_key",
                request_id or f"test-idempotency-{self.sequence + 1}",
            )
        return self.plugin.call(
            method,
            call_payload,
            self.context(
                method,
                call_payload,
                caller=caller,
                actor=actor,
                binding_id=binding_id,
                request_id=request_id,
                operation_id=operation_id,
            ),
        )

    def link_github(self) -> str:
        started = self.call("begin_link", {"provider": "github"})
        state = parse_qs(urlparse(started["authorization_url"]).query)["state"][0]
        session = self.plugin._store.session(started["link_session_ref"])
        verifier_blob = bytes(session["code_verifier_blob"])
        self.plugin = self.make_plugin()
        completed = self.call(
            "complete_github_link",
            {"state": state, "code": "one-time-code"},
            caller=CALLBACK,
            actor={
                "issuer": "service://exchange/callback",
                "subject": "oauth-callback",
                "organization_id": "service",
                "workspace_id": None,
            },
        )
        assert self.github.exchange_requests[-1]["code_verifier"].encode() not in verifier_blob
        return completed["account_ref"]


def _lease_request(account_ref: str, *, digest: str = "a" * 64) -> dict[str, Any]:
    return {
        "account_ref": account_ref,
        "operation_id": TOOL_OPERATION,
        "resource": RESOURCE,
        "arguments_sha256": digest,
    }


def test_github_callback_csrf_and_bound_owner(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    started = harness.call("begin_link", {"provider": "github"})
    state = parse_qs(urlparse(started["authorization_url"]).query)["state"][0]
    with pytest.raises(AccountCenterError, match="link_session_invalid"):
        harness.call(
            "complete_github_link",
            {"state": "wrong-state", "code": "one-time-code"},
            caller=CALLBACK,
        )
    result = harness.call(
        "complete_github_link",
        {"state": state, "code": "one-time-code"},
        caller=CALLBACK,
    )
    assert result["provider"] == "github"
    with pytest.raises(AccountCenterError, match="link_session_invalid"):
        harness.call(
            "complete_github_link",
            {"state": state, "code": "one-time-code"},
            caller=CALLBACK,
        )


def test_link_metadata_is_encrypted_private_and_survives_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "private" / "accounts.sqlite"
    harness = Harness(db_path)
    account_ref = harness.link_github()
    row = harness.plugin._store.get_account(account_ref, ACTOR_ANCHOR())
    stored = bytes(row["credential_blob"])
    assert TOKEN.encode() not in stored
    assert b"refresh_private_token" not in stored
    assert (db_path.stat().st_mode & 0o777) == 0o600
    assert harness.call("list_links", {}) == {
        "links": [
            {
                "account_ref": account_ref,
                "provider": "github",
                "source_authority": "https://github.com",
                "bot_namespace": None,
                "status": "active",
                "linked_at": 10_000,
                "workspace_id": "workspace-a",
            }
        ]
    }
    harness.plugin = harness.make_plugin()
    assert harness.call("list_links", {})["links"][0]["account_ref"] == account_ref
    wrong = Harness(db_path, now=10_001)
    wrong.config["encryption_key"] = b"x" * 32
    wrong.plugin = wrong.make_plugin()
    with pytest.raises(AccountCenterError, match="credential_unavailable"):
        wrong.plugin._decrypt_credential(account_ref, stored)


def test_trusted_context_tampering_wrong_caller_and_cross_org_fail_closed(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    payload = {"provider": "github"}
    context = harness.context("begin_link", payload)
    with pytest.raises(AccountCenterError, match="untrusted_caller"):
        harness.plugin.call("begin_link", {"provider": "wecom"}, context)
    with pytest.raises(AccountCenterError, match="untrusted_caller"):
        harness.call("list_links", {}, caller=PROVIDER)
    other = {**ACTOR, "organization_id": "org-b"}
    with pytest.raises(AccountCenterError, match="account_not_found"):
        harness.call(
            "get_authorization",
            _lease_request("00000000-0000-4000-8000-000000000001"),
            actor=other,
        )


def test_single_use_lease_is_bound_to_actor_binding_resource_and_unlink(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    account_ref = harness.link_github()
    payload = _lease_request(account_ref)
    lease = harness.call(
        "create_credential_lease",
        payload,
        request_id="00000000-0000-4000-8000-000000000101",
    )
    redeem = {
        key: lease[key] for key in ("operation_id", "resource", "arguments_sha256")
    }
    redeem["lease_ref"] = lease["lease_ref"]
    with pytest.raises(AccountCenterError, match="untrusted_caller"):
        harness.call(
            "redeem_credential_lease",
            redeem,
            caller=PROVIDER,
            binding_id="wrong-binding",
            request_id="00000000-0000-4000-8000-000000000102",
        )
    wrong_digest = {**redeem, "arguments_sha256": "b" * 64}
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call(
            "redeem_credential_lease",
            wrong_digest,
            caller=PROVIDER,
            request_id="00000000-0000-4000-8000-000000000108",
        )
    redeemed = harness.call(
        "redeem_credential_lease",
        redeem,
        caller=PROVIDER,
        request_id="00000000-0000-4000-8000-000000000103",
    )
    assert redeemed["access_token"] == TOKEN
    assert redeemed["external_subject"] == "987654"
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call(
            "redeem_credential_lease",
            redeem,
            caller=PROVIDER,
            request_id="00000000-0000-4000-8000-000000000104",
        )

    next_lease = harness.call(
        "create_credential_lease",
        payload,
        request_id="00000000-0000-4000-8000-000000000105",
    )
    harness.call(
        "unlink",
        {"account_ref": account_ref},
        request_id="00000000-0000-4000-8000-000000000106",
    )
    assert harness.github.revoked == [TOKEN]
    assert (
        harness.plugin._store.get_account(account_ref, ACTOR_ANCHOR())["status"]
        == "unlinked"
    )
    denied = {
        key: next_lease[key] for key in ("operation_id", "resource", "arguments_sha256")
    }
    denied["lease_ref"] = next_lease["lease_ref"]
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call(
            "redeem_credential_lease",
            denied,
            caller=PROVIDER,
            request_id="00000000-0000-4000-8000-000000000107",
        )


def test_expired_lease_and_replayed_context_are_rejected(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    account_ref = harness.link_github()
    lease = harness.call(
        "create_credential_lease",
        _lease_request(account_ref),
        request_id="00000000-0000-4000-8000-000000000201",
    )
    request = {
        key: lease[key] for key in ("operation_id", "resource", "arguments_sha256")
    }
    request["lease_ref"] = lease["lease_ref"]
    ctx = harness.context(
        "redeem_credential_lease",
        request,
        caller=PROVIDER,
        request_id="00000000-0000-4000-8000-000000000202",
    )
    harness.now += 46
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.plugin.call("redeem_credential_lease", request, ctx)

    harness.now -= 46
    list_payload: dict[str, Any] = {}
    replay_context = harness.context(
        "list_links", list_payload, request_id="00000000-0000-4000-8000-000000000203"
    )
    harness.plugin.call("list_links", list_payload, replay_context)
    with pytest.raises(AccountCenterError, match="request_replayed"):
        harness.plugin.call("list_links", list_payload, replay_context)


def test_encrypted_sqlite_backup_and_expired_transient_retention(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    harness.link_github()
    destination = tmp_path / "private-backup" / "accounts.sqlite"
    harness.plugin._store.backup(destination)
    assert (destination.stat().st_mode & 0o777) == 0o600
    assert TOKEN.encode() not in destination.read_bytes()
    harness.plugin._store.prune_expired(now=harness.now + 100_000)
    with harness.plugin._store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM link_sessions").fetchone()[0] == 0


def test_expiring_github_user_token_refreshes_before_provider_use(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    account_ref = harness.link_github()
    harness.now = 19_980
    lease = harness.call(
        "create_credential_lease",
        _lease_request(account_ref),
        request_id="00000000-0000-4000-8000-000000000401",
    )
    request = {
        key: lease[key] for key in ("operation_id", "resource", "arguments_sha256")
    }
    request["lease_ref"] = lease["lease_ref"]
    material = harness.call(
        "redeem_credential_lease",
        request,
        caller=PROVIDER,
        request_id="00000000-0000-4000-8000-000000000402",
    )
    assert harness.github.refresh_count == 1
    assert material["access_token"] == "ghu_refreshed_token"
    row = harness.plugin._store.get_account(account_ref, ACTOR_ANCHOR())
    assert b"ghu_refreshed_token" not in bytes(row["credential_blob"])


def test_duplicate_external_identity_cannot_move_to_another_anchor(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    account_ref = harness.link_github()
    assert account_ref
    started = harness.call(
        "begin_link",
        {"provider": "github"},
        actor={**ACTOR, "subject": "different-user"},
    )
    state = parse_qs(urlparse(started["authorization_url"]).query)["state"][0]
    with pytest.raises(AccountCenterError, match="external_identity_owned"):
        harness.call(
            "complete_github_link",
            {
                "state": state,
                "code": "one-time-code",
            },
            caller=CALLBACK,
        )


def test_wecom_challenge_requires_trusted_source_and_primary_confirmation(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    started = harness.call("begin_link", {"provider": "wecom"})
    challenge_id = "00000000-0000-4000-8000-000000000301"
    observed = {
        "bot_namespace": "bot-main",
        "source_authority": "wecom:tenant:9",
        "stable_subject": "wecom-user-88",
        "challenge_id": challenge_id,
        "challenge_value": started["callback_nonce"],
        "expires_at": harness.now + 90,
    }
    harness.call(
        "observe_wecom_challenge",
        observed,
        caller=WECOM,
        actor={
            "issuer": "service://wecom",
            "subject": "connector",
            "organization_id": "org-a",
            "workspace_id": None,
        },
    )
    assert (
        harness.call(
            "observe_wecom_challenge",
            observed,
            caller=WECOM,
            actor={
                "issuer": "service://wecom",
                "subject": "connector",
                "organization_id": "org-a",
                "workspace_id": None,
            },
        )
        == {"accepted": True, "challenge_id": challenge_id, "expires_at": harness.now + 90}
    )
    with pytest.raises(AccountCenterError, match="challenge_rejected"):
        harness.call(
            "observe_wecom_challenge",
            {**observed, "stable_subject": "different-sender"},
            caller=WECOM,
            actor={
                "issuer": "service://wecom",
                "subject": "connector",
                "organization_id": "org-a",
                "workspace_id": None,
            },
        )
    confirm_request = {
        "link_session_ref": started["link_session_ref"],
        "challenge_id": challenge_id,
        "challenge_value": started["callback_nonce"],
        "idempotency_key": "confirm-wecom-once",
    }
    result = harness.call(
        "confirm_wecom_link",
        confirm_request,
    )
    replay = harness.call("confirm_wecom_link", confirm_request)
    assert replay == result
    assert result["provider"] == "wecom"
    assert result["status"] == "active"
    assert (
        harness.plugin._store.get_account(result["account_ref"], ACTOR_ANCHOR())[
            "credential_blob"
        ]
        is None
    )


def test_begin_link_idempotency_replays_encrypted_original_response(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    payload = {"provider": "github", "idempotency_key": "begin-github-once"}
    first = harness.call(
        "begin_link",
        payload,
        request_id="00000000-0000-4000-8000-000000000831",
    )
    replay = harness.call(
        "begin_link",
        payload,
        request_id="00000000-0000-4000-8000-000000000832",
    )
    assert replay == first
    with pytest.raises(AccountCenterError, match="request_replayed"):
        harness.call(
            "begin_link",
            payload,
            request_id="00000000-0000-4000-8000-000000000831",
        )
    state = parse_qs(urlparse(first["authorization_url"]).query)["state"][0]
    with harness.plugin._store._connect() as db:
        assert db.execute("SELECT count(*) FROM link_sessions").fetchone()[0] == 1
        encrypted = db.execute(
            "SELECT response_blob FROM command_idempotency"
        ).fetchone()[0]
    assert state.encode() not in bytes(encrypted)
    with pytest.raises(AccountCenterError, match="idempotency_conflict"):
        harness.call(
            "begin_link",
            {"provider": "wecom", "idempotency_key": "begin-github-once"},
            request_id="00000000-0000-4000-8000-000000000833",
        )


def test_unlink_idempotency_does_not_repeat_remote_revocation(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    account_ref = harness.link_github()
    payload = {"account_ref": account_ref, "idempotency_key": "unlink-once"}
    first = harness.call(
        "unlink", payload, request_id="00000000-0000-4000-8000-000000000841"
    )
    replay = harness.call(
        "unlink", payload, request_id="00000000-0000-4000-8000-000000000842"
    )
    assert replay == first == {"unlinked": True, "status": "unlinked"}
    assert harness.github.revoked == [TOKEN]


def test_restart_marks_interrupted_remote_unlink_unknown_and_replays_result(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    account_ref = harness.link_github()
    key = "unlink-crashed-after-local-commit"
    body = {"account_ref": account_ref}
    key_hash = _idempotency_scope_hash("unlink", ACTOR_ANCHOR(), key)
    harness.plugin._store.begin_idempotent_command(
        key_hash=key_hash,
        method="unlink",
        payload_sha256=hashlib.sha256(canonical_json(body)).hexdigest(),
        now=harness.now,
    )
    harness.plugin._store.unlink(
        account_ref,
        ACTOR_ANCHOR(),
        now=harness.now,
        command_key_hash=key_hash,
    )

    harness.plugin = harness.make_plugin()
    row = harness.plugin._store.get_account(account_ref, ACTOR_ANCHOR())
    assert row["remote_revocation_status"] == "unknown"
    assert row["remote_revocation_command_hash"] is None
    result = harness.call(
        "unlink",
        {**body, "idempotency_key": key},
        request_id="00000000-0000-4000-8000-000000000851",
    )
    assert result == {"unlinked": True, "status": "revocation_unknown"}
    assert harness.github.revoked == []


def test_restart_terminalizes_incomplete_command_reservation(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    key = "begin-reserved-before-crash"
    body = {"provider": "wecom"}
    key_hash = _idempotency_scope_hash("begin_link", ACTOR_ANCHOR(), key)
    harness.plugin._store.begin_idempotent_command(
        key_hash=key_hash,
        method="begin_link",
        payload_sha256=hashlib.sha256(canonical_json(body)).hexdigest(),
        now=harness.now,
    )

    harness.plugin = harness.make_plugin()

    with pytest.raises(AccountCenterError, match="idempotency_result_unknown"):
        harness.call(
            "begin_link",
            {**body, "idempotency_key": key},
            request_id="00000000-0000-4000-8000-000000000852",
        )
    with harness.plugin._store._connect() as db:
        assert db.execute("SELECT count(*) FROM link_sessions").fetchone()[0] == 0


def test_wecom_authority_and_bot_namespace_are_stored_as_distinct_identity_parts(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    sender = "same-wecom-user"
    cases = (
        (WECOM, "wecom:tenant:9", "bot/main", "00000000-0000-4000-8000-000000000411"),
        (WECOM_SECOND, "wecom:tenant:9/bot", "main", "00000000-0000-4000-8000-000000000412"),
    )
    linked_refs: list[str] = []
    for caller, authority, bot, challenge_id in cases:
        started = harness.call("begin_link", {"provider": "wecom"})
        challenge = {
            "bot_namespace": bot,
            "source_authority": authority,
            "stable_subject": sender,
            "challenge_id": challenge_id,
            "challenge_value": started["callback_nonce"],
            "expires_at": harness.now + 90,
        }
        harness.call(
            "observe_wecom_challenge",
            challenge,
            caller=caller,
            actor={
                "issuer": "service://wecom",
                "subject": caller,
                "organization_id": ACTOR["organization_id"],
                "workspace_id": None,
            },
        )
        linked = harness.call(
            "confirm_wecom_link",
            {
                "link_session_ref": started["link_session_ref"],
                "challenge_id": challenge_id,
                "challenge_value": started["callback_nonce"],
            },
        )
        linked_refs.append(linked["account_ref"])

    assert linked_refs[0] != linked_refs[1]
    first = harness.call(
        "resolve_linked_sender",
        {
            "source_authority": "wecom:tenant:9",
            "bot_namespace": "bot/main",
            "stable_subject": sender,
        },
    )
    second = harness.call(
        "resolve_linked_sender",
        {
            "source_authority": "wecom:tenant:9/bot",
            "bot_namespace": "main",
            "stable_subject": sender,
        },
    )
    assert first["account_ref"] == linked_refs[0]
    assert second["account_ref"] == linked_refs[1]
    listed = harness.call("list_links", {})["links"]
    assert {
        (item["source_authority"], item["bot_namespace"])
        for item in listed
    } == {
        ("wecom:tenant:9", "bot/main"),
        ("wecom:tenant:9/bot", "main"),
    }
    listed = harness.call("list_links", {})["links"]
    assert {
        (item["source_authority"], item["bot_namespace"])
        for item in listed
    } == {
        ("wecom:tenant:9", "bot/main"),
        ("wecom:tenant:9/bot", "main"),
    }


def test_wecom_confirmation_rolls_back_challenge_and_session_on_transaction_failure(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    started = harness.call("begin_link", {"provider": "wecom"})
    challenge_id = "00000000-0000-4000-8000-000000000461"
    observed = {
        "bot_namespace": "bot-main",
        "source_authority": "wecom:tenant:9",
        "stable_subject": "transactional-sender",
        "challenge_id": challenge_id,
        "challenge_value": started["callback_nonce"],
        "expires_at": harness.now + 90,
    }
    harness.call(
        "observe_wecom_challenge",
        observed,
        caller=WECOM,
        actor={
            "issuer": "service://wecom",
            "subject": "connector",
            "organization_id": ACTOR["organization_id"],
            "workspace_id": None,
        },
    )

    with pytest.raises(AccountCenterError, match="idempotency_state_invalid"):
        harness.plugin._store.complete_wecom_link(
            session_ref=started["link_session_ref"],
            challenge_id=challenge_id,
            anchor=ACTOR_ANCHOR(),
            expected_value_digest=hashlib.sha256(
                started["callback_nonce"].encode()
            ).hexdigest(),
            account_ref="00000000-0000-4000-8000-000000000462",
            now=harness.now,
            command_result=("missing-reservation", b"invalid-result"),
        )
    session = harness.plugin._store.session(started["link_session_ref"])
    with harness.plugin._store._connect() as db:
        challenge = db.execute(
            "SELECT consumed_at FROM wecom_challenges WHERE challenge_id=?",
            (challenge_id,),
        ).fetchone()
    assert session["status"] == "pending"
    assert challenge["consumed_at"] is None
    assert harness.call("list_links", {})["links"] == []

    result = harness.call(
        "confirm_wecom_link",
        {
            "link_session_ref": started["link_session_ref"],
            "challenge_id": challenge_id,
            "challenge_value": started["callback_nonce"],
        },
    )
    assert result["status"] == "active"


def test_method_caller_configuration_cannot_expand_fixed_roles(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    harness.config["method_callers"]["begin_link"] = [CALLBACK]
    with pytest.raises(AccountCenterError, match="configuration_unavailable"):
        harness.make_plugin()


def test_unlink_records_unknown_remote_revocation_after_local_unlink(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    account_ref = harness.link_github()
    harness.github.revoke_error = AccountCenterError("provider_unavailable")

    result = harness.call("unlink", {"account_ref": account_ref})

    assert result == {"unlinked": True, "status": "revocation_unknown"}
    row = harness.plugin._store.get_account(account_ref, ACTOR_ANCHOR())
    assert row["status"] == "unlinked"
    assert row["remote_revocation_status"] == "unknown"
    assert row["credential_blob"] is None
    assert harness.call("list_links", {})["links"][0]["status"] == "revocation_unknown"


def test_unlink_with_unreadable_ciphertext_still_clears_local_access(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    account_ref = harness.link_github()
    lease = harness.call(
        "create_credential_lease",
        _lease_request(account_ref),
        request_id="00000000-0000-4000-8000-000000000617",
    )
    with harness.plugin._store.transaction() as db:
        db.execute(
            "UPDATE accounts SET credential_blob=? WHERE account_ref=?",
            (b"corrupted-ciphertext", account_ref),
        )

    result = harness.call("unlink", {"account_ref": account_ref})

    assert result == {"unlinked": True, "status": "revocation_unknown"}
    account = harness.plugin._store.get_account(account_ref, ACTOR_ANCHOR())
    assert account["status"] == "unlinked"
    assert account["credential_blob"] is None
    assert account["remote_revocation_status"] == "unknown"
    with harness.plugin._store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM credential_leases WHERE account_ref=?", (account_ref,)
        ).fetchone()[0] == 0
    request = {
        key: lease[key]
        for key in ("lease_ref", "operation_id", "resource", "arguments_sha256")
    }
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call("redeem_credential_lease", request, caller=PROVIDER)
    assert harness.github.revoked == []


def test_failed_github_identity_verification_revokes_and_consumes_callback(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    credential = OAuthCredential(TOKEN, "refresh_private_token", 20_000, 30_000)
    harness.github.exchange_error = GitHubIdentityVerificationError(credential)
    harness.github.revoke_error = AccountCenterError("provider_unavailable")
    started = harness.call("begin_link", {"provider": "github"})
    state = parse_qs(urlparse(started["authorization_url"]).query)["state"][0]

    with pytest.raises(AccountCenterError, match="identity_verification_failed"):
        harness.call(
            "complete_github_link",
            {"state": state, "code": "one-time-code"},
            caller=CALLBACK,
        )
    session = harness.plugin._store.session(started["link_session_ref"])
    assert session["status"] == "failed"
    assert session["failure_revocation_status"] == "unknown"
    assert session["pending_credential_blob"] is None
    assert harness.call("list_links", {})["links"] == []
    with pytest.raises(AccountCenterError, match="link_session_invalid"):
        harness.call(
            "complete_github_link",
            {"state": state, "code": "one-time-code"},
            caller=CALLBACK,
        )


def test_interrupted_callback_recovery_revokes_encrypted_pending_token(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    started = harness.call("begin_link", {"provider": "github"})
    state = parse_qs(urlparse(started["authorization_url"]).query)["state"][0]
    harness.plugin._store.claim_session_by_state(
        hashlib.sha256(state.encode()).hexdigest(),
        harness.config["github_callback_uri"],
        now=harness.now,
    )
    credential = OAuthCredential(TOKEN, "refresh_private_token", 20_000, 30_000)
    harness.plugin._persist_callback_credential(
        started["link_session_ref"], credential
    )

    harness.plugin = harness.make_plugin()

    session = harness.plugin._store.session(started["link_session_ref"])
    assert session["status"] == "failed"
    assert session["failure_revocation_status"] == "revoked"
    assert session["pending_credential_blob"] is None
    assert harness.github.revoked == [TOKEN]
    assert harness.call("list_links", {})["links"] == []


def test_concurrent_expired_leases_rotate_github_refresh_token_once(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite", now=20_000)
    account_ref = harness.link_github()
    harness.github.refresh_started = threading.Event()
    harness.github.refresh_continue = threading.Event()
    lease_requests = []
    for request_id in (
        "00000000-0000-4000-8000-000000000701",
        "00000000-0000-4000-8000-000000000702",
    ):
        lease = harness.call(
            "create_credential_lease",
            _lease_request(account_ref),
            request_id=request_id,
        )
        lease_requests.append(
            {
                key: lease[key]
                for key in ("lease_ref", "operation_id", "resource", "arguments_sha256")
            }
        )
    contexts = [
        harness.context(
            "redeem_credential_lease",
            request,
            caller=PROVIDER,
            request_id=f"00000000-0000-4000-8000-{703 + index:012d}",
        )
        for index, request in enumerate(lease_requests)
    ]

    def redeem(index: int) -> dict[str, Any]:
        return harness.plugin.call(
            "redeem_credential_lease", lease_requests[index], contexts[index]
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(redeem, 0)
        assert harness.github.refresh_started.wait(timeout=3)
        second = pool.submit(redeem, 1)
        time.sleep(0.05)
        harness.github.refresh_continue.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert harness.github.refresh_count == 1
    assert {item["access_token"] for item in results} == {"ghu_refreshed_token"}


def test_interrupted_refresh_claim_requires_relink_after_restart(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "accounts.sqlite", now=20_000)
    account_ref = harness.link_github()
    lease = harness.call(
        "create_credential_lease",
        _lease_request(account_ref),
        request_id="00000000-0000-4000-8000-000000000711",
    )
    harness.plugin._store.claim_refresh(
        account_ref, ACTOR_ANCHOR(), "00000000-0000-4000-8000-000000000712", now=harness.now
    )

    harness.plugin = harness.make_plugin()
    row = harness.plugin._store.get_account(account_ref, ACTOR_ANCHOR())
    assert row["credential_status"] == "reauthorization_required"
    assert row["credential_blob"] is None
    with pytest.raises(AccountCenterError, match="lease_rejected"):
        harness.call(
            "redeem_credential_lease",
            {key: lease[key] for key in ("lease_ref", "operation_id", "resource", "arguments_sha256")},
            caller=PROVIDER,
            request_id="00000000-0000-4000-8000-000000000713",
        )
    assert harness.github.refresh_count == 0


@pytest.mark.parametrize(
    ("provider_error", "expected_error"),
    [
        ("provider_unavailable", "credential_refresh_unknown"),
        ("provider_rejected", "credential_refresh_rejected"),
    ],
)
def test_refresh_rotation_failure_is_classified_and_stale_token_is_discarded(
    tmp_path: Path, provider_error: str, expected_error: str
) -> None:
    harness = Harness(tmp_path / f"{provider_error}.sqlite", now=20_000)
    account_ref = harness.link_github()
    lease = harness.call(
        "create_credential_lease",
        _lease_request(account_ref),
        request_id="00000000-0000-4000-8000-000000000714",
    )
    request = {
        key: lease[key]
        for key in ("lease_ref", "operation_id", "resource", "arguments_sha256")
    }
    context = harness.context(
        "redeem_credential_lease",
        request,
        caller=PROVIDER,
        request_id="00000000-0000-4000-8000-000000000715",
    )
    harness.github.refresh_error = AccountCenterError(provider_error)

    with pytest.raises(AccountCenterError, match=expected_error):
        harness.plugin.call("redeem_credential_lease", request, context)
    row = harness.plugin._store.get_account(account_ref, ACTOR_ANCHOR())
    assert row["credential_status"] == "reauthorization_required"
    assert row["credential_blob"] is None
    assert harness.github.refresh_count == 1


def test_refresh_commit_cas_rejects_owner_change(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "accounts.sqlite", now=20_000)
    account_ref = harness.link_github()
    claim_id = "00000000-0000-4000-8000-000000000721"
    revision = harness.plugin._store.claim_refresh(
        account_ref, ACTOR_ANCHOR(), claim_id, now=harness.now
    )
    with harness.plugin._store.transaction() as db:
        db.execute(
            "UPDATE accounts SET anchor_subject='changed-owner' WHERE account_ref=?",
            (account_ref,),
        )
    with pytest.raises(AccountCenterError, match="credential_refresh_claim_lost"):
        harness.plugin._store.finish_refresh(
            account_ref,
            ACTOR_ANCHOR(),
            claim_id,
            revision,
            b"encrypted-new-token",
            30_000,
            40_000,
        )


def test_schema_v1_migration_preserves_legacy_data_and_fails_closed_on_wecom_tuple(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "accounts-v1.sqlite"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE accounts (
            account_ref TEXT PRIMARY KEY, anchor_issuer TEXT NOT NULL,
            anchor_subject TEXT NOT NULL, organization_id TEXT NOT NULL,
            workspace_id TEXT, provider TEXT NOT NULL, source_authority TEXT NOT NULL,
            stable_subject TEXT NOT NULL, metadata_json TEXT NOT NULL,
            credential_blob BLOB, credential_expires_at INTEGER,
            refresh_expires_at INTEGER, status TEXT NOT NULL,
            linked_at INTEGER NOT NULL, unlinked_at INTEGER
        );
        CREATE UNIQUE INDEX account_external_identity
          ON accounts(provider,source_authority,stable_subject,organization_id);
        CREATE INDEX account_owner_scope
          ON accounts(anchor_issuer,anchor_subject,organization_id,workspace_id,status);
        CREATE TABLE link_sessions (
            link_session_ref TEXT PRIMARY KEY, anchor_issuer TEXT NOT NULL,
            anchor_subject TEXT NOT NULL, organization_id TEXT NOT NULL,
            workspace_id TEXT, provider TEXT NOT NULL, state_digest TEXT NOT NULL,
            nonce_digest TEXT NOT NULL, callback_uri TEXT, expires_at INTEGER NOT NULL,
            status TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE wecom_challenges (
            challenge_id TEXT PRIMARY KEY, bot_namespace TEXT NOT NULL,
            source_authority TEXT NOT NULL, stable_subject TEXT NOT NULL,
            organization_id TEXT NOT NULL, value_digest TEXT NOT NULL,
            expires_at INTEGER NOT NULL, consumed_at INTEGER
        );
        CREATE TABLE credential_leases (
            lease_ref TEXT PRIMARY KEY, account_ref TEXT NOT NULL REFERENCES accounts(account_ref),
            anchor_issuer TEXT NOT NULL, anchor_subject TEXT NOT NULL,
            organization_id TEXT NOT NULL, workspace_id TEXT, binding_id TEXT NOT NULL,
            provider_caller_id TEXT NOT NULL, operation_id TEXT NOT NULL,
            resource_json TEXT NOT NULL, arguments_sha256 TEXT NOT NULL,
            expires_at INTEGER NOT NULL, redeemed_at INTEGER
        );
        CREATE INDEX credential_lease_expiry ON credential_leases(expires_at);
        CREATE TABLE consumed_requests(request_id TEXT PRIMARY KEY, expires_at INTEGER NOT NULL);
        PRAGMA user_version=1;
        """
    )
    connection.execute(
        "INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "00000000-0000-4000-8000-000000000801",
            ACTOR["issuer"],
            ACTOR["subject"],
            ACTOR["organization_id"],
            ACTOR["workspace_id"],
            "wecom",
            "wecom:tenant:9/bot/main",
            "legacy-sender",
            "{}",
            None,
            None,
            None,
            "active",
            9_000,
            None,
        ),
    )
    connection.commit()
    connection.close()

    store = AccountStore(db_path)

    with store._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    links = store.list_accounts(ACTOR_ANCHOR(), ACTOR["workspace_id"])
    assert links[0]["source_authority"] == "wecom:tenant:9/bot/main"
    assert links[0]["status"] == "reauthorization_required"
    assert (
        store.resolve_linked_sender(
            source_authority="wecom:tenant:9",
            bot_namespace="bot/main",
            stable_subject="legacy-sender",
            organization_id=ACTOR["organization_id"],
        )
        is None
    )


def test_fresh_schema_ddl_rolls_back_as_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "interrupted-initialization.sqlite"
    original_connect = sqlite3.connect

    class FailingSchemaConnection(sqlite3.Connection):
        def executescript(self, script: str) -> sqlite3.Cursor:
            broken = script.replace(
                "PRAGMA user_version = 4;",
                "SELECT * FROM missing_schema_failure_probe;\nPRAGMA user_version = 4;",
                1,
            )
            return super().executescript(broken)

    def failing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original_connect(*args, **kwargs, factory=FailingSchemaConnection)

    monkeypatch.setattr(account_store_module.sqlite3, "connect", failing_connect)
    with pytest.raises(AccountCenterError, match="storage_unavailable"):
        AccountStore(db_path)
    monkeypatch.undo()

    with original_connect(db_path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "accounts" not in tables
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0

    store = AccountStore(db_path)
    with store._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4


def test_direct_runtime_rejects_duplicate_json_keys_and_wrong_type_url(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    success, _ = harness.plugin.on_invoke(
        "account.center.v1",
        "begin_link",
        b'{"payload":{},"payload":{},"trusted_context":{}}',
        request_type_url=REQUEST_TYPE_PREFIX + "BeginLinkRequest",
    )
    assert success is False


def test_direct_runtime_returns_fixed_typed_method_payload(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "accounts.sqlite")
    request = {"provider": "wecom", "idempotency_key": "direct-runtime-begin"}
    context = harness.context("begin_link", request)
    envelope = canonical_json({"payload": request, "trusted_context": context})
    success, result = harness.plugin.on_invoke(
        "account.center.v1",
        "begin_link",
        envelope,
        request_type_url=REQUEST_TYPE_PREFIX + "BeginLinkRequest",
    )
    assert success is True
    assert result.type_url == REQUEST_TYPE_PREFIX + "BeginLinkResponse"
    decoded = json.loads(result.value)
    assert set(decoded) == {"link_session_ref", "callback_nonce", "expires_at"}
    success, _ = harness.plugin.on_invoke(
        "account.center.v1",
        "begin_link",
        b"{}",
        request_type_url="type.googleapis.com/other.Request",
    )
    assert success is False


def test_no_argument_entrypoint_uses_standard_configuration_over_real_grpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise standard host configuration and the actual DirectPluginRuntime."""

    caller_refs = {
        EXCHANGE: "account-center/caller-hmac-keys/exchange",
        CALLBACK: "account-center/caller-hmac-keys/callback",
        WECOM: "account-center/caller-hmac-keys/wecom",
        PROVIDER: "account-center/caller-hmac-keys/github-provider",
    }
    secret_refs = {
        "account-center/encryption-key": "TEST_ACCOUNT_CENTER_ENCRYPTION_KEY",
        "github-app/client-secret": "TEST_GITHUB_APP_CLIENT_SECRET",
        **{
            reference: f"TEST_ACCOUNT_CENTER_CALLER_{index}"
            for index, reference in enumerate(caller_refs.values())
        },
    }
    for index, env_name in enumerate(secret_refs.values()):
        value = "test-github-app-secret" if "GITHUB_APP" in env_name else bytes([index + 1]) * 32
        monkeypatch.setenv(
            env_name, value if isinstance(value, str) else value.hex()
        )

    method_callers = {
        method: [EXCHANGE]
        for method in (
            "begin_link",
            "confirm_wecom_link",
            "list_links",
            "get_authorization",
            "unlink",
            "create_credential_lease",
            "resolve_linked_sender",
        )
    }
    method_callers.update(
        {
            "complete_github_link": [CALLBACK],
            "observe_wecom_challenge": [WECOM],
            "redeem_credential_lease": [PROVIDER],
        }
    )
    configuration = {
        "github_callback_uri": "https://exchange.example/api/v1/accounts/github/callback",
        "github_client_id": "test-client-id",
        "github_client_secret_ref": "github-app/client-secret",
        "encryption_key_ref": "account-center/encryption-key",
        "caller_key_refs": caller_refs,
        "hostSecretRefs": secret_refs,
        "exchange_caller_id": EXCHANGE,
        "github_callback_caller_id": CALLBACK,
        "github_provider_caller_id": PROVIDER,
        "method_callers": method_callers,
        "wecom_sources": {
            WECOM: {
                "source_authority": "wecom:tenant:9",
                "bot_namespaces": ["bot-main"],
            }
        },
    }
    os.chmod(tmp_path, 0o700)
    monkeypatch.setenv("CYRENE_PLUGIN_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", "account-center-binding")
    monkeypatch.setenv(
        "CYRENE_CAPABILITY_CONFIGURATION_JSON", json.dumps(configuration)
    )
    plugin = load_entrypoint("account_center.plugin:AccountCenterPlugin")
    server, connection_ref = serve(
        plugin, "account.center.v1", "1", "127.0.0.1:0"
    )
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    try:
        assert plugin._store.path == tmp_path / "accounts.sqlite3"
        request = {"provider": "wecom", "idempotency_key": "grpc-begin-1"}
        issued_at = int(time.time())
        signed = sign_context(
            method="begin_link",
            payload=request,
            caller_id=EXCHANGE,
            issuer=ACTOR["issuer"],
            subject=ACTOR["subject"],
            organization_id=ACTOR["organization_id"],
            workspace_id=ACTOR["workspace_id"],
            binding_id="account-center-binding",
            request_id="00000000-0000-4000-8000-000000000901",
            operation_id="begin_link",
            key=bytes.fromhex(
                os.environ[secret_refs[caller_refs[EXCHANGE]]]
            ),
            issued_at=issued_at,
            expires_at=issued_at + 120,
        )
        response = client.invoke(
            capability="account.center.v1",
            interface_version="1",
            method="begin_link",
            request=DirectPayload(
                type_url=REQUEST_TYPE_PREFIX + "BeginLinkRequest",
                value=canonical_json(
                    {"payload": request, "trusted_context": signed.as_dict()}
                ),
            ),
            deadline_seconds=5,
        )
        assert response.type_url == REQUEST_TYPE_PREFIX + "BeginLinkResponse"
        assert set(json.loads(response.value)) == {
            "link_session_ref",
            "callback_nonce",
            "expires_at",
        }
        wrong_binding_context = sign_context(
            method="list_links",
            payload={},
            caller_id=EXCHANGE,
            issuer=ACTOR["issuer"],
            subject=ACTOR["subject"],
            organization_id=ACTOR["organization_id"],
            workspace_id=ACTOR["workspace_id"],
            binding_id="another-account-center-binding",
            request_id="00000000-0000-4000-8000-000000000902",
            operation_id="list_links",
            key=bytes.fromhex(os.environ[secret_refs[caller_refs[EXCHANGE]]]),
            issued_at=issued_at,
            expires_at=issued_at + 120,
        )
        with pytest.raises(DirectPluginFailure):
            client.invoke(
                capability="account.center.v1",
                interface_version="1",
                method="list_links",
                request=DirectPayload(
                    type_url=REQUEST_TYPE_PREFIX + "ListLinksRequest",
                    value=canonical_json(
                        {"payload": {}, "trusted_context": wrong_binding_context.as_dict()}
                    ),
                ),
                deadline_seconds=5,
            )
        assert plugin._binding_id == "account-center-binding"
    finally:
        client.close()
        server.stop(grace=0).wait(timeout=5)


def ACTOR_ANCHOR() -> Any:
    """Construct the primary test owner without exposing a fake provider identity."""

    from account_center.models import Anchor

    return Anchor(
        ACTOR["issuer"],
        ACTOR["subject"],
        ACTOR["organization_id"],
        ACTOR["workspace_id"],
    )
