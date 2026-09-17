###############################################################################
# File: generate_equivalence_fixtures.py
# Module: Cyrene Plugins Official
# Role: Generate cross-language OneBot behavior fixtures from the Python baseline.
#
# 模块：Cyrene Plugins Official
# 职责：从 Python 基线生成跨语言 OneBot 行为 fixture。
###############################################################################
"""Generate deterministic fixtures consumed by the C# migration tests.

The Python connector remains the behavior reference during migration.  This
tool intentionally calls the public baseline mapping functions and records the
result as reviewable JSON.  C# tests consume the checked-in output without
loading Python, so the same fixture can be used by both test suites.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = REPOSITORY_ROOT / "plugins/connectors/onebot-v11/src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from onebot_v11_connector.connector import (  # noqa: E402
    OneBotInstanceConfig,
    OneBotV11Connector,
    normalize_inbound_event,
    normalize_request_event,
)

FIXTURE_PATH = (
    REPOSITORY_ROOT
    / "plugins/connectors/onebot-v11/tests/fixtures/native-equivalence.json"
)


class FixtureTransport:
    """Deterministic transport used only to derive Python expected results."""

    def __init__(self, response_id: str = "fixture-message-1") -> None:
        self.response_id = response_id
        self.calls: list[dict[str, Any]] = []

    def call(
        self,
        action: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float,
        cancellation: Any,
    ) -> dict[str, Any]:
        del cancellation
        self.calls.append(
            {
                "action": action,
                "params": params,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"message_id": self.response_id}

    def close(self) -> None:
        """Satisfy the connector transport protocol."""


def _send_group_rich() -> dict[str, Any]:
    return {
        "conversation": {
            "vendor": "onebot.v11",
            "account_id": "10001",
            "conversation_id": "20001",
            "kind": "group",
        },
        "reply": {"message_id": "30001"},
        "content": [
            {"text": {"text": "hello"}},
            {
                "mention": {
                    "target": "user",
                    "target_id": "10002",
                    "display_name": "member",
                }
            },
            {
                "image": {
                    "reference": {"remote_uri": "https://cdn.example/image.png"},
                    "mime_type": "image/png",
                }
            },
            {
                "file": {
                    "reference": {
                        "vendor_media": {
                            "vendor": "onebot.v11",
                            "account_id": "10001",
                            "media_id": "opaque-file-id",
                        }
                    },
                    "file_name": "notes.txt",
                    "mime_type": "text/plain",
                }
            },
        ],
    }


def _send_private_everyone() -> dict[str, Any]:
    return {
        "conversation": {
            "vendor": "onebot.v11",
            "account_id": "10001",
            "conversation_id": "10002",
            "kind": "private",
        },
        "content": [
            {"mention": {"target": "everyone"}},
            {"file": {"reference": {"remote_uri": "https://cdn.example/manual.pdf"}}},
        ],
    }


def _inbound_group_rich() -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": 10001,
        "group_id": 20001,
        "message_id": 30001,
        "sender": {"user_id": 10002, "card": "Card", "nickname": "Nick"},
        "message": [
            {"type": "text", "data": {"text": "hello"}},
            {"type": "at", "data": {"qq": "10003", "name": "Bob"}},
            {
                "type": "image",
                "data": {"url": "https://cdn.example/a.png"},
            },
            {"type": "file", "data": {"file": "media-1", "name": "a.txt"}},
            {"type": "reply", "data": {"id": "29999"}},
            {"type": "json", "data": {"foo": "bar"}},
        ],
    }


def _inbound_private_everyone() -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": "10001",
        "user_id": "10002",
        "message_id": "private-1",
        "sender": {"user_id": "10002", "nickname": "Alice"},
        "message": [
            {"type": "at", "data": {"qq": "all"}},
            {"type": "text", "data": {"text": "notice"}},
        ],
    }


def _request_friend() -> dict[str, Any]:
    return {
        "post_type": "request",
        "request_type": "friend",
        "sub_type": "add",
        "self_id": "10001",
        "flag": "friend-1",
        "user_id": 10002,
    }


def _request_group_invite() -> dict[str, Any]:
    return {
        "post_type": "request",
        "request_type": "group",
        "sub_type": "invite",
        "self_id": "10001",
        "flag": "group-1",
        "group_id": 20001,
        "user_id": 10002,
    }


def _send_case(case_id: str, request: dict[str, Any]) -> dict[str, Any]:
    transport = FixtureTransport()
    connector = OneBotV11Connector(
        OneBotInstanceConfig.from_mapping(
            {
                "binding_id": "fixture",
                "http_base_url": "http://fixture.invalid",
                "self_account_id": "10001",
                "timeout_seconds": 2.0,
            }
        ),
        transport=transport,
    )
    result = connector.send_message(request)
    return {
        "id": case_id,
        "kind": "send_message",
        "input": request,
        "expected": {
            "action": transport.calls[0]["action"],
            "params": transport.calls[0]["params"],
            "result": result,
        },
    }


def _respond_case(
    case_id: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    transport = FixtureTransport()
    connector = OneBotV11Connector(
        OneBotInstanceConfig.from_mapping(
            {
                "binding_id": "fixture",
                "http_base_url": "http://fixture.invalid",
                "self_account_id": "10001",
                "timeout_seconds": 2.0,
            }
        ),
        transport=transport,
    )
    result = connector.respond_request(request)
    return {
        "id": case_id,
        "kind": "respond_request",
        "input": request,
        "expected": {
            "action": transport.calls[0]["action"],
            "params": transport.calls[0]["params"],
            "result": result,
        },
    }


def build_fixture() -> dict[str, Any]:
    """Build the complete checked-in cross-language behavior fixture."""

    inbound_group = _inbound_group_rich()
    inbound_private = _inbound_private_everyone()
    request_friend = _request_friend()
    request_group = _request_group_invite()
    return {
        "schema_version": 1,
        "baseline": "python-onebot-v11",
        "cases": [
            _send_case("send-group-rich", _send_group_rich()),
            _send_case("send-private-everyone", _send_private_everyone()),
            _respond_case(
                "respond-friend-approve",
                {
                    "request_id": "friend-1",
                    "request_kind": "friend",
                    "decision": "approve",
                    "comment": "welcome",
                },
            ),
            _respond_case(
                "respond-group-invite-reject",
                {
                    "request_id": "group-1",
                    "request_kind": "group_invite",
                    "decision": "reject",
                    "vendor_request": {"sub_type": "invite"},
                },
            ),
            {
                "id": "inbound-group-rich",
                "kind": "inbound_message",
                "input": inbound_group,
                "expected": normalize_inbound_event(inbound_group),
            },
            {
                "id": "inbound-private-everyone",
                "kind": "inbound_message",
                "input": inbound_private,
                "expected": normalize_inbound_event(inbound_private),
            },
            {
                "id": "inbound-request-friend",
                "kind": "inbound_request",
                "input": request_friend,
                "expected": normalize_request_event(request_friend),
            },
            {
                "id": "inbound-request-group-invite",
                "kind": "inbound_request",
                "input": request_group,
                "expected": normalize_request_event(request_group),
            },
        ],
    }


def write_fixture(path: Path = FIXTURE_PATH) -> None:
    """Write one stable, human-reviewable JSON fixture file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_fixture(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """Run the fixture generator from the repository checkout."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=FIXTURE_PATH)
    args = parser.parse_args()
    write_fixture(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
