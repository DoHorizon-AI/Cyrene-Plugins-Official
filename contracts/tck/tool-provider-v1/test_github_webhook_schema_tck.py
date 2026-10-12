"""TCK for the GitHub provider's owner-defined webhook normalization schema."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = ROOT / "plugins/providers/github-issues/contracts/v1/webhook.schema.json"


def _schema() -> dict[str, object]:
    """Load the static Plugin owner contract used by webhook consumers."""

    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_github_webhook_request_and_response_are_closed_and_direct() -> None:
    """Accept only the signed request and direct success/error variants."""

    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    request_schema = {
        "$schema": schema["$schema"],
        "$defs": schema["$defs"],
        "$ref": "#/$defs/WebhookRequest",
    }
    response_schema = {
        "$schema": schema["$schema"],
        "$defs": schema["$defs"],
        "$ref": "#/$defs/WebhookResponse",
    }
    request = {
        "delivery_id": "delivery-1",
        "event": "issues",
        "signature": "sha256=" + "a" * 64,
        "raw_body_base64": "e30=",
    }
    normalized = {
        "event_type": "issues",
        "action": "opened",
        "repository_id": "4242",
        "issue_number": 12,
        "sender_id": "700",
        "sender_login": "reporter",
        "title": "Issue title",
        "body": "Issue body",
        "self_authored": False,
    }
    error = {"error": {"code": "INVALID_SIGNATURE"}}

    jsonschema.validate(request, request_schema)
    jsonschema.validate(normalized, response_schema)
    jsonschema.validate(error, response_schema)

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**request, "connection_id": "caller-selected"}, request_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"event": normalized}, response_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"error": {"code": "INVALID_SIGNATURE", "message": "raw"}}, response_schema)
