"""Fixture tests for bounded durable write receipts and conservative expiry."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from cloud_management_provider.ledger import LedgerCapacityError, RequestLedger


def test_only_expired_terminal_receipts_are_compacted(tmp_path: Path) -> None:
    """Old unknown tombstones remain protected when safe terminal rows can be purged."""

    ledger = RequestLedger(
        tmp_path,
        max_rows=2,
        max_bytes=256 * 1024,
        terminal_retention_seconds=60,
    )
    assert ledger.begin("terminal", "digest-a", "operation-a") is None
    assert ledger.complete("terminal", {"state": "SUCCEEDED"})["state"] == "SUCCEEDED"
    assert ledger.begin("unknown", "digest-b", "operation-b") is None
    ledger.complete("unknown", {"state": "UNKNOWN_RESULT", "operation_ref": "operation-b"})

    with pytest.raises(LedgerCapacityError):
        ledger.begin("too-early", "digest-c", "operation-c")

    connection = sqlite3.connect(tmp_path / "cloud-management.sqlite3")
    connection.execute(
        "UPDATE cloud_write_requests SET completed_at = ? WHERE request_id = ?",
        (time.time() - 120, "terminal"),
    )
    connection.execute(
        "UPDATE cloud_write_requests SET completed_at = ? WHERE request_id = ?",
        (time.time() - 120, "unknown"),
    )
    connection.commit()
    connection.close()

    assert ledger.begin("replacement", "digest-c", "operation-c") is None
    assert ledger.get("terminal") is None
    assert ledger.get("unknown") is not None
    assert ledger.get("replacement") is not None

    ledger.complete("replacement", {"state": "PENDING", "operation_ref": "operation-c"})
    with pytest.raises(LedgerCapacityError):
        ledger.begin("must-refuse", "digest-d", "operation-d")
    assert ledger.get("unknown") is not None
    assert ledger.get("replacement") is not None
    assert ledger.get("must-refuse") is None


def test_response_over_budget_becomes_unknown_tombstone(tmp_path: Path) -> None:
    """The receipt remains durable when a full response would breach the byte cap."""

    ledger = RequestLedger(
        tmp_path,
        max_rows=4,
        max_bytes=256 * 1024,
        terminal_retention_seconds=60,
    )
    assert ledger.begin("large", "digest-large", "operation-large") is None
    stored = ledger.complete("large", {"state": "SUCCEEDED", "detail": "x" * (256 * 1024)})

    assert stored["state"] == "UNKNOWN_RESULT"
    assert stored["reason_code"] == "LEDGER_RESPONSE_CAPACITY"
    assert stored["operation_ref"] == "operation-large"
    assert ledger.get("large") is not None
    assert ledger.begin("same-capacity", "digest-next", "operation-next") is None


def test_success_with_unmatched_actual_state_is_not_purgeable(tmp_path: Path) -> None:
    """An API-accepted create is protected until remote state matches its expectation."""

    ledger = RequestLedger(
        tmp_path,
        max_rows=1,
        max_bytes=256 * 1024,
        terminal_retention_seconds=60,
    )
    assert ledger.begin("accepted", "digest-a", "operation-a") is None
    ledger.complete(
        "accepted",
        {
            "state": "SUCCEEDED",
            "expected_state": "RUNNING",
            "actual_state": "CREATING",
            "operation_ref": "operation-a",
        },
    )
    connection = sqlite3.connect(tmp_path / "cloud-management.sqlite3")
    terminal = connection.execute(
        "SELECT terminal FROM cloud_write_requests WHERE request_id = ?", ("accepted",)
    ).fetchone()[0]
    connection.execute(
        "UPDATE cloud_write_requests SET completed_at = ? WHERE request_id = ?",
        (time.time() - 120, "accepted"),
    )
    connection.commit()
    connection.close()

    assert terminal == 0
    with pytest.raises(LedgerCapacityError):
        ledger.begin("must-remain-protected", "digest-b", "operation-b")
    assert ledger.get("accepted") is not None


def test_legacy_receipts_migrate_as_protected(tmp_path: Path) -> None:
    """Old ledger rows without terminal metadata are never assumed safe to purge."""

    database = tmp_path / "cloud-management.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE cloud_write_requests ("
        "request_id TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL, "
        "operation_ref TEXT NOT NULL, response_json TEXT)"
    )
    connection.execute(
        "INSERT INTO cloud_write_requests VALUES (?, ?, ?, ?)",
        ("legacy", "digest", "operation", '{"state":"SUCCEEDED"}'),
    )
    connection.commit()
    connection.close()

    ledger = RequestLedger(
        tmp_path,
        max_rows=1,
        max_bytes=256 * 1024,
        terminal_retention_seconds=60,
    )

    with pytest.raises(LedgerCapacityError):
        ledger.begin("new", "digest-new", "operation-new")
    assert ledger.get("legacy") is not None
