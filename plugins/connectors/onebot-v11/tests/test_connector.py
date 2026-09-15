###############################################################################
# 📄 File: plugins/connectors/onebot-v11/tests/test_connector.py
# Module: Cyrene Plugins Official
# Role: Focused automated test coverage.
#
# This file documents an active plugin boundary; runtime behavior is unchanged.
#
# 模块：Cyrene Plugins Official
# 职责：聚焦的自动化测试覆盖。
# 本文件属于活动插件边界；运行时行为保持不变。
###############################################################################
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from onebot_v11_connector import (
    CAPABILITY_ID,
    DELIVERY_RESULT_TYPE_URL,
    INBOUND_MESSAGE_EVENT_TYPE,
    INBOUND_MESSAGE_TYPE_URL,
    INBOUND_REQUEST_EVENT_TYPE,
    INBOUND_REQUEST_TYPE_URL,
    ONEBOT_VENDOR,
    QQ_CAPABILITY_ID,
    RESPOND_REQUEST_METHOD,
    RESPOND_REQUEST_TYPE_URL,
    RESPOND_RESULT_TYPE_URL,
    ConnectorError,
    OneBotV11Connector,
    normalize_inbound_event,
    normalize_request_event,
)
from onebot_v11_connector._generated import (
    message_connector_pb2 as message_contract,
)


class MemoryTransport:
    """Deterministic transport fake that records the configured worker target."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def call(
        self,
        action: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float,
        cancellation: Any,
    ) -> dict[str, Any]:
        if cancellation is not None and cancellation.is_cancelled():
            raise ConnectorError("CANCELLED", "transport call was cancelled")
        self.calls.append(
            {
                "action": action,
                "params": params,
                "timeout_seconds": timeout_seconds,
                "label": self.label,
            }
        )
        return {"message_id": f"{self.label}-message-1"}

    def close(self) -> None:
        self.closed = True


class MemoryEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, bytes, str]] = []
        self.accepting = True

    def emit(self, event_type: str, payload: bytes, type_url: str = "") -> bool:
        if not self.accepting:
            return False
        self.events.append((event_type, payload, type_url))
        return True


class CancelledToken:
    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self.cancelled


class BoundCapabilityHarness:
    """Test-only CES seam; Platform remains the production binding authority."""

    def __init__(self, workers: dict[str, OneBotV11Connector]) -> None:
        self.workers = workers

    def invoke(
        self,
        capability: str,
        method: str,
        request: dict[str, Any],
        *,
        binding_id: str | None = None,
    ) -> dict[str, Any]:
        matches = [
            worker
            for worker in self.workers.values()
            if worker.configured_binding_id is not None and capability == CAPABILITY_ID
        ]
        if binding_id is None:
            if not matches:
                raise ConnectorError("CAPABILITY_UNAVAILABLE", "no matching binding")
            if len(matches) > 1:
                raise ConnectorError(
                    "INVALID_REQUEST", "ambiguous configured capability selection"
                )
            worker = matches[0]
        else:
            worker = self.workers.get(binding_id)
            if worker is None:
                raise ConnectorError(
                    "CAPABILITY_UNAVAILABLE", "unknown configured capability binding"
                )
        if capability != CAPABILITY_ID or method != "send_message":
            raise ConnectorError("INVALID_REQUEST", "capability method is unsupported")
        return worker.send_message(request)

    def subscribe(
        self,
        capability: str,
        emitter: MemoryEmitter,
        *,
        binding_id: str | None = None,
    ) -> None:
        if binding_id is None or binding_id not in self.workers:
            raise ConnectorError(
                "CAPABILITY_UNAVAILABLE", "subscription target is unavailable"
            )
        result = self.workers[binding_id].on_subscribe(
            f"subscription-{binding_id}", capability, b"{}", emitter
        )
        if result is not None:
            code, _, message = result.partition(": ")
            raise ConnectorError(code, message)


def config(binding_id: str, account_id: str) -> dict[str, Any]:
    return {
        "binding_id": binding_id,
        "http_base_url": f"http://{binding_id}.invalid",
        "runtime_profile": "qq-client",
        "self_account_id": account_id,
        "timeout_seconds": 1.25,
    }


def send_request(account_id: str = "10001") -> dict[str, Any]:
    return {
        "conversation": {
            "vendor": "onebot.v11",
            "account_id": account_id,
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
                            "account_id": account_id,
                            "media_id": "opaque-file-id",
                        }
                    },
                    "file_name": "notes.txt",
                    "mime_type": "text/plain",
                }
            },
        ],
    }


def inbound_event(account_id: str, message_id: str) -> dict[str, Any]:
    return {
        "time": 1,
        "self_id": account_id,
        "post_type": "message",
        "message_type": "group",
        "message_id": message_id,
        "group_id": "20001",
        "sender": {"user_id": "10002", "nickname": "member"},
        "message": [
            {"type": "text", "data": {"text": f"event-{account_id}"}},
            {"type": "at", "data": {"qq": "all"}},
            {"type": "reply", "data": {"id": "30001"}},
        ],
    }


def test_manifest_and_package_descriptor_project_both_connector_profiles() -> None:
    package_root = Path(__file__).parents[1]
    manifest = json.loads(
        (package_root / "plugin.manifest.json").read_text(encoding="utf-8")
    )
    descriptor = json.loads(
        (package_root / "package-descriptor.json").read_text(encoding="utf-8")
    )

    assert manifest["id"] == "cyrene.connectors.onebot-v11"
    assert manifest["capabilities"] == [CAPABILITY_ID, QQ_CAPABILITY_ID]
    assert descriptor["capability"]["id"] == CAPABILITY_ID
    assert descriptor["connector"]["type"] == "onebot_v11"
    assert descriptor["publication_status"] == "CANDIDATE"
    assert descriptor["runtime"]["external"]["kind"] == "onebot_v11_runtime"
    assert descriptor["profiles"][1]["name"] == "qqnt-direct"
    assert descriptor["profiles"][1]["runtime"]["ipc"] == "inherited-stdio"


def test_worker_activation_can_receive_one_binding_from_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", "qq-main")
    monkeypatch.setenv(
        "CYRENE_CAPABILITY_CONFIGURATION_JSON",
        json.dumps(
            {
                "http_base_url": "http://127.0.0.1:18080",
                "runtime_profile": "qq-client",
                "self_account_id": "10001",
                "timeout_seconds": "2.5",
            }
        ),
    )

    connector = OneBotV11Connector(transport=MemoryTransport("main"))

    assert connector.configured_binding_id == "qq-main"
    assert connector.runtime_profile == "qq-client"
    connector.send_message(send_request())
    assert connector._transport.calls[0]["timeout_seconds"] == 2.5  # noqa: SLF001


def test_ordered_content_and_reply_map_to_onebot_segments() -> None:
    transport = MemoryTransport("main")
    connector = OneBotV11Connector(config("qq-main", "10001"), transport=transport)

    result = connector.send_message(send_request())

    assert result == {
        "status": "accepted",
        "vendor_message_id": "main-message-1",
    }
    call = transport.calls[0]
    assert call["action"] == "send_group_msg"
    assert call["params"]["group_id"] == 20001
    assert call["params"]["message"] == [
        {"type": "reply", "data": {"id": 30001}},
        {"type": "text", "data": {"text": "hello"}},
        {"type": "at", "data": {"qq": "10002"}},
        {"type": "image", "data": {"url": "https://cdn.example/image.png"}},
        {
            "type": "file",
            "data": {"file": "opaque-file-id", "name": "notes.txt"},
        },
    ]


def test_canonical_protobuf_invoke_returns_typed_delivery_result() -> None:
    transport = MemoryTransport("main")
    connector = OneBotV11Connector(config("qq-main", "10001"), transport=transport)
    request = message_contract.SendMessageRequest()
    request.conversation.vendor = ONEBOT_VENDOR
    request.conversation.account_id = "10001"
    request.conversation.conversation_id = "20001"
    request.conversation.kind = message_contract.CONVERSATION_KIND_GROUP
    request.content.add().text.text = "hello"

    ok, result = connector.on_invoke(
        CAPABILITY_ID, "send_message", request.SerializeToString()
    )

    assert ok
    if isinstance(result, tuple):
        payload, type_url = result
    else:
        payload, type_url = result.value, result.type_url
    assert type_url == DELIVERY_RESULT_TYPE_URL
    response = message_contract.DeliveryResult.FromString(payload)
    assert response.status == message_contract.DELIVERY_STATUS_ACCEPTED
    assert response.vendor_message_id == "main-message-1"
    assert transport.calls[0]["action"] == "send_group_msg"


def test_canonical_protobuf_inbound_event_preserves_type_and_content() -> None:
    connector = OneBotV11Connector(
        config("qq-main", "10001"), transport=MemoryTransport("main")
    )
    emitter = MemoryEmitter()
    assert connector.on_subscribe("s-main", CAPABILITY_ID, b"{}", emitter) is None

    assert connector.publish_inbound_event(inbound_event("10001", "m-main")) == 1

    assert emitter.events[0][0] == INBOUND_MESSAGE_EVENT_TYPE
    assert emitter.events[0][2] == INBOUND_MESSAGE_TYPE_URL
    payload = message_contract.InboundMessagePayload.FromString(emitter.events[0][1])
    assert payload.message_id == "m-main"
    assert payload.conversation.account_id == "10001"
    assert payload.conversation.kind == message_contract.CONVERSATION_KIND_GROUP
    assert payload.content[0].text.text == "event-10001"
    assert payload.content[1].mention.target == (
        message_contract.MENTION_TARGET_EVERYONE
    )
    assert payload.reply.message_id == "30001"


def test_inbound_request_event_is_normalized_for_product_auto_approval() -> None:
    connector = OneBotV11Connector(
        config("qq-main", "10001"), transport=MemoryTransport("main")
    )
    emitter = MemoryEmitter()
    assert connector.on_subscribe("s-main", CAPABILITY_ID, b"{}", emitter) is None

    delivered = connector.publish_inbound_event(
        {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "invite",
            "self_id": "10001",
            "flag": "request-1",
            "group_id": "20001",
            "user_id": "10002",
        }
    )
    assert delivered == 1
    assert emitter.events[0][0] == INBOUND_REQUEST_EVENT_TYPE
    assert emitter.events[0][2] == INBOUND_REQUEST_TYPE_URL
    assert json.loads(emitter.events[0][1]) == {
        "account_id": "10001",
        "request_id": "request-1",
        "request_kind": "group_invite",
        "vendor": "onebot.v11",
        "vendor_request": {
            "group_id": "20001",
            "sub_type": "invite",
            "user_id": "10002",
        },
    }
    assert (
        normalize_request_event(
            {
                "post_type": "request",
                "request_type": "friend",
                "sub_type": "add",
                "self_id": "10001",
                "flag": "friend-1",
                "user_id": "10002",
            }
        )["request_kind"]
        == "friend"
    )


def test_canonical_protobuf_invoke_preserves_cancellation() -> None:
    connector = OneBotV11Connector(
        config("qq-main", "10001"), transport=MemoryTransport("main")
    )
    request = message_contract.SendMessageRequest()
    request.conversation.vendor = ONEBOT_VENDOR
    request.conversation.account_id = "10001"
    request.conversation.conversation_id = "20001"
    request.conversation.kind = message_contract.CONVERSATION_KIND_GROUP
    request.content.add().text.text = "hello"

    ok, error = connector.on_invoke(
        CAPABILITY_ID,
        "send_message",
        request.SerializeToString(),
        cancellation=CancelledToken(True),
    )

    assert not ok
    assert "CANCELLED" in str(error)
    assert connector._transport.calls == []  # noqa: SLF001 - cancellation proof


def test_respond_request_maps_friend_and_group_invite_approvals() -> None:
    transport = MemoryTransport("approval")
    connector = OneBotV11Connector(config("qq-main", "10001"), transport=transport)

    friend = connector.respond_request(
        {
            "request_id": "friend-flag",
            "request_kind": "friend",
            "decision": "approve",
            "comment": "welcome",
        }
    )
    group = connector.respond_request(
        {
            "request_id": "group-flag",
            "request_kind": "group_invite",
            "decision": "reject",
        }
    )
    assert friend["decision"] == "approve"
    assert group["decision"] == "reject"
    assert transport.calls[0]["action"] == "set_friend_add_request"
    assert transport.calls[0]["params"] == {
        "flag": "friend-flag",
        "approve": True,
        "remark": "welcome",
    }
    assert transport.calls[1]["action"] == "set_group_add_request"
    assert transport.calls[1]["params"] == {
        "flag": "group-flag",
        "sub_type": "invite",
        "approve": False,
    }

    ok, typed = connector.on_invoke(
        CAPABILITY_ID,
        RESPOND_REQUEST_METHOD,
        json.dumps(
            {
                "request_id": "json-flag",
                "request_kind": "friend",
                "decision": "reject",
            }
        ).encode(),
        request_type_url=RESPOND_REQUEST_TYPE_URL,
    )
    assert ok is True
    assert typed.type_url == RESPOND_RESULT_TYPE_URL
    assert json.loads(typed.value)["request_id"] == "json-flag"


def test_explicit_invoke_targets_each_same_package_instance() -> None:
    main = OneBotV11Connector(
        config("qq-main", "10001"), transport=MemoryTransport("main")
    )
    secondary = OneBotV11Connector(
        config("qq-secondary", "10002"), transport=MemoryTransport("secondary")
    )
    harness = BoundCapabilityHarness({"qq-main": main, "qq-secondary": secondary})

    harness.invoke(
        CAPABILITY_ID,
        "send_message",
        send_request("10001"),
        binding_id="qq-main",
    )
    harness.invoke(
        CAPABILITY_ID,
        "send_message",
        send_request("10002"),
        binding_id="qq-secondary",
    )

    assert len(main._transport.calls) == 1  # noqa: SLF001 - TCK transport proof
    assert len(secondary._transport.calls) == 1  # noqa: SLF001 - TCK transport proof
    assert main._transport.calls[0]["label"] == "main"  # noqa: SLF001
    assert secondary._transport.calls[0]["label"] == "secondary"  # noqa: SLF001


def test_implicit_selection_is_only_allowed_when_unambiguous() -> None:
    main = OneBotV11Connector(
        config("qq-main", "10001"), transport=MemoryTransport("main")
    )
    single = BoundCapabilityHarness({"qq-main": main})
    single.invoke(CAPABILITY_ID, "send_message", send_request("10001"))

    secondary = OneBotV11Connector(
        config("qq-secondary", "10002"), transport=MemoryTransport("secondary")
    )
    multiple = BoundCapabilityHarness({"qq-main": main, "qq-secondary": secondary})
    with pytest.raises(ConnectorError, match="ambiguous") as error:
        multiple.invoke(CAPABILITY_ID, "send_message", send_request("10001"))
    assert error.value.code == "INVALID_REQUEST"


def test_subscriptions_are_isolated_by_the_configured_binding() -> None:
    main = OneBotV11Connector(
        config("qq-main", "10001"), transport=MemoryTransport("main")
    )
    secondary = OneBotV11Connector(
        config("qq-secondary", "10002"), transport=MemoryTransport("secondary")
    )
    harness = BoundCapabilityHarness({"qq-main": main, "qq-secondary": secondary})
    main_events = MemoryEmitter()
    secondary_events = MemoryEmitter()
    harness.subscribe(CAPABILITY_ID, main_events, binding_id="qq-main")
    harness.subscribe(CAPABILITY_ID, secondary_events, binding_id="qq-secondary")

    assert main.publish_inbound_event(inbound_event("10001", "m-main")) == 1
    assert secondary.publish_inbound_event(inbound_event("10002", "m-secondary")) == 1

    assert [event[0] for event in main_events.events] == [INBOUND_MESSAGE_EVENT_TYPE]
    assert [event[0] for event in secondary_events.events] == [
        INBOUND_MESSAGE_EVENT_TYPE
    ]
    main_payload = message_contract.InboundMessagePayload.FromString(
        main_events.events[0][1]
    )
    secondary_payload = message_contract.InboundMessagePayload.FromString(
        secondary_events.events[0][1]
    )
    assert main_events.events[0][2] == INBOUND_MESSAGE_TYPE_URL
    assert secondary_events.events[0][2] == INBOUND_MESSAGE_TYPE_URL
    assert main_payload.conversation.account_id == "10001"
    assert secondary_payload.conversation.account_id == "10002"
    assert main_payload.content[0].text.text == "event-10001"
    assert secondary_payload.content[0].text.text == "event-10002"


def test_invoke_and_subscribe_share_the_same_configured_identity() -> None:
    transport = MemoryTransport("main")
    connector = OneBotV11Connector(config("qq-main", "10001"), transport=transport)
    emitter = MemoryEmitter()

    assert connector.configured_binding_id == "qq-main"
    assert connector.on_subscribe("s-main", CAPABILITY_ID, b"{}", emitter) is None
    connector.send_message(send_request())
    connector.publish_inbound_event(inbound_event("10001", "m-main"))

    assert connector.configured_binding_id == "qq-main"
    assert transport.calls[0]["label"] == "main"
    assert emitter.events[0][2] == INBOUND_MESSAGE_TYPE_URL
    assert (
        message_contract.InboundMessagePayload.FromString(
            emitter.events[0][1]
        ).message_id
        == "m-main"
    )


def test_binding_identity_survives_a_worker_runtime_restart() -> None:
    generation_one = OneBotV11Connector(
        config("qq-main", "10001"), transport=MemoryTransport("generation-1")
    )
    generation_two = OneBotV11Connector(
        config("qq-main", "10001"), transport=MemoryTransport("generation-2")
    )

    assert generation_one.configured_binding_id == generation_two.configured_binding_id
    assert (
        generation_one.runtime_profile == generation_two.runtime_profile == "qq-client"
    )
    assert generation_one._transport is not generation_two._transport  # noqa: SLF001


def test_unknown_target_and_capability_mismatch_are_deterministic() -> None:
    connector = OneBotV11Connector(
        config("qq-main", "10001"), transport=MemoryTransport("main")
    )
    harness = BoundCapabilityHarness({"qq-main": connector})

    with pytest.raises(ConnectorError, match="unknown configured") as unknown:
        harness.invoke(
            CAPABILITY_ID,
            "send_message",
            send_request(),
            binding_id="missing",
        )
    assert unknown.value.code == "CAPABILITY_UNAVAILABLE"

    result = connector.on_subscribe(
        "bad", "other.capability.v1", b"{}", MemoryEmitter()
    )
    assert result == "INVALID_REQUEST: unsupported capability 'other.capability.v1'"


def test_account_mismatch_cannot_cross_configured_instances() -> None:
    connector = OneBotV11Connector(
        config("qq-main", "10001"), transport=MemoryTransport("main")
    )

    with pytest.raises(ConnectorError, match="does not match") as error:
        connector.publish_inbound_event(inbound_event("10002", "wrong-account"))
    assert error.value.code == "INVALID_REQUEST"


def test_cancellation_is_preserved_before_transport_dispatch() -> None:
    transport = MemoryTransport("main")
    connector = OneBotV11Connector(config("qq-main", "10001"), transport=transport)
    cancellation = CancelledToken(True)

    with pytest.raises(ConnectorError, match="cancelled") as error:
        connector.send_message(send_request(), cancellation=cancellation)
    assert error.value.code == "CANCELLED"
    assert transport.calls == []


def test_configured_timeout_is_passed_to_transport() -> None:
    transport = MemoryTransport("main")
    connector = OneBotV11Connector(config("qq-main", "10001"), transport=transport)

    connector.send_message(send_request())

    assert transport.calls[0]["timeout_seconds"] == 1.25


def test_unknown_segments_are_bounded_vendor_facts() -> None:
    payload = normalize_inbound_event(
        {
            **inbound_event("10001", "m-unknown"),
            "message": [
                {"type": "text", "data": {"text": "before"}},
                {
                    "type": "json",
                    "data": {"foo": "bar", "nested": {"secret": "not forwarded"}},
                },
            ],
        }
    )

    assert payload["content"] == [{"text": {"text": "before"}}]
    facts = payload["vendor_extension"]["facts"]
    assert {fact["name"] for fact in facts} == {
        "segment_1_type",
        "segment_1_foo",
    }
    assert all(len(fact["value"].encode()) <= 2_048 for fact in facts)
