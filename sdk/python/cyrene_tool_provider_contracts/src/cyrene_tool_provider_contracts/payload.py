"""
┌─────────────────────────────────────────────────────────────────────────┐
│ 📄 payload.py                                                           │
│ Module: cyrene_tool_provider_contracts.payload                           │
│ Role: Method-aware typed Direct Plugin payload encode/decode helpers.    │
│                                                                         │
│ 模块职责：提供按方法校验的 Direct Plugin 类型化 payload 编解码。        │
└─────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from dataclasses import dataclass

from google.protobuf.message import DecodeError, Message

from . import tool_provider_pb2
from .contracts import (
    CALL_TOOL_REQUEST_TYPE_URL,
    CALL_TOOL_RESPONSE_TYPE_URL,
    LIST_TOOLS_REQUEST_TYPE_URL,
    LIST_TOOLS_RESPONSE_TYPE_URL,
    METHOD_CALL_TOOL,
    METHOD_LIST_TOOLS,
)

_REQUEST_TYPES = {
    METHOD_LIST_TOOLS: (
        tool_provider_pb2.ListToolsRequest,
        LIST_TOOLS_REQUEST_TYPE_URL,
    ),
    METHOD_CALL_TOOL: (tool_provider_pb2.CallToolRequest, CALL_TOOL_REQUEST_TYPE_URL),
}
_RESPONSE_TYPES = {
    METHOD_LIST_TOOLS: (
        tool_provider_pb2.ListToolsResponse,
        LIST_TOOLS_RESPONSE_TYPE_URL,
    ),
    METHOD_CALL_TOOL: (tool_provider_pb2.CallToolResponse, CALL_TOOL_RESPONSE_TYPE_URL),
}


class ToolProviderCodecError(ValueError):
    """A method, payload type URL, or protobuf message is invalid."""


@dataclass(frozen=True)
class TypedPayload:
    """One Direct Plugin payload with its canonical protobuf type URL."""

    type_url: str
    value: bytes


def _encode(
    method: str,
    message: Message,
    mappings: dict[str, tuple[type[Message], str]],
    role: str,
) -> TypedPayload:
    expected = mappings.get(method)
    if expected is None:
        raise ToolProviderCodecError(f"unsupported tool provider method: {method}")
    message_type, type_url = expected
    if not isinstance(message, message_type):
        raise ToolProviderCodecError(
            f"{role} protobuf type does not match method: {method}"
        )
    if role == "response" and not message.WhichOneof("result"):
        raise ToolProviderCodecError("tool provider response must select a result")
    return TypedPayload(
        type_url=type_url, value=message.SerializeToString(deterministic=True)
    )


def _decode(
    method: str,
    payload: TypedPayload,
    mappings: dict[str, tuple[type[Message], str]],
    role: str,
) -> Message:
    expected = mappings.get(method)
    if expected is None:
        raise ToolProviderCodecError(f"unsupported tool provider method: {method}")
    message_type, type_url = expected
    if not isinstance(payload, TypedPayload):
        raise ToolProviderCodecError("payload must be a TypedPayload")
    if payload.type_url != type_url:
        raise ToolProviderCodecError(f"{role} type URL does not match method: {method}")
    if not isinstance(payload.value, bytes):
        raise ToolProviderCodecError("payload value must be bytes")
    message = message_type()
    try:
        message.ParseFromString(payload.value)
    except DecodeError as error:
        raise ToolProviderCodecError(
            f"invalid tool provider {role}: {error}"
        ) from error
    if role == "response" and not message.WhichOneof("result"):
        raise ToolProviderCodecError("tool provider response must select a result")
    return message


def encode_request(method: str, request: Message) -> TypedPayload:
    """Encode a method-specific request and attach its canonical type URL."""

    return _encode(method, request, _REQUEST_TYPES, "request")


def decode_request(method: str, payload: TypedPayload) -> Message:
    """Decode a method-specific request after validating its type URL."""

    return _decode(method, payload, _REQUEST_TYPES, "request")


def encode_response(method: str, response: Message) -> TypedPayload:
    """Encode a method-specific response and attach its canonical type URL."""

    return _encode(method, response, _RESPONSE_TYPES, "response")


def decode_response(method: str, payload: TypedPayload) -> Message:
    """Decode a method-specific response after validating its type URL."""

    return _decode(method, payload, _RESPONSE_TYPES, "response")


__all__ = [
    "ToolProviderCodecError",
    "TypedPayload",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
]
