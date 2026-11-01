"""Python bindings and codec for the Plugins-owned ``speech.provider.v1``."""

from ._generated import speech_provider_pb2
from .codec import (
    CAPABILITY_ID,
    INTERFACE_VERSION,
    METHOD_TRANSCRIBE,
    METHODS,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    SpeechCodecError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)

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
    "speech_provider_pb2",
]
