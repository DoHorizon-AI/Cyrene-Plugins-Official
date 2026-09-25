"""Contract-mapping tests for the model API connector.

中文：模型 API 连接器的契约映射测试。"""

from __future__ import annotations

import json

import pytest
from cyrene_model_provider_contracts import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatFunction,
    ChatMessage,
    ChatRole,
    ChatTool,
    ChatToolCall,
    ChatToolCallFunction,
    ChatToolChoice,
)

from model_api_connector import (
    ProviderMappingError,
    chunk_from_event,
    response_chunks,
    to_upstream_body,
)


def test_request_projects_messages_tools_and_usage_options() -> None:
    request = ChatCompletionRequest(
        messages=(
            ChatMessage(role=ChatRole.SYSTEM, content="be brief"),
            ChatMessage(
                role=ChatRole.ASSISTANT,
                tool_calls=(
                    ChatToolCall(
                        id="call-1",
                        type="function",
                        function=ChatToolCallFunction(name="weather", arguments="{}"),
                    ),
                ),
            ),
            ChatMessage(role=ChatRole.TOOL, content="sunny", tool_call_id="call-1"),
        ),
        model="served",
        stream=True,
        temperature=0.2,
        max_tokens=64,
        tools=(
            ChatTool(
                type="function",
                function=ChatFunction(
                    name="weather",
                    description="look up weather",
                    parameters_json='{"type":"object"}',
                    strict=True,
                ),
            ),
        ),
        tool_choice=ChatToolChoice(mode="function", function_name="weather"),
        parallel_tool_calls=False,
        include_usage=True,
    )

    body = to_upstream_body(request)

    assert body["model"] == "served"
    assert body["stream"] is True
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 64
    assert body["stream_options"] == {"include_usage": True}
    assert body["parallel_tool_calls"] is False
    assert body["tool_choice"] == {"type": "function", "function": {"name": "weather"}}
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "weather",
                "description": "look up weather",
                "parameters": {"type": "object"},
                "strict": True,
            },
        }
    ]
    # A tool-only assistant turn sends a null content, not an empty string.
    # 中文：仅调用工具的助手轮次会发送 null 内容，而不是空字符串。
    assert body["messages"][1]["content"] is None
    assert body["messages"][1]["tool_calls"][0]["function"]["name"] == "weather"
    assert body["messages"][2]["tool_call_id"] == "call-1"
    assert body["messages"][0]["role"] == "system"


def test_unary_answer_becomes_ordered_chunks() -> None:
    body = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "real path"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }

    chunks = response_chunks(body, structured=True)

    assert [chunk.delta for chunk in chunks] == ["real path", ""]
    assert chunks[0].role == "assistant"
    closing = chunks[-1]
    assert closing.finish_reason == "stop"
    assert (closing.prompt_tokens, closing.completion_tokens) == (1, 2)
    # The provider never reported a total, so none is invented.
    # 中文：提供方从未报告总量，因此不会虚构该值。
    assert closing.total_tokens is None


def test_v1_answer_omits_structured_only_fields() -> None:
    """chat v1 cannot carry a role or a reported total, so neither is claimed.

        中文：chat v1 无法承载角色或已报告的总量，因此两者都不会被声明。"""

    body = {
        "choices": [
            {"message": {"role": "assistant", "content": "real path"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }

    chunks = response_chunks(body)

    assert chunks[0].role is None
    assert chunks[-1].total_tokens is None
    assert (chunks[-1].prompt_tokens, chunks[-1].completion_tokens) == (1, 2)


def test_tool_answers_require_the_v2_method() -> None:
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "w", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    with pytest.raises(ProviderMappingError):
        response_chunks(body)
    assert response_chunks(body, structured=True)[1].tool_calls[0].id == "call-1"


def test_unary_tool_answer_keeps_call_identity_and_usage() -> None:
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-weather",
                            "type": "function",
                            "function": {"name": "weather", "arguments": '{"city":"Paris"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    }

    chunks = response_chunks(body, structured=True)

    assert chunks[0].delta == ""
    call = next(chunk for chunk in chunks if chunk.tool_calls).tool_calls[0]
    assert (call.index, call.id, call.type) == (0, "call-weather", "function")
    assert call.function_name == "weather"
    assert call.function_arguments == '{"city":"Paris"}'
    usage = chunks[-1]
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (8, 4, 12)
    assert any(chunk.finish_reason == "tool_calls" for chunk in chunks)


def test_role_only_event_keeps_the_role_frame() -> None:
    chunk = chunk_from_event(
        {"choices": [{"delta": {"role": "assistant", "content": ""}}]}, structured=True
    )

    assert isinstance(chunk, ChatCompletionChunk)
    assert chunk.role == "assistant"
    assert chunk.delta == ""
    assert chunk.finish_reason is None


def test_usage_only_event_and_keepalive() -> None:
    usage = chunk_from_event(
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
        structured=True,
    )
    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (3, 2, 5)

    assert chunk_from_event({"choices": []}) is None
    assert chunk_from_event({"id": "chunk", "object": "chat.completion.chunk"}) is None


def test_streamed_tool_fragments_keep_index_and_partial_arguments() -> None:
    first = chunk_from_event(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-cancel-weather",
                                "type": "function",
                                "function": {"name": "weather", "arguments": '{"city":'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        structured=True,
    )
    second = chunk_from_event(
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
        structured=True,
    )

    assert first is not None and second is not None
    assert first.tool_calls[0].function_arguments == '{"city":'
    assert first.tool_calls[0].function_name == "weather"
    assert second.tool_calls[0].index == 0
    assert second.tool_calls[0].function_arguments == '"Paris"}'
    assert second.finish_reason == "tool_calls"


def test_malformed_answers_fail_closed() -> None:
    with pytest.raises(ProviderMappingError):
        response_chunks({"choices": []})
    # A choice without a message is unrepresentable rather than empty.
    # 中文：没有消息的选项无法表示，而不是表示为空。
    with pytest.raises(ProviderMappingError):
        response_chunks({"choices": [{"finish_reason": "stop"}]})
    # A streamed fragment without an index cannot be merged by the gateway.
    # 中文：没有索引的流式片段无法由网关合并。
    with pytest.raises(ProviderMappingError):
        chunk_from_event(
            {"choices": [{"delta": {"tool_calls": [{"id": "x", "function": {}}]}}]},
            structured=True,
        )
    # Usage is only reported when the provider actually reports numbers.
    # 中文：只有提供方确实报告数值时才会返回用量。
    assert chunk_from_event({"choices": [], "usage": {"prompt_tokens": "many"}}) is None
    # A version that cannot carry tool calls refuses rather than dropping them.
    # 中文：无法承载工具调用的协议版本会拒绝请求，而不是丢弃工具调用。
    with pytest.raises(ProviderMappingError):
        chunk_from_event(
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "id": "x", "function": {"name": "w"}}]}}
                ]
            }
        )


def test_tool_parameters_must_be_json() -> None:
    request = ChatCompletionRequest(
        messages=(ChatMessage(role=ChatRole.USER, content="hi"),),
        tools=(
            ChatTool(type="function", function=ChatFunction(name="w", parameters_json="not-json")),
        ),
    )
    with pytest.raises(ProviderMappingError):
        to_upstream_body(request)


def test_request_body_has_no_invented_fields() -> None:
    request = ChatCompletionRequest(messages=(ChatMessage(role=ChatRole.USER, content="hello"),))

    body = to_upstream_body(request)

    assert body == {
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    }
    assert json.dumps(body)  # JSON-serialisable for the upstream call
                             # 中文：此值可序列化为 JSON，以便用于上游调用。
