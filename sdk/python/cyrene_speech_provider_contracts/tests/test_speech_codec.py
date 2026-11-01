"""Public speech.provider.v1 payload and boundary checks."""

import pytest
from cyrene_speech_provider_contracts import (
    CAPABILITY_ID,
    METHOD_TRANSCRIBE,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    SpeechCodecError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    speech_provider_pb2,
)


def test_transcribe_bytes_request_and_succeeded_response_round_trip() -> None:
    request = speech_provider_pb2.TranscribeRequest(
        input=speech_provider_pb2.SpeechInput(
            mime_type="audio/wav",
            data=b"wave-data",
        ),
        language_hint="zh-CN",
        request_id="request-42",
    )
    decoded = decode_request(
        METHOD_TRANSCRIBE, encode_request(METHOD_TRANSCRIBE, request)
    )
    assert decoded == request

    response = speech_provider_pb2.TranscribeResponse(
        transcription=speech_provider_pb2.TranscriptionResult(
            status=speech_provider_pb2.TRANSCRIPTION_STATUS_SUCCEEDED,
            text="hello",
            detected_language="en",
            duration_ms=1500,
            model="sensevoice-local",
            segments=[
                speech_provider_pb2.TranscriptSegment(
                    start_ms=0,
                    end_ms=1500,
                    text="hello",
                )
            ],
        )
    )
    assert (
        decode_response(
            METHOD_TRANSCRIBE,
            encode_response(METHOD_TRANSCRIBE, response),
        )
        == response
    )
    assert CAPABILITY_ID == "speech.provider.v1"
    assert REQUEST_TYPE_URLS[METHOD_TRANSCRIBE].endswith("TranscribeRequest")
    assert RESPONSE_TYPE_URLS[METHOD_TRANSCRIBE].endswith("TranscribeResponse")


def test_no_speech_is_a_real_status_and_artifact_reference_is_supported() -> None:
    request = speech_provider_pb2.TranscribeRequest(
        input=speech_provider_pb2.SpeechInput(
            mime_type="audio/ogg",
            artifact_id="media-asset-22",
        )
    )
    assert (
        decode_request(METHOD_TRANSCRIBE, encode_request(METHOD_TRANSCRIBE, request))
        == request
    )

    response = speech_provider_pb2.TranscribeResponse(
        transcription=speech_provider_pb2.TranscriptionResult(
            status=speech_provider_pb2.TRANSCRIPTION_STATUS_NO_SPEECH,
            text="",
        )
    )
    decoded = decode_response(
        METHOD_TRANSCRIBE,
        encode_response(METHOD_TRANSCRIBE, response),
    )
    assert (
        decoded.transcription.status
        == speech_provider_pb2.TRANSCRIPTION_STATUS_NO_SPEECH
    )


@pytest.mark.parametrize(
    "artifact_id", ("https://example.invalid/a.wav", "file:///tmp/a.wav", "/tmp/a.wav")
)
def test_transcribe_rejects_url_and_host_path_references(artifact_id: str) -> None:
    request = speech_provider_pb2.TranscribeRequest(
        input=speech_provider_pb2.SpeechInput(
            mime_type="audio/wav",
            artifact_id=artifact_id,
        )
    )
    with pytest.raises(SpeechCodecError, match="opaque artifact reference"):
        encode_request(METHOD_TRANSCRIBE, request)


def test_transcribe_requires_input_source_and_response_result() -> None:
    with pytest.raises(SpeechCodecError, match="input is required"):
        encode_request(METHOD_TRANSCRIBE, speech_provider_pb2.TranscribeRequest())
    with pytest.raises(SpeechCodecError, match="select a result"):
        encode_response(METHOD_TRANSCRIBE, speech_provider_pb2.TranscribeResponse())
