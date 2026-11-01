"""Typed DirectPluginRuntime boundary tests using the shared speech codec.

Fake inference is used only to check adapter mapping, never as speech acceptance.
中文：模拟推理仅测试协议适配器，不作为真实语音验收。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from cyrene_speech_provider_contracts import (
    CAPABILITY_ID,
    METHOD_TRANSCRIBE,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    decode_response,
    encode_request,
)
from cyrene_speech_provider_contracts import (
    speech_provider_pb2 as wire,
)

from speech_sensevoice.plugin import SenseVoiceSpeechProvider
from speech_sensevoice.service import SenseVoiceService, SpeechFailure, SpeechSettings


@dataclass
class _ModelLabel:
    model_id: str = "unit-test-model"


class _FakeService:
    """Deterministic adapter peer; its output is not model evidence."""

    settings = _ModelLabel()

    def transcribe(
        self,
        data: bytes,
        mime_type: str,
        *,
        language_hint: str | None,
        cancellation_event: Any = None,
    ) -> dict[str, Any]:
        if data == b"fail":
            raise SpeechFailure("PROVIDER_FAILED", retryable=True)
        return {
            "text": "unit transcript",
            "detected_language": language_hint or "zh",
            "duration_ms": 900,
            "segments": [{"start_ms": 0, "end_ms": 900, "text": "unit transcript"}],
        }


def _request(*, source: str = "data") -> wire.TranscribeRequest:
    """Build one request with the canonical protocol fields."""

    request = wire.TranscribeRequest()
    request.input.mime_type = "audio/wav"
    if source == "data":
        request.input.data = b"audio"
    else:
        request.input.artifact_id = "a" * 64
    request.language_hint = "zh"
    return request


def test_typed_request_maps_real_adapter_result_and_type_url() -> None:
    provider = SenseVoiceSpeechProvider(service=_FakeService())  # type: ignore[arg-type]
    request = _request()

    accepted, payload = provider.on_invoke(
        CAPABILITY_ID,
        METHOD_TRANSCRIBE,
        encode_request(METHOD_TRANSCRIBE, request),
        request_type_url=REQUEST_TYPE_URLS[METHOD_TRANSCRIBE],
    )
    response = decode_response(METHOD_TRANSCRIBE, payload.value)

    assert accepted is True
    assert payload.type_url == RESPONSE_TYPE_URLS[METHOD_TRANSCRIBE]
    assert response.WhichOneof("result") == "transcription"
    assert response.transcription.status == wire.TRANSCRIPTION_STATUS_SUCCEEDED
    assert response.transcription.text == "unit transcript"
    assert response.transcription.model == "unit-test-model"
    assert response.transcription.segments[0].end_ms == 900


def test_artifact_id_fails_closed_without_bound_resolver() -> None:
    provider = SenseVoiceSpeechProvider(service=_FakeService())  # type: ignore[arg-type]
    request = _request(source="artifact")

    accepted, payload = provider.on_invoke(
        CAPABILITY_ID,
        METHOD_TRANSCRIBE,
        encode_request(METHOD_TRANSCRIBE, request),
        request_type_url=REQUEST_TYPE_URLS[METHOD_TRANSCRIBE],
    )
    response = decode_response(METHOD_TRANSCRIBE, payload.value)

    assert accepted is True
    assert response.WhichOneof("result") == "error"
    assert response.error.code == wire.SPEECH_ERROR_CODE_INVALID_INPUT


def test_invalid_contract_input_returns_typed_invalid_input() -> None:
    provider = SenseVoiceSpeechProvider(service=_FakeService())  # type: ignore[arg-type]

    accepted, payload = provider.on_invoke(
        CAPABILITY_ID,
        METHOD_TRANSCRIBE,
        wire.TranscribeRequest().SerializeToString(),
        request_type_url=REQUEST_TYPE_URLS[METHOD_TRANSCRIBE],
    )
    response = decode_response(METHOD_TRANSCRIBE, payload.value)

    assert accepted is True
    assert response.WhichOneof("result") == "error"
    assert response.error.code == wire.SPEECH_ERROR_CODE_INVALID_INPUT


def test_missing_model_returns_typed_unavailable_error() -> None:
    service = SenseVoiceService(
        SpeechSettings(
            model_path=None,
            tokens_path=None,
            model_id="not-installed",
            num_threads=1,
            max_input_bytes=1024,
            max_duration_seconds=2,
            ffmpeg_path="missing-ffmpeg",
        )
    )
    provider = SenseVoiceSpeechProvider(service=service)
    request = _request()

    accepted, payload = provider.on_invoke(
        CAPABILITY_ID,
        METHOD_TRANSCRIBE,
        encode_request(METHOD_TRANSCRIBE, request),
        request_type_url=REQUEST_TYPE_URLS[METHOD_TRANSCRIBE],
    )
    response = decode_response(METHOD_TRANSCRIBE, payload.value)

    assert accepted is True
    assert response.WhichOneof("result") == "error"
    assert response.error.code == wire.SPEECH_ERROR_CODE_MODEL_UNAVAILABLE
    assert response.error.message == "MODEL_UNAVAILABLE"


def test_cancelled_inference_propagates_runtime_cancellation() -> None:
    provider = SenseVoiceSpeechProvider(service=_FakeService())  # type: ignore[arg-type]
    request = _request()

    class Cancelled:
        def is_set(self) -> bool:
            return True

    with pytest.raises(InterruptedError):
        provider.on_invoke(
            CAPABILITY_ID,
            METHOD_TRANSCRIBE,
            encode_request(METHOD_TRANSCRIBE, request),
            request_type_url=REQUEST_TYPE_URLS[METHOD_TRANSCRIBE],
            cancellation=Cancelled(),
        )


def test_wrong_payload_type_url_is_rejected_before_decode() -> None:
    provider = SenseVoiceSpeechProvider(service=_FakeService())  # type: ignore[arg-type]
    request = _request()
    accepted, error = provider.on_invoke(
        CAPABILITY_ID,
        METHOD_TRANSCRIBE,
        encode_request(METHOD_TRANSCRIBE, request),
        request_type_url="type.cyrene.io/other.Request",
    )

    assert accepted is False
    assert "payload type mismatch" in error
