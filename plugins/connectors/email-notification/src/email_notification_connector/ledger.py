"""
┌──────────────────────────────────────────────────────────────────────┐
│  ledger.py                                                           │
│  Module: email_notification_connector.ledger                         │
│  Role: Persist private SMTP replay identities and submission facts.   │
│                                                                      │
│  模块职责：持久化私有 SMTP 重放身份和提交事实。                           │
│  · 先写 SENDING 意图  · 崩溃后 SENDING 只能变为 UNKNOWN                 │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cyrene_integration_contracts import EmailSubmission, EmailSubmissionStatus

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = 1
_BUSY_TIMEOUT_SECONDS = 10


class LedgerFailure(RuntimeError):
    """Raised when the private transport ledger cannot safely record an intent."""


class ReplayConflict(RuntimeError):
    """Raised when a scoped request identity is reused with different content."""


@dataclass(frozen=True, slots=True)
class ReplayIdentity:
    """Private hashes and the opaque request reference for one signed send."""

    identity_sha256: str
    request_id_sha256: str
    payload_sha256: str
    request_ref: str


class SubmissionLedger:
    """Serialize sends per scoped request and persist every accepted outcome."""

    def __init__(self, database_path: Path) -> None:
        self._path = database_path
        self._validate_private_path()
        self._initialize()

    def reserve(self, identity: ReplayIdentity) -> EmailSubmission | None:
        """Commit a new send intent or return a previously recorded result.

        Args:
            identity: Scope-bound replay and payload digests from a verified call.
        Returns:
            `None` only when this call owns a newly committed `SENDING` intent.
            An existing in-flight intent becomes and returns `UNKNOWN`.
        Raises:
            ReplayConflict: When a request or context ID is bound to changed content.
            LedgerFailure: When the intent cannot be committed durably.
        """

        _validate_identity(identity)
        try:
            with self._transaction() as connection:
                request_id_row = connection.execute(
                    "SELECT identity_sha256 FROM email_submission_request_ids "
                    "WHERE request_id_sha256 = ?",
                    (identity.request_id_sha256,),
                ).fetchone()
                if (
                    request_id_row is not None
                    and request_id_row["identity_sha256"] != identity.identity_sha256
                ):
                    raise ReplayConflict("signed request ID was already used")

                row = connection.execute(
                    "SELECT payload_sha256, status, vendor_receipt_ref, "
                    "delivery_confirmed FROM email_submissions "
                    "WHERE identity_sha256 = ?",
                    (identity.identity_sha256,),
                ).fetchone()
                if row is not None:
                    if row["payload_sha256"] != identity.payload_sha256:
                        raise ReplayConflict("signed request reference was reused")
                    if request_id_row is None:
                        connection.execute(
                            "INSERT INTO email_submission_request_ids "
                            "(request_id_sha256, identity_sha256) VALUES (?, ?)",
                            (identity.request_id_sha256, identity.identity_sha256),
                        )
                    if row["status"] == "SENDING":
                        connection.execute(
                            "UPDATE email_submissions SET status = 'UNKNOWN', updated_at = ? "
                            "WHERE identity_sha256 = ? AND status = 'SENDING'",
                            (_timestamp(), identity.identity_sha256),
                        )
                        return _submission(identity.request_ref, "UNKNOWN", None, False)
                    if row["status"] == "RETRYABLE":
                        connection.execute(
                            "UPDATE email_submissions SET status = 'SENDING', updated_at = ? "
                            "WHERE identity_sha256 = ? AND status = 'RETRYABLE'",
                            (_timestamp(), identity.identity_sha256),
                        )
                        return None
                    return _submission(
                        identity.request_ref,
                        row["status"],
                        row["vendor_receipt_ref"],
                        bool(row["delivery_confirmed"]),
                    )

                now = _timestamp()
                connection.execute(
                    "INSERT INTO email_submissions "
                    "(identity_sha256, payload_sha256, status, "
                    "vendor_receipt_ref, delivery_confirmed, created_at, updated_at) "
                    "VALUES (?, ?, 'SENDING', NULL, 0, ?, ?)",
                    (
                        identity.identity_sha256,
                        identity.payload_sha256,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO email_submission_request_ids "
                    "(request_id_sha256, identity_sha256) VALUES (?, ?)",
                    (identity.request_id_sha256, identity.identity_sha256),
                )
                return None
        except ReplayConflict:
            raise
        except (OSError, sqlite3.Error) as error:
            raise LedgerFailure("private email submission ledger is unavailable") from error

    def mark_retryable(self, identity: ReplayIdentity) -> None:
        """Release a request only after transport confirms failure before DATA."""

        _validate_identity(identity)
        try:
            with self._transaction() as connection:
                connection.execute(
                    "UPDATE email_submissions SET status = 'RETRYABLE', updated_at = ? "
                    "WHERE identity_sha256 = ? AND payload_sha256 = ? "
                    "AND status IN ('SENDING', 'UNKNOWN')",
                    (
                        _timestamp(),
                        identity.identity_sha256,
                        identity.payload_sha256,
                    ),
                )
        except (OSError, sqlite3.Error) as error:
            raise LedgerFailure("private email submission ledger is unavailable") from error

    def complete(self, identity: ReplayIdentity, submission: EmailSubmission) -> EmailSubmission:
        """Persist a final typed result, including UNKNOWN after an ambiguous send."""

        _validate_identity(identity)
        if (
            submission.request_ref != identity.request_ref
            or submission.delivery_confirmed
            or submission.vendor_receipt_ref is not None
        ):
            raise LedgerFailure("email result violates the provider evidence boundary")
        try:
            with self._transaction() as connection:
                connection.execute(
                    "UPDATE email_submissions SET status = ?, vendor_receipt_ref = ?, "
                    "delivery_confirmed = ?, updated_at = ? "
                    "WHERE identity_sha256 = ? AND payload_sha256 = ? "
                    "AND status IN ('SENDING', 'UNKNOWN')",
                    (
                        submission.status.value,
                        submission.vendor_receipt_ref,
                        int(submission.delivery_confirmed),
                        _timestamp(),
                        identity.identity_sha256,
                        identity.payload_sha256,
                    ),
                )
                row = connection.execute(
                    "SELECT status, vendor_receipt_ref, delivery_confirmed "
                    "FROM email_submissions WHERE identity_sha256 = ?",
                    (identity.identity_sha256,),
                ).fetchone()
                if row is None:
                    raise LedgerFailure("email submission intent is unavailable")
                return _submission(
                    identity.request_ref,
                    row["status"],
                    row["vendor_receipt_ref"],
                    bool(row["delivery_confirmed"]),
                )
        except LedgerFailure:
            raise
        except (OSError, sqlite3.Error) as error:
            raise LedgerFailure("private email submission ledger is unavailable") from error

    def _validate_private_path(self) -> None:
        """Require an owner-only directory and a non-symlink owner-only database."""

        path = self._path
        parent = path.parent
        try:
            if not path.is_absolute() or not parent.is_dir():
                raise LedgerFailure("private email submission directory is unavailable")
            parent_stat = parent.stat()
            parent_mode = stat.S_IMODE(parent_stat.st_mode)
            if parent_mode & 0o077 or _is_other_owner(parent_stat.st_uid):
                raise LedgerFailure("private email submission directory permissions are unsafe")
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                descriptor = -1
            if descriptor >= 0:
                os.close(descriptor)
            path_stat = path.lstat()
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or stat.S_IMODE(path_stat.st_mode) & 0o077
                or _is_other_owner(path_stat.st_uid)
            ):
                raise LedgerFailure("private email submission database permissions are unsafe")
        except LedgerFailure:
            raise
        except OSError as error:
            raise LedgerFailure("private email submission database is unavailable") from error

    def _initialize(self) -> None:
        """Create the fixed schema and select durable SQLite journal settings."""

        try:
            with closing(self._connection()) as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > _SCHEMA_VERSION:
                    raise LedgerFailure("private email submission schema is newer than this Plugin")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS email_submissions ("
                    "identity_sha256 TEXT PRIMARY KEY, "
                    "payload_sha256 TEXT NOT NULL, "
                    "status TEXT NOT NULL CHECK (status IN "
                    "('SENDING', 'RETRYABLE', 'ACCEPTED', 'UNKNOWN', 'REJECTED')), "
                    "vendor_receipt_ref TEXT NULL, "
                    "delivery_confirmed INTEGER NOT NULL CHECK (delivery_confirmed = 0), "
                    "created_at TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL"
                    ")"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS email_submission_request_ids ("
                    "request_id_sha256 TEXT PRIMARY KEY, "
                    "identity_sha256 TEXT NOT NULL REFERENCES email_submissions(identity_sha256)"
                    ")"
                )
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                if stat.S_IMODE(self._path.stat().st_mode) & 0o077:
                    raise LedgerFailure("private email submission database permissions are unsafe")
        except LedgerFailure:
            raise
        except (OSError, sqlite3.Error) as error:
            raise LedgerFailure("private email submission ledger cannot be initialized") from error

    def _connection(self) -> sqlite3.Connection:
        """Open a private connection with bounded waits and durable commits."""

        connection = sqlite3.connect(
            self._path,
            timeout=_BUSY_TIMEOUT_SECONDS,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a FULL-synchronous immediate transaction for one ledger change."""

        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _validate_identity(identity: ReplayIdentity) -> None:
    """Reject malformed identity digests before they reach SQLite queries."""

    if not isinstance(identity, ReplayIdentity):
        raise TypeError("identity must be ReplayIdentity")
    for digest in (
        identity.identity_sha256,
        identity.request_id_sha256,
        identity.payload_sha256,
    ):
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError("replay identity digest is invalid")
    if not isinstance(identity.request_ref, str) or not identity.request_ref:
        raise ValueError("request reference is invalid")


def _timestamp() -> str:
    """Return an RFC3339 UTC ledger timestamp."""

    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_other_owner(owner_id: int) -> bool:
    """Return whether a POSIX path is owned by an identity other than this process."""

    process_owner = getattr(os, "geteuid", None)
    return callable(process_owner) and owner_id != process_owner()


def _submission(
    request_ref: str,
    status: str,
    vendor_receipt_ref: str | None,
    delivery_confirmed: bool,
) -> EmailSubmission:
    """Rebuild the public typed result from one private ledger row."""

    try:
        typed_status = EmailSubmissionStatus(status)
    except ValueError as error:
        raise LedgerFailure("email submission ledger status is invalid") from error
    if typed_status in {EmailSubmissionStatus.UNKNOWN, EmailSubmissionStatus.REJECTED}:
        vendor_receipt_ref = None
        delivery_confirmed = False
    return EmailSubmission(
        request_ref=request_ref,
        status=typed_status,
        vendor_receipt_ref=vendor_receipt_ref,
        delivery_confirmed=delivery_confirmed,
    )
