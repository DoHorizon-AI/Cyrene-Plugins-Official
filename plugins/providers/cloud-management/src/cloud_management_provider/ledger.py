"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud_management_provider.ledger                           │
│  Role: Persist bounded mutation replay guards and receipts.         │
│                                                                      │
│  模块职责：持久化有界写入重放保护记录与回执。                         │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MAX_ROWS = 8192
DEFAULT_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_TERMINAL_RETENTION_SECONDS = 90 * 24 * 60 * 60
_RESPONSE_RESERVATION_BYTES = 64 * 1024
_INSERT_RESERVATION_BYTES = 16 * 1024
_MIN_DATABASE_BYTES = 64 * 1024


class ReplayConflict(ValueError):
    """The same signed request ID was reused with different arguments."""


class LedgerCapacityError(RuntimeError):
    """The replay ledger is full of protected or not-yet-expired receipts."""


@dataclass(frozen=True, slots=True)
class ExistingWrite:
    """Cached response or in-flight evidence for one exact signed write."""

    payload_sha256: str
    operation_ref: str
    response: dict[str, Any] | None


class RequestLedger:
    """Persist request IDs while bounding storage and protecting uncertain writes."""

    def __init__(
        self,
        state_dir: Path,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        terminal_retention_seconds: int = DEFAULT_TERMINAL_RETENTION_SECONDS,
    ) -> None:
        if max_rows < 1 or max_bytes < _MIN_DATABASE_BYTES or terminal_retention_seconds < 1:
            raise ValueError("ledger limits are invalid")
        self._path = state_dir / "cloud-management.sqlite3"
        self._max_rows = max_rows
        self._max_bytes = max_bytes
        self._terminal_retention_seconds = terminal_retention_seconds
        self._initialize()

    def begin(
        self, request_id: str, payload_sha256: str, operation_ref: str
    ) -> ExistingWrite | None:
        """Record a write before dispatch, or return its prior exact receipt."""

        self._compact_expired_terminals()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_sha256, operation_ref, response_json "
                "FROM cloud_write_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                previous_digest, previous_ref, response_json = row
                if previous_digest != payload_sha256:
                    connection.rollback()
                    raise ReplayConflict("signed request ID has already been used")
                response = json.loads(response_json) if response_json is not None else None
                connection.commit()
                return ExistingWrite(previous_digest, previous_ref, response)

            row_count = int(
                connection.execute("SELECT COUNT(*) FROM cloud_write_requests").fetchone()[0]
            )
            pending_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM cloud_write_requests WHERE response_json IS NULL"
                ).fetchone()[0]
            )
            projected_bytes = (
                self._database_bytes()
                + (pending_count + 1) * _RESPONSE_RESERVATION_BYTES
                + _INSERT_RESERVATION_BYTES
            )
            if row_count >= self._max_rows or projected_bytes > self._max_bytes:
                connection.rollback()
                raise LedgerCapacityError("protected request receipts fill the configured ledger")
            connection.execute(
                "INSERT INTO cloud_write_requests "
                "(request_id, payload_sha256, operation_ref, response_json, terminal, "
                "completed_at) "
                "VALUES (?, ?, ?, NULL, 0, NULL)",
                (request_id, payload_sha256, operation_ref),
            )
            connection.commit()
            return None

    def complete(self, request_id: str, response: dict[str, Any]) -> dict[str, Any]:
        """Persist a bounded result, or uncertainty tombstone if storage is tight."""

        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT operation_ref, response_json FROM cloud_write_requests "
                "WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise LedgerCapacityError("request receipt disappeared before completion")
            operation_ref, previous_json = row
            if previous_json is not None:
                connection.commit()
                return json.loads(previous_json)
            pending_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM cloud_write_requests WHERE response_json IS NULL"
                ).fetchone()[0]
            )
            remaining_reservation = max(0, pending_count - 1) * _RESPONSE_RESERVATION_BYTES
            stored_response = response
            if (
                self._database_bytes()
                + len(encoded.encode("utf-8"))
                + remaining_reservation
                + _INSERT_RESERVATION_BYTES
                > self._max_bytes
            ):
                stored_response = {
                    "state": "UNKNOWN_RESULT",
                    "reason_code": "LEDGER_RESPONSE_CAPACITY",
                    "operation_ref": operation_ref,
                }
                encoded = json.dumps(stored_response, separators=(",", ":"))
            terminal = _is_terminal_response(stored_response)
            connection.execute(
                "UPDATE cloud_write_requests SET response_json = ?, terminal = ?, completed_at = ? "
                "WHERE request_id = ?",
                (encoded, int(terminal), time.time(), request_id),
            )
            connection.commit()
        return stored_response

    def get(self, request_id: str) -> ExistingWrite | None:
        """Read one durable receipt for status reconciliation without dispatching work."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_sha256, operation_ref, response_json "
                "FROM cloud_write_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        payload_sha256, operation_ref, response_json = row
        response = json.loads(response_json) if response_json is not None else None
        return ExistingWrite(payload_sha256, operation_ref, response)

    def mark_terminal(self, request_id: str) -> None:
        """Mark a receipt purgeable only after remote status reaches a terminal state."""

        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE cloud_write_requests SET terminal = 1, completed_at = ? "
                "WHERE request_id = ? AND response_json IS NOT NULL",
                (time.time(), request_id),
            )
            connection.commit()

    def _initialize(self) -> None:
        """Create or conservatively migrate the private replay table."""

        connection = sqlite3.connect(self._path, timeout=5)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cloud_write_requests ("
                "request_id TEXT PRIMARY KEY, "
                "payload_sha256 TEXT NOT NULL, "
                "operation_ref TEXT NOT NULL, "
                "response_json TEXT, "
                "terminal INTEGER NOT NULL DEFAULT 0, "
                "completed_at REAL)"
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(cloud_write_requests)")
            }
            if "terminal" not in columns:
                connection.execute(
                    "ALTER TABLE cloud_write_requests ADD COLUMN terminal INTEGER NOT NULL "
                    "DEFAULT 0"
                )
            if "completed_at" not in columns:
                connection.execute("ALTER TABLE cloud_write_requests ADD COLUMN completed_at REAL")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS cloud_write_terminal_age "
                "ON cloud_write_requests(terminal, completed_at)"
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(self._path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        """Open one short-lived connection without sharing transactions."""

        connection = sqlite3.connect(self._path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _compact_expired_terminals(self) -> None:
        """Purge only known terminal receipts older than the configured safety window."""

        cutoff = time.time() - self._terminal_retention_seconds
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM cloud_write_requests WHERE terminal = 1 "
                "AND response_json IS NOT NULL AND completed_at IS NOT NULL AND completed_at <= ?",
                (cutoff,),
            ).rowcount
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if deleted:
                connection.execute("VACUUM")

    def _database_bytes(self) -> int:
        """Count the database and sidecar files against the configured byte bound."""

        total = 0
        for path in (self._path, Path(f"{self._path}-wal"), Path(f"{self._path}-shm")):
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                continue
        return total


def _is_terminal_response(response: object) -> bool:
    """Classify only settled results; pending and uncertain writes remain protected."""

    if not isinstance(response, dict):
        return False
    state = response.get("state")
    if state == "SUCCEEDED":
        expected = response.get("expected_state")
        actual = response.get("actual_state")
        if isinstance(expected, str) and isinstance(actual, str):
            if (
                expected.casefold() != actual.casefold()
                and response.get("release_confirmed") is not True
            ):
                return False
        elif isinstance(expected, str) and response.get("operation_ref"):
            return False
        return True
    if state == "FAILED":
        return True
    if state in {"COMMAND_COMPLETED", "COMMAND_FAILED"}:
        return response.get("remote_state") == "NOT_APPLICABLE"
    return isinstance(response.get("error"), dict)
