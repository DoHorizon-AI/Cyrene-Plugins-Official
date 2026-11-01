#!/usr/bin/env python3
"""Validate generated Python bindings for the direct model-provider payload.

中文:验证直连模型提供方载荷的生成 Python 绑定。"""

from __future__ import annotations

import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("usage: run_tck.py <generated-python-root>")
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
repository_root = Path(__file__).resolve().parents[4]
sys.path.insert(
    0,
    str(repository_root / "sdk/python/cyrene_model_provider_contracts/src"),
)

from cyrene.model.provider.v1 import model_provider_pb2
from cyrene_model_provider_contracts import (
    EmbeddingBatch,
    EmbeddingsRequest,
    EmbeddingsResponse,
    EmbeddingVector,
    decode_embeddings_request,
    decode_embeddings_response,
    encode_embeddings_request,
    encode_embeddings_response,
)
from google.protobuf.any_pb2 import Any

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

# The shared Python codec must encode the same protobuf bytes as protoc output.
sdk_embedding_request = EmbeddingsRequest(
    inputs=("alpha", "beta"), model="deterministic-model"
)
assert encode_embeddings_request(
    sdk_embedding_request
) == embedding_request.SerializeToString(deterministic=True)
assert (
    decode_embeddings_request(embedding_request.SerializeToString())
    == sdk_embedding_request
)
sdk_embedding_response = EmbeddingsResponse(
    embeddings=EmbeddingBatch(
        vectors=(EmbeddingVector((1.0, 2.0)), EmbeddingVector((3.0, 4.0))),
        dimensions=2,
        model="deterministic-model",
    )
)
assert encode_embeddings_response(sdk_embedding_response) == response.SerializeToString(
    deterministic=True
)
decoded_embedding_response = decode_embeddings_response(response.SerializeToString())
assert decoded_embedding_response.embeddings is not None
assert decoded_embedding_response.embeddings.model == "deterministic-model"
assert decoded_embedding_response.embeddings.dimensions == 2
assert tuple(decoded_embedding_response.embeddings.vectors[0].values) == (1.0, 2.0)

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

# Structured chat v2: index, identity, type, function facts, usage, and
# finish_reason must survive the projection byte-for-byte.
# 中文:结构化 chat v2 的 index、identity、type、function 事实、用量和 finish_reason 必须在投影中逐字节保持不变。
tool_chat = model_provider_pb2.ChatCompletionRequest(
    messages=[
        model_provider_pb2.ChatMessage(
            role=model_provider_pb2.ChatMessage.ROLE_USER,
            content="weather?",
        )
    ],
    model="deterministic-model",
    stream=True,
    parallel_tool_calls=False,
    include_usage=True,
    tools=[
        model_provider_pb2.ChatTool(
            type="function",
            function=model_provider_pb2.ChatFunction(
                name="weather",
                description="look up weather",
                parameters_json='{"type":"object"}',
                strict=True,
            ),
        )
    ],
    tool_choice=model_provider_pb2.ChatToolChoice(
        mode="function",
        function_name="weather",
    ),
)
packed_tool_chat = Any()
packed_tool_chat.Pack(tool_chat)
unpacked_tool_chat = model_provider_pb2.ChatCompletionRequest()
assert packed_tool_chat.Unpack(unpacked_tool_chat)
assert unpacked_tool_chat.tools[0].function.name == "weather"
assert unpacked_tool_chat.tools[0].function.parameters_json == '{"type":"object"}'
assert unpacked_tool_chat.tools[0].function.strict is True
assert unpacked_tool_chat.tool_choice.function_name == "weather"
assert unpacked_tool_chat.HasField("parallel_tool_calls")
assert unpacked_tool_chat.parallel_tool_calls is False
assert unpacked_tool_chat.include_usage is True

tool_response = model_provider_pb2.ChatCompletionResponse(
    chunks=[
        model_provider_pb2.ChatCompletionChunk(
            delta="",
            finish_reason="tool_calls",
            prompt_tokens=8,
            completion_tokens=4,
            total_tokens=12,
            role="assistant",
            tool_calls=[
                model_provider_pb2.ChatToolCallDelta(
                    index=0,
                    id="call-1",
                    type="function",
                    function_name="weather",
                    function_arguments='{"city":',
                )
            ],
        )
    ]
)
packed_tool_response = Any()
packed_tool_response.Pack(tool_response)
unpacked_tool_response = model_provider_pb2.ChatCompletionResponse()
assert packed_tool_response.Unpack(unpacked_tool_response)
tool_chunk = unpacked_tool_response.chunks[0]
tool_delta = tool_chunk.tool_calls[0]
assert tool_delta.index == 0
assert tool_delta.id == "call-1"
assert tool_delta.type == "function"
assert tool_delta.function_name == "weather"
assert tool_delta.function_arguments == '{"city":'
assert tool_chunk.finish_reason == "tool_calls"
assert tool_chunk.prompt_tokens == 8
assert tool_chunk.completion_tokens == 4
assert tool_chunk.total_tokens == 12

print("model.provider.v1 generated Python payload TCK: PASS")
