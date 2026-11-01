"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_codec.py                                                    │
│  Module: cyrene_message_connector_contracts                          │
│  Role: Exercise frozen wire codec, validation, and TCK boundaries.    │
│                                                                      │
│  模块职责：验证冻结协议编解码、边界校验与 TCK 场景。                  │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cyrene_message_connector_contracts import (
    CAPABILITY_ID,
    EVENT_TYPE_URLS,
    INTERFACE_VERSION,
    METHODS,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    MessageConnectorValidationError,
    decode_delivery_result,
    decode_inbound_message,
    decode_inbound_request,
    decode_request_response,
    decode_request_response_result,
    decode_send_message_request,
    encode_delivery_result,
    encode_inbound_message,
    encode_inbound_request,
    encode_request_response,
    encode_request_response_result,
    encode_send_message_request,
)
from cyrene_message_connector_contracts import (
    message_connector_pb2 as wire,
)


def test_public_wire_mapping_is_frozen() -> None:
    assert CAPABILITY_ID == "message.connector.v1"
    assert INTERFACE_VERSION == 1
    assert METHODS == ("send_message", "respond_request")
    assert REQUEST_TYPE_URLS["send_message"].endswith("SendMessageRequest")
    assert RESPONSE_TYPE_URLS["send_message"].endswith("DeliveryResult")
    assert EVENT_TYPE_URLS["inbound_message"].endswith("InboundMessagePayload")
    assert EVENT_TYPE_URLS["inbound_request"] == (
        "type.cyrene.io/message.connector.v1.InboundRequestPayload"
    )


def test_send_message_round_trip_and_attachment_scope() -> None:
    request = wire.SendMessageRequest()
    request.conversation.vendor = "qq"
    request.conversation.account_id = "bot-1"
    request.conversation.conversation_id = "group-7"
    request.conversation.kind = wire.CONVERSATION_KIND_GROUP
    request.content.add().text.text = "hello"
    decoded = decode_send_message_request(encode_send_message_request(request))
    assert decoded == request

    request.content.add().image.mime_type = "image/png"
    media = request.content[-1].image.reference.vendor_media
    media.vendor = "qq"
    media.account_id = "other-bot"
    media.media_id = "m-1"
    with pytest.raises(MessageConnectorValidationError, match="match the message"):
        encode_send_message_request(request)


@pytest.mark.parametrize(
    "uri",
    ["file:///tmp/private.png", "relative/file.png", "https://user:secret@example.test/a"],
)
def test_outbound_attachment_rejects_local_or_credential_uri(uri: str) -> None:
    request = _valid_send_request()
    request.content.add().image.mime_type = "image/png"
    request.content[-1].image.reference.remote_uri = uri
    with pytest.raises(MessageConnectorValidationError):
        encode_send_message_request(request)


def test_delivery_result_requires_known_status() -> None:
    accepted = wire.DeliveryResult(status=wire.DELIVERY_STATUS_ACCEPTED)
    assert decode_delivery_result(encode_delivery_result(accepted)) == accepted
    with pytest.raises(MessageConnectorValidationError, match="unspecified"):
        encode_delivery_result(wire.DeliveryResult())


def test_inbound_message_round_trip_and_required_identity() -> None:
    inbound = wire.InboundMessagePayload(
        message_id="msg-1",
        sender_id="user-2",
    )
    inbound.conversation.vendor = "qq"
    inbound.conversation.account_id = "bot-1"
    inbound.conversation.conversation_id = "user-2"
    inbound.conversation.kind = wire.CONVERSATION_KIND_PRIVATE
    inbound.content.add().text.text = "hi"
    assert decode_inbound_message(encode_inbound_message(inbound)) == inbound
    inbound.message_id = ""
    with pytest.raises(MessageConnectorValidationError):
        encode_inbound_message(inbound)


def test_inbound_request_and_response_json_tck() -> None:
    inbound = {
        "account_id": "bot-1",
        "vendor": "onebot.v11",
        "request_id": "opaque-1",
        "request_kind": "friend",
        "vendor_request": {"sub_type": "add", "user_id": "user-9"},
    }
    encoded = encode_inbound_request(inbound)
    assert json.loads(encoded) == inbound
    assert decode_inbound_request(encoded) == inbound

    approval = {
        "request_id": "opaque-1",
        "request_kind": "friend",
        "decision": "approve",
        "comment": "Welcome",
        "vendor_request": {"sub_type": "add", "user_id": "user-9"},
    }
    assert decode_request_response(encode_request_response(approval)) == approval
    response = {
        "status": "accepted",
        "request_id": "opaque-1",
        "request_kind": "friend",
        "decision": "approve",
    }
    assert decode_request_response_result(
        encode_request_response_result(response)
    ) == response


@pytest.mark.parametrize(
    "payload",
    [
        b'{"account_id":"a","account_id":"b"}',
        b'{"account_id":"a","vendor":"v","request_id":"r",'
        b'"request_kind":"friend","vendor_request":{"secret":"x"}}',
        b'{"account_id":"a","vendor":"v","request_id":"r",'
        b'"request_kind":"unknown","vendor_request":{}}',
        b'{"account_id":"a","vendor":"v","request_id":"r",'
        b'"request_kind":"friend","vendor_request":{},"extra":1}',
    ],
)
def test_inbound_json_rejects_invalid_or_ambiguous_payload(payload: bytes) -> None:
    with pytest.raises(MessageConnectorValidationError):
        decode_inbound_request(payload)


def test_response_json_rejects_unknown_request_kind_and_ambiguous_result() -> None:
    with pytest.raises(MessageConnectorValidationError):
        encode_request_response(
            {"request_id": "r", "request_kind": "other", "decision": "approve"}
        )
    with pytest.raises(MessageConnectorValidationError):
        decode_request_response_result(b'{"status":"accepted","status":"rejected"}')


def test_binary_descriptor_coexists_with_private_qq_and_teams_bindings() -> None:
    sdk_root = Path(__file__).resolve().parents[1]
    repo_root = sdk_root.parents[2]
    script = """
import importlib.util
import sys
order = sys.argv[1:]
modules = []
for index, path in enumerate(order):
    spec = importlib.util.spec_from_file_location(f'private_binding_{index}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    modules.append(module)
for module in modules:
    assert module.DESCRIPTOR.name == 'message_connector.proto'
    assert module.DESCRIPTOR.serialized_pb == modules[-1].DESCRIPTOR.serialized_pb
"""
    plugin_source = repo_root / "plugins/connectors"
    qq_binding = (
        plugin_source / "im/src/qq_connector/_generated/message_connector_pb2.py"
    )
    teams_binding = (
        plugin_source / "teams/src/teams_connector/_generated/message_connector_pb2.py"
    )
    sdk_binding = (
        sdk_root
        / "src/cyrene_message_connector_contracts/_generated/message_connector_pb2.py"
    )
    bindings = [str(qq_binding), str(teams_binding), str(sdk_binding)]
    for order in (bindings, list(reversed(bindings))):
        completed = subprocess.run(
            [sys.executable, "-c", script, *order],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr


def _valid_send_request() -> wire.SendMessageRequest:
    request = wire.SendMessageRequest()
    request.conversation.vendor = "qq"
    request.conversation.account_id = "bot-1"
    request.conversation.conversation_id = "group-7"
    request.conversation.kind = wire.CONVERSATION_KIND_GROUP
    request.content.add().text.text = "hello"
    return request
