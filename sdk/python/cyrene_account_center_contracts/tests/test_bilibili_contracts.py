"""Bilibili Account Center SDK contracts stay closed and credential-safe."""

from __future__ import annotations

import pytest

from cyrene_account_center_contracts import (
    BeginBilibiliLinkRequest,
    BilibiliLinkSession,
    BilibiliOperation,
    CompleteBilibiliLinkRequest,
)

_PROFILE_REF = "00000000-0000-4000-8000-000000000501"
_SESSION_REF = "00000000-0000-4000-8000-000000000502"
_COOKIE_SECRET = "synthetic-account-cookie-secret"


def test_bilibili_lease_operations_are_the_closed_data_tool_set() -> None:
    """Expose exactly the eleven account-backed operations, excluding login calls."""

    assert {operation.value for operation in BilibiliOperation} == {
        "bilibili.search",
        "bilibili.user.find",
        "bilibili.video.read",
        "bilibili.comments.list",
        "bilibili.dynamics.list",
        "bilibili.dynamic.read",
        "bilibili.dm.list",
        "bilibili.dm.read",
        "bilibili.comment.create",
        "bilibili.dynamic.publish",
        "bilibili.dm.send",
    }


def test_bilibili_link_request_and_session_validate_private_references() -> None:
    """Validate opaque account references and bounded setup idempotency keys."""

    request = BeginBilibiliLinkRequest(_PROFILE_REF, "qr-request:stable-1")
    session = BilibiliLinkSession(_PROFILE_REF, _SESSION_REF, 1_900_000_000)
    assert request.profile_ref == session.profile_ref == _PROFILE_REF
    assert session.link_session_ref == _SESSION_REF

    with pytest.raises(ValueError, match="profile_ref must be a UUID"):
        BeginBilibiliLinkRequest("not-a-uuid", "qr-request:stable-1")
    with pytest.raises(ValueError, match="idempotency_key"):
        BeginBilibiliLinkRequest(_PROFILE_REF, "contains space")


@pytest.mark.parametrize(
    "external_subject", ["0", "0123", "１２３", "123456789012345678901"]
)
def test_bilibili_completion_requires_positive_ascii_mid(external_subject: str) -> None:
    """Reject zero, noncanonical, Unicode, and oversized account identities."""

    with pytest.raises(ValueError, match="positive ASCII"):
        CompleteBilibiliLinkRequest(
            profile_ref=_PROFILE_REF,
            link_session_ref=_SESSION_REF,
            external_subject=external_subject,
            credential_json=f'{{"cookie":"{_COOKIE_SECRET}"}}',
            idempotency_key="qr-complete:stable-1",
        )


def test_bilibili_completion_does_not_reveal_cookie_material_in_repr() -> None:
    """Keep the private cookie payload out of accidental dataclass logging."""

    request = CompleteBilibiliLinkRequest(
        profile_ref=_PROFILE_REF,
        link_session_ref=_SESSION_REF,
        external_subject="12345",
        credential_json=f'{{"cookie":"{_COOKIE_SECRET}"}}',
        idempotency_key="qr-complete:stable-1",
        credential_expires_at=1_900_000_000,
    )
    assert _COOKIE_SECRET not in repr(request)
