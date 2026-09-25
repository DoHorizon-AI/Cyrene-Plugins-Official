###############################################################################
# 📄 File: sdk/python/cyrene_model_provider_contracts/src/cyrene_model_provider_contracts/chat.py
# Module: Cyrene Plugins Official SDK
# Role: Canonical typed chat provider contract codec.
#
# This file implements the Plugins-owned model.provider.v1 chat messages for both
# the compatible v1 text method and the additive v2 structured-chat method.
#
# 模块：Cyrene Plugins Official
# 职责：规范的类型化 chat provider 契约 codec。
# 本文件实现 Plugins 所有的 model.provider.v1 chat 消息及 v2 增量字段。
###############################################################################
"""Manual protobuf codec for the Plugins-owned typed chat contract.

The schema authority is ``contracts/proto/cyrene/model/provider/v1`` in this
repository. The provider keeps using the zero-dependency wire helpers instead
of introducing a second message protocol.

中文:为 Plugins 所有的类型化 chat contract 提供手工 protobuf 编解码。

本仓库中的 schema 规范来源位于 ``contracts/proto/cyrene/model/provider/v1``。Provider 继续使用零依赖的 wire 辅助函数,而不会引入第二套消息协议。
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from . import _wire

CAPABILITY_ID = "model.provider.v1"
INTERFACE_VERSION = "1"
CHAT_COMPLETION_METHOD = "chat_completion"
CHAT_COMPLETION_V2_METHOD = "chat_completion_v2"
CHAT_COMPLETION_V2_INTERFACE_VERSION = "2"
CHAT_COMPLETION_REQUEST_TYPE_URL = (
    "type.cyrene.io/cyrene.model.provider.v1.ChatCompletionRequest"
)
CHAT_COMPLETION_RESPONSE_TYPE_URL = (
    "type.cyrene.io/cyrene.model.provider.v1.ChatCompletionResponse"
)
CHAT_COMPLETION_CHUNK_TYPE_URL = (
    "type.cyrene.io/cyrene.model.provider.v1.ChatCompletionChunk"
)

_MESSAGE_ROLE = 1
_MESSAGE_CONTENT = 2
_MESSAGE_NAME = 3
_MESSAGE_TOOL_CALL_ID = 4
_MESSAGE_TOOL_CALLS = 5
_REQUEST_MESSAGES = 1
_REQUEST_MODEL = 2
_REQUEST_STREAM = 3
_REQUEST_TEMPERATURE = 4
_REQUEST_MAX_TOKENS = 5
_REQUEST_TOOLS = 6
_REQUEST_TOOL_CHOICE = 7
_REQUEST_PARALLEL_TOOL_CALLS = 8
_REQUEST_INCLUDE_USAGE = 9
_RESPONSE_CHUNKS = 1
_CHUNK_DELTA = 1
_CHUNK_FINISH_REASON = 2
_CHUNK_PROMPT_TOKENS = 3
_CHUNK_COMPLETION_TOKENS = 4
_CHUNK_ROLE = 5
_CHUNK_TOOL_CALLS = 6
_CHUNK_TOTAL_TOKENS = 7
_FUNCTION_NAME = 1
_FUNCTION_DESCRIPTION = 2
_FUNCTION_PARAMETERS_JSON = 3
_FUNCTION_STRICT = 4
_TOOL_TYPE = 1
_TOOL_FUNCTION = 2
_TOOL_CHOICE_MODE = 1
_TOOL_CHOICE_FUNCTION_NAME = 2
_TOOL_CALL_ID = 1
_TOOL_CALL_TYPE = 2
_TOOL_CALL_FUNCTION = 3
_TOOL_CALL_FUNCTION_NAME = 1
_TOOL_CALL_FUNCTION_ARGUMENTS = 2
_TOOL_CALL_DELTA_INDEX = 1
_TOOL_CALL_DELTA_ID = 2
_TOOL_CALL_DELTA_TYPE = 3
_TOOL_CALL_DELTA_FUNCTION_NAME = 4
_TOOL_CALL_DELTA_FUNCTION_ARGUMENTS = 5
_UINT32_MAX = (1 << 32) - 1


class ChatCodecError(ValueError):
    """A typed chat payload is malformed or violates its wire contract.

        中文:类型化 chat 负载格式错误,或违反其 wire contract。
    """


class ChatRole(IntEnum):
    """Stable values from ``ChatMessage.Role`` in the canonical proto.

        中文:规范 proto 中 ``ChatMessage.Role`` 的稳定取值。
    """

    ROLE_UNSPECIFIED = 0
    ROLE_SYSTEM = 1
    ROLE_USER = 2
    ROLE_ASSISTANT = 3
    ROLE_TOOL = 4

    # Short aliases keep direct Python callers readable without changing the
    # canonical enum names or their wire values.
    # 中文:短别名让直接调用 Python 的调用方更容易阅读,同时不会改变规范枚举名称或其 wire 值。
    UNSPECIFIED = ROLE_UNSPECIFIED
    SYSTEM = ROLE_SYSTEM
    USER = ROLE_USER
    ASSISTANT = ROLE_ASSISTANT
    TOOL = ROLE_TOOL


ChatMessageRole = ChatRole


@dataclass(frozen=True)
class ChatMessage:
    """One canonical chat message in request order.

        中文:请求顺序中的一条规范 chat 消息。
    """

    role: ChatRole | int = ChatRole.ROLE_UNSPECIFIED
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ChatToolCall, ...] = ()


@dataclass(frozen=True)
class ChatFunction:
    """One provider function definition carried by structured chat v2.

        中文:structured chat v2 携带的一份 Provider 函数定义。
    """

    name: str
    description: str | None = None
    parameters_json: str = ""
    strict: bool | None = None


@dataclass(frozen=True)
class ChatTool:
    """One function tool offered to the provider.

        中文:提供给 Provider 的一个 function tool。
    """

    type: str
    function: ChatFunction


@dataclass(frozen=True)
class ChatToolChoice:
    """Normalized tool-choice mode, optionally naming one function.

        中文:经过规范化的 tool-choice 模式,也可指定一个函数名称。
    """

    mode: str
    function_name: str | None = None


@dataclass(frozen=True)
class ChatToolCallFunction:
    """Completed function identity and JSON argument text.

        中文:已完成函数的标识和 JSON 参数文本。
    """

    name: str
    arguments: str


@dataclass(frozen=True)
class ChatToolCall:
    """A completed assistant tool call in request history.

        中文:请求历史中的一条已完成 assistant tool call。
    """

    id: str
    type: str
    function: ChatToolCallFunction


@dataclass(frozen=True)
class ChatToolCallDelta:
    """One indexed, possibly partial streamed tool-call fragment.

        中文:一个带索引、可能尚未完整的流式 tool-call 片段。
    """

    index: int
    id: str | None = None
    type: str | None = None
    function_name: str | None = None
    function_arguments: str | None = None


@dataclass(frozen=True)
class ChatCompletionRequest:
    """Decoded or encodable ``ChatCompletionRequest``.

        中文:已解码或可编码的 ``ChatCompletionRequest``。
    """

    messages: tuple[ChatMessage, ...] = ()
    model: str | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    tools: tuple[ChatTool, ...] = ()
    tool_choice: ChatToolChoice | None = None
    parallel_tool_calls: bool | None = None
    include_usage: bool | None = None


@dataclass(frozen=True)
class ChatCompletionChunk:
    """One ordered provider response chunk.

        中文:一段有序的 Provider 响应 chunk。
    """

    delta: str = ""
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    role: str | None = None
    tool_calls: tuple[ChatToolCallDelta, ...] = ()
    total_tokens: int | None = None


@dataclass(frozen=True)
class ChatCompletionResponse:
    """Ordered chunks shared by unary and streamed provider responses.

        中文:供 unary 和流式 Provider 响应共用的有序 chunk 列表。
    """

    chunks: tuple[ChatCompletionChunk, ...] = ()


def encode_chat_message(message: ChatMessage) -> bytes:
    """Encode one canonical ``ChatMessage`` body.

        中文:编码一条规范 ``ChatMessage`` 正文。
    """

    return _encode_chat_message(message, structured=False)


def encode_chat_message_v2(message: ChatMessage) -> bytes:
    """Encode a ``ChatMessage`` including structured tool-call history.

        中文:编码一条包含结构化 tool-call 历史的 ``ChatMessage``。
    """

    return _encode_chat_message(message, structured=True)


def _encode_chat_message(message: ChatMessage, *, structured: bool) -> bytes:
    """Encode one message for the selected chat contract version.

        中文:根据所选 chat contract 版本编码一条消息。
    """

    if not isinstance(message, ChatMessage):
        raise ChatCodecError("message must be a ChatMessage")
    role = _role_value(message.role)
    _require_string(message.content, "ChatMessage.content")
    if not isinstance(message.tool_calls, (list, tuple)):
        raise ChatCodecError("ChatMessage.tool_calls must be a sequence")
    if message.tool_calls and not structured:
        raise ChatCodecError(
            "ChatMessage.tool_calls require the chat_completion_v2 method"
        )
    body = _wire.encode_enum_field(_MESSAGE_ROLE, role)
    body += _wire.encode_string_field(_MESSAGE_CONTENT, message.content)
    body += _optional_string_field(_MESSAGE_NAME, message.name, "ChatMessage.name")
    body += _optional_string_field(
        _MESSAGE_TOOL_CALL_ID, message.tool_call_id, "ChatMessage.tool_call_id"
    )
    if structured:
        body += b"".join(
            _wire.encode_len_delimited(
                _MESSAGE_TOOL_CALLS, _encode_chat_tool_call(call)
            )
            for call in message.tool_calls
        )
    return body


def decode_chat_message(payload: bytes) -> ChatMessage:
    """Decode one canonical ``ChatMessage`` body.

        中文:解码一条规范 ``ChatMessage`` 正文。
    """

    payload = _payload_bytes(payload, "ChatMessage")
    role: ChatRole | int = ChatRole.ROLE_UNSPECIFIED
    content = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ChatToolCall] = []
    try:
        for field, wire_type, value in _wire.iter_fields(payload):
            if field == _MESSAGE_ROLE:
                _require_wire_type("ChatMessage", "role", wire_type, _wire.WIRE_VARINT)
                role = _role_value(value)
            elif field == _MESSAGE_CONTENT:
                _require_wire_type("ChatMessage", "content", wire_type, _wire.WIRE_LEN)
                content = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _MESSAGE_NAME:
                _require_wire_type("ChatMessage", "name", wire_type, _wire.WIRE_LEN)
                name = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _MESSAGE_TOOL_CALL_ID:
                _require_wire_type(
                    "ChatMessage", "tool_call_id", wire_type, _wire.WIRE_LEN
                )
                tool_call_id = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _MESSAGE_TOOL_CALLS:
                _require_wire_type(
                    "ChatMessage", "tool_calls", wire_type, _wire.WIRE_LEN
                )
                tool_calls.append(_decode_chat_tool_call(value))  # type: ignore[arg-type]
    except _wire.WireFormatError as error:
        raise ChatCodecError(f"undecodable ChatMessage: {error}") from error
    return ChatMessage(
        role=role,
        content=content,
        name=name,
        tool_call_id=tool_call_id,
        tool_calls=tuple(tool_calls),
    )


def encode_chat_completion_request(request: ChatCompletionRequest) -> bytes:
    """Encode the v1 text-only ``ChatCompletionRequest`` message.

        中文:编码 v1 仅含文本的 ``ChatCompletionRequest`` 消息。
    """

    return _encode_chat_completion_request(request, structured=False)


def encode_chat_completion_request_v2(request: ChatCompletionRequest) -> bytes:
    """Encode a structured ``ChatCompletionRequest`` for chat v2.

        中文:为 chat v2 编码结构化 ``ChatCompletionRequest``。
    """

    return _encode_chat_completion_request(request, structured=True)


def _encode_chat_completion_request(
    request: ChatCompletionRequest, *, structured: bool
) -> bytes:
    """Encode a request for the selected chat contract version.

        中文:根据所选 chat contract 版本编码请求。
    """

    if not isinstance(request, ChatCompletionRequest):
        raise ChatCodecError("request must be a ChatCompletionRequest")
    if not isinstance(request.messages, (list, tuple)):
        raise ChatCodecError("ChatCompletionRequest.messages must be a sequence")
    if not isinstance(request.stream, bool):
        raise ChatCodecError("ChatCompletionRequest.stream must be a boolean")
    _require_optional_string(request.model, "ChatCompletionRequest.model")
    _require_temperature(request.temperature)
    _require_optional_uint32(request.max_tokens, "ChatCompletionRequest.max_tokens")
    _require_sequence(request.tools, "ChatCompletionRequest.tools")
    _require_optional_bool(
        request.parallel_tool_calls, "ChatCompletionRequest.parallel_tool_calls"
    )
    _require_optional_bool(request.include_usage, "ChatCompletionRequest.include_usage")
    if (
        request.tools
        or request.tool_choice is not None
        or request.parallel_tool_calls is not None
        or request.include_usage is not None
    ) and not structured:
        raise ChatCodecError(
            "structured chat fields require the chat_completion_v2 method"
        )
    if request.tool_choice is not None:
        _validate_tool_choice(request.tool_choice)

    # A repeated message has presence even when its body is empty.  Therefore
    # this uses encode_len_delimited rather than encode_bytes_field.
    # 中文:重复消息即使正文为空也仍具有 presence。因此这里使用 encode_len_delimited,而不是 encode_bytes_field。
    body = b"".join(
        _wire.encode_len_delimited(
            _REQUEST_MESSAGES,
            _encode_chat_message(message, structured=structured),
        )
        for message in request.messages
    )
    body += _optional_string_field(
        _REQUEST_MODEL, request.model, "ChatCompletionRequest.model"
    )
    if request.stream:
        body += _wire.encode_tag(
            _REQUEST_STREAM, _wire.WIRE_VARINT
        ) + _wire.encode_varint(1)
    if request.temperature is not None:
        body += _wire.encode_tag(
            _REQUEST_TEMPERATURE, _wire.WIRE_FIXED64
        ) + struct.pack("<d", request.temperature)
    if request.max_tokens is not None:
        body += _optional_uint32_field(_REQUEST_MAX_TOKENS, request.max_tokens)
    if structured:
        body += b"".join(
            _wire.encode_len_delimited(_REQUEST_TOOLS, _encode_chat_tool(tool))
            for tool in request.tools
        )
        if request.tool_choice is not None:
            body += _wire.encode_len_delimited(
                _REQUEST_TOOL_CHOICE, _encode_chat_tool_choice(request.tool_choice)
            )
        if request.parallel_tool_calls is not None:
            body += _optional_bool_field(
                _REQUEST_PARALLEL_TOOL_CALLS, request.parallel_tool_calls
            )
        if request.include_usage is not None:
            body += _optional_bool_field(_REQUEST_INCLUDE_USAGE, request.include_usage)
    return body


def decode_chat_completion_request(payload: bytes) -> ChatCompletionRequest:
    """Decode the canonical ``ChatCompletionRequest`` message.

        中文:解码规范的 ``ChatCompletionRequest`` 消息。
    """

    payload = _payload_bytes(payload, "ChatCompletionRequest")
    messages: list[ChatMessage] = []
    model: str | None = None
    stream = False
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[ChatTool] = []
    tool_choice: ChatToolChoice | None = None
    parallel_tool_calls: bool | None = None
    include_usage: bool | None = None
    try:
        for field, wire_type, value in _wire.iter_fields(payload):
            if field == _REQUEST_MESSAGES:
                _require_wire_type(
                    "ChatCompletionRequest", "messages", wire_type, _wire.WIRE_LEN
                )
                messages.append(decode_chat_message(value))  # type: ignore[arg-type]
            elif field == _REQUEST_MODEL:
                _require_wire_type(
                    "ChatCompletionRequest", "model", wire_type, _wire.WIRE_LEN
                )
                model = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _REQUEST_STREAM:
                _require_wire_type(
                    "ChatCompletionRequest", "stream", wire_type, _wire.WIRE_VARINT
                )
                stream = bool(value)
            elif field == _REQUEST_TEMPERATURE:
                _require_wire_type(
                    "ChatCompletionRequest",
                    "temperature",
                    wire_type,
                    _wire.WIRE_FIXED64,
                )
                temperature = struct.unpack("<d", value)[0]  # type: ignore[arg-type]
            elif field == _REQUEST_MAX_TOKENS:
                _require_wire_type(
                    "ChatCompletionRequest",
                    "max_tokens",
                    wire_type,
                    _wire.WIRE_VARINT,
                )
                max_tokens = _uint32_value(value, "ChatCompletionRequest.max_tokens")
            elif field == _REQUEST_TOOLS:
                _require_wire_type(
                    "ChatCompletionRequest", "tools", wire_type, _wire.WIRE_LEN
                )
                tools.append(_decode_chat_tool(value))  # type: ignore[arg-type]
            elif field == _REQUEST_TOOL_CHOICE:
                _require_wire_type(
                    "ChatCompletionRequest", "tool_choice", wire_type, _wire.WIRE_LEN
                )
                tool_choice = _decode_chat_tool_choice(value)  # type: ignore[arg-type]
            elif field == _REQUEST_PARALLEL_TOOL_CALLS:
                _require_wire_type(
                    "ChatCompletionRequest",
                    "parallel_tool_calls",
                    wire_type,
                    _wire.WIRE_VARINT,
                )
                parallel_tool_calls = bool(value)
            elif field == _REQUEST_INCLUDE_USAGE:
                _require_wire_type(
                    "ChatCompletionRequest",
                    "include_usage",
                    wire_type,
                    _wire.WIRE_VARINT,
                )
                include_usage = bool(value)
    except _wire.WireFormatError as error:
        raise ChatCodecError(f"undecodable ChatCompletionRequest: {error}") from error
    except struct.error as error:
        raise ChatCodecError(
            f"undecodable ChatCompletionRequest.temperature: {error}"
        ) from error
    _require_temperature(temperature)
    return ChatCompletionRequest(
        messages=tuple(messages),
        model=model,
        stream=stream,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tuple(tools),
        tool_choice=tool_choice,
        parallel_tool_calls=parallel_tool_calls,
        include_usage=include_usage,
    )


def decode_chat_completion_request_v2(payload: bytes) -> ChatCompletionRequest:
    """Decode a structured chat request using the additive protobuf fields.

        中文:使用新增的 protobuf 字段解码结构化 chat 请求。
    """

    return decode_chat_completion_request(payload)


def encode_chat_completion_chunk(chunk: ChatCompletionChunk) -> bytes:
    """Encode one v1 text-only ``ChatCompletionChunk`` body.

        中文:编码一个 v1 仅含文本的 ``ChatCompletionChunk`` 正文。
    """

    return _encode_chat_completion_chunk(chunk, structured=False)


def encode_chat_completion_chunk_v2(chunk: ChatCompletionChunk) -> bytes:
    """Encode one structured ``ChatCompletionChunk`` body for chat v2.

        中文:为 chat v2 编码一个结构化 ``ChatCompletionChunk`` 正文。
    """

    return _encode_chat_completion_chunk(chunk, structured=True)


def _encode_chat_completion_chunk(
    chunk: ChatCompletionChunk, *, structured: bool
) -> bytes:
    """Encode one response chunk for the selected chat contract version.

        中文:根据所选 chat contract 版本编码一个响应 chunk。
    """

    if not isinstance(chunk, ChatCompletionChunk):
        raise ChatCodecError("chunk must be a ChatCompletionChunk")
    _require_string(chunk.delta, "ChatCompletionChunk.delta")
    _require_optional_string(chunk.finish_reason, "ChatCompletionChunk.finish_reason")
    _require_optional_uint32(chunk.prompt_tokens, "ChatCompletionChunk.prompt_tokens")
    _require_optional_uint32(
        chunk.completion_tokens, "ChatCompletionChunk.completion_tokens"
    )
    _require_optional_uint32(chunk.total_tokens, "ChatCompletionChunk.total_tokens")
    _require_optional_string(chunk.role, "ChatCompletionChunk.role")
    _require_sequence(chunk.tool_calls, "ChatCompletionChunk.tool_calls")
    if (
        chunk.role is not None or chunk.tool_calls or chunk.total_tokens is not None
    ) and not structured:
        raise ChatCodecError(
            "structured chat response fields require the chat_completion_v2 method"
        )
    body = _wire.encode_string_field(_CHUNK_DELTA, chunk.delta)
    body += _optional_string_field(
        _CHUNK_FINISH_REASON, chunk.finish_reason, "ChatCompletionChunk.finish_reason"
    )
    if chunk.prompt_tokens is not None:
        body += _optional_uint32_field(_CHUNK_PROMPT_TOKENS, chunk.prompt_tokens)
    if chunk.completion_tokens is not None:
        body += _optional_uint32_field(
            _CHUNK_COMPLETION_TOKENS, chunk.completion_tokens
        )
    if structured:
        body += _optional_string_field(
            _CHUNK_ROLE, chunk.role, "ChatCompletionChunk.role"
        )
        body += b"".join(
            _wire.encode_len_delimited(
                _CHUNK_TOOL_CALLS, _encode_chat_tool_call_delta(call)
            )
            for call in chunk.tool_calls
        )
        if chunk.total_tokens is not None:
            body += _optional_uint32_field(_CHUNK_TOTAL_TOKENS, chunk.total_tokens)
    return body


def decode_chat_completion_chunk(payload: bytes) -> ChatCompletionChunk:
    """Decode one canonical ``ChatCompletionChunk`` body.

        中文:解码一条规范 ``ChatCompletionChunk`` 正文。
    """

    payload = _payload_bytes(payload, "ChatCompletionChunk")
    delta = ""
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    role: str | None = None
    tool_calls: list[ChatToolCallDelta] = []
    total_tokens: int | None = None
    try:
        for field, wire_type, value in _wire.iter_fields(payload):
            if field == _CHUNK_DELTA:
                _require_wire_type(
                    "ChatCompletionChunk", "delta", wire_type, _wire.WIRE_LEN
                )
                delta = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _CHUNK_FINISH_REASON:
                _require_wire_type(
                    "ChatCompletionChunk",
                    "finish_reason",
                    wire_type,
                    _wire.WIRE_LEN,
                )
                finish_reason = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _CHUNK_PROMPT_TOKENS:
                _require_wire_type(
                    "ChatCompletionChunk",
                    "prompt_tokens",
                    wire_type,
                    _wire.WIRE_VARINT,
                )
                prompt_tokens = _uint32_value(
                    value, "ChatCompletionChunk.prompt_tokens"
                )
            elif field == _CHUNK_COMPLETION_TOKENS:
                _require_wire_type(
                    "ChatCompletionChunk",
                    "completion_tokens",
                    wire_type,
                    _wire.WIRE_VARINT,
                )
                completion_tokens = _uint32_value(
                    value, "ChatCompletionChunk.completion_tokens"
                )
            elif field == _CHUNK_ROLE:
                _require_wire_type(
                    "ChatCompletionChunk", "role", wire_type, _wire.WIRE_LEN
                )
                role = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _CHUNK_TOOL_CALLS:
                _require_wire_type(
                    "ChatCompletionChunk", "tool_calls", wire_type, _wire.WIRE_LEN
                )
                tool_calls.append(_decode_chat_tool_call_delta(value))  # type: ignore[arg-type]
            elif field == _CHUNK_TOTAL_TOKENS:
                _require_wire_type(
                    "ChatCompletionChunk", "total_tokens", wire_type, _wire.WIRE_VARINT
                )
                total_tokens = _uint32_value(value, "ChatCompletionChunk.total_tokens")
    except _wire.WireFormatError as error:
        raise ChatCodecError(f"undecodable ChatCompletionChunk: {error}") from error
    return ChatCompletionChunk(
        delta=delta,
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        role=role,
        tool_calls=tuple(tool_calls),
        total_tokens=total_tokens,
    )


def decode_chat_completion_chunk_v2(payload: bytes) -> ChatCompletionChunk:
    """Decode one structured response chunk using additive fields.

        中文:使用新增字段解码一个结构化响应 chunk。
    """

    return decode_chat_completion_chunk(payload)


def encode_chat_completion_response(response: ChatCompletionResponse) -> bytes:
    """Encode one v1 text-only ``ChatCompletionResponse`` message.

        中文:编码一条 v1 仅含文本的 ``ChatCompletionResponse`` 消息。
    """

    return _encode_chat_completion_response(response, structured=False)


def encode_chat_completion_response_v2(response: ChatCompletionResponse) -> bytes:
    """Encode one structured ``ChatCompletionResponse`` for chat v2.

        中文:为 chat v2 编码一个结构化 ``ChatCompletionResponse``。
    """

    return _encode_chat_completion_response(response, structured=True)


def _encode_chat_completion_response(
    response: ChatCompletionResponse, *, structured: bool
) -> bytes:
    """Encode one response for the selected chat contract version.

        中文:根据所选 chat contract 版本编码一个响应。
    """

    if not isinstance(response, ChatCompletionResponse):
        raise ChatCodecError("response must be a ChatCompletionResponse")
    if not isinstance(response.chunks, (list, tuple)):
        raise ChatCodecError("ChatCompletionResponse.chunks must be a sequence")
    # A repeated message has presence even when its body is empty.
    # 中文:重复消息即使正文为空也仍具有 presence。
    return b"".join(
        _wire.encode_len_delimited(
            _RESPONSE_CHUNKS,
            _encode_chat_completion_chunk(chunk, structured=structured),
        )
        for chunk in response.chunks
    )


def decode_chat_completion_response(payload: bytes) -> ChatCompletionResponse:
    """Decode the canonical ``ChatCompletionResponse`` message.

        中文:解码规范的 ``ChatCompletionResponse`` 消息。
    """

    payload = _payload_bytes(payload, "ChatCompletionResponse")
    chunks: list[ChatCompletionChunk] = []
    try:
        for field, wire_type, value in _wire.iter_fields(payload):
            if field == _RESPONSE_CHUNKS:
                _require_wire_type(
                    "ChatCompletionResponse", "chunks", wire_type, _wire.WIRE_LEN
                )
                chunks.append(
                    decode_chat_completion_chunk(value)  # type: ignore[arg-type]
                )
    except _wire.WireFormatError as error:
        raise ChatCodecError(f"undecodable ChatCompletionResponse: {error}") from error
    return ChatCompletionResponse(chunks=tuple(chunks))


def decode_chat_completion_response_v2(payload: bytes) -> ChatCompletionResponse:
    """Decode one structured response using additive protobuf fields.

        中文:使用新增的 protobuf 字段解码一个结构化响应。
    """

    return decode_chat_completion_response(payload)


def _encode_chat_function(function: ChatFunction) -> bytes:
    if not isinstance(function, ChatFunction):
        raise ChatCodecError("ChatTool.function must be a ChatFunction")
    _require_non_empty_string(function.name, "ChatFunction.name")
    _require_optional_string(function.description, "ChatFunction.description")
    _require_string(function.parameters_json, "ChatFunction.parameters_json")
    _require_optional_bool(function.strict, "ChatFunction.strict")
    body = _wire.encode_string_field(_FUNCTION_NAME, function.name)
    body += _optional_string_field(
        _FUNCTION_DESCRIPTION, function.description, "ChatFunction.description"
    )
    body += _wire.encode_string_field(
        _FUNCTION_PARAMETERS_JSON, function.parameters_json
    )
    if function.strict is not None:
        body += _optional_bool_field(_FUNCTION_STRICT, function.strict)
    return body


def _decode_chat_function(payload: bytes) -> ChatFunction:
    payload = _payload_bytes(payload, "ChatFunction")
    name = ""
    description: str | None = None
    parameters_json = ""
    strict: bool | None = None
    try:
        for field, wire_type, value in _wire.iter_fields(payload):
            if field == _FUNCTION_NAME:
                _require_wire_type("ChatFunction", "name", wire_type, _wire.WIRE_LEN)
                name = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _FUNCTION_DESCRIPTION:
                _require_wire_type(
                    "ChatFunction", "description", wire_type, _wire.WIRE_LEN
                )
                description = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _FUNCTION_PARAMETERS_JSON:
                _require_wire_type(
                    "ChatFunction", "parameters_json", wire_type, _wire.WIRE_LEN
                )
                parameters_json = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _FUNCTION_STRICT:
                _require_wire_type(
                    "ChatFunction", "strict", wire_type, _wire.WIRE_VARINT
                )
                strict = bool(value)
    except _wire.WireFormatError as error:
        raise ChatCodecError(f"undecodable ChatFunction: {error}") from error
    _require_non_empty_string(name, "ChatFunction.name")
    return ChatFunction(
        name=name,
        description=description,
        parameters_json=parameters_json,
        strict=strict,
    )


def _encode_chat_tool(tool: ChatTool) -> bytes:
    if not isinstance(tool, ChatTool):
        raise ChatCodecError("ChatCompletionRequest.tools must contain ChatTool values")
    _require_non_empty_string(tool.type, "ChatTool.type")
    body = _wire.encode_string_field(_TOOL_TYPE, tool.type)
    body += _wire.encode_len_delimited(
        _TOOL_FUNCTION, _encode_chat_function(tool.function)
    )
    return body


def _decode_chat_tool(payload: bytes) -> ChatTool:
    payload = _payload_bytes(payload, "ChatTool")
    tool_type = ""
    function: ChatFunction | None = None
    try:
        for field, wire_type, value in _wire.iter_fields(payload):
            if field == _TOOL_TYPE:
                _require_wire_type("ChatTool", "type", wire_type, _wire.WIRE_LEN)
                tool_type = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _TOOL_FUNCTION:
                _require_wire_type("ChatTool", "function", wire_type, _wire.WIRE_LEN)
                function = _decode_chat_function(value)  # type: ignore[arg-type]
    except _wire.WireFormatError as error:
        raise ChatCodecError(f"undecodable ChatTool: {error}") from error
    _require_non_empty_string(tool_type, "ChatTool.type")
    if function is None:
        raise ChatCodecError("ChatTool.function is required")
    return ChatTool(type=tool_type, function=function)


def _encode_chat_tool_choice(choice: ChatToolChoice) -> bytes:
    _validate_tool_choice(choice)
    body = _wire.encode_string_field(_TOOL_CHOICE_MODE, choice.mode)
    body += _optional_string_field(
        _TOOL_CHOICE_FUNCTION_NAME, choice.function_name, "ChatToolChoice.function_name"
    )
    return body


def _decode_chat_tool_choice(payload: bytes) -> ChatToolChoice:
    payload = _payload_bytes(payload, "ChatToolChoice")
    mode = ""
    function_name: str | None = None
    try:
        for field, wire_type, value in _wire.iter_fields(payload):
            if field == _TOOL_CHOICE_MODE:
                _require_wire_type("ChatToolChoice", "mode", wire_type, _wire.WIRE_LEN)
                mode = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _TOOL_CHOICE_FUNCTION_NAME:
                _require_wire_type(
                    "ChatToolChoice", "function_name", wire_type, _wire.WIRE_LEN
                )
                function_name = _wire.decode_utf8(value)  # type: ignore[arg-type]
    except _wire.WireFormatError as error:
        raise ChatCodecError(f"undecodable ChatToolChoice: {error}") from error
    choice = ChatToolChoice(mode=mode, function_name=function_name)
    _validate_tool_choice(choice)
    return choice


def _encode_chat_tool_call_function(function: ChatToolCallFunction) -> bytes:
    if not isinstance(function, ChatToolCallFunction):
        raise ChatCodecError("ChatToolCall.function must be a ChatToolCallFunction")
    _require_non_empty_string(function.name, "ChatToolCallFunction.name")
    _require_string(function.arguments, "ChatToolCallFunction.arguments")
    return _wire.encode_string_field(
        _TOOL_CALL_FUNCTION_NAME, function.name
    ) + _wire.encode_string_field(_TOOL_CALL_FUNCTION_ARGUMENTS, function.arguments)


def _decode_chat_tool_call_function(payload: bytes) -> ChatToolCallFunction:
    payload = _payload_bytes(payload, "ChatToolCallFunction")
    name = ""
    arguments = ""
    try:
        for field, wire_type, value in _wire.iter_fields(payload):
            if field == _TOOL_CALL_FUNCTION_NAME:
                _require_wire_type(
                    "ChatToolCallFunction", "name", wire_type, _wire.WIRE_LEN
                )
                name = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _TOOL_CALL_FUNCTION_ARGUMENTS:
                _require_wire_type(
                    "ChatToolCallFunction", "arguments", wire_type, _wire.WIRE_LEN
                )
                arguments = _wire.decode_utf8(value)  # type: ignore[arg-type]
    except _wire.WireFormatError as error:
        raise ChatCodecError(f"undecodable ChatToolCallFunction: {error}") from error
    _require_non_empty_string(name, "ChatToolCallFunction.name")
    return ChatToolCallFunction(name=name, arguments=arguments)


def _encode_chat_tool_call(call: ChatToolCall) -> bytes:
    if not isinstance(call, ChatToolCall):
        raise ChatCodecError("ChatMessage.tool_calls must contain ChatToolCall values")
    _require_non_empty_string(call.id, "ChatToolCall.id")
    _require_non_empty_string(call.type, "ChatToolCall.type")
    return (
        _wire.encode_string_field(_TOOL_CALL_ID, call.id)
        + _wire.encode_string_field(_TOOL_CALL_TYPE, call.type)
        + _wire.encode_len_delimited(
            _TOOL_CALL_FUNCTION, _encode_chat_tool_call_function(call.function)
        )
    )


def _decode_chat_tool_call(payload: bytes) -> ChatToolCall:
    payload = _payload_bytes(payload, "ChatToolCall")
    call_id = ""
    call_type = ""
    function: ChatToolCallFunction | None = None
    try:
        for field, wire_type, value in _wire.iter_fields(payload):
            if field == _TOOL_CALL_ID:
                _require_wire_type("ChatToolCall", "id", wire_type, _wire.WIRE_LEN)
                call_id = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _TOOL_CALL_TYPE:
                _require_wire_type("ChatToolCall", "type", wire_type, _wire.WIRE_LEN)
                call_type = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _TOOL_CALL_FUNCTION:
                _require_wire_type(
                    "ChatToolCall", "function", wire_type, _wire.WIRE_LEN
                )
                function = _decode_chat_tool_call_function(value)  # type: ignore[arg-type]
    except _wire.WireFormatError as error:
        raise ChatCodecError(f"undecodable ChatToolCall: {error}") from error
    _require_non_empty_string(call_id, "ChatToolCall.id")
    _require_non_empty_string(call_type, "ChatToolCall.type")
    if function is None:
        raise ChatCodecError("ChatToolCall.function is required")
    return ChatToolCall(id=call_id, type=call_type, function=function)


def _encode_chat_tool_call_delta(call: ChatToolCallDelta) -> bytes:
    if not isinstance(call, ChatToolCallDelta):
        raise ChatCodecError(
            "ChatCompletionChunk.tool_calls must contain ChatToolCallDelta values"
        )
    _require_optional_uint32(call.index, "ChatToolCallDelta.index")
    _require_optional_string(call.id, "ChatToolCallDelta.id")
    _require_optional_string(call.type, "ChatToolCallDelta.type")
    _require_optional_string(call.function_name, "ChatToolCallDelta.function_name")
    _require_optional_string(
        call.function_arguments, "ChatToolCallDelta.function_arguments"
    )
    body = _wire.encode_uint32_field(_TOOL_CALL_DELTA_INDEX, call.index)
    body += _optional_string_field(_TOOL_CALL_DELTA_ID, call.id, "ChatToolCallDelta.id")
    body += _optional_string_field(
        _TOOL_CALL_DELTA_TYPE, call.type, "ChatToolCallDelta.type"
    )
    body += _optional_string_field(
        _TOOL_CALL_DELTA_FUNCTION_NAME,
        call.function_name,
        "ChatToolCallDelta.function_name",
    )
    body += _optional_string_field(
        _TOOL_CALL_DELTA_FUNCTION_ARGUMENTS,
        call.function_arguments,
        "ChatToolCallDelta.function_arguments",
    )
    return body


def _decode_chat_tool_call_delta(payload: bytes) -> ChatToolCallDelta:
    payload = _payload_bytes(payload, "ChatToolCallDelta")
    index = 0
    call_id: str | None = None
    call_type: str | None = None
    function_name: str | None = None
    function_arguments: str | None = None
    try:
        for field, wire_type, value in _wire.iter_fields(payload):
            if field == _TOOL_CALL_DELTA_INDEX:
                _require_wire_type(
                    "ChatToolCallDelta", "index", wire_type, _wire.WIRE_VARINT
                )
                index = _uint32_value(value, "ChatToolCallDelta.index")
            elif field == _TOOL_CALL_DELTA_ID:
                _require_wire_type("ChatToolCallDelta", "id", wire_type, _wire.WIRE_LEN)
                call_id = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _TOOL_CALL_DELTA_TYPE:
                _require_wire_type(
                    "ChatToolCallDelta", "type", wire_type, _wire.WIRE_LEN
                )
                call_type = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _TOOL_CALL_DELTA_FUNCTION_NAME:
                _require_wire_type(
                    "ChatToolCallDelta", "function_name", wire_type, _wire.WIRE_LEN
                )
                function_name = _wire.decode_utf8(value)  # type: ignore[arg-type]
            elif field == _TOOL_CALL_DELTA_FUNCTION_ARGUMENTS:
                _require_wire_type(
                    "ChatToolCallDelta", "function_arguments", wire_type, _wire.WIRE_LEN
                )
                function_arguments = _wire.decode_utf8(value)  # type: ignore[arg-type]
    except _wire.WireFormatError as error:
        raise ChatCodecError(f"undecodable ChatToolCallDelta: {error}") from error
    return ChatToolCallDelta(
        index=index,
        id=call_id,
        type=call_type,
        function_name=function_name,
        function_arguments=function_arguments,
    )


def _payload_bytes(payload: Any, message_name: str) -> bytes:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ChatCodecError(f"{message_name} payload must be bytes")
    return bytes(payload)


def _require_wire_type(
    message_name: str, field_name: str, actual: int, expected: int
) -> None:
    """Reject a known field encoded with a different protobuf wire type.

        中文:拒绝使用错误 protobuf wire type 编码的已知字段。
    """

    if actual != expected:
        raise ChatCodecError(
            f"{message_name}.{field_name} has wire type {actual}, expected {expected}"
        )


def _role_value(value: Any) -> ChatRole:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChatCodecError("ChatMessage.role must be a canonical enum value")
    try:
        return ChatRole(value)
    except ValueError as error:
        raise ChatCodecError(f"unsupported ChatMessage.Role value {value}") from error


def _require_string(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise ChatCodecError(f"{field_name} must be a string")


def _require_non_empty_string(value: Any, field_name: str) -> None:
    _require_string(value, field_name)
    if not value.strip():
        raise ChatCodecError(f"{field_name} must be a non-empty string")


def _require_sequence(value: Any, field_name: str) -> None:
    if not isinstance(value, (list, tuple)):
        raise ChatCodecError(f"{field_name} must be a sequence")


def _require_optional_bool(value: Any, field_name: str) -> None:
    if value is not None and not isinstance(value, bool):
        raise ChatCodecError(f"{field_name} must be a boolean")


def _optional_bool_field(field_number: int, value: bool) -> bytes:
    return _wire.encode_tag(field_number, _wire.WIRE_VARINT) + _wire.encode_varint(
        int(value)
    )


def _validate_tool_choice(choice: ChatToolChoice) -> None:
    if not isinstance(choice, ChatToolChoice):
        raise ChatCodecError("ChatCompletionRequest.tool_choice must be ChatToolChoice")
    if choice.mode not in {"none", "auto", "required", "function"}:
        raise ChatCodecError("ChatToolChoice.mode is invalid")
    _require_optional_string(choice.function_name, "ChatToolChoice.function_name")
    if choice.mode == "function":
        _require_non_empty_string(choice.function_name, "ChatToolChoice.function_name")
    elif choice.function_name is not None:
        raise ChatCodecError(
            "ChatToolChoice.function_name is only valid for mode 'function'"
        )


def _require_optional_string(value: Any, field_name: str) -> None:
    if value is not None:
        _require_string(value, field_name)


def _optional_string_field(field_number: int, value: Any, field_name: str) -> bytes:
    _require_optional_string(value, field_name)
    if value is None:
        return b""
    # optional strings preserve explicit empty-string presence.
    # 中文:可选字符串会保留显式设置为空字符串的 presence。
    return _wire.encode_len_delimited(field_number, value.encode("utf-8"))


def _require_temperature(value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChatCodecError("ChatCompletionRequest.temperature must be a number")
    if not math.isfinite(float(value)):
        raise ChatCodecError("ChatCompletionRequest.temperature must be finite")


def _require_optional_uint32(value: Any, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChatCodecError(f"{field_name} must be an unsigned 32-bit integer")
    if not 0 <= value <= _UINT32_MAX:
        raise ChatCodecError(f"{field_name} is outside uint32 range")


def _optional_uint32_field(field_number: int, value: int) -> bytes:
    # optional uint32 preserves explicit zero presence.
    # 中文:可选 uint32 会保留显式设置为零的 presence。
    return _wire.encode_tag(field_number, _wire.WIRE_VARINT) + _wire.encode_varint(
        value
    )


def _uint32_value(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or value < 0 or value > _UINT32_MAX:
        raise ChatCodecError(f"{field_name} is outside uint32 range")
    return value


__all__ = [
    "CAPABILITY_ID",
    "CHAT_COMPLETION_CHUNK_TYPE_URL",
    "CHAT_COMPLETION_METHOD",
    "CHAT_COMPLETION_REQUEST_TYPE_URL",
    "CHAT_COMPLETION_RESPONSE_TYPE_URL",
    "CHAT_COMPLETION_V2_INTERFACE_VERSION",
    "CHAT_COMPLETION_V2_METHOD",
    "INTERFACE_VERSION",
    "ChatCodecError",
    "ChatCompletionChunk",
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "ChatFunction",
    "ChatMessage",
    "ChatMessageRole",
    "ChatRole",
    "ChatTool",
    "ChatToolCall",
    "ChatToolCallDelta",
    "ChatToolCallFunction",
    "ChatToolChoice",
    "decode_chat_completion_chunk",
    "decode_chat_completion_chunk_v2",
    "decode_chat_completion_request",
    "decode_chat_completion_request_v2",
    "decode_chat_completion_response",
    "decode_chat_completion_response_v2",
    "encode_chat_completion_chunk",
    "encode_chat_completion_chunk_v2",
    "encode_chat_completion_request",
    "encode_chat_completion_request_v2",
    "encode_chat_completion_response",
    "encode_chat_completion_response_v2",
    "encode_chat_message",
    "encode_chat_message_v2",
]
