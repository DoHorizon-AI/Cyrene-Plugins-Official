"""Bounded local SenseVoice service tests.

These tests validate real decoding process behavior with tiny local peers, not
model accuracy. 中文：本测试验证本地解码边界，不代表模型识别精度验收。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from speech_sensevoice.service import (
    MAX_REQUEST_BYTES,
    SenseVoiceService,
    SpeechFailure,
    SpeechSettings,
    _decode_audio,
    _make_segments,
    _normalize_detected_language,
    _normalize_language,
)


def _settings(tmp_path: Path, **overrides: object) -> SpeechSettings:
    """Build a test settings record without changing process environment."""

    values: dict[str, object] = {
        "model_path": None,
        "tokens_path": None,
        "model_id": "test-model",
        "num_threads": 1,
        "max_input_bytes": MAX_REQUEST_BYTES,
        "max_duration_seconds": 5,
        "ffmpeg_path": "ffmpeg",
    }
    values.update(overrides)
    return SpeechSettings(**values)  # type: ignore[arg-type]


def _fake_ffmpeg(tmp_path: Path, body: str) -> str:
    """Create one executable test peer that writes only its declared bytes."""

    path = tmp_path / "ffmpeg-peer"
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return str(path)


def test_rejects_oversize_input_before_inference(tmp_path: Path) -> None:
    service = SenseVoiceService(_settings(tmp_path, max_input_bytes=2))

    with pytest.raises(SpeechFailure, match="QUOTA_EXCEEDED"):
        service.transcribe(b"abc", "audio/wav", language_hint=None)


def test_rejects_unsupported_mime_before_inference(tmp_path: Path) -> None:
    service = SenseVoiceService(_settings(tmp_path))

    with pytest.raises(SpeechFailure, match="UNSUPPORTED_MEDIA"):
        service.transcribe(b"audio", "application/octet-stream", language_hint=None)


def test_requires_operator_provisioned_model(tmp_path: Path) -> None:
    service = SenseVoiceService(_settings(tmp_path))

    with pytest.raises(SpeechFailure, match="MODEL_UNAVAILABLE"):
        service._get_recognizer("auto")


def test_cancelled_request_never_returns_transcript(tmp_path: Path) -> None:
    service = SenseVoiceService(_settings(tmp_path))
    cancellation = threading.Event()
    cancellation.set()

    with pytest.raises(SpeechFailure, match="CANCELLED"):
        service.transcribe(
            b"audio",
            "audio/wav",
            language_hint=None,
            cancellation_event=cancellation,
        )


def test_cancelled_request_does_not_wait_for_model_lock(
    tmp_path: Path,
) -> None:
    service = SenseVoiceService(_settings(tmp_path))
    service._recognizer_lock.acquire()
    cancellation = threading.Event()
    result: list[str] = []

    def wait_for_model_slot() -> None:
        try:
            service._get_recognizer("auto", cancellation_event=cancellation)
        except SpeechFailure as error:
            result.append(error.code)

    worker = threading.Thread(target=wait_for_model_slot, daemon=True)
    started = time.monotonic()
    worker.start()
    time.sleep(0.05)
    cancellation.set()
    worker.join(timeout=1)
    service._recognizer_lock.release()

    assert not worker.is_alive()
    assert result == ["CANCELLED"]
    assert time.monotonic() - started < 1


def test_language_hint_is_allowlisted() -> None:
    assert _normalize_language(None) == "auto"
    assert _normalize_language("zh") == "zh"
    with pytest.raises(SpeechFailure, match="INVALID_INPUT"):
        _normalize_language("https://attacker.invalid")


def test_detected_language_normalizes_sensevoice_tags() -> None:
    assert _normalize_detected_language("<|zh|>") == "zh"
    assert _normalize_detected_language("en") == "en"
    assert _normalize_detected_language("<|ar|>") is None


def test_transcript_segments_are_bounded_and_ordered() -> None:
    segments = _make_segments(
        ["hi", "there"],
        [0.4, 1.1],
        duration_ms=1_000,
        offset_ms=200,
    )

    assert segments == [
        {"start_ms": 200, "end_ms": 600, "text": "hi"},
        {"start_ms": 600, "end_ms": 1_200, "text": "there"},
    ]


def test_ffmpeg_peer_receives_audio_over_stdin_and_returns_pcm(tmp_path: Path) -> None:
    peer = _fake_ffmpeg(
        tmp_path,
        "import sys\nsys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(b'\\x00\\x00' * 32)\n",
    )

    output = _decode_audio(
        b"bounded audio input",
        ffmpeg_path=peer,
        max_duration_seconds=1,
        cancellation_event=None,
    )

    assert output == b"\x00\x00" * 32


def test_ffmpeg_output_over_duration_limit_is_rejected(tmp_path: Path) -> None:
    peer = _fake_ffmpeg(
        tmp_path,
        "import sys\nsys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(b'\\x00\\x00' * 32001)\n",
    )

    with pytest.raises(SpeechFailure, match="QUOTA_EXCEEDED"):
        _decode_audio(
            b"bounded audio input",
            ffmpeg_path=peer,
            max_duration_seconds=1,
            cancellation_event=None,
        )


def test_cancellation_kills_blocked_ffmpeg_peer(tmp_path: Path) -> None:
    peer = _fake_ffmpeg(
        tmp_path,
        "import sys, time\nsys.stdin.buffer.read()\ntime.sleep(10)\n",
    )
    cancellation = threading.Event()
    timer = threading.Timer(0.1, cancellation.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(SpeechFailure, match="CANCELLED"):
            _decode_audio(
                b"bounded audio input",
                ffmpeg_path=peer,
                max_duration_seconds=1,
                cancellation_event=cancellation,
            )
    finally:
        timer.cancel()

    assert time.monotonic() - started < 2


def test_environment_integer_bounds_are_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CYRENE_SPEECH_SENSEVOICE_THREADS", "9")

    with pytest.raises(ValueError, match="outside"):
        SpeechSettings.from_environment()


def test_standard_plugin_config_binds_absolute_model_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.int8.onnx"
    tokens_path = tmp_path / "tokens.txt"
    monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", "speech-binding")
    monkeypatch.setenv(
        "CYRENE_CAPABILITY_CONFIGURATION_JSON",
        json.dumps(
            {
                "model_path": str(model_path),
                "tokens_path": str(tokens_path),
                "num_threads": 3,
            }
        ),
    )

    settings = SpeechSettings.from_environment()

    assert settings.model_path == model_path
    assert settings.tokens_path == tokens_path
    assert settings.num_threads == 3
