"""Translate between the typed chat contract and one OpenAI-compatible peer.

Both directions are lossless for the fields the contract carries: a provider
answer never gains invented usage, and a streamed frame is never merged or
reordered, because the gateway's frames follow these one-to-one.
| 上游帧与契约 chunk 一一对应，不合并、不重排、不臆造用量。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cyrene_model_provider_contracts import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatMessage,
    ChatRole,
    ChatToolCallDelta,
)

_ROLE_NAMES = {
    int(ChatRole.ROLE_SYSTEM): "system",
    int(ChatRole.ROLE_USER): "user",
    int(ChatRole.ROLE_ASSISTANT): "assistant",
    int(ChatRole.ROLE_TOOL): "tool",
}


class ProviderMappingError(ValueError):
    """An upstream answer cannot be represented by the typed contract.

        中文：上游响应无法用有类型契约表示。"""


def _role_name(role: ChatRole | int) -> str:
    return _ROLE_NAMES.get(int(role), "user")


def _message_body(message: ChatMessage) -> dict[str, Any]:
    body: dict[str, Any] = {"role": _role_name(message.role)}
    if message.tool_calls:
        # An assistant turn that only calls tools carries no text content.
        # 中文：助手轮次若只调用工具，则不携带文本内容。
        body["content"] = message.content or None
        body["tool_calls"] = [
            {
                "id": call.id,
                "type": call.type,
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls
        ]
    else:
        body["content"] = message.content
    if message.name:
        body["name"] = message.name
    if message.tool_call_id:
        body["tool_call_id"] = message.tool_call_id
    return body


def to_upstream_body(request: ChatCompletionRequest) -> dict[str, Any]:
    """Project one typed request onto an OpenAI-compatible chat body.

        中文：将一个有类型请求映射为 OpenAI 兼容的聊天请求体。"""

    body: dict[str, Any] = {
        "messages": [_message_body(message) for message in request.messages],
        "stream": bool(request.stream),
    }
    if request.model:
        body["model"] = request.model
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.max_tokens is not None:
        body["max_tokens"] = request.max_tokens
    if request.tools:
        body["tools"] = [_tool_body(tool) for tool in request.tools]
    if request.tool_choice is not None:
        body["tool_choice"] = _tool_choice_body(request.tool_choice)
    if request.parallel_tool_calls is not None:
        body["parallel_tool_calls"] = request.parallel_tool_calls
    if request.include_usage:
        body["stream_options"] = {"include_usage": True}
    return body


def _tool_body(tool: Any) -> dict[str, Any]:
    function: dict[str, Any] = {"name": tool.function.name}
    if tool.function.description is not None:
        function["description"] = tool.function.description
    if tool.function.parameters_json:
        try:
            function["parameters"] = json.loads(tool.function.parameters_json)
        except json.JSONDecodeError as error:
            raise ProviderMappingError("tool parameters are not JSON") from error
    if tool.function.strict is not None:
        function["strict"] = tool.function.strict
    return {"type": tool.type, "function": function}


def _tool_choice_body(choice: Any) -> Any:
    if not choice.function_name:
        return choice.mode
    return {"type": "function", "function": {"name": choice.function_name}}


def response_chunks(
    body: Mapping[str, Any], *, structured: bool = False
) -> tuple[ChatCompletionChunk, ...]:
    """Order the chunks of one non-streamed provider answer.

    ``structured`` selects chat v2: the v1 codec cannot carry a role, tool calls
    or a reported total, so those fields are only set when v2 was negotiated.

        中文：排列一个非流式提供方响应的数据块顺序。

        中文：``structured`` 选择 chat v2：v1 编解码器无法承载角色、工具调用或已报告的总量，因此只有在协商启用 v2 时才会设置这些字段。
    """

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderMappingError("upstream answer carried no choices")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise ProviderMappingError("upstream choice is not an object")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ProviderMappingError("upstream choice carried no message")

    chunks: list[ChatCompletionChunk] = []
    role = str(message.get("role") or "assistant")
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    chunks.append(ChatCompletionChunk(delta=text, role=role if structured else None))
    tool_calls = _tool_call_deltas(message.get("tool_calls"), require_index=False)
    if tool_calls:
        _require_structured(tool_calls, structured=structured, what="tool calls")
        chunks.append(ChatCompletionChunk(tool_calls=tool_calls))
    # A single non-streamed answer reports its terminal reason and its usage
    # together, so they travel as one closing chunk.
    # 中文：单个非流式响应会同时报告结束原因和用量，因此二者作为一个结束数据块发送。
    finish_reason = choice.get("finish_reason")
    usage = _usage_chunk(body.get("usage"), structured=structured)
    if isinstance(finish_reason, str) or usage is not None:
        chunks.append(
            ChatCompletionChunk(
                finish_reason=finish_reason if isinstance(finish_reason, str) else None,
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
            )
        )
    return tuple(chunks)


def chunk_from_event(
    event: Mapping[str, Any], *, structured: bool = False
) -> ChatCompletionChunk | None:
    """Map one streamed provider event onto exactly one contract chunk.

        中文：将一个流式提供方事件映射为且仅映射为一个契约数据块。"""

    choices = event.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    if choice is not None and not isinstance(choice, Mapping):
        raise ProviderMappingError("upstream stream choice is not an object")

    delta: Mapping[str, Any] = {}
    finish_reason: str | None = None
    if choice is not None:
        raw_delta = choice.get("delta")
        if raw_delta is not None and not isinstance(raw_delta, Mapping):
            raise ProviderMappingError("upstream stream delta is not an object")
        delta = raw_delta or {}
        raw_finish = choice.get("finish_reason")
        finish_reason = raw_finish if isinstance(raw_finish, str) else None

    content = delta.get("content")
    role = delta.get("role")
    tool_calls = _tool_call_deltas(delta.get("tool_calls"))
    if tool_calls:
        _require_structured(tool_calls, structured=structured, what="streamed tool calls")
    usage = _usage_chunk(event.get("usage"), structured=structured)

    chunk = ChatCompletionChunk(
        delta=content if isinstance(content, str) else "",
        finish_reason=finish_reason,
        role=str(role) if structured and isinstance(role, str) else None,
        tool_calls=tool_calls if structured else (),
        prompt_tokens=usage.prompt_tokens if usage else None,
        completion_tokens=usage.completion_tokens if usage else None,
        total_tokens=usage.total_tokens if usage else None,
    )
    if (
        not chunk.delta
        and chunk.finish_reason is None
        and chunk.role is None
        and not chunk.tool_calls
        and usage is None
    ):
        return None
    return chunk


def _require_structured(value: Any, *, structured: bool, what: str) -> None:
    if value and not structured:
        raise ProviderMappingError(f"{what} require the chat_completion_v2 method")


def _tool_call_deltas(raw: Any, *, require_index: bool = True) -> tuple[ChatToolCallDelta, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProviderMappingError("upstream tool_calls is not a list")
    deltas: list[ChatToolCallDelta] = []
    for position, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ProviderMappingError("upstream tool call is not an object")
        function = item.get("function")
        function_map = function if isinstance(function, Mapping) else {}
        raw_index = item.get("index")
        if require_index and not isinstance(raw_index, int):
            # Streamed fragments identify their call by index; without one the
            # gateway cannot tell which call a fragment belongs to.
            # 中文：流式片段通过索引标识所属调用；没有索引时，网关无法判断片段属于哪个调用。
            raise ProviderMappingError("streamed tool call carried no index")
        index = int(raw_index) if isinstance(raw_index, int) else position
        name = function_map.get("name")
        arguments = function_map.get("arguments")
        deltas.append(
            ChatToolCallDelta(
                index=index,
                id=str(item["id"]) if isinstance(item.get("id"), str) else None,
                type=str(item["type"]) if isinstance(item.get("type"), str) else None,
                function_name=str(name) if isinstance(name, str) else None,
                function_arguments=str(arguments) if isinstance(arguments, str) else None,
            )
        )
    return tuple(deltas)


def _usage_chunk(raw: Any, *, structured: bool = False) -> ChatCompletionChunk | None:
    """Return provider-reported usage only; never fabricate a total.

    The v1 codec cannot carry a reported total, so it is dropped unless v2 was
    negotiated; the prompt and completion counts remain exact.

        中文：仅返回提供方报告的用量；绝不虚构总量。

        中文：v1 编解码器无法承载已报告的总量，因此除非协商启用 v2，否则会丢弃该总量；提示词和生成内容的计数仍保持准确。
    """

    if not isinstance(raw, Mapping):
        return None
    prompt = _token_value(raw.get("prompt_tokens"))
    completion = _token_value(raw.get("completion_tokens"))
    total = _token_value(raw.get("total_tokens")) if structured else None
    if prompt is None and completion is None and total is None:
        return None
    return ChatCompletionChunk(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )


def _token_value(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw
