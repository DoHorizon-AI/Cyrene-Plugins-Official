"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 delivery_spool.py                                               │
│  Module: teams_connector.delivery_spool                             │
│  Role: Persist verified Teams events before acknowledging delivery.  │
│                                                                      │
│  模块职责：ACK 前持久化已验证 Teams 事件并重试 Navigator 投递            │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .navigator import (
    AdmissionResult,
    NavigatorAdmissionUnavailable,
    canonical_event_body,
)

MAX_EVENT_BYTES = 256 * 1024
DEFAULT_MAX_ROWS = 10_000
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MAX_ATTEMPTS = 12
DELIVERED_RETENTION_SECONDS = 24 * 60 * 60
TERMINAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_RETRY_DELAY_SECONDS = 5 * 60
POLL_INTERVAL_SECONDS = 0.25
log = logging.getLogger("teams_connector.delivery_spool")


class DeliverySpoolError(RuntimeError):
    """Base error for durable delivery state and administration failures.

    中文：持久投递状态和管理操作的基础错误。
    """


class DeliverySpoolUnavailable(DeliverySpoolError):
    """The state directory or SQLite transaction could not be used.

    中文：状态目录或 SQLite 事务不可用。
    """


class DeliverySpoolFull(DeliverySpoolError):
    """The bounded durable queue has no room for another event.

    中文：有界持久队列已满，无法接收新事件。
    """


class DeliveryIdentityConflict(DeliverySpoolError):
    """A stable Teams activity identity was reused with a different body.

    中文：同一稳定 Teams activity 标识对应了不同的事件正文。
    """


class DeliveryAdminConflict(DeliverySpoolError):
    """A pause or resume request lost its holder/revision compare-and-swap.

    中文：pause/resume 的 holder 或 revision CAS 不匹配。
    """


class DeliveryPauseTimeout(DeliverySpoolError):
    """A pause was persisted but an existing Navigator forward did not drain.

    中文：pause 已持久化，但现有 Navigator 投递未能在期限内排空。
    """


@dataclass(frozen=True)
class EnqueueResult:
    """Describe whether a verified event was newly persisted or duplicated.

    中文：说明已验证事件是首次持久化还是相同正文的重复投递。
    """

    delivery_id: str
    duplicate: bool


@dataclass(frozen=True)
class DeliveryClaim:
    """One leased body and its fencing identity for a single forward attempt.

    中文：一次转发尝试持有的正文、租约和 fencing 身份。
    """

    delivery_id: str
    body: bytes
    body_sha256: str
    lease_id: str
    lease_epoch: int
    attempt: int


@dataclass(frozen=True)
class DeliveryStatus:
    """Metadata-only view of the durable queue and pause gate.

    中文：只含元数据的持久队列与暂停门状态。
    """

    paused: bool
    revision: int
    active_delivery_count: int
    queued_count: int
    leased_count: int
    retry_wait_count: int
    delivered_count: int
    rejected_count: int
    dead_count: int
    pending_bytes: int
    oldest_pending_at: float | None
    last_delivered_at: float | None

    def as_dict(self) -> dict[str, Any]:
        """Return the stable owner-admin JSON projection without payload data.

        中文：生成稳定的 owner-admin JSON 投影，不返回事件正文。
        """

        return asdict(self)


class DeliverySpool:
    """Persist canonical Teams event bodies in a private, bounded SQLite queue.

    Every enqueue commits before returning. SQLite's write transaction serializes
    duplicate checks, pause changes, and leases across processes sharing one volume.

    中文：将规范 Teams event body 保存到私有、有界的 SQLite 队列。enqueue 只有在
    commit 后才返回；SQLite 写事务会串行化共享卷上的去重、暂停和租约操作。
    """

    def __init__(
        self,
        state_dir: Path | str,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_rows < 1 or max_bytes < 1:
            raise ValueError("delivery spool limits must be positive")
        self._clock = clock
        self._max_rows = max_rows
        self._max_bytes = max_bytes
        self._state_dir = Path(state_dir)
        self._database_path = self._state_dir / "delivery-spool.sqlite3"
        self._prepare_private_state()
        self._initialize_database()

    @property
    def database_path(self) -> Path:
        """Expose the private DB path to lifecycle code and tests, never status JSON.

        中文：向生命周期代码和测试提供私有 DB 路径，不放入 status JSON。
        """

        return self._database_path

    async def enqueue(self, event: dict[str, Any]) -> EnqueueResult:
        """Commit one canonical event or recognize an exact duplicate.

        A reused stable identity with different canonical bytes is rejected. No
        Teams success response may be sent if this transaction fails or is full.

        中文：提交规范 event 或识别重复项。身份对应不同原始字节时拒绝；
        事务失败或队列已满时，Teams 不能收到成功 ACK。
        """

        raw_body = canonical_event_body(event)
        if not raw_body or len(raw_body) > MAX_EVENT_BYTES:
            raise DeliverySpoolFull(
                "normalized Teams event exceeds the spool item limit"
            )
        delivery_id = stable_delivery_id(event)
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        return await self._run_sync(
            self._enqueue_sync, delivery_id, raw_body, body_sha256
        )

    async def claim_next(self, *, lease_seconds: float) -> DeliveryClaim | None:
        """Atomically claim one due row with a monotonically fenced lease.

        中文：通过单个 SQLite 写事务原子领取一项到期记录并递增 fencing epoch。
        """

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        return await self._run_sync(self._claim_next_sync, lease_seconds)

    async def mark_delivered(self, claim: DeliveryClaim) -> bool:
        """Complete a matching lease and erase its message body.

        中文：仅完成匹配的租约并清除消息正文。
        """

        return await self._run_sync(self._finish_sync, claim, "delivered", None, None)

    async def mark_rejected(
        self, claim: DeliveryClaim, *, status_code: int, code: str | None
    ) -> bool:
        """Record a verified terminal business rejection without retrying it.

        中文：记录已验证的业务终态拒绝，不再重试。
        """

        return await self._run_sync(
            self._finish_sync, claim, "rejected", status_code, safe_error_code(code)
        )

    async def mark_dead(
        self, claim: DeliveryClaim, *, status_code: int | None, code: str | None
    ) -> bool:
        """Record permanent failure metadata and erase the private message body.

        中文：保存永久失败元数据，并清除私有消息正文。
        """

        return await self._run_sync(
            self._finish_sync, claim, "dead", status_code, safe_error_code(code)
        )

    async def mark_retry(
        self,
        claim: DeliveryClaim,
        *,
        delay_seconds: float,
        status_code: int | None,
        code: str | None,
    ) -> bool:
        """Return a transient failure to the queue or dead-letter exhausted work.

        中文：临时错误回到队列等待重试；超过次数上限后转为终态 dead。
        """

        return await self._run_sync(
            self._retry_sync,
            claim,
            max(0.0, min(delay_seconds, MAX_RETRY_DELAY_SECONDS)),
            status_code,
            safe_error_code(code),
        )

    async def is_paused(self) -> bool:
        """Read the durable claim gate.

        中文：读取持久化的 claim 暂停门。
        """

        return await self._run_sync(self._is_paused_sync)

    async def pause(self, *, holder: str, expected_revision: int) -> DeliveryStatus:
        """Persist a CAS-protected pause and retain its first holder.

        Repeating the same holder is idempotent. A different holder cannot
        overwrite an existing pause.

        中文：以 CAS 持久化暂停并保留首次 holder；相同 holder 重放幂等，其他 holder
        不能覆盖当前暂停。
        """

        validate_admin_cas(holder, expected_revision)
        return await self._run_sync(self._pause_sync, holder, expected_revision)

    async def resume(self, *, holder: str, expected_revision: int) -> DeliveryStatus:
        """Clear only the matching pause holder at the expected revision.

        Repeating the completed resume with that same holder is idempotent.

        中文：仅允许当前 holder 在指定 revision 下解除暂停；同一 holder 重放已完成
        的 resume 时幂等返回。
        """

        validate_admin_cas(holder, expected_revision)
        return await self._run_sync(self._resume_sync, holder, expected_revision)

    async def status(self) -> DeliveryStatus:
        """Return counts and timestamps, never a row body or holder token.

        中文：只返回计数和时间戳，不返回正文或 holder token。
        """

        return await self._run_sync(self._status_sync)

    async def active_lease_deadline(self) -> float | None:
        """Return the earliest unexpired lease deadline for pause draining.

        中文：返回 pause 排空所需等待的最早未过期租约截止时间。
        """

        return await self._run_sync(self._active_lease_deadline_sync)

    async def _run_sync(self, operation: Callable[..., Any], *args: Any) -> Any:
        try:
            return await asyncio.to_thread(operation, *args)
        except DeliverySpoolError:
            raise
        except (OSError, sqlite3.Error):
            # SQLite errors may contain a local path. Keep them out of SDK logs.
            raise DeliverySpoolUnavailable(
                "Teams durable delivery state is unavailable"
            ) from None

    def _prepare_private_state(self) -> None:
        """Create and validate the owner-only directory and SQLite file.

        中文：创建并验证仅 owner 可访问的状态目录和 SQLite 文件。
        """

        try:
            self._state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory_stat = self._state_dir.lstat()
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_uid != os.geteuid()
                or directory_stat.st_mode & 0o077
            ):
                raise DeliverySpoolUnavailable(
                    "Teams delivery state directory must be an owner-only directory"
                )
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._database_path, flags, 0o600)
            try:
                database_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(database_stat.st_mode)
                    or database_stat.st_uid != os.geteuid()
                    or database_stat.st_mode & 0o077
                ):
                    raise DeliverySpoolUnavailable(
                        "Teams delivery database must be an owner-only regular file"
                    )
            finally:
                os.close(descriptor)
        except DeliverySpoolError:
            raise
        except OSError:
            raise DeliverySpoolUnavailable(
                "Teams delivery state directory is unavailable"
            ) from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize_database(self) -> None:
        """Create the bounded queue schema or reject an unknown future schema.

        中文：初始化有界队列 schema；遇到未知的未来 schema 时 fail closed。
        """

        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                schema_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if schema_version not in {0, 1}:
                    raise DeliverySpoolUnavailable(
                        "Teams delivery database schema is not supported"
                    )
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS delivery_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS deliveries (
                        delivery_id TEXT PRIMARY KEY,
                        body_sha256 TEXT NOT NULL CHECK(length(body_sha256) = 64),
                        body BLOB,
                        state TEXT NOT NULL CHECK(state IN
                            ('queued', 'leased', 'delivered', 'rejected', 'dead')),
                        received_at REAL NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                        next_attempt_at REAL NOT NULL,
                        lease_id TEXT,
                        lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK(lease_epoch >= 0),
                        lease_expires_at REAL,
                        last_http_status INTEGER,
                        last_error_code TEXT,
                        finished_at REAL,
                        CHECK((state = 'leased' AND lease_id IS NOT NULL
                               AND lease_expires_at IS NOT NULL)
                           OR (state != 'leased' AND lease_id IS NULL
                               AND lease_expires_at IS NULL)),
                        CHECK(
                            (state IN ('queued', 'leased') AND body IS NOT NULL)
                            OR (state IN ('delivered', 'rejected', 'dead')
                                AND body IS NULL)
                        )
                    );
                    CREATE INDEX IF NOT EXISTS deliveries_due
                        ON deliveries(
                            state, next_attempt_at, lease_expires_at, received_at
                        );
                    CREATE INDEX IF NOT EXISTS deliveries_retention
                        ON deliveries(state, finished_at);
                    INSERT OR IGNORE INTO delivery_meta(key, value) VALUES
                        ('paused', '0'),
                        ('revision', '0'),
                        ('pause_holder', ''),
                        ('last_resumed_holder', '');
                    PRAGMA user_version=1;
                    """
                )
        except DeliverySpoolError:
            raise
        except (OSError, sqlite3.Error):
            raise DeliverySpoolUnavailable(
                "Teams delivery database could not be initialized"
            ) from None

    def _enqueue_sync(
        self, delivery_id: str, body: bytes, body_sha256: str
    ) -> EnqueueResult:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._purge_expired(connection, now)
            existing = connection.execute(
                "SELECT body_sha256 FROM deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if existing is not None:
                if not secrets.compare_digest(existing["body_sha256"], body_sha256):
                    raise DeliveryIdentityConflict(
                        "Teams activity identity conflicts with a stored body"
                    )
                connection.commit()
                return EnqueueResult(delivery_id=delivery_id, duplicate=True)

            counts = connection.execute(
                "SELECT COUNT(*) AS rows, COALESCE(SUM(length(body)), 0) AS bytes "
                "FROM deliveries"
            ).fetchone()
            if (
                counts["rows"] >= self._max_rows
                or counts["bytes"] + len(body) > self._max_bytes
            ):
                raise DeliverySpoolFull("Teams durable delivery queue is full")

            connection.execute(
                """INSERT INTO deliveries
                   (delivery_id, body_sha256, body, state, received_at, next_attempt_at)
                   VALUES (?, ?, ?, 'queued', ?, ?)""",
                (delivery_id, body_sha256, sqlite3.Binary(body), now, now),
            )
            # The explicit FULL-synchronous commit is the ACK boundary.
            connection.commit()
        return EnqueueResult(delivery_id=delivery_id, duplicate=False)

    def _claim_next_sync(self, lease_seconds: float) -> DeliveryClaim | None:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._meta(connection, "paused") == "1":
                connection.commit()
                return None
            row = connection.execute(
                """SELECT delivery_id, body, body_sha256, attempts, lease_epoch
                   FROM deliveries
                   WHERE (state = 'queued' AND next_attempt_at <= ?)
                      OR (state = 'leased' AND lease_expires_at <= ?)
                   ORDER BY received_at, delivery_id
                   LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            lease_id = secrets.token_hex(24)
            lease_epoch = int(row["lease_epoch"]) + 1
            attempt = int(row["attempts"]) + 1
            changed = connection.execute(
                """UPDATE deliveries
                   SET state = 'leased', attempts = ?, lease_id = ?, lease_epoch = ?,
                       lease_expires_at = ?, last_http_status = NULL,
                       last_error_code = NULL
                   WHERE delivery_id = ? AND lease_epoch = ?
                     AND ((state = 'queued' AND next_attempt_at <= ?)
                       OR (state = 'leased' AND lease_expires_at <= ?))""",
                (
                    attempt,
                    lease_id,
                    lease_epoch,
                    now + lease_seconds,
                    row["delivery_id"],
                    row["lease_epoch"],
                    now,
                    now,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            connection.commit()
            return DeliveryClaim(
                delivery_id=row["delivery_id"],
                body=bytes(row["body"]),
                body_sha256=row["body_sha256"],
                lease_id=lease_id,
                lease_epoch=lease_epoch,
                attempt=attempt,
            )

    def _finish_sync(
        self,
        claim: DeliveryClaim,
        state: str,
        status_code: int | None,
        error_code: str | None,
    ) -> bool:
        if state not in {"delivered", "rejected", "dead"}:
            raise ValueError("invalid terminal delivery state")
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE deliveries
                   SET state = ?, body = NULL, lease_id = NULL, lease_expires_at = NULL,
                       last_http_status = ?, last_error_code = ?, finished_at = ?
                   WHERE delivery_id = ? AND state = 'leased' AND lease_id = ?
                     AND lease_epoch = ?""",
                (
                    state,
                    status_code,
                    error_code,
                    now,
                    claim.delivery_id,
                    claim.lease_id,
                    claim.lease_epoch,
                ),
            ).rowcount
            connection.commit()
        return changed == 1

    def _retry_sync(
        self,
        claim: DeliveryClaim,
        delay_seconds: float,
        status_code: int | None,
        error_code: str | None,
    ) -> bool:
        now = self._clock()
        exhausted = claim.attempt >= MAX_ATTEMPTS
        state = "dead" if exhausted else "queued"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE deliveries
                   SET state = ?, body = CASE WHEN ? THEN NULL ELSE body END,
                       next_attempt_at = ?, lease_id = NULL, lease_expires_at = NULL,
                       last_http_status = ?, last_error_code = ?, finished_at = ?
                   WHERE delivery_id = ? AND state = 'leased' AND lease_id = ?
                     AND lease_epoch = ?""",
                (
                    state,
                    1 if exhausted else 0,
                    now + delay_seconds if not exhausted else now,
                    status_code,
                    "RETRY_EXHAUSTED" if exhausted else error_code,
                    now if exhausted else None,
                    claim.delivery_id,
                    claim.lease_id,
                    claim.lease_epoch,
                ),
            ).rowcount
            connection.commit()
        return changed == 1

    def _is_paused_sync(self) -> bool:
        with self._connect() as connection:
            return self._meta(connection, "paused") == "1"

    def _pause_sync(self, holder: str, expected_revision: int) -> DeliveryStatus:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision = int(self._meta(connection, "revision"))
            paused = self._meta(connection, "paused") == "1"
            current_holder = self._meta(connection, "pause_holder")
            last_resumed_holder = self._meta(connection, "last_resumed_holder")
            if paused:
                if current_holder == holder and expected_revision in {
                    revision,
                    revision - 1,
                }:
                    connection.commit()
                    return self._status_sync_on_connection(connection)
                raise DeliveryAdminConflict(
                    "delivery pause is held by another revision"
                )
            if last_resumed_holder == holder or expected_revision != revision:
                raise DeliveryAdminConflict("delivery pause revision is stale")
            next_revision = revision + 1
            self._set_meta(connection, "paused", "1")
            self._set_meta(connection, "pause_holder", holder)
            self._set_meta(connection, "last_resumed_holder", "")
            self._set_meta(connection, "revision", str(next_revision))
            connection.commit()
        return self._status_sync()

    def _resume_sync(self, holder: str, expected_revision: int) -> DeliveryStatus:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision = int(self._meta(connection, "revision"))
            paused = self._meta(connection, "paused") == "1"
            current_holder = self._meta(connection, "pause_holder")
            last_resumed_holder = self._meta(connection, "last_resumed_holder")
            if not paused:
                if last_resumed_holder == holder and expected_revision in {
                    revision,
                    revision - 1,
                }:
                    connection.commit()
                    return self._status_sync_on_connection(connection)
                raise DeliveryAdminConflict("delivery pause holder does not match")
            if current_holder != holder or expected_revision != revision:
                raise DeliveryAdminConflict(
                    "delivery pause holder or revision does not match"
                )
            self._set_meta(connection, "paused", "0")
            self._set_meta(connection, "pause_holder", "")
            self._set_meta(connection, "last_resumed_holder", holder)
            self._set_meta(connection, "revision", str(revision + 1))
            connection.commit()
        return self._status_sync()

    def _status_sync(self) -> DeliveryStatus:
        with self._connect() as connection:
            connection.execute("BEGIN")
            result = self._status_sync_on_connection(connection)
            connection.commit()
            return result

    def _status_sync_on_connection(
        self, connection: sqlite3.Connection
    ) -> DeliveryStatus:
        now = self._clock()
        counts = connection.execute(
            """SELECT
                 SUM(CASE WHEN state = 'queued' THEN 1 ELSE 0 END) AS queued_count,
                 SUM(CASE WHEN state = 'leased' THEN 1 ELSE 0 END) AS leased_count,
                 SUM(CASE WHEN state = 'queued' AND next_attempt_at > ?
                          THEN 1 ELSE 0 END)
                     AS retry_wait_count,
                 SUM(CASE WHEN state = 'leased' AND lease_expires_at > ?
                          THEN 1 ELSE 0 END)
                     AS active_delivery_count,
                 SUM(CASE WHEN state = 'delivered' THEN 1 ELSE 0 END)
                     AS delivered_count,
                 SUM(CASE WHEN state = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
                 SUM(CASE WHEN state = 'dead' THEN 1 ELSE 0 END) AS dead_count,
                 COALESCE(SUM(CASE WHEN state IN ('queued', 'leased')
                                   THEN length(body) ELSE 0 END), 0) AS pending_bytes,
                 MIN(CASE WHEN state IN ('queued', 'leased') THEN received_at END)
                     AS oldest_pending_at,
                 MAX(CASE WHEN state = 'delivered' THEN finished_at END)
                     AS last_delivered_at
               FROM deliveries""",
            (now, now),
        ).fetchone()
        return DeliveryStatus(
            paused=self._meta(connection, "paused") == "1",
            revision=int(self._meta(connection, "revision")),
            active_delivery_count=int(counts["active_delivery_count"] or 0),
            queued_count=int(counts["queued_count"] or 0),
            leased_count=int(counts["leased_count"] or 0),
            retry_wait_count=int(counts["retry_wait_count"] or 0),
            delivered_count=int(counts["delivered_count"] or 0),
            rejected_count=int(counts["rejected_count"] or 0),
            dead_count=int(counts["dead_count"] or 0),
            pending_bytes=int(counts["pending_bytes"] or 0),
            oldest_pending_at=counts["oldest_pending_at"],
            last_delivered_at=counts["last_delivered_at"],
        )

    def _active_lease_deadline_sync(self) -> float | None:
        now = self._clock()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(lease_expires_at) FROM deliveries "
                "WHERE state = 'leased' AND lease_expires_at > ?",
                (now,),
            ).fetchone()
            return row[0]

    def _purge_expired(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            "DELETE FROM deliveries WHERE state = 'delivered' AND finished_at <= ?",
            (now - DELIVERED_RETENTION_SECONDS,),
        )
        connection.execute(
            "DELETE FROM deliveries WHERE state IN ('rejected', 'dead') "
            "AND finished_at <= ?",
            (now - TERMINAL_RETENTION_SECONDS,),
        )

    @staticmethod
    def _meta(connection: sqlite3.Connection, key: str) -> str:
        row = connection.execute(
            "SELECT value FROM delivery_meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise DeliverySpoolUnavailable(
                "Teams delivery state metadata is incomplete"
            )
        return row[0]

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "UPDATE delivery_meta SET value = ? WHERE key = ?", (value, key)
        )


class NavigatorDeliveryWorker:
    """Forward only queued Teams bodies to Navigator with bounded retries.

    This worker has no Plugin, provider, model, or task-execution dependency.

    中文：只把 Teams 正文转发给 Navigator 并进行有界重试；不依赖 Plugin、provider、
    model 或任务执行器。
    """

    def __init__(
        self,
        spool: DeliverySpool,
        submit_body: Callable[[bytes], Awaitable[AdmissionResult]],
        *,
        navigator_timeout_seconds: float,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._spool = spool
        self._submit_body = submit_body
        self._poll_interval_seconds = poll_interval_seconds
        self._lease_seconds = max(15.0, navigator_timeout_seconds * 2.0 + 1.0)
        self._shutdown_grace_seconds = navigator_timeout_seconds + 2.0
        self._stopping = asyncio.Event()
        self._wakeup = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start one background delivery loop for the application lifetime.

        中文：为应用生命周期启动一个后台投递循环。
        """

        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._wakeup.set()
        self._task = asyncio.create_task(
            self._run(), name="teams-navigator-delivery-spool"
        )

    async def close(self) -> None:
        """Stop claiming and wait a bounded time for the current forward.

        On timeout, cancellation leaves the durable lease to expire; the next
        process reclaims the same body and Navigator's event idempotency applies.

        中文：停止领取并有界等待当前转发。超时取消时保留租约，待它过期后由下个进程
        领取相同正文，Navigator 既有事件幂等负责去重。
        """

        task = self._task
        if task is None:
            return
        self._stopping.set()
        self._wakeup.set()
        done, _pending = await asyncio.wait(
            {task}, timeout=self._shutdown_grace_seconds
        )
        if not done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None

    async def notify(self) -> None:
        """Wake the loop after a new enqueue or an admin state transition.

        中文：新入队或 admin 状态变化后唤醒循环。
        """

        self._wakeup.set()

    async def pause_and_wait(
        self, *, holder: str, expected_revision: int
    ) -> DeliveryStatus:
        """Persist pause before waiting for every live forward lease to drain.

        中文：先持久化暂停，再等待所有仍有效的转发租约排空。
        """

        await self._spool.pause(holder=holder, expected_revision=expected_revision)
        await self.notify()
        deadline = time.monotonic() + self._lease_seconds + self._shutdown_grace_seconds
        while True:
            await self._idle.wait()
            status = await self._spool.status()
            if status.active_delivery_count == 0:
                return status
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeliveryPauseTimeout("Teams Navigator delivery did not drain")
            lease_deadline = await self._spool.active_lease_deadline()
            wait_seconds = min(0.25, remaining)
            if lease_deadline is not None:
                wait_seconds = min(
                    wait_seconds, max(0.01, lease_deadline - time.time())
                )
            await asyncio.sleep(wait_seconds)

    async def resume(self, *, holder: str, expected_revision: int) -> DeliveryStatus:
        """Release the matching durable pause and wake pending deliveries.

        中文：解除匹配的持久暂停并唤醒待投递消息。
        """

        status = await self._spool.resume(
            holder=holder, expected_revision=expected_revision
        )
        await self.notify()
        return status

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                if await self._spool.is_paused():
                    await self._wait_for_wakeup()
                    continue
                claim = await self._spool.claim_next(lease_seconds=self._lease_seconds)
                if claim is None:
                    await self._wait_for_wakeup()
                    continue
                self._idle.clear()
                try:
                    await self._forward(claim)
                finally:
                    self._idle.set()
            except asyncio.CancelledError:
                raise
            except DeliverySpoolError:
                # A failed state transaction leaves the row leased for fencing recovery.
                log.error("Teams durable delivery state is unavailable")
                await asyncio.sleep(0.25)

    async def _wait_for_wakeup(self) -> None:
        try:
            await asyncio.wait_for(
                self._wakeup.wait(), timeout=self._poll_interval_seconds
            )
        except TimeoutError:
            pass
        finally:
            self._wakeup.clear()

    async def _forward(self, claim: DeliveryClaim) -> None:
        try:
            result = await self._submit_body(claim.body)
        except NavigatorAdmissionUnavailable as exc:
            if exc.retryable:
                await self._spool.mark_retry(
                    claim,
                    delay_seconds=retry_delay_seconds(
                        claim.attempt, exc.retry_after_seconds
                    ),
                    status_code=exc.status_code,
                    code=exc.code,
                )
            else:
                await self._spool.mark_dead(
                    claim, status_code=exc.status_code, code=exc.code
                )
            return
        except asyncio.CancelledError:
            # The lease stays durable; a replacement process retries it after expiry.
            raise
        except Exception:
            # Unknown client/runtime faults are bounded retries, never a success ACK.
            await self._spool.mark_retry(
                claim,
                delay_seconds=retry_delay_seconds(claim.attempt, None),
                status_code=None,
                code="DELIVERY_CLIENT_ERROR",
            )
            return

        if result.accepted:
            await self._spool.mark_delivered(claim)
        elif result.terminal_rejection:
            await self._spool.mark_rejected(
                claim,
                status_code=result.status_code,
                code=result.code,
            )
        else:
            await self._spool.mark_dead(
                claim,
                status_code=result.status_code,
                code=result.code or "UNEXPECTED_NAVIGATOR_RESULT",
            )


def stable_delivery_id(event: dict[str, Any]) -> str:
    """Hash the verified tenant/team/channel/conversation/activity identity.

    The event body remains the exact canonical Navigator body; this separate
    key lets the spool reject a conflicting body for the same Teams activity.

    中文：对经过验证的租户/团队/频道/会话/activity 身份生成哈希。该 key 与规范
    Navigator body 分开保存，以便拒绝同一 Teams activity 对应不同正文。
    """

    trust = event.get("trust")
    payload = event.get("payload")
    conversation = payload.get("conversation") if isinstance(payload, dict) else None
    if not isinstance(trust, dict) or not isinstance(conversation, dict):
        raise DeliverySpoolError("normalized Teams event identity is incomplete")
    identity = [
        trust.get("provider"),
        trust.get("tenant_id"),
        trust.get("team_id"),
        trust.get("channel_id"),
        trust.get("bot_app_id"),
        conversation.get("conversation_id"),
        trust.get("message_id"),
    ]
    if any(not isinstance(part, str) or not part for part in identity):
        raise DeliverySpoolError("normalized Teams event identity is incomplete")
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(b"cyrene-teams-delivery-v1\0" + encoded).hexdigest()


def retry_delay_seconds(attempt: int, retry_after_seconds: float | None) -> float:
    """Apply bounded exponential retry delay and respect numeric Retry-After.

    中文：应用有界指数退避，并遵守数值型 Retry-After。
    """

    backoff = min(2 ** max(0, attempt - 1), MAX_RETRY_DELAY_SECONDS)
    if retry_after_seconds is None:
        return float(backoff)
    return min(
        MAX_RETRY_DELAY_SECONDS,
        max(float(backoff), max(0.0, retry_after_seconds)),
    )


def validate_admin_cas(holder: str, expected_revision: int) -> None:
    """Reject malformed holder ids and revisions without echoing their values.

    中文：拒绝格式错误的 holder/revision，不回显输入内容。
    """

    if (
        not isinstance(holder, str)
        or not holder
        or len(holder) > 128
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in holder)
    ):
        raise DeliveryAdminConflict("delivery pause holder is invalid")
    if type(expected_revision) is not int or expected_revision < 0:
        raise DeliveryAdminConflict("delivery pause revision is invalid")


def safe_error_code(code: str | None) -> str | None:
    """Keep only bounded public error identifiers in spool metadata.

    中文：spool 元数据只保留有界的公开错误标识。
    """

    if not isinstance(code, str) or not code:
        return None
    if len(code) > 100 or any(
        not (
            character.isascii()
            and (character.isupper() or character.isdigit() or character in "_.-")
        )
        for character in code
    ):
        return "UNKNOWN"
    return code
