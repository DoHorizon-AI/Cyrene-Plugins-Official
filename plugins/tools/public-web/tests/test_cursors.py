from __future__ import annotations

import pytest

from cyrene_public_web.cursors import CursorError, actor_scope_digest, decode_cursor, encode_cursor


def test_cursor_is_bound_to_actor_query_and_operation() -> None:
    key = b"c" * 32
    scope = actor_scope_digest(
        issuer="issuer",
        subject="actor-one",
        organization_id="org",
        workspace_id="workspace",
        binding_id="binding",
        key=key,
    )
    cursor = encode_cursor(
        {
            "kind": "web.search",
            "scope_digest": scope,
            "query_digest": "a" * 64,
            "position": "backend-cursor-2",
            "expires_at": 4_102_444_800,
        },
        key,
    )
    decoded = decode_cursor(
        cursor,
        key=key,
        expected_kind="web.search",
        expected_scope=scope,
        expected_query_digest="a" * 64,
    )
    assert decoded["position"] == "backend-cursor-2"
    with pytest.raises(CursorError, match="outside"):
        decode_cursor(
            cursor,
            key=key,
            expected_kind="web.search",
            expected_scope="b" * 64,
            expected_query_digest="a" * 64,
        )
    with pytest.raises(CursorError, match="authentication"):
        decode_cursor(
            cursor[:-1] + ("0" if cursor[-1] != "0" else "1"),
            key=key,
            expected_kind="web.search",
            expected_scope=scope,
            expected_query_digest="a" * 64,
        )
