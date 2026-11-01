from __future__ import annotations

import sqlite3
import time

import pytest

from cyrene_bilibili.receipts import ReceiptStore, ReceiptStoreError


def _actor(subject: str, *, binding: str = "bili-binding") -> dict[str, str]:
    return {
        "issuer": "https://id.example.test",
        "subject": subject,
        "organization_id": "org-one",
        "workspace_id": "workspace-one",
        "binding_id": binding,
    }


def test_write_receipt_is_scoped_and_exactly_idempotent(tmp_path) -> None:
    store = ReceiptStore(tmp_path, b"s" * 32)
    actor = _actor("actor-one")
    scope = store.scope_key(actor)
    other_scope = store.scope_key(_actor("actor-two"))
    key = "work-operation:00000000-0000-4000-8000-000000000001"
    profile = "00000000-0000-4000-8000-000000000010"
    receipt = "00000000-0000-4000-8000-000000000020"

    assert store.start_write(
        scope_key=scope,
        idempotency_key=key,
        profile_ref=profile,
        tool_id="bilibili.dm.send",
        arguments_sha256="a" * 64,
        receipt_id=receipt,
    ) == ("NEW", None)
    result = {
        "status": "SUCCEEDED",
        "receipt_id": receipt,
        "external_id": "987",
        "readback_verified": True,
        "readback_sha256": "b" * 64,
    }
    store.finish_write(
        scope_key=scope,
        idempotency_key=key,
        receipt_id=receipt,
        status="SUCCEEDED",
        result=result,
    )

    assert store.start_write(
        scope_key=scope,
        idempotency_key=key,
        profile_ref=profile,
        tool_id="bilibili.dm.send",
        arguments_sha256="a" * 64,
        receipt_id="00000000-0000-4000-8000-000000000021",
    ) == ("SUCCEEDED", result)
    assert store.start_write(
        scope_key=other_scope,
        idempotency_key=key,
        profile_ref=profile,
        tool_id="bilibili.dm.send",
        arguments_sha256="a" * 64,
        receipt_id="00000000-0000-4000-8000-000000000022",
    ) == ("NEW", None)

    with pytest.raises(ReceiptStoreError, match="idempotency key"):
        store.start_write(
            scope_key=scope,
            idempotency_key=key,
            profile_ref=profile,
            tool_id="bilibili.dm.send",
            arguments_sha256="c" * 64,
            receipt_id="00000000-0000-4000-8000-000000000023",
        )


def test_in_progress_replay_does_not_start_a_second_write(tmp_path) -> None:
    store = ReceiptStore(tmp_path, b"s" * 32)
    scope = store.scope_key(_actor("actor-one"))
    key = "work-operation:00000000-0000-4000-8000-000000000001"
    receipt = "00000000-0000-4000-8000-000000000020"
    profile = "00000000-0000-4000-8000-000000000010"
    assert store.start_write(
        scope_key=scope,
        idempotency_key=key,
        profile_ref=profile,
        tool_id="bilibili.comment.create",
        arguments_sha256="a" * 64,
        receipt_id=receipt,
    ) == ("NEW", None)
    assert store.start_write(
        scope_key=scope,
        idempotency_key=key,
        profile_ref=profile,
        tool_id="bilibili.comment.create",
        arguments_sha256="a" * 64,
        receipt_id="00000000-0000-4000-8000-000000000021",
    ) == (
        "IN_PROGRESS",
        {"status": "IN_PROGRESS", "receipt_id": receipt},
    )

    with sqlite3.connect(tmp_path / "bilibili-state.sqlite3") as db:
        db.execute(
            "UPDATE write_receipts SET updated_at=? WHERE scope_key=? AND idempotency_key=?",
            (int(time.time()) - 120, scope, key),
        )
    assert store.start_write(
        scope_key=scope,
        idempotency_key=key,
        profile_ref=profile,
        tool_id="bilibili.comment.create",
        arguments_sha256="a" * 64,
        receipt_id="00000000-0000-4000-8000-000000000022",
    ) == (
        "UNKNOWN_RESULT",
        {"status": "UNKNOWN_RESULT", "receipt_id": receipt},
    )


def test_reopened_store_never_replays_stale_in_progress_mutation(tmp_path) -> None:
    store = ReceiptStore(tmp_path, b"s" * 32)
    scope = store.scope_key(_actor("actor-one"))
    key = "work-operation:00000000-0000-4000-8000-000000000001"
    receipt = "00000000-0000-4000-8000-000000000020"
    profile = "00000000-0000-4000-8000-000000000010"
    store.start_write(
        scope_key=scope,
        idempotency_key=key,
        profile_ref=profile,
        tool_id="bilibili.dm.send",
        arguments_sha256="a" * 64,
        receipt_id=receipt,
    )
    reopened = ReceiptStore(tmp_path, b"s" * 32)
    assert (
        reopened.start_write(
            scope_key=scope,
            idempotency_key=key,
            profile_ref=profile,
            tool_id="bilibili.dm.send",
            arguments_sha256="a" * 64,
            receipt_id="00000000-0000-4000-8000-000000000021",
        )[0]
        == "IN_PROGRESS"
    )
