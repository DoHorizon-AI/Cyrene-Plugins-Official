#!/usr/bin/env python3
"""Compare the shared speech SDK with a separately generated protobuf view."""

from __future__ import annotations

import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("usage: run_tck.py <generated-python-root>")

repo = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
sys.path.insert(0, str(repo / "sdk/python/cyrene_speech_provider_contracts/src"))

from cyrene.speech.provider.v1 import speech_provider_pb2 as generated_pb2
from cyrene_speech_provider_contracts import (
    METHOD_TRANSCRIBE,
    decode_request,
    encode_request,
    speech_provider_pb2,
)

request = speech_provider_pb2.TranscribeRequest(
    input=speech_provider_pb2.SpeechInput(
        mime_type="audio/wav",
        data=b"wave-bytes",
    ),
    language_hint="en",
    request_id="request-7",
)
payload = encode_request(METHOD_TRANSCRIBE, request)
independent = generated_pb2.TranscribeRequest.FromString(payload)
assert independent == request
assert decode_request(METHOD_TRANSCRIBE, independent.SerializeToString()) == request
assert generated_pb2.DESCRIPTOR.name == speech_provider_pb2.DESCRIPTOR.name

print("speech.provider.v1 Python SDK/generated binding TCK: PASS")
