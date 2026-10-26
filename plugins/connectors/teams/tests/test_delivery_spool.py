"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_delivery_spool.py                                          │
│  Module: tests.test_delivery_spool                                  │
│  Role: Verify durable Teams delivery, fencing, and pause control.    │
│                                                                      │
│  模块职责：验证 Teams 持久投递、fencing 与暂停控制                     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import stat

import httpx
import pytest

from teams_connector.delivery_spool import (
    DELIVERED_RETENTION_SECONDS,
    MAX_RETRY_DELAY_SECONDS,
    REJECTED_RETENTION_SECONDS,
    DeliveryAdminConflict,
    DeliveryIdentityConflict,
    DeliveryPauseTimeout,
    DeliverySpool,
    DeliverySpoolFull,
    DeliverySpoolUnavailable,
    NavigatorDeliveryWorker,
    retry_delay_seconds,
    stable_delivery_id,
)
from teams_connector.navigator import (
    AdmissionResult,
    NavigatorAdmissionUnavailable,
    NavigatorEventClient,
    canonical_event_body,
)

HMAC_KEY = b"teams-test-key-material-with-32-bytes-minimum"


def _event(
    *,
    message_id: str = "activity-100",
    text: str = "你好，Cyrene",
    root_thread_id: str = "100",
    conversation_id: str = "19:channel;messageid=100",
) -> dict:
    """Build one normalized event with stable Teams identity fields.

    中文：构造一个带稳定 Teams 身份字段的规范 event。
    """

    return {
        "event_type": "inbound_message",
        "payload": {
            "message_id": message_id,
            "conversation": {
                "vendor": "msteams",
                "conversation_id": conversation_id,
            },
            "content": [{"text": {"text": text}}],
        },
        "trust": {
            "provider": "teams",
            "tenant_id": "tenant-1",
            "team_id": "team-1",
            "channel_id": "channel-1",
            "bot_app_id": "bot-1",
            "message_id": message_id,
            "root_thread_id": root_thread_id,
        },
    }


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    """Wait for an async state predicate without a fixed long sleep.

    中文：等待异步状态到达，避免固定长时间 sleep。
    """

    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_enqueue_commits_exact_body_and_rejects_identity_body_conflict(tmp_path):
    """Persist exact canonical bytes, deduplicate them, and reject body reuse.

    中文：持久化精确规范字节、识别相同重复项并拒绝身份冲突正文。
    """

    spool = DeliverySpool(tmp_path / "state")
    event = _event()
    expected_body = canonical_event_body(event)

    first = await spool.enqueue(event)
    duplicate = await spool.enqueue(event)

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.delivery_id == first.delivery_id
    with sqlite3.connect(spool.database_path) as connection:
        row = connection.execute(
            "SELECT body, body_sha256, state FROM deliveries WHERE delivery_id = ?",
            (first.delivery_id,),
        ).fetchone()
    assert row[0] == expected_body
    assert row[1] == hashlib.sha256(expected_body).hexdigest()
    assert row[2] == "queued"

    with pytest.raises(DeliveryIdentityConflict):
        await spool.enqueue(_event(text="同一 activity id 的不同正文"))

    status = await spool.status()
    assert status.queued_count == 1
    assert status.pending_bytes == len(expected_body)
    assert "你好，Cyrene" not in json.dumps(status.as_dict(), ensure_ascii=False)


@pytest.mark.asyncio
async def test_full_queue_rejects_before_inserting_or_acknowledging(tmp_path):
    """Enforce row and byte bounds without inserting the overflowing event.

    中文：队列行数或字节上限触发时拒绝插入溢出事件。
    """

    spool = DeliverySpool(tmp_path / "state", max_rows=1, max_bytes=1024)
    await spool.enqueue(_event(message_id="first"))

    with pytest.raises(DeliverySpoolFull):
        await spool.enqueue(_event(message_id="second"))

    with sqlite3.connect(spool.database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    assert count == 1


def test_state_directory_and_database_are_owner_only(tmp_path):
    """Create private spool paths and reject a broadly accessible state directory.

    中文：spool 目录和 DB 默认仅 owner 可访问，并拒绝公开权限目录。
    """

    state_dir = tmp_path / "private-state"
    spool = DeliverySpool(state_dir)
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(spool.database_path.stat().st_mode) == 0o600

    unsafe_dir = tmp_path / "unsafe-state"
    unsafe_dir.mkdir(mode=0o755)
    unsafe_dir.chmod(0o755)
    with pytest.raises(DeliverySpoolUnavailable, match="owner-only"):
        DeliverySpool(unsafe_dir)


@pytest.mark.asyncio
async def test_shared_sqlite_claim_has_one_winner_and_fences_expired_worker(tmp_path):
    """Serialize simultaneous claims and reject a stale lease completion.

    中文：并发 claim 只有一个赢家，租约过期后的旧 worker 不能提交结果。
    """

    now = [1000.0]
    state = tmp_path / "state"
    first_spool = DeliverySpool(state, clock=lambda: now[0])
    second_spool = DeliverySpool(state, clock=lambda: now[0])
    event = _event()
    await first_spool.enqueue(event)

    first, second = await asyncio.gather(
        first_spool.claim_next(lease_seconds=2),
        second_spool.claim_next(lease_seconds=2),
    )
    claims = [claim for claim in (first, second) if claim is not None]
    assert len(claims) == 1
    stale = claims[0]

    now[0] += 3
    replacement = await second_spool.claim_next(lease_seconds=2)
    assert replacement is not None
    assert replacement.body == canonical_event_body(event)
    assert replacement.lease_epoch == stale.lease_epoch + 1
    assert await first_spool.mark_delivered(stale) is False
    assert await second_spool.mark_delivered(replacement) is True
    assert (await first_spool.status()).delivered_count == 1


@pytest.mark.asyncio
async def test_thread_head_of_line_blocks_followup_but_other_threads_progress(tmp_path):
    """A retried root stays ahead of its replies without blocking other threads.

    中文：根消息退避时同 thread reply 不越过，其他 thread 仍可继续投递。
    """

    now = [5000.0]
    spool = DeliverySpool(tmp_path / "state", clock=lambda: now[0])
    root = _event(
        message_id="root-100",
        root_thread_id="root-100",
        conversation_id="19:channel",
    )
    reply = _event(
        message_id="reply-message",
        root_thread_id="root-100",
        conversation_id="19:channel;messageid=root-100",
        text="继续刚才的讨论",
    )
    other = _event(
        message_id="other-message",
        root_thread_id="root-200",
        conversation_id="19:channel",
        text="另一个线程",
    )

    await spool.enqueue(root)
    root_claim = await spool.claim_next(lease_seconds=5)
    assert root_claim is not None
    await spool.mark_retry(
        root_claim,
        delay_seconds=60,
        status_code=503,
        code="NAVIGATOR_UNAVAILABLE",
    )
    await spool.enqueue(reply)
    await spool.enqueue(other)

    unrelated_claim = await spool.claim_next(lease_seconds=5)
    assert unrelated_claim is not None
    assert unrelated_claim.delivery_id == stable_delivery_id(other)
    await spool.mark_delivered(unrelated_claim)
    assert await spool.claim_next(lease_seconds=5) is None

    now[0] += 61
    retried_root = await spool.claim_next(lease_seconds=5)
    assert retried_root is not None
    assert retried_root.delivery_id == stable_delivery_id(root)
    await spool.mark_delivered(retried_root)
    followup = await spool.claim_next(lease_seconds=5)
    assert followup is not None
    assert followup.delivery_id == stable_delivery_id(reply)
    await spool.mark_delivered(followup)


@pytest.mark.asyncio
async def test_new_spool_instance_recovers_queued_body_after_restart(tmp_path):
    """A fresh process object can claim the same committed queued row.

    中文：新 spool 实例可以恢复并领取已提交的 queued row。
    """

    state = tmp_path / "state"
    first_process = DeliverySpool(state)
    event = _event(message_id="restart-1")
    await first_process.enqueue(event)

    restarted_process = DeliverySpool(state)
    status = await restarted_process.status()
    claim = await restarted_process.claim_next(lease_seconds=10)

    assert status.queued_count == 1
    assert claim is not None
    assert claim.body == canonical_event_body(event)
    assert claim.attempt == 1


@pytest.mark.asyncio
async def test_long_transient_outage_keeps_retrying_the_same_private_body(tmp_path):
    """Keep ambiguous/transient work queued after many capped-backoff retries.

    中文：多轮临时错误后仍保留原正文和队列状态，退避间隔有上限。
    """

    now = [3000.0]
    spool = DeliverySpool(tmp_path / "state", clock=lambda: now[0])
    await spool.enqueue(_event(message_id="bounded-retry"))

    expected_body = canonical_event_body(_event(message_id="bounded-retry"))
    for attempt in range(1, 21):
        claim = await spool.claim_next(lease_seconds=1)
        assert claim is not None
        assert claim.attempt == attempt
        assert claim.body == expected_body
        await spool.mark_retry(
            claim,
            delay_seconds=MAX_RETRY_DELAY_SECONDS,
            status_code=503,
            code="NAVIGATOR_UNAVAILABLE",
        )
        now[0] += MAX_RETRY_DELAY_SECONDS + 1

    status = await spool.status()
    with sqlite3.connect(spool.database_path) as connection:
        row = connection.execute(
            "SELECT state, body, last_error_code, attempts FROM deliveries"
        ).fetchone()
    assert status.dead_count == 0
    assert status.queued_count == 1
    assert row == ("queued", expected_body, "NAVIGATOR_UNAVAILABLE", 20)


@pytest.mark.asyncio
async def test_terminal_retention_purges_old_rows_on_next_admission(tmp_path):
    """Expire delivered and terminal metadata at the documented retention bounds.

    中文：下一次入队时清理超过文档保留期的 delivered/terminal 元数据。
    """

    now = [4000.0]
    spool = DeliverySpool(tmp_path / "state", clock=lambda: now[0])
    delivered = await spool.enqueue(_event(message_id="retained-delivered"))
    delivered_claim = await spool.claim_next(lease_seconds=1)
    assert delivered_claim is not None
    await spool.mark_delivered(delivered_claim)

    now[0] += DELIVERED_RETENTION_SECONDS + 1
    await spool.enqueue(_event(message_id="after-delivery-retention"))
    with sqlite3.connect(spool.database_path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM deliveries WHERE delivery_id = ?",
                (delivered.delivery_id,),
            ).fetchone()
            is None
        )

    terminal_claim = await spool.claim_next(lease_seconds=1)
    assert terminal_claim is not None
    await spool.mark_rejected(
        terminal_claim,
        status_code=422,
        code="TEAMS_TEAM_NOT_ALLOWED",
    )
    now[0] += REJECTED_RETENTION_SECONDS + 1
    await spool.enqueue(_event(message_id="after-terminal-retention"))
    with sqlite3.connect(spool.database_path) as connection:
        states = connection.execute(
            "SELECT state FROM deliveries ORDER BY received_at"
        ).fetchall()
    assert states == [("queued",)]


@pytest.mark.asyncio
async def test_dead_letter_is_retained_and_requeued_only_by_paused_cas(tmp_path):
    """Keep permanent failures/body as HOL blockers until explicit owner recovery.

    中文：永久失败保留正文并阻塞同 thread；只能由已暂停 holder 显式重排恢复。
    """

    now = [8000.0]
    spool = DeliverySpool(tmp_path / "state", clock=lambda: now[0])
    root = _event(
        message_id="dead-root",
        root_thread_id="dead-thread",
        conversation_id="19:channel",
    )
    reply = _event(
        message_id="dead-reply",
        root_thread_id="dead-thread",
        conversation_id="19:channel;messageid=dead-thread",
        text="reply waits for root recovery",
    )
    expected_body = canonical_event_body(root)
    await spool.enqueue(root)
    root_claim = await spool.claim_next(lease_seconds=1)
    assert root_claim is not None
    await spool.mark_dead(
        root_claim,
        status_code=400,
        code="INVALID_EVENT",
    )
    await spool.enqueue(reply)
    assert await spool.claim_next(lease_seconds=1) is None
    await spool.enqueue(
        _event(
            message_id="dead-other-thread",
            root_thread_id="other-thread",
            text="independent thread",
        )
    )
    # Dead bodies stay private and retained rather than expiring into a lost reply.
    now[0] += 365 * 24 * 60 * 60
    with sqlite3.connect(spool.database_path) as connection:
        dead_row = connection.execute(
            "SELECT body, state FROM deliveries WHERE delivery_id = ?",
            (stable_delivery_id(root),),
        ).fetchone()
    assert dead_row == (expected_body, "dead")
    status = await spool.status()
    other = _event(
        message_id="dead-other-thread",
        root_thread_id="other-thread",
        text="independent thread",
    )
    assert status.pending_bytes == len(canonical_event_body(reply)) + len(
        canonical_event_body(other)
    )
    assert status.retained_body_bytes == len(expected_body) + len(
        canonical_event_body(reply)
    ) + len(canonical_event_body(other))

    active_other_thread = await spool.claim_next(lease_seconds=3600)
    assert active_other_thread is not None
    assert active_other_thread.delivery_id == stable_delivery_id(other)

    with pytest.raises(DeliveryAdminConflict):
        await spool.retry_dead(holder="recover-1", expected_revision=0)
    paused = await spool.pause(holder="recover-1", expected_revision=0)
    assert paused.revision == 1
    with pytest.raises(DeliveryAdminConflict):
        await spool.retry_dead(holder="recover-1", expected_revision=1)
    await spool.mark_delivered(active_other_thread)
    with pytest.raises(DeliveryAdminConflict):
        await spool.retry_dead(holder="wrong-holder", expected_revision=1)
    with pytest.raises(DeliveryAdminConflict):
        await spool.retry_dead(holder="recover-1", expected_revision=0)

    retried_count, status = await spool.retry_dead(
        holder="recover-1", expected_revision=1
    )
    assert retried_count == 1
    assert status.dead_count == 0
    assert status.queued_count == 2
    assert await spool.retry_dead(holder="recover-1", expected_revision=1) == (
        0,
        status,
    )
    assert await spool.claim_next(lease_seconds=1) is None

    await spool.resume(holder="recover-1", expected_revision=1)
    recovered_root = await spool.claim_next(lease_seconds=1)
    assert recovered_root is not None
    assert recovered_root.body == expected_body
    assert recovered_root.delivery_id == stable_delivery_id(root)
    await spool.mark_delivered(recovered_root)
    recovered_reply = await spool.claim_next(lease_seconds=1)
    assert recovered_reply is not None
    assert recovered_reply.delivery_id == stable_delivery_id(reply)
    await spool.mark_delivered(recovered_reply)


@pytest.mark.asyncio
async def test_dead_letter_body_counts_toward_capacity_and_status(tmp_path):
    """A retained dead body consumes bounded storage and blocks new ACKs.

    中文：保留的 dead body 计入容量；空间耗尽时新 webhook 不能 ACK。
    """

    event = _event(message_id="dead-capacity")
    body = canonical_event_body(event)
    spool = DeliverySpool(tmp_path / "state", max_rows=4, max_bytes=len(body))
    await spool.enqueue(event)
    claim = await spool.claim_next(lease_seconds=5)
    assert claim is not None
    await spool.mark_dead(claim, status_code=400, code="INVALID_EVENT")

    status = await spool.status()
    assert status.dead_count == 1
    assert status.pending_bytes == 0
    assert status.retained_body_bytes == len(body)
    with pytest.raises(DeliverySpoolFull):
        await spool.enqueue(_event(message_id="cannot-ack"))


@pytest.mark.asyncio
async def test_timeout_after_navigator_commit_replays_same_body_idempotently(tmp_path):
    """Retry after an ambiguous timeout with the same idempotent event body.

    中文：Navigator 已接纳但回执超时时，使用完全相同正文安全重送。
    """

    now = [2000.0]
    spool = DeliverySpool(tmp_path / "state", clock=lambda: now[0])
    accepted_by_navigator: set[str] = set()
    observed_bodies: list[bytes] = []
    first_attempt = asyncio.Event()
    duplicate_attempt = asyncio.Event()

    async def fake_navigator_submit(body: bytes) -> AdmissionResult:
        observed_bodies.append(body)
        event = json.loads(body)
        delivery_id = stable_delivery_id(event)
        if delivery_id in accepted_by_navigator:
            duplicate_attempt.set()
            return AdmissionResult(True, True, 200)
        accepted_by_navigator.add(delivery_id)
        first_attempt.set()
        # Model Navigator committing the Task/event before its HTTP response is lost.
        raise NavigatorAdmissionUnavailable(
            "Navigator event admission could not be confirmed", retryable=True
        )

    worker = NavigatorDeliveryWorker(
        spool,
        fake_navigator_submit,
        navigator_timeout_seconds=0.1,
        poll_interval_seconds=0.01,
    )
    event = _event(message_id="ambiguous-1")
    await spool.enqueue(event)
    await worker.start()
    await worker.notify()
    await asyncio.wait_for(first_attempt.wait(), timeout=2)
    await _wait_until(lambda: _is_retry_waiting(spool))

    now[0] += 2
    await worker.notify()
    await asyncio.wait_for(duplicate_attempt.wait(), timeout=2)
    await _wait_until(lambda: _is_delivered(spool))
    await worker.close()

    expected_body = canonical_event_body(event)
    assert observed_bodies == [expected_body, expected_body]
    assert len(accepted_by_navigator) == 1
    status = await spool.status()
    assert status.delivered_count == 1
    assert status.queued_count == 0


@pytest.mark.asyncio
async def test_worker_enforces_total_forward_deadline_before_lease_expiry(tmp_path):
    """Cancel a slow trickle-style fake forward before a shared lease expires.

    中文：即使 fake forward 持续慢速响应，也在 lease 过期前取消并安全重排。
    """

    spool = DeliverySpool(tmp_path / "state")
    cancelled = asyncio.Event()

    async def slow_forward(_body: bytes) -> AdmissionResult:
        try:
            await asyncio.sleep(1)
        finally:
            cancelled.set()
        return AdmissionResult(True, False, 202)

    worker = NavigatorDeliveryWorker(
        spool,
        slow_forward,
        navigator_timeout_seconds=0.05,
        poll_interval_seconds=0.01,
    )
    assert worker._lease_seconds > worker._navigator_timeout_seconds
    await spool.enqueue(_event(message_id="slow-trickle"))
    await worker.start()
    await worker.notify()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await _wait_until(lambda: _is_retry_waiting(spool))

    status = await spool.status()
    assert status.leased_count == 0
    assert status.queued_count == 1
    await worker.close()


async def _is_retry_waiting(spool: DeliverySpool) -> bool:
    """Return true after a transient failure is transactionally rescheduled.

    中文：临时失败完成事务重排后返回 true。
    """

    status = await spool.status()
    return status.retry_wait_count == 1


async def _is_delivered(spool: DeliverySpool) -> bool:
    """Return true after the Navigator duplicate receipt is persisted.

    中文：Navigator duplicate 回执写入持久状态后返回 true。
    """

    status = await spool.status()
    return status.delivered_count == 1


@pytest.mark.asyncio
async def test_hmac_retry_changes_timestamp_not_persisted_body():
    """Sign exact raw bytes on each attempt and refresh only HMAC headers.

    中文：每轮对精确原始字节重新签名，只刷新 HMAC headers。
    """

    clock = [1_790_000_000]
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503 if len(requests) == 1 else 202)

    event = _event(message_id="fresh-signature")
    body = canonical_event_body(event)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        navigator = NavigatorEventClient(
            base_url="http://navigator:8080",
            key_id="teams-v1",
            hmac_key=HMAC_KEY,
            client=client,
            clock=lambda: clock[0],
        )
        with pytest.raises(NavigatorAdmissionUnavailable) as first_error:
            await navigator.submit_body(body)
        assert first_error.value.retryable is True
        clock[0] += 1
        accepted = await navigator.submit_body(body)

    assert accepted.accepted is True
    assert [request.content for request in requests] == [body, body]
    timestamps = [request.headers["x-cyrene-source-timestamp"] for request in requests]
    signatures = [request.headers["x-cyrene-source-signature"] for request in requests]
    assert timestamps == ["1790000000", "1790000001"]
    assert signatures[0] != signatures[1]
    for timestamp, signature in zip(timestamps, signatures, strict=True):
        expected = hmac.new(
            HMAC_KEY,
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        assert signature == f"v1={expected}"
    assert all("authorization" not in request.headers for request in requests)


@pytest.mark.asyncio
async def test_pause_gates_claims_waits_for_forward_and_preserves_cas_holder(tmp_path):
    """Pause claims atomically while webhook commits continue and drain completes.

    中文：pause 原子阻止 claim，webhook 仍可提交；当前 forward 完成后才返回。
    """

    spool = DeliverySpool(tmp_path / "state")
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[bytes] = []

    async def fake_navigator_submit(body: bytes) -> AdmissionResult:
        calls.append(body)
        if len(calls) == 1:
            started.set()
            await release.wait()
        return AdmissionResult(True, False, 202)

    worker = NavigatorDeliveryWorker(
        spool,
        fake_navigator_submit,
        navigator_timeout_seconds=0.1,
        poll_interval_seconds=0.01,
    )
    first_event = _event(message_id="pause-in-flight")
    await spool.enqueue(first_event)
    await worker.start()
    await worker.notify()
    await asyncio.wait_for(started.wait(), timeout=2)

    pause_task = asyncio.create_task(
        worker.pause_and_wait(holder="rollout-1", expected_revision=0)
    )
    await _wait_until(lambda: _is_paused(spool))
    await spool.enqueue(_event(message_id="during-pause"))
    await worker.notify()
    await asyncio.sleep(0.02)

    assert pause_task.done() is False
    assert len(calls) == 1
    release.set()
    paused_status = await asyncio.wait_for(pause_task, timeout=2)
    assert paused_status.paused is True
    assert paused_status.revision == 1
    assert paused_status.active_delivery_count == 0
    assert paused_status.queued_count == 1

    same_holder = await spool.pause(holder="rollout-1", expected_revision=0)
    assert same_holder.revision == 1
    with pytest.raises(DeliveryAdminConflict):
        await spool.pause(holder="rollout-2", expected_revision=1)
    with pytest.raises(DeliveryAdminConflict):
        await worker.resume(holder="wrong-holder", expected_revision=1)

    resumed = await worker.resume(holder="rollout-1", expected_revision=1)
    assert resumed.paused is False
    assert resumed.revision == 2
    replayed_resume = await spool.resume(holder="rollout-1", expected_revision=1)
    assert replayed_resume.revision == 2
    await _wait_until(lambda: _is_two_delivered(spool))
    await worker.close()

    assert len(calls) == 2


async def _is_paused(spool: DeliverySpool) -> bool:
    """Return true after pause wins the SQLite transaction race.

    中文：SQLite 事务中的 pause 成功后返回 true。
    """

    return (await spool.status()).paused


async def _is_two_delivered(spool: DeliverySpool) -> bool:
    """Return true after resumed work is delivered.

    中文：恢复后的待投递事件完成后返回 true。
    """

    return (await spool.status()).delivered_count == 2


@pytest.mark.asyncio
async def test_pause_transaction_prevents_later_claim_and_resume_checks_holder(
    tmp_path,
):
    """A persisted pause blocks new leases and stale holders cannot release it.

    中文：已提交的 pause 会阻止新 lease，旧 holder 不能解除当前暂停。
    """

    spool = DeliverySpool(tmp_path / "state")
    await spool.enqueue(_event())
    paused = await spool.pause(holder="migration-1", expected_revision=0)

    assert paused.revision == 1
    assert await spool.claim_next(lease_seconds=5) is None
    with pytest.raises(DeliveryAdminConflict):
        await spool.resume(holder="migration-2", expected_revision=1)


@pytest.mark.asyncio
async def test_worker_close_waits_for_current_forward_within_grace(tmp_path):
    """Graceful shutdown waits for the active Navigator forward to finish.

    中文：有界关闭等待当前 Navigator forward 完成。
    """

    spool = DeliverySpool(tmp_path / "state")
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_forward(_body: bytes) -> AdmissionResult:
        started.set()
        await release.wait()
        return AdmissionResult(True, False, 202)

    await spool.enqueue(_event(message_id="shutdown-drain"))
    worker = NavigatorDeliveryWorker(
        spool,
        blocked_forward,
        navigator_timeout_seconds=1,
        poll_interval_seconds=0.01,
    )
    await worker.start()
    await worker.notify()
    await asyncio.wait_for(started.wait(), timeout=2)

    close_task = asyncio.create_task(worker.close())
    await asyncio.sleep(0.02)
    assert close_task.done() is False
    release.set()
    await asyncio.wait_for(close_task, timeout=2)
    assert (await spool.status()).delivered_count == 1


@pytest.mark.asyncio
async def test_pause_deadline_is_bounded_and_keeps_persisted_pause(tmp_path):
    """A stuck local forward cannot hang pause beyond its drain deadline.

    中文：本地 forward 卡住时 pause 在期限后返回，并保留已持久化的暂停。
    """

    spool = DeliverySpool(tmp_path / "state")
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_forward(_body: bytes) -> AdmissionResult:
        started.set()
        await release.wait()
        return AdmissionResult(True, False, 202)

    await spool.enqueue(_event(message_id="pause-timeout"))
    worker = NavigatorDeliveryWorker(
        spool,
        blocked_forward,
        navigator_timeout_seconds=10,
        poll_interval_seconds=0.01,
    )
    worker._lease_seconds = 0.05
    worker._shutdown_grace_seconds = 0.05
    await worker.start()
    await worker.notify()
    await asyncio.wait_for(started.wait(), timeout=2)

    with pytest.raises(DeliveryPauseTimeout):
        await worker.pause_and_wait(holder="rollout-timeout", expected_revision=0)
    paused_status = await spool.status()
    assert paused_status.paused is True
    assert paused_status.revision == 1
    assert release.is_set() is False

    release.set()
    await _wait_until(lambda: _is_delivered(spool))
    await worker.close()


def test_retry_delay_is_bounded_and_respects_retry_after():
    """Keep exponential and upstream retry delays within the documented cap.

    中文：指数退避与上游 Retry-After 都受最大延迟约束。
    """

    assert retry_delay_seconds(1, None) == 1.0
    assert retry_delay_seconds(4, 30) == 30
    assert retry_delay_seconds(50, 24 * 60 * 60) == 300
