#!/usr/bin/env python3
"""Validate generated Python bindings for the direct model-provider payload."""

from __future__ import annotations

import sys
from pathlib import Path


if len(sys.argv) != 2:
    raise SystemExit("usage: run_tck.py <generated-python-root>")
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))

from google.protobuf.any_pb2 import Any  # noqa: E402
from cyrene.model.provider.v1 import model_provider_pb2  # noqa: E402


embedding_request = model_provider_pb2.EmbeddingsRequest(
    inputs=["alpha", "beta"], model="deterministic-model"
)
packed_request = Any()
packed_request.Pack(embedding_request)
unpacked_request = model_provider_pb2.EmbeddingsRequest()
assert packed_request.Unpack(unpacked_request)
assert list(unpacked_request.inputs) == ["alpha", "beta"]

response = model_provider_pb2.EmbeddingsResponse(
    embeddings=model_provider_pb2.EmbeddingBatch(
        vectors=[
            model_provider_pb2.EmbeddingVector(values=[1.0, 2.0]),
            model_provider_pb2.EmbeddingVector(values=[3.0, 4.0]),
        ],
        dimensions=2,
        model="deterministic-model",
    )
)
packed_response = Any()
packed_response.Pack(response)
unpacked_response = model_provider_pb2.EmbeddingsResponse()
assert packed_response.Unpack(unpacked_response)
assert len(unpacked_response.embeddings.vectors) == len(embedding_request.inputs)

chat = model_provider_pb2.ChatCompletionRequest(
    messages=[
        model_provider_pb2.ChatMessage(
            role=model_provider_pb2.ChatMessage.ROLE_USER,
            content="hello",
        )
    ],
    model="deterministic-model",
    stream=True,
    include_usage=True,
)
packed_chat = Any()
packed_chat.Pack(chat)
unpacked_chat = model_provider_pb2.ChatCompletionRequest()
assert packed_chat.Unpack(unpacked_chat)
assert unpacked_chat.HasField("include_usage")
assert unpacked_chat.include_usage is True

print("model.provider.v1 generated Python payload TCK: PASS")
