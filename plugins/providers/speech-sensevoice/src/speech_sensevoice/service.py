"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 service.py                                                       │
│  Module: speech_sensevoice.service                                  │
│  Role: Bound audio decoding and local SenseVoice inference.          │
│                                                                      │
│  模块职责：执行受限音频解码和本地 SenseVoice 推理。                   │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cyrene_plugin_runtime.configuration import read_environment_settings

SUPPORTED_MIME_TYPES = frozenset(
    {
        "audio/aac",
        "audio/flac",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
        "audio/x-m4a",
        "audio/x-wav",
    }
)
MAX_REQUEST_BYTES = 25 * 1024 * 1024
MAX_DURATION_SECONDS = 300
SAMPLE_RATE = 16_000
_PCM_BYTES_PER_SECOND = SAMPLE_RATE * 2


class SpeechFailure(RuntimeError):
    """Sanitized speech failure with a stable public error code."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SpeechSettings:
    """Operator-owned model and resource limits for one provider process."""

    model_path: Path | None
    tokens_path: Path | None
    model_id: str
    num_threads: int
    max_input_bytes: int
    max_duration_seconds: int
    ffmpeg_path: str

    @classmethod
    def from_environment(cls) -> SpeechSettings:
        """Read fixed model bindings and bounded limits from host configuration."""

        generic = read_environment_settings()
        config: dict[str, Any] = {}
        if generic is not None:
            raw = generic["config"]
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as error:
                raise ValueError(
                    "speech activation configuration is malformed"
                ) from error
            if not isinstance(decoded, dict):
                raise ValueError("speech activation configuration must be an object")
            allowed = {
                "model_path",
                "tokens_path",
                "model_id",
                "num_threads",
                "max_input_bytes",
                "max_duration_seconds",
                "ffmpeg_path",
            }
            if set(decoded) - allowed:
                raise ValueError(
                    "speech activation configuration has unsupported fields"
                )
            config = decoded

        model_path = _configured_path(
            config.get("model_path") or os.getenv("CYRENE_SPEECH_SENSEVOICE_MODEL"),
            "model_path",
        )
        tokens_path = _configured_path(
            config.get("tokens_path") or os.getenv("CYRENE_SPEECH_SENSEVOICE_TOKENS"),
            "tokens_path",
        )
        return cls(
            model_path=model_path,
            tokens_path=tokens_path,
            model_id=_bounded_text(
                config.get(
                    "model_id",
                    os.getenv("CYRENE_SPEECH_SENSEVOICE_MODEL_ID", "sensevoice-local"),
                ),
                "model_id",
                128,
            ),
            num_threads=_bounded_value(
                config.get(
                    "num_threads",
                    os.getenv("CYRENE_SPEECH_SENSEVOICE_THREADS", "2"),
                ),
                "num_threads",
                1,
                8,
            ),
            max_input_bytes=_bounded_value(
                config.get(
                    "max_input_bytes",
                    os.getenv("CYRENE_SPEECH_MAX_INPUT_BYTES", str(MAX_REQUEST_BYTES)),
                ),
                "max_input_bytes",
                1,
                MAX_REQUEST_BYTES,
            ),
            max_duration_seconds=_bounded_value(
                config.get(
                    "max_duration_seconds",
                    os.getenv(
                        "CYRENE_SPEECH_MAX_DURATION_SECONDS",
                        str(MAX_DURATION_SECONDS),
                    ),
                ),
                "max_duration_seconds",
                1,
                MAX_DURATION_SECONDS,
            ),
            ffmpeg_path=_bounded_text(
                config.get("ffmpeg_path", os.getenv("CYRENE_SPEECH_FFMPEG", "ffmpeg")),
                "ffmpeg_path",
                512,
            ),
        )


class SenseVoiceService:
    """Decode bounded audio bytes and run a cached CPU SenseVoice recognizer.

    Model files are operator-provisioned. Every request is decoded by a fixed
    FFmpeg argv and bounded by input size, output duration, and one process-local
    inference lock. No caller value is interpreted as a command or path.
    中文：模型由运维提供；请求仅作为有界字节传入固定 FFmpeg 参数。
    """

    def __init__(self, settings: SpeechSettings | None = None) -> None:
        self.settings = settings or SpeechSettings.from_environment()
        self._recognizers: dict[str, Any] = {}
        self._recognizer_lock = threading.Lock()

    def transcribe(
        self,
        data: bytes,
        mime_type: str,
        *,
        language_hint: str | None,
        cancellation_event: Any = None,
    ) -> dict[str, Any]:
        """Return transcript text, language, duration, and bounded time segments."""

        _check_cancelled(cancellation_event)
        if not isinstance(data, bytes) or not data:
            raise SpeechFailure("INVALID_INPUT")
        if not isinstance(mime_type, str):
            raise SpeechFailure("INVALID_INPUT")
        if len(data) > self.settings.max_input_bytes:
            raise SpeechFailure("QUOTA_EXCEEDED")
        normalized_mime = mime_type.strip().lower().split(";", maxsplit=1)[0]
        if normalized_mime not in SUPPORTED_MIME_TYPES:
            raise SpeechFailure("UNSUPPORTED_MEDIA")
        language = _normalize_language(language_hint)
        recognizer = self._get_recognizer(
            language,
            cancellation_event=cancellation_event,
        )

        pcm = _decode_audio(
            data,
            ffmpeg_path=self.settings.ffmpeg_path,
            max_duration_seconds=self.settings.max_duration_seconds,
            cancellation_event=cancellation_event,
        )
        if not pcm:
            raise SpeechFailure("INVALID_INPUT")
        duration_ms = round(len(pcm) / _PCM_BYTES_PER_SECOND * 1000)
        if duration_ms <= 0:
            raise SpeechFailure("INVALID_INPUT")

        try:
            import numpy as np
        except ImportError as error:
            raise SpeechFailure("MODEL_UNAVAILABLE") from error
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        chunk_samples = SAMPLE_RATE * 30
        texts: list[str] = []
        detected_languages: list[str] = []
        segments: list[dict[str, Any]] = []
        try:
            for offset in range(0, len(samples), chunk_samples):
                _check_cancelled(cancellation_event)
                _acquire_cancellable(self._recognizer_lock, cancellation_event)
                try:
                    stream = recognizer.create_stream()
                    chunk = samples[offset : offset + chunk_samples]
                    stream.accept_waveform(SAMPLE_RATE, chunk)
                    recognizer.decode_stream(stream)
                    _check_cancelled(cancellation_event)
                    raw_result = stream.result
                finally:
                    self._recognizer_lock.release()
                chunk_text = str(getattr(raw_result, "text", "")).strip()
                if chunk_text:
                    texts.append(chunk_text)
                detected = str(getattr(raw_result, "lang", "")).strip()
                if detected:
                    detected_languages.append(detected)
                tokens = _string_sequence(getattr(raw_result, "tokens", ()))
                timestamps = _number_sequence(getattr(raw_result, "timestamps", ()))
                chunk_segments = _make_segments(
                    tokens,
                    timestamps,
                    min(30_000, duration_ms - offset // 16),
                    offset_ms=offset // 16,
                )
                segments.extend(chunk_segments)
        except SpeechFailure:
            raise
        except Exception as error:
            raise SpeechFailure("PROVIDER_FAILED", retryable=True) from error

        return {
            "text": " ".join(texts),
            "detected_language": (
                _normalize_detected_language(detected_languages[0])
                if detected_languages
                else None
            ),
            "duration_ms": duration_ms,
            "segments": segments,
        }

    def _get_recognizer(
        self,
        language: str,
        *,
        cancellation_event: Any = None,
    ) -> Any:
        """Construct a CPU recognizer only after validating provisioned assets."""

        _acquire_cancellable(self._recognizer_lock, cancellation_event)
        try:
            recognizer = self._recognizers.get(language)
            if recognizer is not None:
                return recognizer
            model = self.settings.model_path
            tokens = self.settings.tokens_path
            if (
                model is None
                or tokens is None
                or not model.is_file()
                or not tokens.is_file()
            ):
                raise SpeechFailure("MODEL_UNAVAILABLE")
            if shutil.which(self.settings.ffmpeg_path) is None:
                raise SpeechFailure("MODEL_UNAVAILABLE")
            try:
                import sherpa_onnx
            except ImportError as error:
                raise SpeechFailure("MODEL_UNAVAILABLE") from error
            try:
                recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(model),
                    tokens=str(tokens),
                    num_threads=self.settings.num_threads,
                    sample_rate=SAMPLE_RATE,
                    feature_dim=80,
                    provider="cpu",
                    language=language,
                    use_itn=True,
                )
            except Exception as error:
                raise SpeechFailure("MODEL_UNAVAILABLE") from error
            self._recognizers[language] = recognizer
            return recognizer
        finally:
            self._recognizer_lock.release()


def _decode_audio(
    data: bytes,
    *,
    ffmpeg_path: str,
    max_duration_seconds: int,
    cancellation_event: Any,
) -> bytes:
    """Decode from stdin with no temporary paths and a strict PCM output cap."""

    if shutil.which(ffmpeg_path) is None:
        raise SpeechFailure("MODEL_UNAVAILABLE")
    argv = [
        ffmpeg_path,
        "-v",
        "error",
        "-nostdin",
        "-threads",
        "1",
        "-i",
        "pipe:0",
        "-t",
        str(max_duration_seconds + 1),
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "s16le",
        "pipe:1",
    ]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError as error:
        raise SpeechFailure("MODEL_UNAVAILABLE") from error

    completed: queue.Queue[tuple[bytes, bytes] | BaseException] = queue.Queue(maxsize=1)

    def communicate() -> None:
        try:
            completed.put(process.communicate(input=data))
        except BaseException as error:
            completed.put(error)

    worker = threading.Thread(target=communicate, name="speech-ffmpeg", daemon=True)
    worker.start()
    deadline = time.monotonic() + max_duration_seconds + 20
    try:
        while True:
            _check_cancelled(cancellation_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SpeechFailure("PROVIDER_FAILED", retryable=True)
            try:
                result = completed.get(timeout=min(0.1, remaining))
                break
            except queue.Empty:
                continue
    except SpeechFailure:
        process.kill()
        worker.join(timeout=1)
        raise
    except BaseException:
        process.kill()
        worker.join(timeout=1)
        raise

    if isinstance(result, BaseException):
        raise SpeechFailure("PROVIDER_FAILED", retryable=True) from result
    _check_cancelled(cancellation_event)
    stdout, _stderr = result
    if process.returncode != 0:
        raise SpeechFailure("UNSUPPORTED_MEDIA")
    if len(stdout) % 2:
        raise SpeechFailure("PROVIDER_FAILED")
    duration_limit = max_duration_seconds * _PCM_BYTES_PER_SECOND
    output_limit = (max_duration_seconds + 1) * _PCM_BYTES_PER_SECOND
    if len(stdout) > output_limit:
        raise SpeechFailure("QUOTA_EXCEEDED")
    if len(stdout) > duration_limit:
        raise SpeechFailure("QUOTA_EXCEEDED")
    return stdout


def _check_cancelled(cancellation_event: Any) -> None:
    """Raise the internal cancellation signal without manufacturing success."""

    if cancellation_event is None:
        return
    is_set = getattr(cancellation_event, "is_set", None)
    cancelled = getattr(cancellation_event, "cancelled", False)
    if (callable(is_set) and is_set()) or cancelled is True:
        raise SpeechFailure("CANCELLED")


def _acquire_cancellable(
    lock: Any,
    cancellation_event: Any,
    *,
    poll_seconds: float = 0.1,
) -> None:
    """Wait for one inference slot while honoring cancellation during contention."""

    while True:
        _check_cancelled(cancellation_event)
        if lock.acquire(timeout=poll_seconds):
            try:
                _check_cancelled(cancellation_event)
            except SpeechFailure:
                lock.release()
                raise
            return


def _normalize_language(language_hint: str | None) -> str:
    """Validate known SenseVoice language labels without passing raw values on."""

    if language_hint is None or not language_hint:
        return "auto"
    language = language_hint.strip().lower()
    if language not in {"auto", "zh", "en", "ja", "ko", "yue"}:
        raise SpeechFailure("INVALID_INPUT")
    return language


def _normalize_detected_language(value: str) -> str | None:
    """Convert SenseVoice's tokenized language marker to a stable short code."""

    normalized = value.strip().lower()
    if normalized.startswith("<|") and normalized.endswith("|>"):
        normalized = normalized[2:-2]
    return normalized if normalized in {"zh", "en", "ja", "ko", "yue"} else None


def _make_segments(
    tokens: list[str],
    timestamps: list[float],
    duration_ms: int,
    *,
    offset_ms: int = 0,
) -> list[dict[str, Any]]:
    """Convert SenseVoice token end-times into conservative ordered segments."""

    if not tokens or len(tokens) != len(timestamps):
        return []
    segments: list[dict[str, Any]] = []
    previous_ms = 0
    for token, timestamp in zip(tokens, timestamps, strict=True):
        relative_end_ms = min(duration_ms, max(previous_ms, round(timestamp * 1000)))
        end_ms = offset_ms + relative_end_ms
        start_ms = offset_ms + previous_ms
        if token and end_ms >= start_ms:
            segments.append({"start_ms": start_ms, "end_ms": end_ms, "text": token})
        previous_ms = relative_end_ms
    return segments


def _number_sequence(value: Any) -> list[float]:
    """Keep only finite nonnegative numeric timestamp items."""

    if not isinstance(value, (tuple, list)):
        return []
    result: list[float] = []
    for item in value:
        try:
            number = float(item)
        except (TypeError, ValueError):
            return []
        if number < 0 or number != number or number == float("inf"):
            return []
        result.append(number)
    return result


def _string_sequence(value: Any) -> list[str]:
    """Return a bounded sequence of string tokens from model output."""

    if not isinstance(value, (tuple, list)) or len(value) > 100_000:
        return []
    return [str(item) for item in value]


def _configured_path(value: Any, name: str) -> Path | None:
    """Accept an absolute operator model path, never a request-provided path."""

    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute operator path")
    return path


def _bounded_value(
    raw: Any,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    """Parse and validate one integer resource limit."""

    if isinstance(raw, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(raw, str):
        if not raw.isdecimal():
            raise ValueError(f"{name} must be an integer")
        value = int(raw)
    elif isinstance(raw, int):
        value = raw
    else:
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside its supported range")
    return value


def _bounded_text(value: Any, name: str, maximum: int) -> str:
    """Validate one non-secret configuration label or fixed executable."""

    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} is invalid")
    return value.strip()
