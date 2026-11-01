"""
┌─────────────────────────────────────────────────────────────────────────┐
│ 📄 codec.py                                                             │
│ Module: cyrene_speech_provider_contracts.codec                           │
│ Role: Typed method map and protobuf request/response codec.              │
│                                                                         │
│ 模块职责：提供 speech.provider.v1 方法标识与 protobuf 编解码。          │
└─────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from google.protobuf.message import DecodeError, Message

from ._generated import speech_provider_pb2

CAPABILITY_ID = "speech.provider.v1"
INTERFACE_VERSION = "1"
METHOD_TRANSCRIBE = "transcribe"
METHODS = (METHOD_TRANSCRIBE,)
REQUEST_TYPE_URLS = {
    METHOD_TRANSCRIBE: "type.cyrene.io/cyrene.speech.provider.v1.TranscribeRequest"
}
RESPONSE_TYPE_URLS = {
    METHOD_TRANSCRIBE: "type.cyrene.io/cyrene.speech.provider.v1.TranscribeResponse"
}
_REQUEST_TYPES = {METHOD_TRANSCRIBE: speech_provider_pb2.TranscribeRequest}
_RESPONSE_TYPES = {METHOD_TRANSCRIBE: speech_provider_pb2.TranscribeResponse}


class SpeechCodecError(ValueError):
    """A speech method or payload does not match the canonical contract."""


def _validate_request(message: Message) -> None:
    if not message.HasField("input"):
        raise SpeechCodecError("TranscribeRequest.input is required")
    source = message.input.WhichOneof("source")
    if source not in ("data", "artifact_id"):
        raise SpeechCodecError("SpeechInput must select bytes or artifact_id")
    if not message.input.mime_type:
        raise SpeechCodecError("SpeechInput.mime_type is required")
    if source == "data" and not message.input.data:
        raise SpeechCodecError("SpeechInput.data must contain non-empty bytes")
    if source == "artifact_id":
        artifact_id = message.input.artifact_id
        if (
            not artifact_id
            or "://" in artifact_id
            or "/" in artifact_id
            or "\\" in artifact_id
            or artifact_id.startswith("~")
        ):
            raise SpeechCodecError("artifact_id must be an opaque artifact reference")


def _decode(
    method: str, payload: bytes, messages: dict[str, type[Message]], role: str
) -> Message:
    message_type = messages.get(method)
    if message_type is None:
        raise SpeechCodecError(f"unsupported speech method: {method}")
    if not isinstance(payload, bytes):
        raise SpeechCodecError("protobuf payload must be bytes")
    message = message_type()
    try:
        message.ParseFromString(payload)
    except DecodeError as error:
        raise SpeechCodecError(f"invalid speech {role}: {error}") from error
    if role == "request":
        _validate_request(message)
    elif not message.WhichOneof("result"):
        raise SpeechCodecError("TranscribeResponse must select a result")
    return message


def encode_request(method: str, request: Message) -> bytes:
    """Validate and encode a speech method request."""

    expected = _REQUEST_TYPES.get(method)
    if expected is None:
        raise SpeechCodecError(f"unsupported speech method: {method}")
    if not isinstance(request, expected):
        raise SpeechCodecError(f"request does not match method: {method}")
    _validate_request(request)
    return request.SerializeToString(deterministic=True)


def decode_request(method: str, payload: bytes) -> Message:
    """Decode and validate a speech method request."""

    return _decode(method, payload, _REQUEST_TYPES, "request")


def encode_response(method: str, response: Message) -> bytes:
    """Validate and encode a speech method response."""

    expected = _RESPONSE_TYPES.get(method)
    if expected is None:
        raise SpeechCodecError(f"unsupported speech method: {method}")
    if not isinstance(response, expected):
        raise SpeechCodecError(f"response does not match method: {method}")
    if not response.WhichOneof("result"):
        raise SpeechCodecError("TranscribeResponse must select a result")
    return response.SerializeToString(deterministic=True)


def decode_response(method: str, payload: bytes) -> Message:
    """Decode and validate a speech method response."""

    return _decode(method, payload, _RESPONSE_TYPES, "response")


__all__ = [
    "CAPABILITY_ID",
    "INTERFACE_VERSION",
    "METHODS",
    "METHOD_TRANSCRIBE",
    "REQUEST_TYPE_URLS",
    "RESPONSE_TYPE_URLS",
    "SpeechCodecError",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
]
