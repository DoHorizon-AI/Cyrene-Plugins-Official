"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 codec.py                                                         │
│  Module: cyrene_message_connector_contracts                          │
│  Role: Encode and decode frozen message.connector.v1 payloads.        │
│                                                                      │
│  模块职责：编解码冻结的 message.connector.v1 载荷。                   │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from typing import TypeVar

from google.protobuf.message import DecodeError, Message

from ._generated import message_connector_pb2 as wire
from .validation import (
    MessageConnectorValidationError,
    decode_json_object,
    validate_delivery_result,
    validate_inbound_message,
    validate_inbound_request,
    validate_request_response,
    validate_send_message_request,
)

CAPABILITY_ID = "message.connector.v1"
INTERFACE_VERSION = 1
METHODS = ("send_message", "respond_request")
EVENT_TYPES = ("inbound_message", "inbound_request")

SEND_MESSAGE_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest"
DELIVERY_RESULT_TYPE_URL = "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult"
INBOUND_MESSAGE_TYPE_URL = "type.cyrene.io/cyrene.message.connector.v1.InboundMessagePayload"
INBOUND_REQUEST_TYPE_URL = "type.cyrene.io/message.connector.v1.InboundRequestPayload"
RESPOND_REQUEST_REQUEST_TYPE_URL = "type.cyrene.io/message.connector.v1.respond_request.request"
RESPOND_REQUEST_RESPONSE_TYPE_URL = "type.cyrene.io/message.connector.v1.respond_request.response"

REQUEST_TYPE_URLS = {
    "send_message": SEND_MESSAGE_REQUEST_TYPE_URL,
    "respond_request": RESPOND_REQUEST_REQUEST_TYPE_URL,
}
RESPONSE_TYPE_URLS = {
    "send_message": DELIVERY_RESULT_TYPE_URL,
    "respond_request": RESPOND_REQUEST_RESPONSE_TYPE_URL,
}
EVENT_TYPE_URLS = {
    "inbound_message": INBOUND_MESSAGE_TYPE_URL,
    "inbound_request": INBOUND_REQUEST_TYPE_URL,
}

_MessageT = TypeVar("_MessageT", bound=Message)


def encode_send_message_request(request: wire.SendMessageRequest) -> bytes:
    """Validate and serialize the protobuf send request."""

    validate_send_message_request(request)
    return request.SerializeToString(deterministic=True)


def decode_send_message_request(payload: bytes) -> wire.SendMessageRequest:
    """Parse, validate, and return the protobuf send request."""

    result = _decode_proto(payload, wire.SendMessageRequest)
    validate_send_message_request(result)
    return result


def encode_delivery_result(result: wire.DeliveryResult) -> bytes:
    """Validate and serialize a send result."""

    validate_delivery_result(result)
    return result.SerializeToString(deterministic=True)


def decode_delivery_result(payload: bytes) -> wire.DeliveryResult:
    """Parse, validate, and return a send result."""

    result = _decode_proto(payload, wire.DeliveryResult)
    validate_delivery_result(result)
    return result


def encode_inbound_message(message: wire.InboundMessagePayload) -> bytes:
    """Validate and serialize one inbound-message event."""

    validate_inbound_message(message)
    return message.SerializeToString(deterministic=True)


def decode_inbound_message(payload: bytes) -> wire.InboundMessagePayload:
    """Parse, validate, and return one inbound-message event."""

    result = _decode_proto(payload, wire.InboundMessagePayload)
    validate_inbound_message(result)
    return result


def encode_inbound_request(value: dict[str, object]) -> bytes:
    """Validate and serialize one canonical inbound-request JSON event."""

    validated = validate_inbound_request(value)
    return _encode_json(validated)


def decode_inbound_request(payload: bytes | str) -> dict[str, object]:
    """Parse and validate one canonical inbound-request JSON event."""

    return validate_inbound_request(decode_json_object(payload, "inbound_request"))


def encode_request_response(value: dict[str, object]) -> bytes:
    """Validate and serialize a respond_request JSON request."""

    return _encode_json(validate_request_response(value))


def decode_request_response(payload: bytes | str) -> dict[str, object]:
    """Parse and validate a respond_request JSON request."""

    return validate_request_response(decode_json_object(payload, "respond_request"))


def decode_request_response_result(payload: bytes | str) -> dict[str, object]:
    """Parse the bounded JSON response without inventing an unfrozen schema."""

    return decode_json_object(payload, "respond_request response")


def encode_request_response_result(value: dict[str, object]) -> bytes:
    """Serialize a bounded JSON response without narrowing the frozen contract."""

    return _encode_json(value)


def _decode_proto(payload: bytes, message_type: type[_MessageT]) -> _MessageT:
    if not isinstance(payload, bytes):
        raise MessageConnectorValidationError("protobuf payload must be bytes")
    result = message_type()
    try:
        result.ParseFromString(payload)
    except DecodeError as error:
        raise MessageConnectorValidationError("protobuf payload is malformed") from error
    return result


def _encode_json(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
