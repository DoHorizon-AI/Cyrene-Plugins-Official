"""Private SQLite persistence for Account Center links, sessions, and leases.

Credential columns contain only authenticated ciphertext. 中文：凭据列仅保存认证密文。
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import AccountCenterError, Anchor

SCHEMA_VERSION = 4
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

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            if str(self.path) != ":memory:":
                connection.execute("PRAGMA journal_mode = WAL")
            return connection
        except sqlite3.Error:
            raise AccountCenterError("storage_unavailable") from None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run an immediate transaction to serialize one-use lease changes."""

        connection = self._connect()
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
        with self.transaction() as db:
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
                db.execute("ALTER TABLE accounts ADD COLUMN remote_revocation_status TEXT")
                db.execute("ALTER TABLE accounts ADD COLUMN remote_revocation_owner TEXT")
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
                db.execute("ALTER TABLE accounts ADD COLUMN remote_revocation_owner TEXT")
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
                db.execute("ALTER TABLE accounts ADD COLUMN remote_revocation_owner TEXT")
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

    def _recover_interrupted_operations(self) -> None:
        """Mark token rotations, callbacks, and revocations interrupted by restart."""

        with self.transaction() as db:
            db.execute(
                "UPDATE accounts SET credential_status='reauthorization_required', "
                "credential_blob=NULL,refresh_claim_id=NULL,refresh_claim_owner=NULL, "
                "refresh_claimed_at=NULL WHERE credential_status='refresh_pending'"
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
                "UPDATE link_sessions SET status='failed',failure_revocation_status='unknown', "
                "code_verifier_blob=NULL WHERE provider='github' AND status='processing' "
                "AND pending_credential_blob IS NULL"
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
                db.execute("DELETE FROM command_idempotency WHERE key_hash=?", (key_hash,))
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

    def complete_idempotent_command(
        self, key_hash: str, response_blob: bytes
    ) -> None:
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
