"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_embedding.py                                                │
│  Module: memory_sqlite_provider.tests                                │
│  Role: Check real typed model-provider request and profile validation.│
│                                                                      │
│  模块职责：检查类型化模型调用和向量 profile 校验。                    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from cyrene_model_provider_contracts import (
    CAPABILITY_ID,
    EMBEDDINGS_RESPONSE_TYPE_URL,
    EmbeddingBatch,
    EmbeddingsResponse,
    EmbeddingVector,
    decode_embeddings_request,
    encode_embeddings_response,
)
from cyrene_plugin_runtime.server import serve

from memory_sqlite_provider.embedding import EmbeddingFailure, ModelEmbeddingClient
from memory_sqlite_provider.plugin import TypedPayload


@dataclass(frozen=True, slots=True)
class ModelFixture:
    """One local test endpoint; no external model or account is contacted."""

    model: str
    vector: tuple[float, ...]


class FixtureModelProvider:
    """Serve typed embedding responses over an actual local gRPC runtime."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = "fixture.model-provider"
    version = "0.0.1"

    def __init__(self, fixture: ModelFixture) -> None:
        self.fixture = fixture

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        **_: Any,
    ) -> tuple[bool, TypedPayload | str]:
        request = decode_embeddings_request(payload)
        if capability != CAPABILITY_ID or action != "embeddings":
            return False, "METHOD_NOT_FOUND"
        assert request_type_url == "type.cyrene.io/cyrene.model.provider.v1.EmbeddingsRequest"
        return True, TypedPayload(
            value=encode_embeddings_response(
                EmbeddingsResponse(
                    embeddings=EmbeddingBatch(
                        vectors=tuple(
                            EmbeddingVector(values=self.fixture.vector) for _ in request.inputs
                        ),
                        dimensions=len(self.fixture.vector),
                        model=self.fixture.model,
                    )
                )
            ),
            type_url=EMBEDDINGS_RESPONSE_TYPE_URL,
        )


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        (ModelFixture("embed-v1", (1.0, 0.2, 0.1)), ((1.0, 0.2, 0.1),)),
    ],
)
def test_calls_typed_model_provider_and_returns_verified_vectors(
    fixture: ModelFixture, expected: Sequence[tuple[float, ...]]
) -> None:
    server, endpoint = _serve(fixture)
    client = ModelEmbeddingClient(
        endpoint,
        model_id="embed-v1",
        dimensions=3,
        timeout_seconds=2,
    )
    try:
        vectors = client.embed(("a real query",))
        assert len(vectors) == len(expected)
        for actual_vector, expected_vector in zip(vectors, expected, strict=True):
            assert actual_vector == pytest.approx(expected_vector)
    finally:
        client.close()
        server.stop(0).wait()


@pytest.mark.parametrize(
    "fixture",
    [
        ModelFixture("wrong-model", (1.0, 0.0, 0.0)),
        ModelFixture("embed-v1", (0.0, 0.0, 0.0)),
        ModelFixture("embed-v1", (1.0, 0.0)),
    ],
)
def test_rejects_model_identity_zero_vector_and_dimension_mismatch(
    fixture: ModelFixture,
) -> None:
    server, endpoint = _serve(fixture)
    client = ModelEmbeddingClient(
        endpoint,
        model_id="embed-v1",
        dimensions=3,
        timeout_seconds=2,
    )
    try:
        with pytest.raises(EmbeddingFailure):
            client.embed(("a real query",))
    finally:
        client.close()
        server.stop(0).wait()


def _serve(fixture: ModelFixture) -> tuple[Any, str]:
    return serve(FixtureModelProvider(fixture), CAPABILITY_ID, "1", "127.0.0.1:0")
