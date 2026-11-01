"""People directory SDK contracts stay equal to the Account Center wire schema."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from cyrene_account_center_contracts import (
    DeclareAccountHandleRequest,
    FindPeopleRequest,
    RetractAccountHandleRequest,
    RevokePersonLinksRequest,
)

_SCHEMA = (
    Path(__file__).parents[4]
    / "plugins/accounts/account-center/contracts/v1/schema.json"
)


@pytest.mark.parametrize(
    ("dto", "definition"),
    [
        (DeclareAccountHandleRequest, "DeclareAccountHandleRequest"),
        (RetractAccountHandleRequest, "RetractAccountHandleRequest"),
        (FindPeopleRequest, "FindPeopleRequest"),
        (RevokePersonLinksRequest, "RevokePersonLinksRequest"),
    ],
)
def test_request_fields_match_the_account_center_schema(
    dto: type, definition: str
) -> None:
    """Fail when the SDK and the wire contract disagree on a field or on required-ness."""

    if not _SCHEMA.is_file():
        pytest.skip("the Account Center schema is not part of this checkout")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))["$defs"][definition]
    fields = dataclasses.fields(dto)
    required = {
        field.name
        for field in fields
        if field.default is dataclasses.MISSING
        and field.default_factory is dataclasses.MISSING
    }
    assert {field.name for field in fields} == set(schema["properties"])
    assert required == set(schema.get("required", []))
    assert schema["additionalProperties"] is False


def test_optional_fields_default_to_none_and_are_dropped_on_the_wire() -> None:
    """Optional fields are absent unless given; callers strip None before sending."""

    declared = DeclareAccountHandleRequest("github", "octocat", "key-1")
    assert (declared.subject, declared.mail, declared.display_name) == (None, None, None)
    wire = {key: value for key, value in dataclasses.asdict(declared).items() if value is not None}
    assert wire == {"provider": "github", "handle": "octocat", "idempotency_key": "key-1"}

    assert dataclasses.asdict(FindPeopleRequest()) == {
        "workspace_id": None,
        "subject": None,
        "mail": None,
        "provider": None,
        "handle": None,
        "name": None,
        "limit": None,
    }
