"""Private SQLite persistence for Account Center links, sessions, and leases.

Credential columns contain only authenticated ciphertext. 中文：凭据列仅保存认证密文。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import AccountCenterError, Anchor

SCHEMA_VERSION = 12
IDEMPOTENCY_RETENTION_SECONDS = 86_400


class AccountStore:
    """Own a private, versioned SQLite ledger for external account bindings."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._instance_id = uuid.uuid4().hex
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                raise AccountCenterError("storage_unavailable") from None
        self._initialize()
        self._recover_interrupted_operations()

    def _connect(self, *, foreign_keys: bool = True) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute(
                "PRAGMA foreign_keys = ON"
                if foreign_keys
                else "PRAGMA foreign_keys = OFF"
            )
            connection.execute("PRAGMA busy_timeout = 5000")
            if str(self.path) != ":memory:":
                connection.execute("PRAGMA journal_mode = WAL")
            return connection
        except sqlite3.Error:
            raise AccountCenterError("storage_unavailable") from None

    @contextmanager
    def transaction(self, *, foreign_keys: bool = True) -> Iterator[sqlite3.Connection]:
        """Run an immediate transaction to serialize one-use lease changes."""

        connection = self._connect(foreign_keys=foreign_keys)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
            self._protect_files()
        except sqlite3.Error:
            connection.rollback()
            raise AccountCenterError("storage_unavailable") from None
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.transaction(foreign_keys=False) as db:
            current = int(db.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise AccountCenterError("storage_version_unsupported")
            if current == 0:
                db.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE accounts (
                        account_ref TEXT PRIMARY KEY,
                        anchor_issuer TEXT NOT NULL,
                        anchor_subject TEXT NOT NULL,
                        organization_id TEXT NOT NULL,
                        workspace_id TEXT,
                        provider TEXT NOT NULL CHECK(provider IN ('github','wecom')),
                        source_authority TEXT NOT NULL,
                        bot_namespace TEXT,
                        stable_subject TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        credential_blob BLOB,
                        credential_expires_at INTEGER,
                        refresh_expires_at INTEGER,
                        status TEXT NOT NULL CHECK(status IN ('active','unlinked','revocation_pending','revocation_failed')),
                        remote_revocation_status TEXT,
                        remote_revocation_owner TEXT,
                        remote_revocation_command_hash TEXT,
                        credential_status TEXT NOT NULL DEFAULT 'ready',
                        credential_revision INTEGER NOT NULL DEFAULT 0,
                        refresh_claim_id TEXT,
                        refresh_claim_owner TEXT,
                        refresh_claimed_at INTEGER,
                        linked_at INTEGER NOT NULL,
                        unlinked_at INTEGER
                    );
                    CREATE UNIQUE INDEX account_external_identity
                      ON accounts(provider, source_authority, ifnull(bot_namespace,''), stable_subject, organization_id);
                    CREATE INDEX account_owner_scope
                      ON accounts(anchor_issuer, anchor_subject, organization_id, workspace_id, status);
                    CREATE TABLE link_sessions (
                        link_session_ref TEXT PRIMARY KEY,
                        anchor_issuer TEXT NOT NULL,
                        anchor_subject TEXT NOT NULL,
                        organization_id TEXT NOT NULL,
                        workspace_id TEXT,
                        provider TEXT NOT NULL CHECK(provider IN ('github','wecom')),
                        state_digest TEXT NOT NULL,
                        nonce_digest TEXT NOT NULL,
                        callback_uri TEXT,
                        code_verifier_blob BLOB,
                        pending_credential_blob BLOB,
                        expires_at INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','cancelled','failed')),
                        failure_revocation_status TEXT,
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE wecom_challenges (
                        challenge_id TEXT PRIMARY KEY,
                        bot_namespace TEXT NOT NULL,
                        source_authority TEXT NOT NULL,
                        stable_subject TEXT NOT NULL,
                        organization_id TEXT NOT NULL,
                        value_digest TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        consumed_at INTEGER
                    );
                    CREATE TABLE credential_leases (
                        lease_ref TEXT PRIMARY KEY,
                        account_ref TEXT NOT NULL REFERENCES accounts(account_ref),
                        anchor_issuer TEXT NOT NULL,
                        anchor_subject TEXT NOT NULL,
                        organization_id TEXT NOT NULL,
                        workspace_id TEXT,
                        binding_id TEXT NOT NULL,
                        provider_caller_id TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        resource_json TEXT NOT NULL,
                        arguments_sha256 TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        redeemed_at INTEGER
                    );
                    CREATE INDEX credential_lease_expiry ON credential_leases(expires_at);
                    CREATE UNIQUE INDEX link_session_state_unique ON link_sessions(state_digest);
                    CREATE TABLE consumed_requests (
                        request_id TEXT PRIMARY KEY,
                        expires_at INTEGER NOT NULL
                    );
                    CREATE TABLE command_idempotency (
                        key_hash TEXT PRIMARY KEY,
                        method TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('pending','completed')),
                        response_blob BLOB,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL
                    );
                    CREATE INDEX command_idempotency_expiry ON command_idempotency(expires_at);
                    PRAGMA user_version = 4;
                    COMMIT;
                    """
                )
            elif current == 1:
                # Keep legacy WeCom rows unresolved rather than guessing how a
                # slash-joined source authority was originally partitioned.
                db.execute("ALTER TABLE accounts ADD COLUMN bot_namespace TEXT")
                db.execute(
                    "ALTER TABLE accounts ADD COLUMN remote_revocation_status TEXT"
                )
                db.execute(
                    "ALTER TABLE accounts ADD COLUMN remote_revocation_owner TEXT"
                )
                db.execute(
                    "ALTER TABLE accounts ADD COLUMN remote_revocation_command_hash TEXT"
                )
                db.execute(
                    "UPDATE accounts SET status='unlinked',remote_revocation_status='unknown' "
                    "WHERE status='revocation_pending'"
                )
                db.execute(
                    "ALTER TABLE accounts ADD COLUMN credential_status TEXT NOT NULL DEFAULT 'ready'"
                )
                db.execute(
                    "ALTER TABLE accounts ADD COLUMN credential_revision INTEGER NOT NULL DEFAULT 0"
                )
                db.execute("ALTER TABLE accounts ADD COLUMN refresh_claim_id TEXT")
                db.execute("ALTER TABLE accounts ADD COLUMN refresh_claim_owner TEXT")
                db.execute("ALTER TABLE accounts ADD COLUMN refresh_claimed_at INTEGER")
                db.execute("DROP INDEX IF EXISTS link_session_state_unique")
                db.execute("ALTER TABLE link_sessions RENAME TO link_sessions_v1")
                db.execute(
                    "CREATE TABLE link_sessions ("
                    "link_session_ref TEXT PRIMARY KEY,anchor_issuer TEXT NOT NULL,"
                    "anchor_subject TEXT NOT NULL,organization_id TEXT NOT NULL,workspace_id TEXT,"
                    "provider TEXT NOT NULL CHECK(provider IN ('github','wecom')),"
                    "state_digest TEXT NOT NULL,nonce_digest TEXT NOT NULL,callback_uri TEXT,"
                    "code_verifier_blob BLOB,pending_credential_blob BLOB,expires_at INTEGER NOT NULL,"
                    "status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','cancelled','failed')),"
                    "failure_revocation_status TEXT,created_at INTEGER NOT NULL)"
                )
                db.execute(
                    "INSERT INTO link_sessions(link_session_ref,anchor_issuer,anchor_subject,organization_id,workspace_id,provider,state_digest,nonce_digest,callback_uri,code_verifier_blob,pending_credential_blob,expires_at,status,failure_revocation_status,created_at) "
                    "SELECT link_session_ref,anchor_issuer,anchor_subject,organization_id,workspace_id,provider,state_digest,nonce_digest,callback_uri,NULL,NULL,expires_at,status,NULL,created_at "
                    "FROM link_sessions_v1"
                )
                db.execute("DROP TABLE link_sessions_v1")
                db.execute("DROP INDEX account_external_identity")
                db.execute(
                    "CREATE UNIQUE INDEX account_external_identity "
                    "ON accounts(provider, source_authority, ifnull(bot_namespace,''), "
                    "stable_subject, organization_id)"
                )
                db.execute(
                    "CREATE UNIQUE INDEX link_session_state_unique "
                    "ON link_sessions(state_digest)"
                )
                db.execute("PRAGMA user_version = 2")
                db.execute(
                    "CREATE TABLE command_idempotency ("
                    "key_hash TEXT PRIMARY KEY,method TEXT NOT NULL,payload_sha256 TEXT NOT NULL,"
                    "status TEXT NOT NULL CHECK(status IN ('pending','completed')),"
                    "response_blob BLOB,created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX command_idempotency_expiry "
                    "ON command_idempotency(expires_at)"
                )
                db.execute("PRAGMA user_version = 4")
            elif current == 2:
                db.execute(
                    "CREATE TABLE command_idempotency ("
                    "key_hash TEXT PRIMARY KEY,method TEXT NOT NULL,payload_sha256 TEXT NOT NULL,"
                    "status TEXT NOT NULL CHECK(status IN ('pending','completed')),"
                    "response_blob BLOB,created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX command_idempotency_expiry "
                    "ON command_idempotency(expires_at)"
                )
                db.execute(
                    "ALTER TABLE accounts ADD COLUMN remote_revocation_owner TEXT"
                )
                db.execute(
                    "ALTER TABLE accounts ADD COLUMN remote_revocation_command_hash TEXT"
                )
                db.execute(
                    "ALTER TABLE link_sessions ADD COLUMN pending_credential_blob BLOB"
                )
                db.execute(
                    "UPDATE accounts SET status='unlinked',remote_revocation_status='unknown' "
                    "WHERE status='revocation_pending'"
                )
                db.execute("PRAGMA user_version = 4")
            elif current == 3:
                db.execute(
                    "ALTER TABLE accounts ADD COLUMN remote_revocation_owner TEXT"
                )
                db.execute(
                    "ALTER TABLE accounts ADD COLUMN remote_revocation_command_hash TEXT"
                )
                db.execute(
                    "ALTER TABLE link_sessions ADD COLUMN pending_credential_blob BLOB"
                )
                db.execute(
                    "UPDATE accounts SET status='unlinked',remote_revocation_status='unknown' "
                    "WHERE status='revocation_pending'"
                )
                db.execute("PRAGMA user_version = 4")
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_profiles ("
                "profile_ref TEXT PRIMARY KEY,anchor_issuer TEXT NOT NULL,"
                "anchor_subject TEXT NOT NULL,organization_id TEXT NOT NULL,workspace_id TEXT,"
                "provider TEXT NOT NULL CHECK(provider IN "
                "('github','huggingface','azure','gcp','runpod','microsoft_graph','azure_devops','bilibili')),"
                "identity_ref TEXT NOT NULL,target_scope_json TEXT NOT NULL,"
                "auth_status TEXT NOT NULL CHECK(auth_status IN "
                "('unknown','configured','healthy','unavailable','reauthorization_required','revoked')),"
                "external_subject TEXT,credential_blob BLOB,credential_expires_at INTEGER,"
                "credential_secret_ref TEXT,status TEXT NOT NULL CHECK(status IN ('active','unlinked')) ,"
                "created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS provider_profile_owner_scope ON provider_profiles("
                "anchor_issuer,anchor_subject,organization_id,workspace_id,status)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS provider_external_identity "
                "ON provider_profiles(provider,identity_ref,organization_id) "
                "WHERE status='active' AND provider IN "
                "('github','huggingface','microsoft_graph','bilibili')"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_credential_leases ("
                "lease_ref TEXT PRIMARY KEY,profile_ref TEXT NOT NULL "
                "REFERENCES provider_profiles(profile_ref),anchor_issuer TEXT NOT NULL,"
                "anchor_subject TEXT NOT NULL,organization_id TEXT NOT NULL,workspace_id TEXT,"
                "binding_id TEXT NOT NULL,provider_caller_id TEXT NOT NULL,operation_id TEXT NOT NULL,"
                "target_json TEXT NOT NULL,arguments_sha256 TEXT NOT NULL,expires_at INTEGER NOT NULL,"
                "redeemed_at INTEGER)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS provider_credential_lease_expiry "
                "ON provider_credential_leases(expires_at)"
            )
            if current < 5:
                db.execute("PRAGMA user_version = 5")
            if current < 6:
                db.execute(
                    "ALTER TABLE provider_profiles ADD COLUMN auth_mode TEXT NOT NULL "
                    "DEFAULT 'legacy' CHECK(auth_mode IN "
                    "('legacy','fine_grained_token','device_oauth','github_app','host_identity',"
                    "'delegated_oauth','session_cookie'))"
                )
                db.execute(
                    "ALTER TABLE provider_profiles ADD COLUMN credential_refresh_blob BLOB"
                )
                db.execute(
                    "ALTER TABLE provider_profiles ADD COLUMN credential_refresh_expires_at INTEGER"
                )
                db.execute(
                    "ALTER TABLE provider_profiles ADD COLUMN oauth_client_id TEXT"
                )
                db.execute(
                    "ALTER TABLE provider_profiles ADD COLUMN oauth_scopes_json TEXT"
                )
                db.execute(
                    "ALTER TABLE provider_profiles ADD COLUMN refresh_claim_id TEXT"
                )
                db.execute(
                    "ALTER TABLE provider_profiles ADD COLUMN refresh_claim_owner TEXT"
                )
                db.execute(
                    "ALTER TABLE provider_profiles ADD COLUMN refresh_claimed_at INTEGER"
                )
                db.execute(
                    "ALTER TABLE provider_profiles ADD COLUMN credential_revision INTEGER "
                    "NOT NULL DEFAULT 0"
                )
                db.execute(
                    "UPDATE provider_profiles SET auth_mode=CASE provider "
                    "WHEN 'github' THEN 'github_app' "
                    "WHEN 'huggingface' THEN 'fine_grained_token' "
                    "ELSE 'host_identity' END"
                )
                db.execute("PRAGMA user_version = 6")
            if current < 7:
                db.execute(
                    "ALTER TABLE provider_profiles ADD COLUMN access_mode TEXT NOT NULL "
                    "DEFAULT 'actor_owned' CHECK(access_mode IN "
                    "('actor_owned','workspace_operator'))"
                )
                db.execute(
                    "ALTER TABLE provider_credential_leases ADD COLUMN connection_ref TEXT"
                )
                db.execute(
                    "CREATE TABLE consumed_connector_grants ("
                    "grant_ref TEXT PRIMARY KEY,connection_ref TEXT NOT NULL,"
                    "profile_ref TEXT NOT NULL,provider_id TEXT NOT NULL,"
                    "operation_id TEXT NOT NULL,resource_ref TEXT NOT NULL,"
                    "arguments_sha256 TEXT NOT NULL,anchor_issuer TEXT NOT NULL,"
                    "anchor_subject TEXT NOT NULL,organization_id TEXT NOT NULL,"
                    "workspace_id TEXT NOT NULL,issued_at INTEGER NOT NULL,"
                    "expires_at INTEGER NOT NULL,consumed_at INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX consumed_connector_grants_expiry "
                    "ON consumed_connector_grants(expires_at)"
                )
                db.execute("PRAGMA user_version = 7")
            if current < 8:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS huggingface_device_renewal_sessions ("
                    "session_ref TEXT PRIMARY KEY,profile_ref TEXT NOT NULL "
                    "REFERENCES provider_profiles(profile_ref),anchor_issuer TEXT NOT NULL,"
                    "anchor_subject TEXT NOT NULL,organization_id TEXT NOT NULL,workspace_id TEXT,"
                    "status TEXT NOT NULL CHECK(status IN "
                    "('starting','waiting_user','succeeded','failed','expired','cancelled','unknown')),"
                    "device_code_blob BLOB,user_code TEXT,verification_uri TEXT,expires_at INTEGER,"
                    "interval_seconds INTEGER,next_poll_at INTEGER,profile_revision INTEGER NOT NULL,"
                    "poll_claim_id TEXT,poll_claim_owner TEXT,poll_claimed_at INTEGER,"
                    "reason_code TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS huggingface_device_renewal_owner "
                    "ON huggingface_device_renewal_sessions(anchor_issuer,anchor_subject,"
                    "organization_id,workspace_id,status,expires_at)"
                )
                db.execute("PRAGMA user_version = 8")
            if 5 <= current < 9:
                # Rebuild the parent with FK enforcement disabled only on this
                # initialization connection. Child rows remain untouched and
                # are validated as a whole before the migration commits.
                db.execute(
                    "CREATE TABLE provider_profiles_new ("
                    "profile_ref TEXT PRIMARY KEY,anchor_issuer TEXT NOT NULL,"
                    "anchor_subject TEXT NOT NULL,organization_id TEXT NOT NULL,workspace_id TEXT,"
                    "provider TEXT NOT NULL CHECK(provider IN "
                    "('github','huggingface','azure','gcp','runpod','microsoft_graph','azure_devops','bilibili')),"
                    "identity_ref TEXT NOT NULL,target_scope_json TEXT NOT NULL,"
                    "auth_status TEXT NOT NULL CHECK(auth_status IN "
                    "('unknown','configured','healthy','unavailable','reauthorization_required','revoked')) ,"
                    "external_subject TEXT,credential_blob BLOB,credential_expires_at INTEGER,"
                    "credential_secret_ref TEXT,status TEXT NOT NULL CHECK(status IN ('active','unlinked')) ,"
                    "created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,"
                    "auth_mode TEXT NOT NULL DEFAULT 'legacy' CHECK(auth_mode IN "
                    "('legacy','fine_grained_token','device_oauth','github_app','host_identity','delegated_oauth','session_cookie')) ,"
                    "credential_refresh_blob BLOB,credential_refresh_expires_at INTEGER,"
                    "oauth_client_id TEXT,oauth_scopes_json TEXT,refresh_claim_id TEXT,"
                    "refresh_claim_owner TEXT,refresh_claimed_at INTEGER,"
                    "credential_revision INTEGER NOT NULL DEFAULT 0,"
                    "access_mode TEXT NOT NULL DEFAULT 'actor_owned' CHECK(access_mode IN "
                    "('actor_owned','workspace_operator')))"
                )
                profile_columns = (
                    "profile_ref,anchor_issuer,anchor_subject,organization_id,workspace_id,"
                    "provider,identity_ref,target_scope_json,auth_status,external_subject,"
                    "credential_blob,credential_expires_at,credential_secret_ref,status,"
                    "created_at,updated_at,auth_mode,credential_refresh_blob,"
                    "credential_refresh_expires_at,oauth_client_id,oauth_scopes_json,"
                    "refresh_claim_id,refresh_claim_owner,refresh_claimed_at,"
                    "credential_revision,access_mode"
                )
                db.execute(
                    f"INSERT INTO provider_profiles_new({profile_columns}) "
                    f"SELECT {profile_columns} FROM provider_profiles"
                )
                db.execute("DROP TABLE provider_profiles")
                db.execute(
                    "ALTER TABLE provider_profiles_new RENAME TO provider_profiles"
                )
                db.execute(
                    "CREATE INDEX provider_profile_owner_scope ON provider_profiles("
                    "anchor_issuer,anchor_subject,organization_id,workspace_id,status)"
                )
                db.execute(
                    "CREATE UNIQUE INDEX provider_external_identity "
                    "ON provider_profiles(provider,identity_ref,organization_id) "
                    "WHERE status='active' AND provider IN "
                    "('github','huggingface','microsoft_graph','bilibili')"
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS microsoft_entra_oauth_sessions ("
                "session_ref TEXT PRIMARY KEY,profile_ref TEXT NOT NULL,"
                "provider TEXT NOT NULL CHECK(provider IN ('microsoft_graph','azure_devops')) ,"
                "flow TEXT NOT NULL CHECK(flow IN ('link','scope_upgrade')),"
                "anchor_issuer TEXT NOT NULL,anchor_subject TEXT NOT NULL,"
                "organization_id TEXT NOT NULL,workspace_id TEXT,state_digest TEXT NOT NULL UNIQUE,"
                "nonce_digest TEXT NOT NULL,callback_uri TEXT NOT NULL,"
                "code_verifier_blob BLOB NOT NULL,target_scope_json TEXT NOT NULL,"
                "oauth_scopes_json TEXT NOT NULL,oauth_client_id TEXT NOT NULL,"
                "access_mode TEXT NOT NULL CHECK(access_mode IN "
                "('actor_owned','workspace_operator')),"
                "profile_revision INTEGER,expires_at INTEGER NOT NULL,"
                "status TEXT NOT NULL CHECK(status IN "
                "('pending','processing','completed','failed','expired')) ,"
                "created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS microsoft_entra_oauth_owner ON "
                "microsoft_entra_oauth_sessions(anchor_issuer,anchor_subject,"
                "organization_id,workspace_id,status,expires_at)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS provider_azure_devops_profile_scope "
                "ON provider_profiles(provider,identity_ref,organization_id,target_scope_json) "
                "WHERE status='active' AND provider='azure_devops'"
            )
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS provider_azure_devops_identity_owner "
                "BEFORE INSERT ON provider_profiles WHEN NEW.provider='azure_devops' "
                "AND EXISTS(SELECT 1 FROM provider_profiles AS existing "
                "WHERE existing.provider='azure_devops' AND existing.identity_ref=NEW.identity_ref "
                "AND existing.organization_id=NEW.organization_id AND existing.status='active' "
                "AND (existing.anchor_issuer!=NEW.anchor_issuer "
                "OR existing.anchor_subject!=NEW.anchor_subject "
                "OR existing.workspace_id IS NOT NEW.workspace_id)) "
                "BEGIN SELECT RAISE(ABORT,'external_identity_owned'); END"
            )
            if current < 9:
                db.execute("PRAGMA user_version = 9")
            if current == 9:
                # Rebuild the profile parent so existing v9 databases can accept
                # the new provider and session-cookie auth mode without touching
                # child lease rows.
                db.execute(
                    "CREATE TABLE provider_profiles_new ("
                    "profile_ref TEXT PRIMARY KEY,anchor_issuer TEXT NOT NULL,"
                    "anchor_subject TEXT NOT NULL,organization_id TEXT NOT NULL,workspace_id TEXT,"
                    "provider TEXT NOT NULL CHECK(provider IN "
                    "('github','huggingface','azure','gcp','runpod','microsoft_graph','azure_devops','bilibili')) ,"
                    "identity_ref TEXT NOT NULL,target_scope_json TEXT NOT NULL,"
                    "auth_status TEXT NOT NULL CHECK(auth_status IN "
                    "('unknown','configured','healthy','unavailable','reauthorization_required','revoked')) ,"
                    "external_subject TEXT,credential_blob BLOB,credential_expires_at INTEGER,"
                    "credential_secret_ref TEXT,status TEXT NOT NULL CHECK(status IN ('active','unlinked')) ,"
                    "created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,"
                    "auth_mode TEXT NOT NULL DEFAULT 'legacy' CHECK(auth_mode IN "
                    "('legacy','fine_grained_token','device_oauth','github_app','host_identity','delegated_oauth','session_cookie')) ,"
                    "credential_refresh_blob BLOB,credential_refresh_expires_at INTEGER,"
                    "oauth_client_id TEXT,oauth_scopes_json TEXT,refresh_claim_id TEXT,"
                    "refresh_claim_owner TEXT,refresh_claimed_at INTEGER,"
                    "credential_revision INTEGER NOT NULL DEFAULT 0,"
                    "access_mode TEXT NOT NULL DEFAULT 'actor_owned' CHECK(access_mode IN "
                    "('actor_owned','workspace_operator')) )"
                )
                profile_columns = (
                    "profile_ref,anchor_issuer,anchor_subject,organization_id,workspace_id,"
                    "provider,identity_ref,target_scope_json,auth_status,external_subject,"
                    "credential_blob,credential_expires_at,credential_secret_ref,status,"
                    "created_at,updated_at,auth_mode,credential_refresh_blob,"
                    "credential_refresh_expires_at,oauth_client_id,oauth_scopes_json,"
                    "refresh_claim_id,refresh_claim_owner,refresh_claimed_at,"
                    "credential_revision,access_mode"
                )
                db.execute(
                    f"INSERT INTO provider_profiles_new({profile_columns}) "
                    f"SELECT {profile_columns} FROM provider_profiles"
                )
                db.execute("DROP TABLE provider_profiles")
                db.execute("ALTER TABLE provider_profiles_new RENAME TO provider_profiles")
                db.execute(
                    "CREATE INDEX provider_profile_owner_scope ON provider_profiles("
                    "anchor_issuer,anchor_subject,organization_id,workspace_id,status)"
                )
                db.execute(
                    "CREATE UNIQUE INDEX provider_external_identity "
                    "ON provider_profiles(provider,identity_ref,organization_id) "
                    "WHERE status='active' AND provider IN "
                    "('github','huggingface','microsoft_graph','bilibili')"
                )
            if current < 10:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS bilibili_link_sessions ("
                    "session_digest TEXT PRIMARY KEY,profile_ref TEXT NOT NULL,"
                    "anchor_issuer TEXT NOT NULL,anchor_subject TEXT NOT NULL,"
                    "organization_id TEXT NOT NULL,workspace_id TEXT,binding_id TEXT NOT NULL,"
                    "caller_id TEXT NOT NULL,access_mode TEXT NOT NULL CHECK(access_mode IN "
                    "('actor_owned','workspace_operator')),expires_at INTEGER NOT NULL,"
                    "status TEXT NOT NULL CHECK(status IN ('pending','completed','cancelled')),"
                    "created_at INTEGER NOT NULL,completed_at INTEGER)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS bilibili_link_session_profile "
                    "ON bilibili_link_sessions(profile_ref,status,expires_at)"
                )
            if current < 11:
                # People directory: addresses an operator declared for a person.
                # Nothing here carries credentials.
                db.execute(
                    "CREATE TABLE IF NOT EXISTS workspace_people ("
                    "person_ref TEXT PRIMARY KEY,organization_id TEXT NOT NULL,"
                    "workspace_id TEXT,issuer TEXT NOT NULL,subject TEXT,mail TEXT,"
                    "display_name TEXT,created_by_issuer TEXT NOT NULL,"
                    "created_by_subject TEXT NOT NULL,created_at INTEGER NOT NULL,"
                    "updated_at INTEGER NOT NULL,"
                    "CHECK(subject IS NOT NULL OR mail IS NOT NULL))"
                )
                db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS workspace_person_subject "
                    "ON workspace_people(organization_id,ifnull(workspace_id,''),issuer,subject) "
                    "WHERE subject IS NOT NULL"
                )
                db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS workspace_person_mail "
                    "ON workspace_people(organization_id,ifnull(workspace_id,''),mail) "
                    "WHERE mail IS NOT NULL"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS account_handles ("
                    "handle_ref TEXT PRIMARY KEY,"
                    "person_ref TEXT NOT NULL REFERENCES workspace_people(person_ref),"
                    "organization_id TEXT NOT NULL,workspace_id TEXT,"
                    "provider TEXT NOT NULL CHECK(provider IN "
                    "('github','huggingface','azure_devops','wecom','qq','bilibili')),"
                    "handle TEXT NOT NULL,handle_key TEXT NOT NULL,"
                    "status TEXT NOT NULL CHECK(status IN ('active','retracted')),"
                    "declared_by_issuer TEXT NOT NULL,declared_by_subject TEXT NOT NULL,"
                    "created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS account_handle_owner "
                    "ON account_handles(organization_id,ifnull(workspace_id,''),provider,handle_key) "
                    "WHERE status='active'"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS account_handle_person "
                    "ON account_handles(person_ref,status)"
                )
            if current < 12:
                # Audit trail for administrator actions taken on another person's links.
                db.execute(
                    "CREATE TABLE IF NOT EXISTS account_admin_audit ("
                    "action_ref TEXT PRIMARY KEY,organization_id TEXT NOT NULL,"
                    "workspace_id TEXT,actor_issuer TEXT NOT NULL,actor_subject TEXT NOT NULL,"
                    "action TEXT NOT NULL CHECK(action IN ('revoke_person_links')),"
                    "target_subject TEXT NOT NULL,provider TEXT NOT NULL,"
                    "unlinked INTEGER NOT NULL,skipped_shared INTEGER NOT NULL,"
                    "created_at INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS account_admin_audit_scope "
                    "ON account_admin_audit(organization_id,ifnull(workspace_id,''),created_at)"
                )
            db.execute("PRAGMA user_version = 12")
            if 0 < current < SCHEMA_VERSION:
                # FK checks are connection-local and were disabled only to allow
                # the parent-table rebuild. Check all rows before commit so a
                # violation rolls the schema and data migration back atomically.
                violations = db.execute("PRAGMA foreign_key_check").fetchmany(1)
                if violations:
                    raise sqlite3.IntegrityError(
                        "account center schema migration violates foreign keys"
                    )

    def _recover_interrupted_operations(self) -> None:
        """Mark token rotations, callbacks, and revocations interrupted by restart."""

        stale_claim_before = int(time.time()) - 45
        with self.transaction() as db:
            db.execute(
                "UPDATE accounts SET credential_status='reauthorization_required', "
                "credential_blob=NULL,refresh_claim_id=NULL,refresh_claim_owner=NULL, "
                "refresh_claimed_at=NULL WHERE credential_status='refresh_pending'"
            )
            db.execute(
                "UPDATE provider_profiles SET auth_status='reauthorization_required',"
                "credential_blob=NULL,credential_refresh_blob=NULL,refresh_claim_id=NULL,"
                "refresh_claim_owner=NULL,refresh_claimed_at=NULL,"
                "credential_revision=credential_revision+1,updated_at=? "
                "WHERE status='active' AND refresh_claim_id IS NOT NULL "
                "AND refresh_claimed_at<=?",
                (int(time.time()), stale_claim_before),
            )
            db.execute(
                "DELETE FROM provider_credential_leases WHERE profile_ref IN "
                "(SELECT profile_ref FROM provider_profiles "
                "WHERE auth_status='reauthorization_required' AND provider='huggingface')"
            )
            db.execute(
                "DELETE FROM credential_leases WHERE account_ref IN "
                "(SELECT account_ref FROM accounts WHERE credential_status='reauthorization_required')"
            )
            db.execute(
                "UPDATE accounts SET remote_revocation_status='unknown', "
                "remote_revocation_owner=NULL WHERE status='unlinked' "
                "AND remote_revocation_status='pending' "
                "AND (remote_revocation_owner IS NULL OR remote_revocation_owner!=?)",
                (self._instance_id,),
            )
            db.execute(
                "UPDATE microsoft_entra_oauth_sessions SET status='failed',"
                "code_verifier_blob=X'',updated_at=? WHERE status='processing'",
                (int(time.time()),),
            )
            db.execute(
                "UPDATE link_sessions SET status='failed',failure_revocation_status='unknown', "
                "code_verifier_blob=NULL WHERE provider='github' AND status='processing' "
                "AND pending_credential_blob IS NULL"
            )
            db.execute(
                "UPDATE huggingface_device_renewal_sessions SET status='unknown',"
                "device_code_blob=NULL,user_code=NULL,verification_uri=NULL,"
                "poll_claim_id=NULL,poll_claim_owner=NULL,"
                "poll_claimed_at=NULL,reason_code='device_flow_interrupted',updated_at=? "
                "WHERE status='starting' AND created_at<=?",
                (int(time.time()), int(time.time()) - 60),
            )
            db.execute(
                "UPDATE huggingface_device_renewal_sessions SET status='unknown',"
                "device_code_blob=NULL,user_code=NULL,verification_uri=NULL,"
                "poll_claim_id=NULL,poll_claim_owner=NULL,"
                "poll_claimed_at=NULL,reason_code='device_poll_outcome_unknown',updated_at=? "
                "WHERE status='waiting_user' AND poll_claim_id IS NOT NULL "
                "AND poll_claimed_at<=? AND poll_claim_owner!=?",
                (int(time.time()), int(time.time()) - 45, self._instance_id),
            )

    def _protect_files(self) -> None:
        """Keep SQLite database, WAL, and shared-memory files owner-only."""

        if str(self.path) == ":memory:":
            return
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            if candidate.exists():
                try:
                    os.chmod(candidate, 0o600)
                except OSError:
                    raise AccountCenterError("storage_unavailable") from None

    def backup(self, destination: str | Path) -> None:
        """Create a SQLite-consistent private backup using SQLite's backup API."""

        target = Path(destination)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        source = self._connect()
        backup = sqlite3.connect(target)
        try:
            source.backup(backup)
            backup.commit()
            os.chmod(target, 0o600)
            os.chmod(target.parent, 0o700)
        except (sqlite3.Error, OSError):
            raise AccountCenterError("backup_failed") from None
        finally:
            source.close()
            backup.close()

    def prune_expired(self, *, now: int | None = None) -> None:
        """Retain completed records while removing expired transient secrets."""

        timestamp = int(time.time()) if now is None else now
        with self.transaction() as db:
            db.execute(
                "DELETE FROM link_sessions WHERE expires_at < ? OR status != 'pending'",
                (timestamp - 86400,),
            )
            db.execute(
                "DELETE FROM wecom_challenges WHERE expires_at < ? OR consumed_at IS NOT NULL",
                (timestamp,),
            )
            db.execute(
                "DELETE FROM credential_leases WHERE expires_at < ? OR redeemed_at IS NOT NULL",
                (timestamp,),
            )
            db.execute(
                "DELETE FROM provider_credential_leases WHERE expires_at < ? "
                "OR redeemed_at IS NOT NULL",
                (timestamp,),
            )
            db.execute(
                "DELETE FROM bilibili_link_sessions WHERE expires_at < ? "
                "OR (status!='pending' AND completed_at < ?)",
                (timestamp - IDEMPOTENCY_RETENTION_SECONDS, timestamp - IDEMPOTENCY_RETENTION_SECONDS),
            )
            db.execute(
                "DELETE FROM consumed_connector_grants WHERE expires_at < ?",
                (timestamp,),
            )
            db.execute(
                "DELETE FROM consumed_requests WHERE expires_at < ?", (timestamp,)
            )
            db.execute(
                "DELETE FROM command_idempotency WHERE expires_at < ?", (timestamp,)
            )

    def begin_idempotent_command(
        self,
        *,
        key_hash: str,
        method: str,
        payload_sha256: str,
        now: int,
    ) -> bytes | None:
        """Reserve one scoped command or return its prior encrypted result."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT method,payload_sha256,status,response_blob,expires_at "
                "FROM command_idempotency WHERE key_hash=?",
                (key_hash,),
            ).fetchone()
            if row is not None and row["expires_at"] <= now:
                db.execute(
                    "DELETE FROM command_idempotency WHERE key_hash=?", (key_hash,)
                )
                row = None
            if row is not None:
                if row["method"] != method or row["payload_sha256"] != payload_sha256:
                    raise AccountCenterError("idempotency_conflict")
                if row["status"] != "completed" or row["response_blob"] is None:
                    raise AccountCenterError("idempotency_in_progress")
                return bytes(row["response_blob"])
            db.execute(
                "INSERT INTO command_idempotency VALUES(?,?,?,?,NULL,?,?)",
                (
                    key_hash,
                    method,
                    payload_sha256,
                    "pending",
                    now,
                    now + IDEMPOTENCY_RETENTION_SECONDS,
                ),
            )
        return None

    def complete_idempotent_command(self, key_hash: str, response_blob: bytes) -> None:
        """Store one encrypted bounded result after the command finishes."""

        with self.transaction() as db:
            self._complete_command_in_transaction(db, key_hash, response_blob)

    def pending_idempotent_commands(self) -> list[str]:
        """Return reserved command hashes left incomplete across process restart."""

        with self._connect() as db:
            rows = db.execute(
                "SELECT key_hash FROM command_idempotency WHERE status='pending'"
            ).fetchall()
        return [str(row["key_hash"]) for row in rows]

    def interrupted_unlink_commands(self) -> list[sqlite3.Row]:
        """Return unlink commands whose remote outcome became unknown at restart."""

        with self._connect() as db:
            rows = db.execute(
                "SELECT account_ref,remote_revocation_command_hash "
                "FROM accounts WHERE status='unlinked' AND remote_revocation_status='unknown' "
                "AND remote_revocation_command_hash IS NOT NULL"
            ).fetchall()
        return list(rows)

    def finalize_interrupted_unlink(
        self, account_ref: str, key_hash: str, response_blob: bytes
    ) -> None:
        """Commit the unknown unlink result after restart reconciliation."""

        with self.transaction() as db:
            account = db.execute(
                "SELECT remote_revocation_status,remote_revocation_command_hash "
                "FROM accounts WHERE account_ref=?",
                (account_ref,),
            ).fetchone()
            if (
                account is None
                or account["remote_revocation_status"] != "unknown"
                or account["remote_revocation_command_hash"] != key_hash
            ):
                raise AccountCenterError("invalid_state")
            command = db.execute(
                "SELECT status FROM command_idempotency WHERE key_hash=?",
                (key_hash,),
            ).fetchone()
            if command is not None and command["status"] == "pending":
                self._complete_command_in_transaction(db, key_hash, response_blob)
            db.execute(
                "UPDATE accounts SET remote_revocation_command_hash=NULL "
                "WHERE account_ref=? AND remote_revocation_command_hash=?",
                (account_ref, key_hash),
            )

    @staticmethod
    def _complete_command_in_transaction(
        db: sqlite3.Connection, key_hash: str, response_blob: bytes
    ) -> None:
        """Commit one command response alongside its state mutation."""

        changed = db.execute(
            "UPDATE command_idempotency SET status='completed',response_blob=? "
            "WHERE key_hash=? AND status='pending'",
            (response_blob, key_hash),
        ).rowcount
        if changed != 1:
            raise AccountCenterError("idempotency_state_invalid")

    def consume_request_id(self, request_id: str, expires_at: int) -> None:
        """Reject replay of any signed caller request identifier."""

        with self.transaction() as db:
            try:
                db.execute(
                    "INSERT INTO consumed_requests VALUES(?,?)",
                    (request_id, expires_at),
                )
            except sqlite3.IntegrityError:
                raise AccountCenterError("request_replayed") from None

    def create_session(
        self,
        anchor: Anchor,
        provider: str,
        state_digest: str,
        nonce_digest: str,
        callback_uri: str | None,
        code_verifier_blob: bytes | None,
        expires_at: int,
        *,
        now: int,
        session_ref: str | None = None,
        command_result: tuple[str, bytes] | None = None,
    ) -> str:
        """Persist one actor-bound, expiring OAuth or private challenge session."""

        session_ref = session_ref or str(uuid.uuid4())
        with self.transaction() as db:
            db.execute(
                "INSERT INTO link_sessions(link_session_ref,anchor_issuer,anchor_subject,organization_id,workspace_id,provider,state_digest,nonce_digest,callback_uri,code_verifier_blob,pending_credential_blob,expires_at,status,failure_revocation_status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_ref,
                    anchor.issuer,
                    anchor.subject,
                    anchor.organization_id,
                    anchor.workspace_id,
                    provider,
                    state_digest,
                    nonce_digest,
                    callback_uri,
                    code_verifier_blob,
                    None,
                    expires_at,
                    "pending",
                    None,
                    now,
                ),
            )
            if command_result is not None:
                self._complete_command_in_transaction(db, *command_result)
        return session_ref

    def create_microsoft_entra_oauth_session(
        self,
        *,
        session_ref: str,
        profile_ref: str,
        provider: str,
        flow: str,
        anchor: Anchor,
        state_digest: str,
        nonce_digest: str,
        callback_uri: str,
        code_verifier_blob: bytes,
        target_scope_json: str,
        oauth_scopes_json: str,
        oauth_client_id: str,
        access_mode: str,
        profile_revision: int | None,
        expires_at: int,
        now: int,
        command_result: tuple[str, bytes] | None = None,
    ) -> None:
        """Persist one actor-bound Entra PKCE session and its idempotent result."""

        if (
            provider not in {"microsoft_graph", "azure_devops"}
            or flow
            not in {
                "link",
                "scope_upgrade",
            }
            or access_mode
            not in {
                "actor_owned",
                "workspace_operator",
            }
        ):
            raise AccountCenterError("invalid_request")
        if (flow == "link") != (profile_revision is None):
            raise AccountCenterError("invalid_request")
        with self.transaction() as db:
            try:
                db.execute(
                    "INSERT INTO microsoft_entra_oauth_sessions(session_ref,profile_ref,"
                    "provider,flow,anchor_issuer,anchor_subject,organization_id,workspace_id,"
                    "state_digest,nonce_digest,callback_uri,code_verifier_blob,"
                    "target_scope_json,oauth_scopes_json,oauth_client_id,access_mode,"
                    "profile_revision,expires_at,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                    (
                        session_ref,
                        profile_ref,
                        provider,
                        flow,
                        anchor.issuer,
                        anchor.subject,
                        anchor.organization_id,
                        anchor.workspace_id,
                        state_digest,
                        nonce_digest,
                        callback_uri,
                        code_verifier_blob,
                        target_scope_json,
                        oauth_scopes_json,
                        oauth_client_id,
                        access_mode,
                        profile_revision,
                        expires_at,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                raise AccountCenterError("link_session_invalid") from None
            if command_result is not None:
                self._complete_command_in_transaction(db, *command_result)

    def claim_microsoft_entra_oauth_session(
        self, state_digest: str, callback_uri: str, *, now: int
    ) -> sqlite3.Row:
        """Atomically claim one unexpired Graph callback and prevent state replay."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM microsoft_entra_oauth_sessions WHERE state_digest=? "
                "AND callback_uri=? AND status='pending' AND expires_at>?",
                (state_digest, callback_uri, now),
            ).fetchone()
            if row is None:
                raise AccountCenterError("link_session_invalid")
            changed = db.execute(
                "UPDATE microsoft_entra_oauth_sessions SET status='processing',updated_at=? "
                "WHERE session_ref=? AND status='pending' AND expires_at>?",
                (now, row["session_ref"], now),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("link_session_invalid")
            return row

    def fail_microsoft_entra_oauth_session(self, session_ref: str, *, now: int) -> None:
        """Erase the encrypted verifier after a consumed or failed Graph callback."""

        with self.transaction() as db:
            changed = db.execute(
                "UPDATE microsoft_entra_oauth_sessions SET status='failed',"
                "code_verifier_blob=X'',updated_at=? WHERE session_ref=? "
                "AND status='processing'",
                (now, session_ref),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("link_session_invalid")

    def complete_microsoft_entra_oauth_session(
        self,
        *,
        session_ref: str,
        provider: str,
        anchor: Anchor,
        identity_ref: str,
        external_subject: str,
        target_scope_json: str,
        credential_blob: bytes,
        credential_refresh_blob: bytes,
        credential_expires_at: int,
        credential_refresh_expires_at: int | None,
        oauth_scopes_json: str,
        oauth_client_id: str,
        now: int,
    ) -> sqlite3.Row:
        """Atomically create or upgrade one Entra profile under its saved owner."""

        with self.transaction() as db:
            session = db.execute(
                "SELECT * FROM microsoft_entra_oauth_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if (
                session is None
                or session["status"] != "processing"
                or session["provider"] != provider
                or session["expires_at"] <= now
                or session["callback_uri"] == ""
                or session["oauth_client_id"] != oauth_client_id
                or not self._owns(session, anchor)
                or session["oauth_scopes_json"] != oauth_scopes_json
            ):
                raise AccountCenterError("link_session_invalid")
            profile_ref = str(session["profile_ref"])
            if session["flow"] == "link":
                if session["profile_revision"] is not None:
                    raise AccountCenterError("link_session_invalid")
                try:
                    scope = json.loads(target_scope_json)
                    tenant_id, object_id = external_subject.split(":", 1)
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise AccountCenterError("external_identity_mismatch") from None
                if (
                    identity_ref != external_subject
                    or not isinstance(scope, dict)
                    or scope.get("tenant_id") != tenant_id
                    or not tenant_id
                    or not object_id
                    or ":" in object_id
                ):
                    raise AccountCenterError("external_identity_mismatch")
                try:
                    db.execute(
                        "INSERT INTO provider_profiles(profile_ref,anchor_issuer,"
                        "anchor_subject,organization_id,workspace_id,provider,identity_ref,"
                        "target_scope_json,auth_status,external_subject,credential_blob,"
                        "credential_expires_at,credential_refresh_blob,"
                        "credential_refresh_expires_at,oauth_client_id,oauth_scopes_json,"
                        "credential_secret_ref,status,created_at,updated_at,auth_mode,"
                        "credential_revision,access_mode) "
                        "VALUES(?,?,?,?,?,?,?,?,'healthy',?,?,?,?,?,?,?,NULL,"
                        "'active',?,?,'delegated_oauth',0,?)",
                        (
                            profile_ref,
                            anchor.issuer,
                            anchor.subject,
                            anchor.organization_id,
                            anchor.workspace_id,
                            provider,
                            identity_ref,
                            target_scope_json,
                            external_subject,
                            credential_blob,
                            credential_expires_at,
                            credential_refresh_blob,
                            credential_refresh_expires_at,
                            oauth_client_id,
                            oauth_scopes_json,
                            now,
                            now,
                            session["access_mode"],
                        ),
                    )
                except sqlite3.IntegrityError:
                    raise AccountCenterError("external_identity_owned") from None
            elif session["flow"] == "scope_upgrade":
                profile = db.execute(
                    "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                    (profile_ref,),
                ).fetchone()
                if (
                    profile is None
                    or not self._owns_provider_profile(profile, anchor)
                    or profile["provider"] != provider
                    or profile["auth_mode"] != "delegated_oauth"
                    or profile["auth_status"] != "healthy"
                    or profile["external_subject"] != external_subject
                    or profile["identity_ref"] != identity_ref
                    or profile["credential_revision"] != session["profile_revision"]
                    or profile["refresh_claim_id"] is not None
                    or session["target_scope_json"] != target_scope_json
                ):
                    raise AccountCenterError("external_identity_mismatch")
                changed = db.execute(
                    "UPDATE provider_profiles SET target_scope_json=?,credential_blob=?,"
                    "credential_refresh_blob=?,credential_expires_at=?,"
                    "credential_refresh_expires_at=?,oauth_client_id=?,oauth_scopes_json=?,"
                    "auth_status='healthy',credential_revision=credential_revision+1,"
                    "updated_at=? WHERE profile_ref=? AND credential_revision=? "
                    "AND refresh_claim_id IS NULL",
                    (
                        target_scope_json,
                        credential_blob,
                        credential_refresh_blob,
                        credential_expires_at,
                        credential_refresh_expires_at,
                        oauth_client_id,
                        oauth_scopes_json,
                        now,
                        profile_ref,
                        session["profile_revision"],
                    ),
                ).rowcount
                if changed != 1:
                    raise AccountCenterError("credential_refresh_claim_lost")
                db.execute(
                    "DELETE FROM provider_credential_leases WHERE profile_ref=?",
                    (profile_ref,),
                )
            else:
                raise AccountCenterError("link_session_invalid")
            changed = db.execute(
                "UPDATE microsoft_entra_oauth_sessions SET status='completed',"
                "code_verifier_blob=X'',updated_at=? WHERE session_ref=? "
                "AND status='processing' AND expires_at>?",
                (now, session_ref, now),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("link_session_invalid")
            result = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (profile_ref,),
            ).fetchone()
            if result is None:
                raise AccountCenterError("storage_unavailable")
            return result

    def claim_session_by_state(
        self, state_digest: str, callback_uri: str, *, now: int
    ) -> sqlite3.Row:
        """Atomically claim the one active callback session for a state hash."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM link_sessions WHERE state_digest=? "
                "AND callback_uri=? AND provider='github' AND status='pending' AND expires_at>?",
                (state_digest, callback_uri, now),
            ).fetchone()
            if row is None:
                raise AccountCenterError("link_session_invalid")
            changed = db.execute(
                "UPDATE link_sessions SET status='processing' "
                "WHERE link_session_ref=? AND status='pending' AND expires_at>?",
                (row["link_session_ref"], now),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("link_session_invalid")
            return row

    def session(self, session_ref: str) -> sqlite3.Row:
        """Read a link session without returning credential material."""

        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM link_sessions WHERE link_session_ref = ?", (session_ref,)
            ).fetchone()
        if row is None:
            raise AccountCenterError("link_session_invalid")
        return row

    def save_pending_callback_credential(
        self, session_ref: str, credential_blob: bytes
    ) -> None:
        """Persist encrypted OAuth response material before `/user` verification."""

        with self.transaction() as db:
            changed = db.execute(
                "UPDATE link_sessions SET pending_credential_blob=? "
                "WHERE link_session_ref=? AND provider='github' AND status='processing'",
                (credential_blob, session_ref),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("link_session_invalid")

    def interrupted_callback_credentials(self) -> list[sqlite3.Row]:
        """Return encrypted token material left by a crashed callback process."""

        with self._connect() as db:
            rows = db.execute(
                "SELECT link_session_ref,pending_credential_blob FROM link_sessions "
                "WHERE provider='github' AND status='processing' "
                "AND pending_credential_blob IS NOT NULL"
            ).fetchall()
        return list(rows)

    def complete_session(self, session_ref: str, *, now: int) -> None:
        """Atomically consume a pending link session once."""

        with self.transaction() as db:
            changed = db.execute(
                "UPDATE link_sessions SET status='completed',code_verifier_blob=NULL, "
                "pending_credential_blob=NULL "
                "WHERE link_session_ref=? AND status='processing' AND expires_at>?",
                (session_ref, now),
            ).rowcount
        if changed != 1:
            raise AccountCenterError("link_session_invalid")

    def fail_session(self, session_ref: str, revocation_status: str) -> None:
        """Persist only the sanitized outcome of a consumed GitHub callback."""

        if revocation_status not in {"not_issued", "revoked", "failed", "unknown"}:
            raise AccountCenterError("invalid_state")
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE link_sessions SET status='failed',failure_revocation_status=?, "
                "code_verifier_blob=NULL,pending_credential_blob=NULL "
                "WHERE link_session_ref=? AND status='processing'",
                (revocation_status, session_ref),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("link_session_invalid")

    def claim_session(self, session_ref: str, *, now: int) -> None:
        """Claim one callback session before making the provider network call."""

        with self.transaction() as db:
            changed = db.execute(
                "UPDATE link_sessions SET status='processing' WHERE link_session_ref=? AND status='pending' AND expires_at>?",
                (session_ref, now),
            ).rowcount
        if changed != 1:
            raise AccountCenterError("link_session_invalid")

    def add_wecom_challenge(
        self,
        *,
        challenge_id: str,
        bot_namespace: str,
        source_authority: str,
        stable_subject: str,
        organization_id: str,
        value_digest: str,
        expires_at: int,
    ) -> None:
        """Store a trusted connector observation without trusting message text."""

        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM wecom_challenges WHERE challenge_id=?",
                (challenge_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["bot_namespace"],
                    existing["source_authority"],
                    existing["stable_subject"],
                    existing["organization_id"],
                    existing["value_digest"],
                    existing["expires_at"],
                ) == (
                    bot_namespace,
                    source_authority,
                    stable_subject,
                    organization_id,
                    value_digest,
                    expires_at,
                ):
                    return
                raise AccountCenterError("challenge_rejected")
            try:
                db.execute(
                    "INSERT INTO wecom_challenges VALUES(?,?,?,?,?,?,?,NULL)",
                    (
                        challenge_id,
                        bot_namespace,
                        source_authority,
                        stable_subject,
                        organization_id,
                        value_digest,
                        expires_at,
                    ),
                )
            except sqlite3.IntegrityError:
                raise AccountCenterError("challenge_rejected") from None

    def consume_wecom_challenge(
        self,
        challenge_id: str,
        *,
        expected_organization_id: str,
        expected_value_digest: str,
        now: int,
    ) -> sqlite3.Row:
        """Consume a source challenge once and return its trusted identity facts."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM wecom_challenges WHERE challenge_id=?", (challenge_id,)
            ).fetchone()
            if (
                row is None
                or row["expires_at"] <= now
                or row["consumed_at"] is not None
                or row["organization_id"] != expected_organization_id
                or row["value_digest"] != expected_value_digest
            ):
                raise AccountCenterError("challenge_rejected")
            db.execute(
                "UPDATE wecom_challenges SET consumed_at=? WHERE challenge_id=?",
                (now, challenge_id),
            )
        return row

    def complete_wecom_link(
        self,
        *,
        session_ref: str,
        challenge_id: str,
        anchor: Anchor,
        expected_value_digest: str,
        account_ref: str,
        now: int,
        command_result: tuple[str, bytes] | None = None,
    ) -> str:
        """Consume source challenge, session, account, and replay result atomically."""

        with self.transaction() as db:
            session = db.execute(
                "SELECT * FROM link_sessions WHERE link_session_ref=?", (session_ref,)
            ).fetchone()
            if (
                session is None
                or session["provider"] != "wecom"
                or session["status"] != "pending"
                or session["expires_at"] <= now
                or not self._owns(session, anchor)
            ):
                raise AccountCenterError("link_session_invalid")
            challenge = db.execute(
                "SELECT * FROM wecom_challenges WHERE challenge_id=?", (challenge_id,)
            ).fetchone()
            if (
                challenge is None
                or challenge["expires_at"] <= now
                or challenge["consumed_at"] is not None
                or challenge["organization_id"] != anchor.organization_id
                or challenge["value_digest"] != expected_value_digest
            ):
                raise AccountCenterError("challenge_rejected")
            existing = db.execute(
                "SELECT * FROM accounts WHERE provider='wecom' AND source_authority=? "
                "AND bot_namespace=? AND stable_subject=? AND organization_id=?",
                (
                    challenge["source_authority"],
                    challenge["bot_namespace"],
                    challenge["stable_subject"],
                    anchor.organization_id,
                ),
            ).fetchone()
            if existing is not None:
                if (existing["anchor_issuer"], existing["anchor_subject"]) == (
                    anchor.issuer,
                    anchor.subject,
                ):
                    raise AccountCenterError("duplicate_link")
                raise AccountCenterError("external_identity_owned")
            db.execute(
                "INSERT INTO accounts(account_ref,anchor_issuer,anchor_subject,organization_id,workspace_id,provider,source_authority,bot_namespace,stable_subject,metadata_json,credential_blob,credential_expires_at,refresh_expires_at,status,remote_revocation_status,credential_status,credential_revision,refresh_claim_id,refresh_claim_owner,refresh_claimed_at,linked_at,unlinked_at) "
                "VALUES(?,?,?,?,?,'wecom',?,?,?,'{}',NULL,NULL,NULL,'active',NULL,'ready',0,NULL,NULL,NULL,?,NULL)",
                (
                    account_ref,
                    anchor.issuer,
                    anchor.subject,
                    anchor.organization_id,
                    anchor.workspace_id,
                    challenge["source_authority"],
                    challenge["bot_namespace"],
                    challenge["stable_subject"],
                    now,
                ),
            )
            consumed = db.execute(
                "UPDATE wecom_challenges SET consumed_at=? WHERE challenge_id=? "
                "AND consumed_at IS NULL",
                (now, challenge_id),
            ).rowcount
            completed = db.execute(
                "UPDATE link_sessions SET status='completed' WHERE link_session_ref=? "
                "AND status='pending' AND expires_at>?",
                (session_ref, now),
            ).rowcount
            if consumed != 1 or completed != 1:
                raise AccountCenterError("challenge_rejected")
            if command_result is not None:
                self._complete_command_in_transaction(db, *command_result)
        return account_ref

    def insert_account(
        self,
        *,
        account_ref: str | None = None,
        anchor: Anchor,
        provider: str,
        source_authority: str,
        stable_subject: str,
        metadata_json: str,
        credential_blob: bytes | None,
        credential_expires_at: int | None,
        refresh_expires_at: int | None,
        now: int,
        bot_namespace: str | None = None,
        complete_session_ref: str | None = None,
        command_result: tuple[str, bytes] | None = None,
    ) -> str:
        """Create a binding without permitting cross-anchor identity movement."""

        account_ref = account_ref or str(uuid.uuid4())
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM accounts WHERE provider=? AND source_authority=? "
                "AND bot_namespace IS ? AND stable_subject=? AND organization_id=?",
                (
                    provider,
                    source_authority,
                    bot_namespace,
                    stable_subject,
                    anchor.organization_id,
                ),
            ).fetchone()
            if existing is not None:
                if (existing["anchor_issuer"], existing["anchor_subject"]) == (
                    anchor.issuer,
                    anchor.subject,
                ):
                    raise AccountCenterError("duplicate_link")
                raise AccountCenterError("external_identity_owned")
            db.execute(
                "INSERT INTO accounts(account_ref,anchor_issuer,anchor_subject,organization_id,workspace_id,provider,source_authority,bot_namespace,stable_subject,metadata_json,credential_blob,credential_expires_at,refresh_expires_at,status,remote_revocation_status,credential_status,credential_revision,refresh_claim_id,refresh_claim_owner,refresh_claimed_at,linked_at,unlinked_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    account_ref,
                    anchor.issuer,
                    anchor.subject,
                    anchor.organization_id,
                    anchor.workspace_id,
                    provider,
                    source_authority,
                    bot_namespace,
                    stable_subject,
                    metadata_json,
                    credential_blob,
                    credential_expires_at,
                    refresh_expires_at,
                    "active",
                    None,
                    "ready",
                    0,
                    None,
                    None,
                    None,
                    now,
                ),
            )
            if complete_session_ref is not None:
                changed = db.execute(
                    "UPDATE link_sessions SET status='completed',code_verifier_blob=NULL, "
                    "pending_credential_blob=NULL "
                    "WHERE link_session_ref=? AND status='processing' AND expires_at>?",
                    (complete_session_ref, now),
                ).rowcount
                if changed != 1:
                    raise AccountCenterError("link_session_invalid")
            if command_result is not None:
                self._complete_command_in_transaction(db, *command_result)
        return account_ref

    def list_accounts(
        self, anchor: Anchor, workspace_id: str | None
    ) -> list[sqlite3.Row]:
        """List metadata only for links owned by the full trusted anchor."""

        with self._connect() as db:
            rows = db.execute(
                "SELECT account_ref,provider,source_authority,bot_namespace,"
                "CASE WHEN credential_status!='ready' OR "
                "(provider='wecom' AND bot_namespace IS NULL) "
                "THEN 'reauthorization_required' "
                "WHEN remote_revocation_status='pending' THEN 'revocation_pending' "
                "WHEN remote_revocation_status='failed' THEN 'revocation_failed' "
                "WHEN remote_revocation_status='unknown' THEN 'revocation_unknown' "
                "ELSE status END AS status,linked_at,workspace_id "
                "FROM accounts WHERE anchor_issuer=? AND anchor_subject=? AND organization_id=? "
                "AND workspace_id IS ? ORDER BY linked_at,account_ref",
                (anchor.issuer, anchor.subject, anchor.organization_id, workspace_id),
            ).fetchall()
        return list(rows)

    def resolve_linked_sender(
        self,
        *,
        source_authority: str,
        bot_namespace: str,
        stable_subject: str,
        organization_id: str,
    ) -> sqlite3.Row | None:
        """Resolve one active source identity to its owning primary actor."""

        with self._connect() as db:
            return db.execute(
                "SELECT account_ref,provider,anchor_issuer,anchor_subject,organization_id,workspace_id "
                "FROM accounts WHERE provider='wecom' AND source_authority=? "
                "AND bot_namespace=? AND stable_subject=? AND organization_id=? AND status='active'",
                (source_authority, bot_namespace, stable_subject, organization_id),
            ).fetchone()

    def get_account(self, account_ref: str, anchor: Anchor) -> sqlite3.Row:
        """Return one account row only after full owner and workspace checks."""

        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE account_ref=?", (account_ref,)
            ).fetchone()
        if row is None or not self._owns(row, anchor):
            raise AccountCenterError("account_not_found")
        return row

    @staticmethod
    def _owns(row: Mapping[str, Any], anchor: Anchor) -> bool:
        return (
            row["anchor_issuer"] == anchor.issuer
            and row["anchor_subject"] == anchor.subject
            and row["organization_id"] == anchor.organization_id
            and row["workspace_id"] == anchor.workspace_id
        )

    @staticmethod
    def _owns_provider_profile(
        row: Mapping[str, Any],
        anchor: Anchor,
        *,
        allow_workspace_operator: bool = False,
    ) -> bool:
        """Keep actor-owned profiles private and share only exact workspace scope."""

        if AccountStore._owns(row, anchor):
            return True
        return (
            allow_workspace_operator
            and row["access_mode"] == "workspace_operator"
            and row["anchor_issuer"] == anchor.issuer
            and row["organization_id"] == anchor.organization_id
            and row["workspace_id"] is not None
            and row["workspace_id"] == anchor.workspace_id
        )

    def claim_refresh(
        self,
        account_ref: str,
        anchor: Anchor,
        claim_id: str,
        *,
        now: int,
    ) -> int:
        """Persist one refresh owner before contacting GitHub."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE account_ref=?", (account_ref,)
            ).fetchone()
            if (
                row is None
                or not self._owns(row, anchor)
                or row["status"] != "active"
                or row["credential_status"] != "ready"
            ):
                raise AccountCenterError("credential_reauthentication_required")
            changed = db.execute(
                "UPDATE accounts SET credential_status='refresh_pending', "
                "credential_revision=credential_revision+1,refresh_claim_id=?, "
                "refresh_claim_owner=?,refresh_claimed_at=? "
                "WHERE account_ref=? AND status='active' AND credential_status='ready' "
                "AND credential_revision=?",
                (
                    claim_id,
                    self._instance_id,
                    now,
                    account_ref,
                    row["credential_revision"],
                ),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("credential_reauthentication_required")
            return int(row["credential_revision"]) + 1

    def finish_refresh(
        self,
        account_ref: str,
        anchor: Anchor,
        claim_id: str,
        revision: int,
        blob: bytes,
        expires_at: int | None,
        refresh_expires_at: int | None,
    ) -> None:
        """Commit rotated material only for the exact active refresh claim."""

        with self.transaction() as db:
            changed = db.execute(
                "UPDATE accounts SET credential_blob=?,credential_expires_at=?, "
                "refresh_expires_at=?,credential_status='ready',refresh_claim_id=NULL, "
                "refresh_claim_owner=NULL,refresh_claimed_at=NULL "
                "WHERE account_ref=? AND anchor_issuer=? AND anchor_subject=? "
                "AND organization_id=? AND workspace_id IS ? AND status='active' "
                "AND credential_status='refresh_pending' AND refresh_claim_id=? "
                "AND refresh_claim_owner=? AND credential_revision=?",
                (
                    blob,
                    expires_at,
                    refresh_expires_at,
                    account_ref,
                    anchor.issuer,
                    anchor.subject,
                    anchor.organization_id,
                    anchor.workspace_id,
                    claim_id,
                    self._instance_id,
                    revision,
                ),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("credential_refresh_claim_lost")

    def fail_refresh(self, account_ref: str, claim_id: str) -> None:
        """Invalidate stale token material after an uncertain rotation result."""

        with self.transaction() as db:
            changed = db.execute(
                "UPDATE accounts SET credential_blob=NULL, "
                "credential_status='reauthorization_required',refresh_claim_id=NULL, "
                "refresh_claim_owner=NULL,refresh_claimed_at=NULL "
                "WHERE account_ref=? AND status='active' "
                "AND credential_status='refresh_pending' AND refresh_claim_id=? "
                "AND refresh_claim_owner=?",
                (account_ref, claim_id, self._instance_id),
            ).rowcount
            if changed == 1:
                db.execute(
                    "DELETE FROM credential_leases WHERE account_ref=?", (account_ref,)
                )

    def create_provider_profile(
        self,
        *,
        profile_ref: str,
        anchor: Anchor,
        provider: str,
        identity_ref: str,
        target_scope_json: str,
        auth_status: str,
        external_subject: str | None,
        credential_blob: bytes | None,
        credential_expires_at: int | None,
        credential_secret_ref: str | None,
        auth_mode: str | None = None,
        credential_refresh_blob: bytes | None = None,
        credential_refresh_expires_at: int | None = None,
        oauth_client_id: str | None = None,
        oauth_scopes_json: str | None = None,
        access_mode: str = "actor_owned",
        now: int,
    ) -> None:
        """Persist one owner-bound provider profile and encrypted credential."""

        if auth_mode is None:
            auth_mode = {
                "github": "github_app",
                "huggingface": "fine_grained_token",
                "microsoft_graph": "delegated_oauth",
                "azure_devops": "delegated_oauth",
            }.get(provider, "host_identity")
        if auth_mode not in {
            "fine_grained_token",
            "device_oauth",
            "github_app",
            "host_identity",
            "delegated_oauth",
        }:
            raise AccountCenterError("invalid_request")
        if access_mode not in {"actor_owned", "workspace_operator"}:
            raise AccountCenterError("invalid_request")
        if access_mode == "workspace_operator" and anchor.workspace_id is None:
            raise AccountCenterError("profile_unavailable")
        with self.transaction() as db:
            try:
                db.execute(
                    "INSERT INTO provider_profiles(profile_ref,anchor_issuer,anchor_subject,"
                    "organization_id,workspace_id,provider,identity_ref,target_scope_json,"
                    "auth_status,external_subject,credential_blob,credential_expires_at,"
                    "credential_secret_ref,status,created_at,updated_at,auth_mode,"
                    "credential_refresh_blob,credential_refresh_expires_at,oauth_client_id,"
                    "oauth_scopes_json,access_mode,credential_revision) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?,0)",
                    (
                        profile_ref,
                        anchor.issuer,
                        anchor.subject,
                        anchor.organization_id,
                        anchor.workspace_id,
                        provider,
                        identity_ref,
                        target_scope_json,
                        auth_status,
                        external_subject,
                        credential_blob,
                        credential_expires_at,
                        credential_secret_ref,
                        now,
                        now,
                        auth_mode,
                        credential_refresh_blob,
                        credential_refresh_expires_at,
                        oauth_client_id,
                        oauth_scopes_json,
                        access_mode,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = db.execute(
                    "SELECT * FROM provider_profiles WHERE profile_ref=?",
                    (profile_ref,),
                ).fetchone()
                if existing is not None and self._owns(existing, anchor):
                    raise AccountCenterError("profile_already_exists") from None
                external = db.execute(
                    "SELECT * FROM provider_profiles WHERE provider=? AND identity_ref=? "
                    "AND organization_id=? AND status='active'",
                    (provider, identity_ref, anchor.organization_id),
                ).fetchone()
                if external is not None and not self._owns(external, anchor):
                    raise AccountCenterError("external_identity_owned") from None
                if external is not None:
                    raise AccountCenterError("duplicate_provider_identity") from None
                raise AccountCenterError("profile_ref_unavailable") from None

    def begin_bilibili_link_session(
        self,
        *,
        profile_ref: str,
        session_digest: str,
        anchor: Anchor,
        binding_id: str,
        caller_id: str,
        access_mode: str,
        expires_at: int,
        now: int,
    ) -> None:
        """Persist one short-lived QR session under its verified owner and binding."""

        if (
            len(session_digest) != 64
            or any(character not in "0123456789abcdef" for character in session_digest)
            or not binding_id
            or len(binding_id) > 200
            or caller_id != "cyrene.connectors.bilibili"
            or access_mode not in {"actor_owned", "workspace_operator"}
            or (access_mode == "workspace_operator" and anchor.workspace_id is None)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or expires_at <= now
            or expires_at - now > 600
        ):
            raise AccountCenterError("invalid_request")

        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=?",
                (profile_ref,),
            ).fetchone()
            if existing is not None:
                if not self._owns_provider_profile(
                    existing,
                    anchor,
                    allow_workspace_operator=access_mode == "workspace_operator",
                ):
                    raise AccountCenterError("profile_ref_unavailable")
                if existing["provider"] != "bilibili":
                    raise AccountCenterError("profile_provider_mismatch")
                if existing["access_mode"] != access_mode:
                    raise AccountCenterError("profile_unavailable")

            pending = db.execute(
                "SELECT * FROM bilibili_link_sessions WHERE profile_ref=? "
                "AND status='pending'",
                (profile_ref,),
            ).fetchall()
            for session in pending:
                if (
                    session["anchor_issuer"] != anchor.issuer
                    or session["anchor_subject"] != anchor.subject
                    or session["organization_id"] != anchor.organization_id
                    or session["workspace_id"] != anchor.workspace_id
                ):
                    raise AccountCenterError("profile_ref_unavailable")
            db.execute(
                "UPDATE bilibili_link_sessions SET status='cancelled',completed_at=? "
                "WHERE profile_ref=? AND status='pending'",
                (now, profile_ref),
            )
            try:
                db.execute(
                    "INSERT INTO bilibili_link_sessions(session_digest,profile_ref,"
                    "anchor_issuer,anchor_subject,organization_id,workspace_id,binding_id,"
                    "caller_id,access_mode,expires_at,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?)",
                    (
                        session_digest,
                        profile_ref,
                        anchor.issuer,
                        anchor.subject,
                        anchor.organization_id,
                        anchor.workspace_id,
                        binding_id,
                        caller_id,
                        access_mode,
                        expires_at,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                raise AccountCenterError("link_session_invalid") from None

    def complete_bilibili_link_session(
        self,
        *,
        profile_ref: str,
        session_digest: str,
        anchor: Anchor,
        binding_id: str,
        caller_id: str,
        external_subject: str,
        target_scope_json: str,
        credential_blob: bytes,
        credential_expires_at: int | None,
        now: int,
    ) -> dict[str, Any]:
        """Consume one verified QR session and atomically store its encrypted cookie jar."""

        if (
            len(session_digest) != 64
            or any(character not in "0123456789abcdef" for character in session_digest)
            or caller_id != "cyrene.connectors.bilibili"
            or not credential_blob
            or not external_subject.isascii()
            or not external_subject.isdecimal()
            or not 1 <= len(external_subject) <= 20
            or (
                credential_expires_at is not None
                and (
                    isinstance(credential_expires_at, bool)
                    or not isinstance(credential_expires_at, int)
                    or credential_expires_at <= now
                )
            )
        ):
            raise AccountCenterError("invalid_request")

        with self.transaction() as db:
            session = db.execute(
                "SELECT * FROM bilibili_link_sessions WHERE session_digest=?",
                (session_digest,),
            ).fetchone()
            if (
                session is None
                or session["profile_ref"] != profile_ref
                or session["anchor_issuer"] != anchor.issuer
                or session["anchor_subject"] != anchor.subject
                or session["organization_id"] != anchor.organization_id
                or session["workspace_id"] != anchor.workspace_id
                or session["binding_id"] != binding_id
                or session["caller_id"] != caller_id
                or session["status"] != "pending"
                or session["expires_at"] <= now
            ):
                raise AccountCenterError("link_session_invalid")

            access_mode = str(session["access_mode"])
            existing = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=?",
                (profile_ref,),
            ).fetchone()
            if existing is not None:
                if not self._owns_provider_profile(
                    existing,
                    anchor,
                    allow_workspace_operator=access_mode == "workspace_operator",
                ):
                    raise AccountCenterError("profile_ref_unavailable")
                if existing["provider"] != "bilibili":
                    raise AccountCenterError("profile_provider_mismatch")
                if existing["access_mode"] != access_mode:
                    raise AccountCenterError("profile_unavailable")
                if (
                    existing["status"] == "active"
                    and existing["external_subject"]
                    and existing["external_subject"] != external_subject
                ):
                    raise AccountCenterError("external_identity_mismatch")

            external = db.execute(
                "SELECT * FROM provider_profiles WHERE provider='bilibili' "
                "AND identity_ref=? AND organization_id=? AND status='active' "
                "AND profile_ref!=?",
                (external_subject, anchor.organization_id, profile_ref),
            ).fetchone()
            if external is not None:
                if not self._owns_provider_profile(
                    external,
                    anchor,
                    allow_workspace_operator=access_mode == "workspace_operator",
                ):
                    raise AccountCenterError("external_identity_owned")
                raise AccountCenterError("duplicate_provider_identity")

            if existing is None:
                try:
                    db.execute(
                        "INSERT INTO provider_profiles(profile_ref,anchor_issuer,anchor_subject,"
                        "organization_id,workspace_id,provider,identity_ref,target_scope_json,"
                        "auth_status,external_subject,credential_blob,credential_expires_at,"
                        "credential_secret_ref,status,created_at,updated_at,auth_mode,"
                        "credential_refresh_blob,credential_refresh_expires_at,oauth_client_id,"
                        "oauth_scopes_json,refresh_claim_id,refresh_claim_owner,refresh_claimed_at,"
                        "credential_revision,access_mode) VALUES(?,?,?,?,?,'bilibili',?,?,'healthy',"
                        "?,?,?,NULL,'active',?,?,'session_cookie',NULL,NULL,NULL,NULL,NULL,NULL,NULL,1,?)",
                        (
                            profile_ref,
                            anchor.issuer,
                            anchor.subject,
                            anchor.organization_id,
                            anchor.workspace_id,
                            external_subject,
                            target_scope_json,
                            external_subject,
                            credential_blob,
                            credential_expires_at,
                            now,
                            now,
                            access_mode,
                        ),
                    )
                except sqlite3.IntegrityError:
                    raise AccountCenterError("duplicate_provider_identity") from None
                revision = 1
            else:
                revision = int(existing["credential_revision"]) + 1
                db.execute(
                    "UPDATE provider_profiles SET identity_ref=?,target_scope_json=?,"
                    "auth_status='healthy',external_subject=?,credential_blob=?,"
                    "credential_expires_at=?,credential_secret_ref=NULL,status='active',"
                    "updated_at=?,auth_mode='session_cookie',credential_refresh_blob=NULL,"
                    "credential_refresh_expires_at=NULL,oauth_client_id=NULL,oauth_scopes_json=NULL,"
                    "refresh_claim_id=NULL,refresh_claim_owner=NULL,refresh_claimed_at=NULL,"
                    "credential_revision=?,access_mode=? WHERE profile_ref=?",
                    (
                        external_subject,
                        target_scope_json,
                        external_subject,
                        credential_blob,
                        credential_expires_at,
                        now,
                        revision,
                        access_mode,
                        profile_ref,
                    ),
                )

            changed = db.execute(
                "UPDATE bilibili_link_sessions SET status='completed',completed_at=? "
                "WHERE session_digest=? AND status='pending' AND expires_at>?",
                (now, session_digest, now),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("link_session_invalid")
            db.execute(
                "UPDATE bilibili_link_sessions SET status='cancelled',completed_at=? "
                "WHERE profile_ref=? AND session_digest!=? AND status='pending'",
                (now, profile_ref, session_digest),
            )
            db.execute(
                "DELETE FROM provider_credential_leases WHERE profile_ref=?",
                (profile_ref,),
            )
            row = db.execute(
                "SELECT profile_ref,provider,auth_status,external_subject,"
                "credential_expires_at,auth_mode,access_mode,workspace_id,credential_revision "
                "FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (profile_ref,),
            ).fetchone()
            if row is None:
                raise AccountCenterError("storage_unavailable")
            return dict(row)

    def list_workspace_links(
        self, organization_id: str, workspace_id: str | None, *, limit: int
    ) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        """Return metadata-only links and profiles of every owner in one workspace.

        Only the owner key, provider, health, link time, and external subject are
        selected; credential material, leases, and scopes never leave storage. At
        most ``limit + 1`` rows per table are read so the caller can fail closed.
        中文：只读取属主、提供方、状态、时间和外部主体，不读取任何凭据。
        """

        with self._connect() as db:
            accounts = db.execute(
                "SELECT anchor_issuer,anchor_subject,provider,"
                "stable_subject AS external_subject,"
                "CASE WHEN credential_status!='ready' OR "
                "(provider='wecom' AND bot_namespace IS NULL) "
                "THEN 'reauthorization_required' "
                "WHEN remote_revocation_status='pending' THEN 'revocation_pending' "
                "WHEN remote_revocation_status='failed' THEN 'revocation_failed' "
                "WHEN remote_revocation_status='unknown' THEN 'revocation_unknown' "
                "ELSE status END AS status,linked_at "
                "FROM accounts WHERE organization_id=? AND workspace_id IS ? "
                "AND status!='unlinked' "
                "ORDER BY anchor_issuer,anchor_subject,linked_at,account_ref LIMIT ?",
                (organization_id, workspace_id, limit + 1),
            ).fetchall()
            profiles = db.execute(
                "SELECT anchor_issuer,anchor_subject,provider,external_subject,"
                "auth_status AS status,created_at AS linked_at "
                "FROM provider_profiles WHERE organization_id=? AND workspace_id IS ? "
                "AND status='active' "
                "ORDER BY anchor_issuer,anchor_subject,created_at,profile_ref LIMIT ?",
                (organization_id, workspace_id, limit + 1),
            ).fetchall()
        return list(accounts), list(profiles)

    def list_active_links(
        self, anchor: Anchor, provider: str
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Return one owner's active link refs for one provider.

        Accounts come back as refs; profiles as ``(ref, access_mode)`` so the
        caller can leave shared workspace-operator profiles alone.
        中文：返回某人在某平台的有效关联引用；共享运维档案附带访问模式。
        """

        owner = (anchor.issuer, anchor.subject, anchor.organization_id, anchor.workspace_id)
        with self._connect() as db:
            accounts = db.execute(
                "SELECT account_ref FROM accounts WHERE anchor_issuer=? AND anchor_subject=? "
                "AND organization_id=? AND workspace_id IS ? AND provider=? AND status='active' "
                "ORDER BY linked_at,account_ref",
                (*owner, provider),
            ).fetchall()
            profiles = db.execute(
                "SELECT profile_ref,access_mode FROM provider_profiles WHERE anchor_issuer=? "
                "AND anchor_subject=? AND organization_id=? AND workspace_id IS ? "
                "AND provider=? AND status='active' ORDER BY created_at,profile_ref",
                (*owner, provider),
            ).fetchall()
        return (
            [row["account_ref"] for row in accounts],
            [(row["profile_ref"], row["access_mode"]) for row in profiles],
        )

    def record_admin_action(
        self,
        *,
        anchor: Anchor,
        action: str,
        target_subject: str,
        provider: str,
        unlinked: int,
        skipped_shared: int,
        now: int,
        result: dict[str, Any],
        command: tuple[str, Callable[[dict[str, Any]], bytes]] | None = None,
    ) -> None:
        """Audit one administrator action and finish its idempotent command atomically.

        中文：记录管理员对他人关联的操作，并在同一事务里完成幂等记录。
        """

        with self.transaction() as db:
            db.execute(
                "INSERT INTO account_admin_audit(action_ref,organization_id,workspace_id,"
                "actor_issuer,actor_subject,action,target_subject,provider,unlinked,"
                "skipped_shared,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    anchor.organization_id,
                    anchor.workspace_id,
                    anchor.issuer,
                    anchor.subject,
                    action,
                    target_subject,
                    provider,
                    unlinked,
                    skipped_shared,
                    now,
                ),
            )
            if command is not None:
                key_hash, encode = command
                self._complete_command_in_transaction(db, key_hash, encode({"value": result}))

    def list_workspace_people(
        self, organization_id: str, workspace_id: str | None, *, limit: int
    ) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        """Return declared people and their active handles for one workspace.

        At most ``limit + 1`` rows per table are read so the caller can fail
        closed instead of truncating. 中文：最多读取 limit+1 行，超限由调用方拒绝。
        """

        with self._connect() as db:
            people = db.execute(
                "SELECT person_ref,issuer,subject,mail,display_name FROM workspace_people "
                "WHERE organization_id=? AND workspace_id IS ? "
                "ORDER BY created_at,person_ref LIMIT ?",
                (organization_id, workspace_id, limit + 1),
            ).fetchall()
            handles = db.execute(
                "SELECT person_ref,provider,handle,updated_at FROM account_handles "
                "WHERE organization_id=? AND workspace_id IS ? AND status='active' "
                "ORDER BY created_at,handle_ref LIMIT ?",
                (organization_id, workspace_id, limit + 1),
            ).fetchall()
        return list(people), list(handles)

    def declare_account_handle(
        self,
        *,
        anchor: Anchor,
        subject: str | None,
        mail: str | None,
        display_name: str | None,
        provider: str,
        handle: str,
        key: str,
        now: int,
        command: tuple[str, Callable[[dict[str, Any]], bytes]] | None = None,
    ) -> dict[str, Any]:
        """Attach one declared handle to a person, creating the person if new.

        A person is found by identity subject or by mail. Two different people
        matching the two keys, or a conflicting stored identity, fail closed with
        ``person_conflict``; a handle already held by someone else fails with
        ``handle_owned``. Re-declaring the same handle for the same person is a
        no-op. When ``command`` is given, its replay response is stored in the
        same transaction. 中文：同一账号只能属于一个人；冲突时整笔回滚。
        """

        scope = (anchor.organization_id, anchor.workspace_id)
        with self.transaction() as db:
            by_subject = None
            if subject is not None:
                by_subject = db.execute(
                    "SELECT * FROM workspace_people WHERE organization_id=? "
                    "AND workspace_id IS ? AND issuer=? AND subject=?",
                    (*scope, anchor.issuer, subject),
                ).fetchone()
            by_mail = None
            if mail is not None:
                by_mail = db.execute(
                    "SELECT * FROM workspace_people WHERE organization_id=? "
                    "AND workspace_id IS ? AND mail=?",
                    (*scope, mail),
                ).fetchone()
            if (
                by_subject is not None
                and by_mail is not None
                and by_subject["person_ref"] != by_mail["person_ref"]
            ):
                raise AccountCenterError("person_conflict")
            person = by_subject if by_subject is not None else by_mail
            if person is None:
                person_ref = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO workspace_people(person_ref,organization_id,workspace_id,"
                    "issuer,subject,mail,display_name,created_by_issuer,created_by_subject,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        person_ref,
                        *scope,
                        anchor.issuer,
                        subject,
                        mail,
                        display_name,
                        anchor.issuer,
                        anchor.subject,
                        now,
                        now,
                    ),
                )
            else:
                person_ref = person["person_ref"]
                if (subject is not None and person["subject"] not in (None, subject)) or (
                    mail is not None and person["mail"] not in (None, mail)
                ):
                    raise AccountCenterError("person_conflict")
                merged = (
                    person["subject"] or subject,
                    person["mail"] or mail,
                    display_name or person["display_name"],
                )
                if merged != (person["subject"], person["mail"], person["display_name"]):
                    db.execute(
                        "UPDATE workspace_people SET subject=?,mail=?,display_name=?,"
                        "updated_at=? WHERE person_ref=?",
                        (*merged, now, person_ref),
                    )
            existing = db.execute(
                "SELECT handle_ref,person_ref,handle FROM account_handles "
                "WHERE organization_id=? AND workspace_id IS ? AND provider=? "
                "AND handle_key=? AND status='active'",
                (*scope, provider, key),
            ).fetchone()
            if existing is not None:
                if existing["person_ref"] != person_ref:
                    raise AccountCenterError("handle_owned")
                result = {
                    "person_ref": person_ref,
                    "handle_ref": existing["handle_ref"],
                    "provider": provider,
                    "handle": existing["handle"],
                    "source": "declared",
                    "created": False,
                }
            else:
                handle_ref = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO account_handles(handle_ref,person_ref,organization_id,"
                    "workspace_id,provider,handle,handle_key,status,declared_by_issuer,"
                    "declared_by_subject,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'active',?,?,?,?)",
                    (
                        handle_ref,
                        person_ref,
                        *scope,
                        provider,
                        handle,
                        key,
                        anchor.issuer,
                        anchor.subject,
                        now,
                        now,
                    ),
                )
                result = {
                    "person_ref": person_ref,
                    "handle_ref": handle_ref,
                    "provider": provider,
                    "handle": handle,
                    "source": "declared",
                    "created": True,
                }
            if command is not None:
                key_hash, encode = command
                self._complete_command_in_transaction(db, key_hash, encode({"value": result}))
            return result

    def retract_account_handle(
        self,
        *,
        anchor: Anchor,
        provider: str,
        key: str,
        now: int,
        command: tuple[str, Callable[[dict[str, Any]], bytes]] | None = None,
    ) -> dict[str, Any]:
        """Retire one active declared handle; the row stays for audit.

        中文：撤回后记录保留用于审计，同一账号之后可以重新声明。
        """

        with self.transaction() as db:
            row = db.execute(
                "SELECT handle_ref,person_ref,handle FROM account_handles "
                "WHERE organization_id=? AND workspace_id IS ? AND provider=? "
                "AND handle_key=? AND status='active'",
                (anchor.organization_id, anchor.workspace_id, provider, key),
            ).fetchone()
            if row is None:
                raise AccountCenterError("handle_not_found")
            db.execute(
                "UPDATE account_handles SET status='retracted',updated_at=? "
                "WHERE handle_ref=?",
                (now, row["handle_ref"]),
            )
            result = {
                "retracted": True,
                "person_ref": row["person_ref"],
                "provider": provider,
                "handle": row["handle"],
            }
            if command is not None:
                key_hash, encode = command
                self._complete_command_in_transaction(db, key_hash, encode({"value": result}))
            return result

    def list_provider_profiles(self, anchor: Anchor) -> list[sqlite3.Row]:
        """Return metadata-only profiles owned by the exact actor/workspace."""

        with self._connect() as db:
            rows = db.execute(
                "SELECT profile_ref,provider,identity_ref,target_scope_json,auth_status,"
                "external_subject,credential_expires_at,status,created_at,updated_at,"
                "auth_mode,oauth_scopes_json,access_mode,credential_revision,"
                "credential_refresh_expires_at "
                "FROM provider_profiles WHERE anchor_issuer=? AND anchor_subject=? "
                "AND organization_id=? AND workspace_id IS ? AND status='active' "
                "ORDER BY created_at,profile_ref",
                (
                    anchor.issuer,
                    anchor.subject,
                    anchor.organization_id,
                    anchor.workspace_id,
                ),
            ).fetchall()
        return list(rows)

    def begin_huggingface_device_renewal(
        self,
        *,
        profile_ref: str,
        session_ref: str,
        anchor: Anchor,
        now: int,
    ) -> tuple[dict[str, Any], bool]:
        """Persist one owner-bound start intent before calling HF's device endpoint."""

        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if existing is not None:
                if existing["profile_ref"] != profile_ref or not self._owns(
                    existing, anchor
                ):
                    raise AccountCenterError("renewal_session_not_found")
                if (
                    existing["status"] == "starting"
                    and int(existing["created_at"]) <= now - 60
                ):
                    db.execute(
                        "UPDATE huggingface_device_renewal_sessions SET status='unknown',"
                        "reason_code='device_start_outcome_unknown',updated_at=? "
                        "WHERE session_ref=? AND status='starting'",
                        (now, session_ref),
                    )
                    existing = db.execute(
                        "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                        (session_ref,),
                    ).fetchone()
                return dict(existing), False

            profile = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (profile_ref,),
            ).fetchone()
            if profile is None or not self._owns_provider_profile(profile, anchor):
                raise AccountCenterError("profile_not_found")
            if (
                profile["provider"] != "huggingface"
                or profile["auth_mode"] != "device_oauth"
                or not profile["oauth_client_id"]
                or not profile["oauth_scopes_json"]
            ):
                raise AccountCenterError("profile_provider_mismatch")
            db.execute(
                "INSERT INTO huggingface_device_renewal_sessions "
                "(session_ref,profile_ref,anchor_issuer,anchor_subject,organization_id,"
                "workspace_id,status,profile_revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'starting',?,?,?)",
                (
                    session_ref,
                    profile_ref,
                    anchor.issuer,
                    anchor.subject,
                    anchor.organization_id,
                    anchor.workspace_id,
                    profile["credential_revision"],
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if row is None:
                raise AccountCenterError("storage_unavailable")
            return dict(row), True

    def complete_huggingface_device_renewal_start(
        self,
        session_ref: str,
        anchor: Anchor,
        *,
        encrypted_device_code: bytes,
        user_code: str,
        verification_uri: str,
        expires_at: int,
        interval_seconds: int,
        now: int,
    ) -> dict[str, Any]:
        """Persist encrypted device code and the user-visible short-lived code."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if row is None or not self._owns(row, anchor):
                raise AccountCenterError("renewal_session_not_found")
            if row["status"] != "starting":
                return dict(row)
            db.execute(
                "UPDATE huggingface_device_renewal_sessions SET status='waiting_user',"
                "device_code_blob=?,user_code=?,verification_uri=?,expires_at=?,"
                "interval_seconds=?,next_poll_at=?,updated_at=? WHERE session_ref=? "
                "AND status='starting'",
                (
                    encrypted_device_code,
                    user_code,
                    verification_uri,
                    expires_at,
                    interval_seconds,
                    now + interval_seconds,
                    now,
                    session_ref,
                ),
            )
            updated = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if updated is None:
                raise AccountCenterError("storage_unavailable")
            return dict(updated)

    def fail_huggingface_device_renewal_start(
        self,
        session_ref: str,
        anchor: Anchor,
        *,
        state: str,
        reason_code: str,
        now: int,
    ) -> dict[str, Any]:
        """Record a sanitized start failure without replaying device creation."""

        if (
            state not in {"failed", "unknown"}
            or not reason_code
            or len(reason_code) > 100
        ):
            raise AccountCenterError("invalid_request")
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if row is None or not self._owns(row, anchor):
                raise AccountCenterError("renewal_session_not_found")
            if row["status"] == "starting":
                db.execute(
                    "UPDATE huggingface_device_renewal_sessions SET status=?,reason_code=?,"
                    "device_code_blob=NULL,updated_at=? WHERE session_ref=? AND status='starting'",
                    (state, reason_code, now, session_ref),
                )
            updated = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if updated is None:
                raise AccountCenterError("storage_unavailable")
            return dict(updated)

    def get_huggingface_device_renewal(
        self, session_ref: str, anchor: Anchor
    ) -> dict[str, Any]:
        """Read one device renewal without returning a decrypted device code."""

        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
        if row is None or not self._owns(row, anchor):
            raise AccountCenterError("renewal_session_not_found")
        return dict(row)

    def claim_huggingface_device_renewal_poll(
        self,
        session_ref: str,
        anchor: Anchor,
        *,
        now: int,
        stale_seconds: int = 45,
    ) -> tuple[dict[str, Any], str | None]:
        """Claim one due device-code poll or return its current safe state."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if row is None or not self._owns(row, anchor):
                raise AccountCenterError("renewal_session_not_found")
            if row["status"] == "starting":
                if int(row["created_at"]) <= now - 60:
                    db.execute(
                        "UPDATE huggingface_device_renewal_sessions SET status='unknown',"
                        "reason_code='device_start_outcome_unknown',updated_at=? "
                        "WHERE session_ref=? AND status='starting'",
                        (now, session_ref),
                    )
                    row = db.execute(
                        "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                        (session_ref,),
                    ).fetchone()
                return dict(row), None
            if row["status"] != "waiting_user":
                return dict(row), None
            expires_at = row["expires_at"]
            if isinstance(expires_at, int) and expires_at <= now:
                db.execute(
                    "UPDATE huggingface_device_renewal_sessions SET status='expired',"
                    "device_code_blob=NULL,user_code=NULL,verification_uri=NULL,"
                    "reason_code='device_code_expired',updated_at=? "
                    "WHERE session_ref=? AND status='waiting_user'",
                    (now, session_ref),
                )
                row = db.execute(
                    "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                    (session_ref,),
                ).fetchone()
                return dict(row), None
            if isinstance(row["next_poll_at"], int) and row["next_poll_at"] > now:
                return dict(row), None
            if row["poll_claim_id"] is not None:
                claimed_at = row["poll_claimed_at"]
                if isinstance(claimed_at, int) and now - claimed_at <= stale_seconds:
                    return dict(row), None
                db.execute(
                    "UPDATE huggingface_device_renewal_sessions SET status='unknown',"
                    "device_code_blob=NULL,user_code=NULL,verification_uri=NULL,"
                    "poll_claim_id=NULL,poll_claim_owner=NULL,"
                    "poll_claimed_at=NULL,reason_code='device_poll_outcome_unknown',updated_at=? "
                    "WHERE session_ref=? AND status='waiting_user'",
                    (now, session_ref),
                )
                row = db.execute(
                    "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                    (session_ref,),
                ).fetchone()
                return dict(row), None
            claim_id = str(uuid.uuid4())
            changed = db.execute(
                "UPDATE huggingface_device_renewal_sessions SET poll_claim_id=?,"
                "poll_claim_owner=?,poll_claimed_at=?,updated_at=? WHERE session_ref=? "
                "AND status='waiting_user' AND poll_claim_id IS NULL",
                (claim_id, self._instance_id, now, now, session_ref),
            ).rowcount
            if changed != 1:
                current = db.execute(
                    "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                    (session_ref,),
                ).fetchone()
                if current is None:
                    raise AccountCenterError("renewal_session_not_found")
                return dict(current), None
            claimed = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if claimed is None:
                raise AccountCenterError("storage_unavailable")
            return dict(claimed), claim_id

    def defer_huggingface_device_renewal_poll(
        self,
        session_ref: str,
        anchor: Anchor,
        claim_id: str,
        *,
        interval_seconds: int,
        reason_code: str | None,
        now: int,
    ) -> dict[str, Any]:
        """Persist pending/slow_down and release the one-poll claim."""

        if not 1 <= interval_seconds <= 300:
            raise AccountCenterError("invalid_request")
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if (
                row is None
                or not self._owns(row, anchor)
                or row["status"] != "waiting_user"
                or row["poll_claim_id"] != claim_id
                or row["poll_claim_owner"] != self._instance_id
            ):
                raise AccountCenterError("renewal_poll_claim_lost")
            db.execute(
                "UPDATE huggingface_device_renewal_sessions SET interval_seconds=?,"
                "next_poll_at=?,poll_claim_id=NULL,poll_claim_owner=NULL,poll_claimed_at=NULL,"
                "reason_code=?,updated_at=? WHERE session_ref=? AND poll_claim_id=?",
                (
                    interval_seconds,
                    now + interval_seconds,
                    reason_code,
                    now,
                    session_ref,
                    claim_id,
                ),
            )
            updated = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if updated is None:
                raise AccountCenterError("storage_unavailable")
            return dict(updated)

    def fail_huggingface_device_renewal_poll(
        self,
        session_ref: str,
        anchor: Anchor,
        claim_id: str,
        *,
        state: str,
        reason_code: str,
        now: int,
    ) -> dict[str, Any]:
        """Finalize a denied/expired/ambiguous poll and destroy device-code material."""

        if state not in {"failed", "expired", "cancelled", "unknown"}:
            raise AccountCenterError("invalid_request")
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if (
                row is None
                or not self._owns(row, anchor)
                or row["status"] != "waiting_user"
                or row["poll_claim_id"] != claim_id
                or row["poll_claim_owner"] != self._instance_id
            ):
                raise AccountCenterError("renewal_poll_claim_lost")
            db.execute(
                "UPDATE huggingface_device_renewal_sessions SET status=?,device_code_blob=NULL,"
                "user_code=NULL,verification_uri=NULL,poll_claim_id=NULL,poll_claim_owner=NULL,"
                "poll_claimed_at=NULL,reason_code=?,"
                "updated_at=? WHERE session_ref=? AND poll_claim_id=?",
                (state, reason_code[:100], now, session_ref, claim_id),
            )
            updated = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if updated is None:
                raise AccountCenterError("storage_unavailable")
            return dict(updated)

    def complete_huggingface_device_renewal(
        self,
        session_ref: str,
        anchor: Anchor,
        claim_id: str,
        *,
        credential_blob: bytes,
        credential_refresh_blob: bytes,
        credential_expires_at: int,
        credential_refresh_expires_at: int | None,
        oauth_scopes_json: str | None,
        external_subject: str,
        now: int,
    ) -> dict[str, Any]:
        """Atomically replace OAuth material and complete the owner-scoped session."""

        with self.transaction() as db:
            session = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if (
                session is None
                or not self._owns(session, anchor)
                or session["status"] != "waiting_user"
                or session["poll_claim_id"] != claim_id
                or session["poll_claim_owner"] != self._instance_id
            ):
                raise AccountCenterError("renewal_poll_claim_lost")
            profile = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (session["profile_ref"],),
            ).fetchone()
            if profile is None or not self._owns_provider_profile(profile, anchor):
                raise AccountCenterError("profile_not_found")
            if (
                profile["provider"] != "huggingface"
                or profile["auth_mode"] != "device_oauth"
                or profile["credential_revision"] != session["profile_revision"]
                or profile["refresh_claim_id"] is not None
                or profile["external_subject"] != external_subject
            ):
                db.execute(
                    "UPDATE huggingface_device_renewal_sessions SET status='failed',"
                    "device_code_blob=NULL,poll_claim_id=NULL,poll_claim_owner=NULL,"
                    "poll_claimed_at=NULL,reason_code='profile_changed_during_renewal',"
                    "updated_at=? WHERE session_ref=? AND poll_claim_id=?",
                    (now, session_ref, claim_id),
                )
                updated = db.execute(
                    "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                    (session_ref,),
                ).fetchone()
                if updated is None:
                    raise AccountCenterError("storage_unavailable")
                return dict(updated)
            updated_profile = db.execute(
                "UPDATE provider_profiles SET credential_blob=?,credential_refresh_blob=?,"
                "credential_expires_at=?,credential_refresh_expires_at=?,oauth_scopes_json=?,"
                "auth_status='healthy',refresh_claim_id=NULL,refresh_claim_owner=NULL,"
                "refresh_claimed_at=NULL,credential_revision=credential_revision+1,updated_at=? "
                "WHERE profile_ref=? AND credential_revision=? AND refresh_claim_id IS NULL",
                (
                    credential_blob,
                    credential_refresh_blob,
                    credential_expires_at,
                    credential_refresh_expires_at,
                    oauth_scopes_json,
                    now,
                    session["profile_ref"],
                    session["profile_revision"],
                ),
            )
            if updated_profile.rowcount != 1:
                raise AccountCenterError("credential_refresh_claim_lost")
            db.execute(
                "DELETE FROM provider_credential_leases WHERE profile_ref=?",
                (session["profile_ref"],),
            )
            db.execute(
                "UPDATE huggingface_device_renewal_sessions SET status='succeeded',"
                "device_code_blob=NULL,user_code=NULL,verification_uri=NULL,"
                "poll_claim_id=NULL,poll_claim_owner=NULL,"
                "poll_claimed_at=NULL,reason_code=NULL,updated_at=? WHERE session_ref=? "
                "AND poll_claim_id=?",
                (now, session_ref, claim_id),
            )
            updated = db.execute(
                "SELECT * FROM huggingface_device_renewal_sessions WHERE session_ref=?",
                (session_ref,),
            ).fetchone()
            if updated is None:
                raise AccountCenterError("storage_unavailable")
            return dict(updated)

    def get_provider_profile(
        self,
        profile_ref: str,
        anchor: Anchor,
        *,
        allow_workspace_operator: bool = False,
    ) -> sqlite3.Row:
        """Return a profile to its owner or a caller with verified workspace proof."""

        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (profile_ref,),
            ).fetchone()
        if row is None or not self._owns_provider_profile(
            row, anchor, allow_workspace_operator=allow_workspace_operator
        ):
            raise AccountCenterError("profile_not_found")
        return row

    def update_provider_profile_credential(
        self,
        *,
        profile_ref: str,
        anchor: Anchor,
        credential_blob: bytes,
        credential_expires_at: int | None,
        external_subject: str,
        auth_status: str,
        now: int,
        allow_workspace_operator: bool = False,
    ) -> None:
        """Replace encrypted profile credentials only when identity remains stable."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (profile_ref,),
            ).fetchone()
            if row is None or not self._owns_provider_profile(
                row, anchor, allow_workspace_operator=allow_workspace_operator
            ):
                raise AccountCenterError("profile_not_found")
            if row["external_subject"] and row["external_subject"] != external_subject:
                raise AccountCenterError("external_identity_mismatch")
            db.execute(
                "UPDATE provider_profiles SET credential_blob=?,credential_expires_at=?,"
                "external_subject=?,auth_status=?,updated_at=? WHERE profile_ref=?",
                (
                    credential_blob,
                    credential_expires_at,
                    external_subject,
                    auth_status,
                    now,
                    profile_ref,
                ),
            )

    def update_provider_profile_target_scope(
        self,
        *,
        profile_ref: str,
        anchor: Anchor,
        expected_revision: int,
        target_scope_json: str,
        now: int,
    ) -> int:
        """CAS-update one owner-bound provider target scope and bump its revision."""

        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise AccountCenterError("profile_revision_conflict")
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (profile_ref,),
            ).fetchone()
            if row is None or not self._owns_provider_profile(row, anchor):
                raise AccountCenterError("profile_not_found")
            if (
                row["provider"] != "microsoft_graph"
                or row["access_mode"] != "actor_owned"
                or row["auth_status"] != "healthy"
                or row["credential_revision"] != expected_revision
            ):
                raise AccountCenterError("profile_revision_conflict")
            updated = db.execute(
                "UPDATE provider_profiles SET target_scope_json=?,"
                "credential_revision=credential_revision+1,updated_at=? "
                "WHERE profile_ref=? AND credential_revision=? AND status='active'",
                (target_scope_json, now, profile_ref, expected_revision),
            ).rowcount
            if updated != 1:
                raise AccountCenterError("profile_revision_conflict")
            return expected_revision + 1

    def update_huggingface_profile_credentials(
        self,
        *,
        profile_ref: str,
        anchor: Anchor,
        credential_blob: bytes,
        credential_refresh_blob: bytes | None,
        credential_expires_at: int | None,
        credential_refresh_expires_at: int | None,
        oauth_client_id: str | None,
        oauth_scopes_json: str | None,
        auth_mode: str,
        credential_secret_ref: str,
        external_subject: str,
        now: int,
    ) -> None:
        """Replace HF access and refresh material atomically for the pinned owner."""

        if auth_mode not in {"fine_grained_token", "device_oauth"}:
            raise AccountCenterError("invalid_request")
        if auth_mode == "device_oauth" and (
            credential_refresh_blob is None or not oauth_client_id
        ):
            raise AccountCenterError("credential_unavailable")
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (profile_ref,),
            ).fetchone()
            if row is None or not self._owns(row, anchor):
                raise AccountCenterError("profile_not_found")
            if row["provider"] != "huggingface":
                raise AccountCenterError("profile_provider_mismatch")
            if row["external_subject"] and row["external_subject"] != external_subject:
                raise AccountCenterError("external_identity_mismatch")
            db.execute(
                "UPDATE provider_profiles SET credential_blob=?,credential_refresh_blob=?,"
                "credential_expires_at=?,credential_refresh_expires_at=?,oauth_client_id=?,"
                "oauth_scopes_json=?,auth_mode=?,credential_secret_ref=?,external_subject=?,"
                "auth_status='healthy',refresh_claim_id=NULL,refresh_claim_owner=NULL,"
                "refresh_claimed_at=NULL,credential_revision=credential_revision+1,"
                "updated_at=? WHERE profile_ref=?",
                (
                    credential_blob,
                    credential_refresh_blob,
                    credential_expires_at,
                    credential_refresh_expires_at,
                    oauth_client_id,
                    oauth_scopes_json,
                    auth_mode,
                    credential_secret_ref,
                    external_subject,
                    now,
                    profile_ref,
                ),
            )
            db.execute(
                "UPDATE huggingface_device_renewal_sessions SET status='failed',"
                "device_code_blob=NULL,user_code=NULL,verification_uri=NULL,poll_claim_id=NULL,"
                "poll_claim_owner=NULL,poll_claimed_at=NULL,"
                "reason_code='profile_credentials_replaced',updated_at=? "
                "WHERE profile_ref=? AND status IN ('starting','waiting_user')",
                (now, profile_ref),
            )
            db.execute(
                "DELETE FROM provider_credential_leases WHERE profile_ref=?",
                (profile_ref,),
            )

    def claim_provider_oauth_refresh(
        self,
        profile_ref: str,
        anchor: Anchor,
        *,
        provider: str,
        auth_mode: str,
        now: int,
        refresh_margin_seconds: int = 60,
        claim_stale_seconds: int = 45,
    ) -> tuple[str, sqlite3.Row | None, str | None]:
        """Claim one durable OAuth rotation or report its current state."""

        with self.transaction() as db:
            profile = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (profile_ref,),
            ).fetchone()
            if profile is None or not self._owns_provider_profile(
                profile, anchor, allow_workspace_operator=True
            ):
                raise AccountCenterError("profile_not_found")
            if profile["provider"] != provider or profile["auth_mode"] != auth_mode:
                raise AccountCenterError("profile_provider_mismatch")
            if profile["auth_status"] != "healthy":
                raise AccountCenterError("credential_reauthorization_required")
            expires_at = profile["credential_expires_at"]
            if (
                isinstance(expires_at, int)
                and expires_at > now + refresh_margin_seconds
                and profile["credential_blob"] is not None
            ):
                return "ready", profile, None
            if profile["credential_refresh_blob"] is None:
                self._mark_provider_reauthorization_required(db, profile_ref, now)
                return "reauthorization_required", None, None
            refresh_expires_at = profile["credential_refresh_expires_at"]
            if isinstance(refresh_expires_at, int) and refresh_expires_at <= now:
                self._mark_provider_reauthorization_required(db, profile_ref, now)
                return "reauthorization_required", None, None
            claim_id = profile["refresh_claim_id"]
            if claim_id is not None:
                claimed_at = profile["refresh_claimed_at"]
                if (
                    isinstance(claimed_at, int)
                    and now - claimed_at <= claim_stale_seconds
                ):
                    return "waiting", None, None
                self._mark_provider_reauthorization_required(db, profile_ref, now)
                return "reauthorization_required", None, None
            claim_id = str(uuid.uuid4())
            changed = db.execute(
                "UPDATE provider_profiles SET refresh_claim_id=?,refresh_claim_owner=?,"
                "refresh_claimed_at=?,updated_at=? WHERE profile_ref=? AND status='active' "
                "AND auth_status='healthy' AND provider=? AND auth_mode=? "
                "AND credential_revision=? AND refresh_claim_id IS NULL",
                (
                    claim_id,
                    self._instance_id,
                    now,
                    now,
                    profile_ref,
                    provider,
                    auth_mode,
                    profile["credential_revision"],
                ),
            ).rowcount
            if changed != 1:
                return "waiting", None, None
            claimed = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=?",
                (profile_ref,),
            ).fetchone()
            return "claimed", claimed, claim_id

    def complete_provider_oauth_refresh(
        self,
        *,
        profile_ref: str,
        anchor: Anchor,
        claim_id: str,
        credential_blob: bytes,
        credential_refresh_blob: bytes,
        credential_expires_at: int,
        credential_refresh_expires_at: int | None,
        oauth_scopes_json: str | None,
        provider: str,
        auth_mode: str,
        now: int,
    ) -> None:
        """Commit rotated token material before the caller can return access."""

        with self.transaction() as db:
            changed = db.execute(
                "UPDATE provider_profiles SET credential_blob=?,credential_refresh_blob=?,"
                "credential_expires_at=?,credential_refresh_expires_at=?,oauth_scopes_json=?,"
                "auth_status='healthy',"
                "refresh_claim_id=NULL,refresh_claim_owner=NULL,refresh_claimed_at=NULL,"
                "credential_revision=credential_revision+1,updated_at=? "
                "WHERE profile_ref=? AND anchor_issuer=? AND organization_id=? "
                "AND workspace_id IS ? AND (anchor_subject=? OR access_mode='workspace_operator') "
                "AND provider=? "
                "AND status='active' AND auth_mode=? "
                "AND refresh_claim_id=? AND refresh_claim_owner=?",
                (
                    credential_blob,
                    credential_refresh_blob,
                    credential_expires_at,
                    credential_refresh_expires_at,
                    oauth_scopes_json,
                    now,
                    profile_ref,
                    anchor.issuer,
                    anchor.organization_id,
                    anchor.workspace_id,
                    anchor.subject,
                    provider,
                    auth_mode,
                    claim_id,
                    self._instance_id,
                ),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("credential_refresh_claim_lost")

    def release_provider_oauth_refresh(
        self,
        *,
        profile_ref: str,
        anchor: Anchor,
        claim_id: str,
        now: int,
        provider: str,
        auth_mode: str,
        auth_status: str = "healthy",
        clear_credentials: bool = False,
    ) -> None:
        """Release a refresh claim, clearing credentials when rotation is unknown."""

        if auth_status not in {"healthy", "unavailable", "reauthorization_required"}:
            raise AccountCenterError("invalid_request")
        clear = (
            ",credential_blob=NULL,credential_refresh_blob=NULL"
            if clear_credentials
            else ""
        )
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE provider_profiles SET auth_status=?" + clear + ","
                "refresh_claim_id=NULL,refresh_claim_owner=NULL,refresh_claimed_at=NULL,"
                "credential_revision=credential_revision+1,updated_at=? "
                "WHERE profile_ref=? AND anchor_issuer=? AND organization_id=? "
                "AND workspace_id IS ? AND (anchor_subject=? OR access_mode='workspace_operator') "
                "AND provider=? AND status='active' AND auth_mode=? "
                "AND refresh_claim_id=? AND refresh_claim_owner=?",
                (
                    auth_status,
                    now,
                    profile_ref,
                    anchor.issuer,
                    anchor.organization_id,
                    anchor.workspace_id,
                    anchor.subject,
                    provider,
                    auth_mode,
                    claim_id,
                    self._instance_id,
                ),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("credential_refresh_claim_lost")
            if clear_credentials or auth_status == "unavailable":
                db.execute(
                    "DELETE FROM provider_credential_leases WHERE profile_ref=?",
                    (profile_ref,),
                )

    @staticmethod
    def _mark_provider_reauthorization_required(
        db: sqlite3.Connection, profile_ref: str, now: int
    ) -> None:
        db.execute(
            "UPDATE provider_profiles SET auth_status='reauthorization_required',"
            "credential_blob=NULL,credential_refresh_blob=NULL,refresh_claim_id=NULL,"
            "refresh_claim_owner=NULL,refresh_claimed_at=NULL,"
            "credential_revision=credential_revision+1,updated_at=? WHERE profile_ref=?",
            (now, profile_ref),
        )
        db.execute(
            "DELETE FROM provider_credential_leases WHERE profile_ref=?", (profile_ref,)
        )

    def update_provider_profile_auth_status(
        self,
        profile_ref: str,
        anchor: Anchor,
        auth_status: str,
        *,
        now: int,
        allow_workspace_operator: bool = False,
    ) -> None:
        """Record a sanitized authentication health transition for a profile."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (profile_ref,),
            ).fetchone()
            if row is None or not self._owns_provider_profile(
                row, anchor, allow_workspace_operator=allow_workspace_operator
            ):
                raise AccountCenterError("profile_not_found")
            db.execute(
                "UPDATE provider_profiles SET auth_status=?,updated_at=? WHERE profile_ref=?",
                (auth_status, now, profile_ref),
            )

    def require_provider_reauthorization(
        self, profile_ref: str, anchor: Anchor, *, now: int
    ) -> None:
        """Clear mismatched Entra credentials and revoke their pending leases."""

        with self.transaction() as db:
            profile = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (profile_ref,),
            ).fetchone()
            if (
                profile is None
                or not self._owns(profile, anchor)
                or profile["provider"] not in {"microsoft_graph", "azure_devops"}
                or profile["auth_mode"] != "delegated_oauth"
            ):
                raise AccountCenterError("profile_not_found")
            db.execute(
                "UPDATE provider_profiles SET auth_status='reauthorization_required',"
                "credential_blob=NULL,credential_refresh_blob=NULL,refresh_claim_id=NULL,"
                "refresh_claim_owner=NULL,refresh_claimed_at=NULL,"
                "credential_revision=credential_revision+1,updated_at=? WHERE profile_ref=?",
                (now, profile_ref),
            )
            db.execute(
                "DELETE FROM provider_credential_leases WHERE profile_ref=?",
                (profile_ref,),
            )

    def set_provider_profile_secret_ref(
        self, profile_ref: str, anchor: Anchor, secret_ref: str, *, now: int
    ) -> None:
        """Persist a stable host secret reference without copying its value."""

        with self.transaction() as db:
            changed = db.execute(
                "UPDATE provider_profiles SET credential_secret_ref=?,updated_at=? "
                "WHERE profile_ref=? AND anchor_issuer=? AND anchor_subject=? "
                "AND organization_id=? AND workspace_id IS ? AND provider='huggingface' "
                "AND status='active'",
                (
                    secret_ref,
                    now,
                    profile_ref,
                    anchor.issuer,
                    anchor.subject,
                    anchor.organization_id,
                    anchor.workspace_id,
                ),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("profile_not_found")

    def unlink_provider_profile(
        self, profile_ref: str, anchor: Anchor, *, now: int
    ) -> None:
        """Remove local credentials and invalidate outstanding provider leases."""

        with self.transaction() as db:
            changed = db.execute(
                "UPDATE provider_profiles SET status='unlinked',auth_status='revoked',"
                "credential_blob=NULL,credential_refresh_blob=NULL,credential_secret_ref=NULL,"
                "refresh_claim_id=NULL,refresh_claim_owner=NULL,refresh_claimed_at=NULL,"
                "updated_at=? "
                "WHERE profile_ref=? AND anchor_issuer=? AND anchor_subject=? "
                "AND organization_id=? AND workspace_id IS ? AND status='active'",
                (
                    now,
                    profile_ref,
                    anchor.issuer,
                    anchor.subject,
                    anchor.organization_id,
                    anchor.workspace_id,
                ),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("profile_not_found")
            db.execute(
                "DELETE FROM provider_credential_leases WHERE profile_ref=?",
                (profile_ref,),
            )
            db.execute(
                "UPDATE bilibili_link_sessions SET status='cancelled',completed_at=? "
                "WHERE profile_ref=? AND status='pending'",
                (now, profile_ref),
            )
            db.execute(
                "UPDATE huggingface_device_renewal_sessions SET status='cancelled',"
                "device_code_blob=NULL,user_code=NULL,verification_uri=NULL,poll_claim_id=NULL,"
                "poll_claim_owner=NULL,poll_claimed_at=NULL,reason_code='profile_unlinked',"
                "updated_at=? WHERE profile_ref=? AND status IN ('starting','waiting_user')",
                (now, profile_ref),
            )

    def create_provider_lease(
        self,
        *,
        profile_ref: str,
        anchor: Anchor,
        binding_id: str,
        provider_caller_id: str,
        operation_id: str,
        target_json: str,
        arguments_sha256: str,
        expires_at: int,
        now: int,
        allow_workspace_operator: bool = False,
        grant_ref: str | None = None,
        connection_ref: str | None = None,
        grant_provider_id: str | None = None,
        grant_operation_id: str | None = None,
        resource_ref: str | None = None,
        grant_issued_at: int | None = None,
        grant_expires_at: int | None = None,
    ) -> str:
        """Persist one exact lease and atomically consume shared-mode grant proof."""

        self.prune_expired(now=now)
        lease_ref = str(uuid.uuid4())
        with self.transaction() as db:
            profile = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (profile_ref,),
            ).fetchone()
            shared = (
                profile is not None and profile["access_mode"] == "workspace_operator"
            )
            if (
                profile is None
                or not self._owns_provider_profile(
                    profile,
                    anchor,
                    allow_workspace_operator=allow_workspace_operator,
                )
                or profile["auth_status"] != "healthy"
                or profile["provider"]
                not in {
                    "github",
                    "huggingface",
                    "microsoft_graph",
                    "azure_devops",
                    "bilibili",
                }
            ):
                raise AccountCenterError("profile_unavailable")
            has_grant = grant_ref is not None
            if shared and (not allow_workspace_operator or not has_grant):
                raise AccountCenterError("connector_grant_required")
            if has_grant:
                if (
                    not connection_ref
                    or grant_provider_id != profile["provider"]
                    or grant_operation_id != operation_id
                    or not resource_ref
                    or not isinstance(grant_issued_at, int)
                    or isinstance(grant_issued_at, bool)
                    or not isinstance(grant_expires_at, int)
                    or isinstance(grant_expires_at, bool)
                    or grant_issued_at > now
                    or now - grant_issued_at > 15
                    or grant_expires_at <= now
                    or grant_expires_at <= grant_issued_at
                    or grant_expires_at - grant_issued_at > 15
                    or expires_at > grant_expires_at
                    or anchor.workspace_id is None
                ):
                    raise AccountCenterError("connector_grant_required")
                try:
                    db.execute(
                        "INSERT INTO consumed_connector_grants(grant_ref,connection_ref,"
                        "profile_ref,provider_id,operation_id,resource_ref,arguments_sha256,"
                        "anchor_issuer,anchor_subject,organization_id,workspace_id,issued_at,"
                        "expires_at,consumed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            grant_ref,
                            connection_ref,
                            profile_ref,
                            grant_provider_id,
                            grant_operation_id,
                            resource_ref,
                            arguments_sha256,
                            anchor.issuer,
                            anchor.subject,
                            anchor.organization_id,
                            anchor.workspace_id,
                            grant_issued_at,
                            grant_expires_at,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    raise AccountCenterError("connector_grant_replayed") from None
            elif shared or any(
                value is not None
                for value in (
                    grant_ref,
                    connection_ref,
                    grant_provider_id,
                    grant_operation_id,
                    resource_ref,
                    grant_issued_at,
                    grant_expires_at,
                )
            ):
                raise AccountCenterError("invalid_request")
            try:
                db.execute(
                    "INSERT INTO provider_credential_leases(lease_ref,profile_ref,"
                    "anchor_issuer,anchor_subject,organization_id,workspace_id,binding_id,"
                    "provider_caller_id,operation_id,target_json,arguments_sha256,expires_at,"
                    "redeemed_at,connection_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
                    (
                        lease_ref,
                        profile_ref,
                        anchor.issuer,
                        anchor.subject,
                        anchor.organization_id,
                        anchor.workspace_id,
                        binding_id,
                        provider_caller_id,
                        operation_id,
                        target_json,
                        arguments_sha256,
                        expires_at,
                        connection_ref,
                    ),
                )
            except sqlite3.IntegrityError:
                raise AccountCenterError("connector_grant_replayed") from None
        return lease_ref

    def redeem_provider_lease(
        self,
        *,
        lease_ref: str,
        profile_ref: str,
        anchor: Anchor,
        caller_id: str,
        binding_id: str,
        operation_id: str,
        target_json: str,
        arguments_sha256: str,
        now: int,
    ) -> sqlite3.Row:
        """Atomically consume a lease after every signed field matches."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM provider_credential_leases WHERE lease_ref=?",
                (lease_ref,),
            ).fetchone()
            if (
                row is None
                or row["profile_ref"] != profile_ref
                or row["anchor_issuer"] != anchor.issuer
                or row["anchor_subject"] != anchor.subject
                or row["organization_id"] != anchor.organization_id
                or row["workspace_id"] != anchor.workspace_id
                or row["binding_id"] != binding_id
                or row["provider_caller_id"] != caller_id
                or row["operation_id"] != operation_id
                or row["target_json"] != target_json
                or row["arguments_sha256"] != arguments_sha256
                or row["expires_at"] <= now
                or row["redeemed_at"] is not None
            ):
                raise AccountCenterError("lease_rejected")
            changed = db.execute(
                "UPDATE provider_credential_leases SET redeemed_at=? WHERE lease_ref=? "
                "AND redeemed_at IS NULL AND expires_at>?",
                (now, lease_ref, now),
            ).rowcount
            if changed != 1:
                raise AccountCenterError("lease_rejected")
            profile = db.execute(
                "SELECT * FROM provider_profiles WHERE profile_ref=? AND status='active'",
                (profile_ref,),
            ).fetchone()
            if (
                profile is None
                or not self._owns_provider_profile(
                    profile, anchor, allow_workspace_operator=True
                )
                or profile["auth_status"] != "healthy"
            ):
                raise AccountCenterError("profile_unavailable")
        return profile

    def create_lease(
        self,
        *,
        account_ref: str,
        anchor: Anchor,
        binding_id: str,
        provider_caller_id: str,
        operation_id: str,
        resource_json: str,
        arguments_sha256: str,
        expires_at: int,
        now: int,
    ) -> str:
        """Persist a short, single-use lease bound to owner and exact operation."""

        lease_ref = str(uuid.uuid4())
        with self.transaction() as db:
            account = db.execute(
                "SELECT * FROM accounts WHERE account_ref=?", (account_ref,)
            ).fetchone()
            if (
                account is None
                or not self._owns(account, anchor)
                or account["status"] != "active"
                or account["credential_status"] != "ready"
            ):
                raise AccountCenterError("account_not_found")
            db.execute(
                "INSERT INTO credential_leases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    lease_ref,
                    account_ref,
                    anchor.issuer,
                    anchor.subject,
                    anchor.organization_id,
                    anchor.workspace_id,
                    binding_id,
                    provider_caller_id,
                    operation_id,
                    resource_json,
                    arguments_sha256,
                    expires_at,
                ),
            )
        return lease_ref

    def redeem_lease(
        self,
        *,
        lease_ref: str,
        anchor: Anchor,
        caller_id: str,
        binding_id: str,
        operation_id: str,
        resource_json: str,
        arguments_sha256: str,
        now: int,
    ) -> tuple[sqlite3.Row, bytes]:
        """Atomically consume the lease and return encrypted credential bytes."""

        with self.transaction() as db:
            lease = db.execute(
                "SELECT * FROM credential_leases WHERE lease_ref=?", (lease_ref,)
            ).fetchone()
            if (
                lease is None
                or lease["redeemed_at"] is not None
                or lease["expires_at"] <= now
                or lease["provider_caller_id"] != caller_id
                or lease["operation_id"] != operation_id
                or lease["binding_id"] != binding_id
                or lease["resource_json"] != resource_json
                or lease["arguments_sha256"] != arguments_sha256
                or (
                    lease["anchor_issuer"],
                    lease["anchor_subject"],
                    lease["organization_id"],
                    lease["workspace_id"],
                )
                != (
                    anchor.issuer,
                    anchor.subject,
                    anchor.organization_id,
                    anchor.workspace_id,
                )
            ):
                raise AccountCenterError("lease_rejected")
            account = db.execute(
                "SELECT * FROM accounts WHERE account_ref=?", (lease["account_ref"],)
            ).fetchone()
            if (
                account is None
                or not self._owns(account, anchor)
                or account["status"] != "active"
                or account["credential_status"] not in {"ready", "refresh_pending"}
            ):
                raise AccountCenterError("lease_rejected")
            if account["credential_blob"] is None:
                raise AccountCenterError("credential_unavailable")
            db.execute(
                "UPDATE credential_leases SET redeemed_at=? WHERE lease_ref=?",
                (now, lease_ref),
            )
            return account, bytes(account["credential_blob"])

    def unlink(
        self,
        account_ref: str,
        anchor: Anchor,
        *,
        now: int,
        command_result: tuple[str, bytes] | None = None,
        command_key_hash: str | None = None,
        force_remote_unknown: bool = False,
    ) -> sqlite3.Row:
        """Invalidate every outstanding lease before reporting a local unlink."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE account_ref=?", (account_ref,)
            ).fetchone()
            if row is None or not self._owns(row, anchor):
                raise AccountCenterError("account_not_found")
            if row["status"] != "active":
                raise AccountCenterError("account_not_found")
            should_revoke = (
                row["provider"] == "github"
                and row["credential_blob"]
                and not force_remote_unknown
            )
            remote_status = (
                "pending"
                if should_revoke
                else "unknown"
                if row["provider"] == "github"
                and (row["credential_status"] != "ready" or force_remote_unknown)
                else None
            )
            db.execute(
                "UPDATE accounts SET status='unlinked',remote_revocation_status=?, "
                "remote_revocation_owner=?,remote_revocation_command_hash=?, "
                "unlinked_at=?,credential_blob=NULL,credential_status='ready', "
                "refresh_claim_id=NULL,refresh_claim_owner=NULL,refresh_claimed_at=NULL "
                "WHERE account_ref=?",
                (
                    remote_status,
                    self._instance_id if remote_status == "pending" else None,
                    command_key_hash if remote_status == "pending" else None,
                    now,
                    account_ref,
                ),
            )
            db.execute(
                "DELETE FROM credential_leases WHERE account_ref=?", (account_ref,)
            )
            if command_result is not None:
                self._complete_command_in_transaction(db, *command_result)
        return row

    def set_revocation_status(
        self,
        account_ref: str,
        status: str,
        *,
        command_result: tuple[str, bytes] | None = None,
    ) -> None:
        """Record remote revocation outcome after local unlink already took effect."""

        if status not in {"revoked", "failed", "unknown"}:
            raise AccountCenterError("invalid_state")
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE accounts SET remote_revocation_status=?,remote_revocation_owner=NULL, "
                "remote_revocation_command_hash=NULL WHERE account_ref=? "
                "AND status='unlinked' AND remote_revocation_status='pending' "
                "AND remote_revocation_owner=?",
                (status, account_ref, self._instance_id),
            )
            if changed.rowcount != 1:
                raise AccountCenterError("invalid_state")
            if command_result is not None:
                self._complete_command_in_transaction(db, *command_result)
