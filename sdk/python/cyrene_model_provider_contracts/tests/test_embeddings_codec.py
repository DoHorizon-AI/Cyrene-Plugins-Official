"""Contract round trips for model.provider.v1 embeddings."""

import pytest
from cyrene_model_provider_contracts import (
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingErrorCode,
    EmbeddingsCodecError,
    EmbeddingsRequest,
    EmbeddingsResponse,
    EmbeddingVector,
    decode_embeddings_request,
    decode_embeddings_response,
    encode_embeddings_request,
    encode_embeddings_response,
)


def test_embeddings_request_preserves_order_empty_strings_and_optional_model() -> None:
    request = EmbeddingsRequest(inputs=("first", "", "third"), model="embed-v3")
    assert decode_embeddings_request(encode_embeddings_request(request)) == request


def test_embeddings_response_round_trips_vectors_dimensions_and_resolved_model() -> (
    None
):
    response = EmbeddingsResponse(
        embeddings=EmbeddingBatch(
            vectors=(EmbeddingVector((0.1, -0.2)), EmbeddingVector((0.3, 0.4))),
            dimensions=2,
            model="resolved-embed-v3",
        )
    )
    decoded = decode_embeddings_response(encode_embeddings_response(response))
    assert decoded.embeddings is not None
    assert decoded.embeddings.dimensions == response.embeddings.dimensions
    assert decoded.embeddings.model == response.embeddings.model
    assert decoded.embeddings.vectors[0].values == pytest.approx((0.1, -0.2))
    assert decoded.embeddings.vectors[1].values == pytest.approx((0.3, 0.4))


def test_embeddings_error_round_trips_code_and_retry_delay() -> None:
    response = EmbeddingsResponse(
        error=EmbeddingError(
            code=EmbeddingErrorCode.RATE_LIMITED,
            message="provider throttled request",
            retry_after_seconds=1.25,
        )
    )
    assert decode_embeddings_response(encode_embeddings_response(response)) == response


def test_embeddings_response_rejects_missing_oneof_result() -> None:
    with pytest.raises(EmbeddingsCodecError, match="select a result"):
        encode_embeddings_response(EmbeddingsResponse())
    with pytest.raises(EmbeddingsCodecError, match="select a result"):
        decode_embeddings_response(b"")
