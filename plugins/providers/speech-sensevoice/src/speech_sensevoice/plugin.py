"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 plugin.py                                                        │
│  Module: speech_sensevoice.plugin                                   │
│  Role: Implement the typed speech.provider.v1 runtime boundary.     │
│                                                                      │
│  模块职责：实现 speech.provider.v1 的类型化运行时边界。              │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cyrene_speech_provider_contracts import (
    CAPABILITY_ID,
    METHOD_TRANSCRIBE,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    SpeechCodecError,
    decode_request,
    encode_response,
)
from cyrene_speech_provider_contracts import (
    speech_provider_pb2 as wire,
)
from google.protobuf.message import DecodeError

from .service import (
    MAX_REQUEST_BYTES,
    SenseVoiceService,
    SpeechFailure,
    SpeechSettings,
)

PLUGIN_ID = "cyrene.providers.speech-sensevoice"
PLUGIN_VERSION = "0.1.0"
METHOD = METHOD_TRANSCRIBE
REQUEST_TYPE_URL = REQUEST_TYPE_URLS[METHOD]
RESPONSE_TYPE_URL = RESPONSE_TYPE_URLS[METHOD]


class SenseVoiceSpeechProvider:
    """Expose bounded local SenseVoice transcription through DirectPluginRuntime."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: SpeechSettings | None = None,
        service: SenseVoiceService | None = None,
        artifact_resolver: Callable[[str], tuple[bytes, str]] | None = None,
    ) -> None:
        self._service = service or SenseVoiceService(settings)
        self._artifact_resolver = artifact_resolver

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        cancellation: Any = None,
        cancellation_event: Any = None,
        **_: Any,
    ) -> tuple[bool, Any]:
        """Parse one typed request and return one typed response without raw errors."""

        if cancellation_event is None:
            cancellation_event = cancellation
        if capability != CAPABILITY_ID or action != METHOD:
            return False, "METHOD_NOT_FOUND: unsupported speech method"
        if len(payload) > MAX_REQUEST_BYTES:
            return False, "INVALID_REQUEST: speech request exceeds its byte limit"
        if request_type_url not in (None, REQUEST_TYPE_URL):
            return False, "INVALID_REQUEST: speech payload type mismatch"
        _check_cancelled(cancellation_event)
        try:
            request = decode_request(
                METHOD,
                payload,
            )
        except SpeechCodecError:
            request = wire.TranscribeRequest()
            try:
                request.ParseFromString(payload)
            except DecodeError:
                return False, "INVALID_REQUEST: speech payload is malformed"
            response = wire.TranscribeResponse()
            _set_error(
                response,
                wire.SPEECH_ERROR_CODE_INVALID_INPUT,
                "INVALID_INPUT",
            )
            return True, _typed(response)
        except (ValueError, TypeError):
            return False, "INVALID_REQUEST: speech payload is malformed"

        response = wire.TranscribeResponse()
        if not request.HasField("input"):
            _set_error(response, wire.SPEECH_ERROR_CODE_INVALID_INPUT, "INVALID_INPUT")
            return True, _typed(response)
        input_source = request.input.WhichOneof("source")
        if input_source == "data":
            data = bytes(request.input.data)
            mime_type = request.input.mime_type
        elif input_source == "artifact_id":
            if self._artifact_resolver is None:
                _set_error(
                    response,
                    wire.SPEECH_ERROR_CODE_INVALID_INPUT,
                    "INVALID_INPUT",
                )
                return True, _typed(response)
            try:
                data, mime_type = self._artifact_resolver(request.input.artifact_id)
            except Exception:
                _set_error(
                    response,
                    wire.SPEECH_ERROR_CODE_INVALID_INPUT,
                    "INVALID_INPUT",
                )
                return True, _typed(response)
        else:
            _set_error(response, wire.SPEECH_ERROR_CODE_INVALID_INPUT, "INVALID_INPUT")
            return True, _typed(response)

        language = request.language_hint if request.HasField("language_hint") else None
        try:
            result = self._service.transcribe(
                data,
                mime_type,
                language_hint=language,
                cancellation_event=cancellation_event,
            )
            _check_cancelled(cancellation_event)
        except SpeechFailure as error:
            if error.code == "CANCELLED":
                raise InterruptedError("speech transcription cancelled") from error
            _set_error(
                response,
                _error_code(error.code),
                error.code,
                retryable=error.retryable,
            )
            return True, _typed(response)
        except Exception:
            _set_error(
                response,
                wire.SPEECH_ERROR_CODE_PROVIDER_FAILED,
                "PROVIDER_FAILED",
                retryable=True,
            )
            return True, _typed(response)

        transcription = response.transcription
        transcription.status = (
            wire.TRANSCRIPTION_STATUS_SUCCEEDED
            if result["text"]
            else wire.TRANSCRIPTION_STATUS_NO_SPEECH
        )
        transcription.text = result["text"]
        if result["detected_language"]:
            transcription.detected_language = result["detected_language"]
        transcription.duration_ms = result["duration_ms"]
        transcription.model = self._service.settings.model_id
        for segment in result["segments"]:
            item = transcription.segments.add()
            item.start_ms = segment["start_ms"]
            item.end_ms = segment["end_ms"]
            item.text = segment["text"]
        return True, _typed(response)


class _TypedPayload:
    """DirectPluginRuntime payload bytes paired with the canonical type URL."""

    def __init__(self, value: bytes, type_url: str) -> None:
        self.value = value
        self.type_url = type_url


def _typed(response: wire.TranscribeResponse) -> _TypedPayload:
    """Serialize the public response and attach its canonical descriptor URL."""

    return _TypedPayload(encode_response(METHOD, response), RESPONSE_TYPE_URL)


def _set_error(
    response: wire.TranscribeResponse,
    code: int,
    message: str,
    *,
    retryable: bool = False,
) -> None:
    """Set a sanitized protocol error without process paths or model output."""

    response.error.code = code
    response.error.message = message
    response.error.retryable = retryable


def _error_code(name: str) -> int:
    """Map internal stable failures to the versioned protobuf enum."""

    return {
        "INVALID_INPUT": wire.SPEECH_ERROR_CODE_INVALID_INPUT,
        "UNSUPPORTED_MEDIA": wire.SPEECH_ERROR_CODE_UNSUPPORTED_MEDIA,
        "MODEL_UNAVAILABLE": wire.SPEECH_ERROR_CODE_MODEL_UNAVAILABLE,
        "PROVIDER_FAILED": wire.SPEECH_ERROR_CODE_PROVIDER_FAILED,
        "QUOTA_EXCEEDED": wire.SPEECH_ERROR_CODE_QUOTA_EXCEEDED,
    }.get(name, wire.SPEECH_ERROR_CODE_PROVIDER_FAILED)


def _check_cancelled(cancellation_event: Any) -> None:
    """Propagate host cancellation before returning any success response."""

    if cancellation_event is None:
        return
    is_set = getattr(cancellation_event, "is_set", None)
    cancelled = getattr(cancellation_event, "cancelled", False)
    if (callable(is_set) and is_set()) or cancelled:
        raise InterruptedError("speech transcription cancelled")
