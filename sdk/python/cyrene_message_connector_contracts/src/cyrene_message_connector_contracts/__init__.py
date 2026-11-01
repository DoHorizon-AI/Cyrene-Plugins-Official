"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 __init__.py                                                      │
│  Module: cyrene_message_connector_contracts                          │
│  Role: Public frozen message.connector.v1 SDK surface.                │
│                                                                      │
│  模块职责：公开冻结的 message.connector.v1 SDK 接口。                 │
└─────────────────────────────────────────────────────────────────────┘
"""

from ._generated import message_connector_pb2
from .codec import (
    CAPABILITY_ID,
    DELIVERY_RESULT_TYPE_URL,
    EVENT_TYPE_URLS,
    EVENT_TYPES,
    INBOUND_MESSAGE_TYPE_URL,
    INBOUND_REQUEST_TYPE_URL,
    INTERFACE_VERSION,
    METHODS,
    REQUEST_TYPE_URLS,
    RESPOND_REQUEST_REQUEST_TYPE_URL,
    RESPOND_REQUEST_RESPONSE_TYPE_URL,
    RESPONSE_TYPE_URLS,
    SEND_MESSAGE_REQUEST_TYPE_URL,
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
from .validation import MessageConnectorValidationError

__all__ = [
    "CAPABILITY_ID",
    "DELIVERY_RESULT_TYPE_URL",
    "EVENT_TYPES",
    "EVENT_TYPE_URLS",
    "INBOUND_MESSAGE_TYPE_URL",
    "INBOUND_REQUEST_TYPE_URL",
    "INTERFACE_VERSION",
    "METHODS",
    "MessageConnectorValidationError",
    "REQUEST_TYPE_URLS",
    "RESPONSE_TYPE_URLS",
    "RESPOND_REQUEST_REQUEST_TYPE_URL",
    "RESPOND_REQUEST_RESPONSE_TYPE_URL",
    "SEND_MESSAGE_REQUEST_TYPE_URL",
    "decode_delivery_result",
    "decode_inbound_message",
    "decode_inbound_request",
    "decode_request_response",
    "decode_request_response_result",
    "decode_send_message_request",
    "encode_delivery_result",
    "encode_inbound_message",
    "encode_inbound_request",
    "encode_request_response",
    "encode_request_response_result",
    "encode_send_message_request",
    "message_connector_pb2",
]
