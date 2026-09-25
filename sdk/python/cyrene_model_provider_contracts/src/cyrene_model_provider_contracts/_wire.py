###############################################################################
# 📄 File: sdk/python/cyrene_model_provider_contracts/src/cyrene_model_provider_contracts/_wire.py
# Module: Cyrene Plugins Official SDK
# Role: Shared protobuf wire primitives for owner contracts.
#
# This file documents an active plugin boundary; runtime behavior is unchanged.
#
# 模块：Cyrene Plugins Official
# 职责：provider 分发或服务适配器实现。
# 本文件属于活动插件边界；运行时行为保持不变。
###############################################################################
"""Zero-dependency protobuf wire primitives.

The canonical ``model.provider.v1`` payloads are protobuf messages exchanged
directly with the selected Plugin endpoint. This module encodes and decodes just
the wire format, so a minimal provider does not need a ``protobuf`` runtime.
The encoders follow the Plugins-owned schema byte for byte, including proto3
default-value omission.

This module defines no message shapes. The canonical field numbers live in
``embeddings`` and ``chat``, next to the contracts they belong to.

中文：零依赖的 protobuf wire 基础操作。规范的 `model.provider.v1` 负载是与选定 Plugin Endpoint 直接交换的 protobuf 消息。本模块只编解码 wire 格式，因此最精简的 Provider 无需依赖 `protobuf` Runtime。编码器会逐字节遵循 Plugins 所有的 schema，包括省略 proto3 默认值。本模块不定义消息结构；规范字段编号位于 `embeddings` 和 `chat` 模块，并紧邻各自所属的 contract。
"""

from __future__ import annotations

import struct
from collections.abc import Iterator

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5


class WireFormatError(ValueError):
    """The payload is not decodable protobuf for the expected message.

        中文：对于预期消息类型，该负载无法解码为 protobuf。
    """


def encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a base-128 varint.

        中文：将非负整数编码为 base-128 varint。
    """

    if value < 0:
        raise WireFormatError("varint value must be non-negative")
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode one varint, returning its value and the next offset.

        中文：解码一个 varint，并返回其值和下一个偏移量。
    """

    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            raise WireFormatError("varint is too long")
    raise WireFormatError("truncated varint")


def encode_tag(field_number: int, wire_type: int) -> bytes:
    """Encode a field tag.

        中文：编码一个字段标记。
    """

    return encode_varint((field_number << 3) | wire_type)


def encode_len_delimited(field_number: int, payload: bytes) -> bytes:
    """Encode a length-delimited field.

        中文：编码一个长度分隔字段。
    """

    return encode_tag(field_number, WIRE_LEN) + encode_varint(len(payload)) + payload


def encode_string_field(field_number: int, value: str) -> bytes:
    """Encode a string field, omitting the proto3 default.

        中文：编码字符串字段；若为 proto3 默认值则省略。
    """

    if not value:
        return b""
    return encode_len_delimited(field_number, value.encode("utf-8"))


def encode_bytes_field(field_number: int, value: bytes) -> bytes:
    """Encode a bytes or embedded-message field, omitting the proto3 default.

        中文：编码 bytes 字段或内嵌消息字段；若为 proto3 默认值则省略。
    """

    if not value:
        return b""
    return encode_len_delimited(field_number, value)


def encode_uint32_field(field_number: int, value: int) -> bytes:
    """Encode a uint32 field, omitting the proto3 default.

        中文：编码 uint32 字段；若为 proto3 默认值则省略。
    """

    if value == 0:
        return b""
    return encode_tag(field_number, WIRE_VARINT) + encode_varint(value)


def encode_enum_field(field_number: int, value: int) -> bytes:
    """Encode an enum field, omitting the proto3 default.

        中文：编码枚举字段；若为 proto3 默认值则省略。
    """

    return encode_uint32_field(field_number, value)


def encode_packed_floats(field_number: int, values: tuple[float, ...]) -> bytes:
    """Encode a packed repeated float field as little-endian 32-bit values.

        中文：将打包的 repeated float 字段编码为小端序 32 位数值。
    """

    if not values:
        return b""
    return encode_len_delimited(field_number, struct.pack(f"<{len(values)}f", *values))


def iter_fields(data: bytes) -> Iterator[tuple[int, int, bytes | int]]:
    """Yield ``(field_number, wire_type, value)`` for every field present.

    Length-delimited and fixed-width fields yield their raw bytes; varint fields
    yield an int. Unknown fields are yielded rather than rejected so that a
    forward-compatible payload stays decodable.

        中文：针对每个存在的字段，依次产生 `(field_number, wire_type, value)`。长度分隔和固定宽度字段会产生原始字节；varint 字段会产生整数。遇到未知字段时也会产出，而不是拒绝，因此向前兼容的负载仍可解码。
    """

    offset = 0
    size = len(data)
    while offset < size:
        tag, offset = decode_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if field_number == 0:
            raise WireFormatError("field number 0 is invalid")
        if wire_type == WIRE_VARINT:
            value, offset = decode_varint(data, offset)
            yield field_number, wire_type, value
        elif wire_type == WIRE_LEN:
            length, offset = decode_varint(data, offset)
            end = offset + length
            if end > size:
                raise WireFormatError("truncated length-delimited field")
            yield field_number, wire_type, data[offset:end]
            offset = end
        elif wire_type == WIRE_FIXED64:
            if offset + 8 > size:
                raise WireFormatError("truncated 64-bit field")
            yield field_number, wire_type, data[offset : offset + 8]
            offset += 8
        elif wire_type == WIRE_FIXED32:
            if offset + 4 > size:
                raise WireFormatError("truncated 32-bit field")
            yield field_number, wire_type, data[offset : offset + 4]
            offset += 4
        else:
            raise WireFormatError(f"unsupported wire type {wire_type}")


def decode_packed_floats(payload: bytes) -> tuple[float, ...]:
    """Decode a packed repeated float field.

        中文：解码一个打包的 repeated float 字段。
    """

    if len(payload) % 4:
        raise WireFormatError("packed float field is not a multiple of 4 bytes")
    return struct.unpack(f"<{len(payload) // 4}f", payload)


def decode_utf8(payload: bytes) -> str:
    """Decode a protobuf string field.

        中文：解码 protobuf 字符串字段。
    """

    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WireFormatError("string field is not valid UTF-8") from error
