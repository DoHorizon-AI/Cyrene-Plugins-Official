"""Typed multimodal v3 chat codec and input-boundary checks."""

import pytest
from cyrene_model_provider_contracts import (
    AudioContentPart,
    ChatCodecError,
    ChatCompletionRequestV3,
    ChatMessageV3,
    ChatRole,
    FileContentPart,
    ImageContentPart,
    TextContentPart,
    decode_chat_completion_request_v3,
    encode_chat_completion_request_v3,
)


def test_typed_media_request_round_trips_bytes_and_artifact_ids() -> None:
    request = ChatCompletionRequestV3(
        messages=(
            ChatMessageV3(
                role=ChatRole.USER,
                content=(
                    TextContentPart("describe these inputs"),
                    ImageContentPart("image/png", data=b"png-bytes"),
                    AudioContentPart("audio/wav", artifact_id="artifact-42"),
                    FileContentPart(
                        "application/pdf",
                        data=b"pdf-bytes",
                        filename="report.pdf",
                    ),
                ),
            ),
        ),
        model="vision-model",
        stream=True,
        temperature=0.1,
        include_usage=True,
    )
    decoded = decode_chat_completion_request_v3(
        encode_chat_completion_request_v3(request)
    )
    assert decoded == request


@pytest.mark.parametrize(
    "artifact_id",
    ("https://example.invalid/image.png", "file:///tmp/image.png", "/tmp/image.png"),
)
def test_media_reference_rejects_urls_and_host_paths(artifact_id: str) -> None:
    with pytest.raises(ChatCodecError, match="opaque artifact reference"):
        ImageContentPart("image/png", artifact_id=artifact_id)


def test_media_part_requires_exactly_one_nonempty_source() -> None:
    with pytest.raises(ChatCodecError, match="exactly one source"):
        ImageContentPart("image/png")
    with pytest.raises(ChatCodecError, match="non-empty bytes"):
        ImageContentPart("image/png", data=b"")
