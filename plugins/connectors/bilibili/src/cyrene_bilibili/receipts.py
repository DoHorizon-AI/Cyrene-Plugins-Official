"""Private durable QR-flow, account-binding, and mutation receipt state."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ReceiptStoreError(RuntimeError):
    """The private connector state cannot be read or safely updated."""


@dataclass(frozen=True, slots=True)
class LoginFlow:
    """Private short-lived login material; never serialize to a tool response."""

    login_flow_id: str
    scope_key: str
    profile_ref: str
    link_session_ref: str
    qrcode_key: str
    qr_png_base64: str
    qr_png_sha256: str
    qr_cookies_json: str
    expires_at: int
    status: str
    idempotency_key: str


class ReceiptStore:
    """Serialize side effects and isolate durable state by verified actor scope."""

    def __init__(self, state_dir: Path, scope_hmac_key: bytes) -> None:
        self._path = state_dir / "bilibili-state.sqlite3"
        self._scope_hmac_key = scope_hmac_key
        if len(scope_hmac_key) != 32:
            raise ValueError("state namespace HMAC key must contain 32 bytes")
        self._initialize()

    def scope_key(self, actor: Mapping[str, Any]) -> str:
        """Derive a private actor/workspace namespace from authenticated claims."""

        fields = [
            actor.get(name)
            for name in ("issuer", "subject", "organization_id", "workspace_id", "binding_id")
        ]
        if any(value is not None and not isinstance(value, str) for value in fields):
            raise ReceiptStoreError("authenticated scope is invalid")
        if any(not value for value in fields[:3]):
            raise ReceiptStoreError("authenticated actor is incomplete")
        payload = "\0".join("" if value is None else value for value in fields).encode("utf-8")
        return hmac.new(self._scope_hmac_key, payload, hashlib.sha256).hexdigest()

    def begin_flow(
        self,
        *,
        scope_key: str,
        profile_ref: str,
        link_session_ref: str,
        idempotency_key: str,
        qrcode_key: str,
        qr_png_base64: str,
        qr_png_sha256: str,
        qr_cookies_json: str,
        expires_at: int,
        login_flow_id: str,
    ) -> LoginFlow:
        """Persist one private flow and make begin idempotent per actor."""

        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM login_flows WHERE scope_key=? AND idempotency_key=?",
                (scope_key, idempotency_key),
            ).fetchone()
            if row is not None:
                if row["profile_ref"] != profile_ref:
                    raise ReceiptStoreError("idempotency key conflicts with another profile")
                return _flow(row)
            db.execute(
                "INSERT INTO login_flows(login_flow_id,scope_key,profile_ref,link_session_ref,"
                "qrcode_key,qr_png_base64,qr_png_sha256,qr_cookies_json,expires_at,status,"
                "idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,'PENDING',?,?)",
                (
                    login_flow_id,
                    scope_key,
                    profile_ref,
                    link_session_ref,
                    qrcode_key,
                    qr_png_base64,
                    qr_png_sha256,
                    qr_cookies_json,
                    expires_at,
                    idempotency_key,
                    int(time.time()),
                ),
            )
            row = db.execute(
                "SELECT * FROM login_flows WHERE login_flow_id=?", (login_flow_id,)
            ).fetchone()
            return _flow(row)

    def get_flow(self, scope_key: str, login_flow_id: str) -> LoginFlow | None:
        """Return one actor-owned flow by opaque ID."""

        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM login_flows WHERE login_flow_id=? AND scope_key=?",
                (login_flow_id, scope_key),
            ).fetchone()
        return None if row is None else _flow(row)

    def mark_flow(self, scope_key: str, login_flow_id: str, status: str) -> None:
        """Advance the actor-owned QR flow using a closed status enum."""

        if status not in {"PENDING", "LINKED", "EXPIRED", "FAILED", "UNKNOWN_RESULT"}:
            raise ValueError("invalid flow status")
        with self._connection() as db:
            db.execute(
                "UPDATE login_flows SET status=?,updated_at=? "
                "WHERE scope_key=? AND login_flow_id=?",
                (status, int(time.time()), scope_key, login_flow_id),
            )

    def complete_flow(
        self,
        scope_key: str,
        login_flow_id: str,
        profile: Mapping[str, Any],
    ) -> None:
        """Persist only safe linked profile metadata after Account Center commits."""

        encoded = json.dumps(dict(profile), ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise ReceiptStoreError("linked profile metadata exceeds its limit")
        with self._connection() as db:
            db.execute(
                "UPDATE login_flows SET status='LINKED',profile_json=?,updated_at=? "
                "WHERE scope_key=? AND login_flow_id=?",
                (encoded, int(time.time()), scope_key, login_flow_id),
            )

    def get_linked_profile(self, scope_key: str, login_flow_id: str) -> dict[str, Any] | None:
        """Return safe linked profile metadata saved for one actor-owned flow."""

        with self._connection() as db:
            row = db.execute(
                "SELECT profile_json FROM login_flows "
                "WHERE scope_key=? AND login_flow_id=? AND status='LINKED'",
                (scope_key, login_flow_id),
            ).fetchone()
        if row is None or row["profile_json"] is None:
            return None
        return _parse_json(row["profile_json"])

    def start_write(
        self,
        *,
        scope_key: str,
        idempotency_key: str,
        profile_ref: str,
        tool_id: str,
        arguments_sha256: str,
        receipt_id: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Create a non-replayable mutation claim or return its prior receipt."""

        now = int(time.time())
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT profile_ref,tool_id,arguments_sha256,status,receipt_json,receipt_id,"
                "updated_at "
                "FROM write_receipts WHERE scope_key=? AND idempotency_key=?",
                (scope_key, idempotency_key),
            ).fetchone()
            if row is not None:
                if (
                    row["profile_ref"] != profile_ref
                    or row["tool_id"] != tool_id
                    or row["arguments_sha256"] != arguments_sha256
                ):
                    raise ReceiptStoreError("idempotency key was reused with another operation")
                status = str(row["status"])
                if status == "IN_PROGRESS":
                    if now - int(row["updated_at"]) > 60:
                        db.execute(
                            "UPDATE write_receipts SET status='UNKNOWN_RESULT',"
                            "receipt_json=?,updated_at=? "
                            "WHERE scope_key=? AND idempotency_key=? AND status='IN_PROGRESS'",
                            (
                                json.dumps(
                                    {
                                        "status": "UNKNOWN_RESULT",
                                        "receipt_id": row["receipt_id"],
                                        "error_code": "UNKNOWN_RESULT",
                                    },
                                    separators=(",", ":"),
                                ),
                                now,
                                scope_key,
                                idempotency_key,
                            ),
                        )
                        status = "UNKNOWN_RESULT"
                        return status, {
                            "status": "UNKNOWN_RESULT",
                            "receipt_id": str(row["receipt_id"]),
                            "error_code": "UNKNOWN_RESULT",
                        }
                    else:
                        return "UNKNOWN_RESULT", {
                            "status": "UNKNOWN_RESULT",
                            "receipt_id": str(row["receipt_id"]),
                            "error_code": "IN_PROGRESS",
                        }
                value = None if row["receipt_json"] is None else _parse_json(row["receipt_json"])
                return status, value
            db.execute(
                "INSERT INTO write_receipts(scope_key,idempotency_key,profile_ref,tool_id,"
                "arguments_sha256,receipt_id,status,receipt_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'IN_PROGRESS',NULL,?,?)",
                (
                    scope_key,
                    idempotency_key,
                    profile_ref,
                    tool_id,
                    arguments_sha256,
                    receipt_id,
                    now,
                    now,
                ),
            )
            return "NEW", None

    def finish_write(
        self,
        *,
        scope_key: str,
        idempotency_key: str,
        receipt_id: str,
        status: str,
        result: Mapping[str, Any],
    ) -> None:
        """Persist the final receipt result after the external outcome is known."""

        if status not in {"SUCCEEDED", "UNKNOWN_RESULT", "REJECTED"}:
            raise ValueError("invalid receipt status")
        encoded = json.dumps(dict(result), ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ReceiptStoreError("receipt exceeds its storage limit")
        with self._connection() as db:
            db.execute(
                "UPDATE write_receipts SET status=?,receipt_json=?,updated_at=? "
                "WHERE scope_key=? AND idempotency_key=? AND receipt_id=? AND status='IN_PROGRESS'",
                (status, encoded, int(time.time()), scope_key, idempotency_key, receipt_id),
            )
            if db.total_changes != 1:
                raise ReceiptStoreError("write receipt claim was lost")

    def cancel_write_claim(
        self,
        *,
        scope_key: str,
        idempotency_key: str,
        receipt_id: str,
    ) -> None:
        """Release a claim when no Bilibili mutation was dispatched."""

        with self._connection() as db:
            db.execute(
                "DELETE FROM write_receipts WHERE scope_key=? AND idempotency_key=? "
                "AND receipt_id=? AND status='IN_PROGRESS'",
                (scope_key, idempotency_key, receipt_id),
            )

    def close(self) -> None:
        """No persistent connection is held by the store."""

    def _initialize(self) -> None:
        """Create private tables and reject symlink or non-regular database paths."""

        if self._path.exists() or self._path.is_symlink():
            mode = self._path.lstat().st_mode
            if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                raise ReceiptStoreError("state database must be a regular file")
            if stat.S_IMODE(mode) & 0o077:
                raise ReceiptStoreError("state database permissions are not private")
        with self._connection() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS login_flows ("
                "login_flow_id TEXT PRIMARY KEY,scope_key TEXT NOT NULL,profile_ref TEXT NOT NULL,"
                "link_session_ref TEXT NOT NULL,qrcode_key TEXT NOT NULL,"
                "qr_png_base64 TEXT NOT NULL,"
                "qr_png_sha256 TEXT NOT NULL,qr_cookies_json TEXT NOT NULL,"
                "expires_at INTEGER NOT NULL,"
                "status TEXT NOT NULL,idempotency_key TEXT NOT NULL,created_at INTEGER NOT NULL,"
                "updated_at INTEGER,profile_json TEXT);"
                "CREATE UNIQUE INDEX IF NOT EXISTS login_flow_idempotency "
                "ON login_flows(scope_key,idempotency_key);"
                "CREATE INDEX IF NOT EXISTS login_flow_expiry ON login_flows(expires_at);"
                "CREATE TABLE IF NOT EXISTS write_receipts ("
                "scope_key TEXT NOT NULL,idempotency_key TEXT NOT NULL,profile_ref TEXT NOT NULL,"
                "tool_id TEXT NOT NULL,"
                "arguments_sha256 TEXT NOT NULL,receipt_id TEXT NOT NULL,status TEXT NOT NULL,"
                "receipt_json TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,"
                "PRIMARY KEY(scope_key,idempotency_key));"
            )
        with self._connection() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(login_flows)")}
            if "profile_json" not in columns:
                db.execute("ALTER TABLE login_flows ADD COLUMN profile_json TEXT")
            db.execute(
                "DELETE FROM login_flows WHERE expires_at < ?",
                (int(time.time()) - 86_400,),
            )
        os.chmod(self._path, 0o600)

    @contextmanager
    def _connection(self):
        """Close every SQLite handle and finalize explicit transactions."""

        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        try:
            db = sqlite3.connect(self._path, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            os.chmod(self._path, 0o600)
            return db
        except (OSError, sqlite3.Error) as error:
            raise ReceiptStoreError("private state storage is unavailable") from error


def _flow(row: sqlite3.Row) -> LoginFlow:
    return LoginFlow(
        login_flow_id=str(row["login_flow_id"]),
        scope_key=str(row["scope_key"]),
        profile_ref=str(row["profile_ref"]),
        link_session_ref=str(row["link_session_ref"]),
        qrcode_key=str(row["qrcode_key"]),
        qr_png_base64=str(row["qr_png_base64"]),
        qr_png_sha256=str(row["qr_png_sha256"]),
        qr_cookies_json=str(row["qr_cookies_json"]),
        expires_at=int(row["expires_at"]),
        status=str(row["status"]),
        idempotency_key=str(row["idempotency_key"]),
    )


def _parse_json(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ReceiptStoreError("stored receipt is invalid")
    return parsed
