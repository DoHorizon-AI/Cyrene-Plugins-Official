"""Small protobuf wire helpers local to the dependency-free memory SDK."""

from __future__ import annotations

import struct
from collections.abc import Iterator

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5


class WireFormatError(ValueError):
    """The encoded bytes are not a valid protobuf wire payload."""


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise WireFormatError("varint must be non-negative")
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            raise WireFormatError("varint is too long")
    raise WireFormatError("truncated varint")


def encode_tag(field_number: int, wire_type: int) -> bytes:
    return encode_varint((field_number << 3) | wire_type)


def encode_len_delimited(field_number: int, payload: bytes) -> bytes:
    return encode_tag(field_number, WIRE_LEN) + encode_varint(len(payload)) + payload


def encode_string_field(field_number: int, value: str) -> bytes:
    return encode_len_delimited(field_number, value.encode("utf-8")) if value else b""


def encode_packed_floats(field_number: int, values: tuple[float, ...]) -> bytes:
    if not values:
        return b""
    return encode_len_delimited(field_number, struct.pack(f"<{len(values)}f", *values))


def iter_fields(data: bytes) -> Iterator[tuple[int, int, bytes | int]]:
    offset = 0
    while offset < len(data):
        tag, offset = decode_varint(data, offset)
        number = tag >> 3
        wire_type = tag & 7
        if number == 0:
            raise WireFormatError("field number zero is invalid")
        if wire_type == WIRE_VARINT:
            value, offset = decode_varint(data, offset)
            yield number, wire_type, value
        elif wire_type == WIRE_LEN:
            length, offset = decode_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise WireFormatError("truncated length-delimited field")
            yield number, wire_type, data[offset:end]
            offset = end
        elif wire_type == WIRE_FIXED64:
            if offset + 8 > len(data):
                raise WireFormatError("truncated fixed64 field")
            yield number, wire_type, data[offset : offset + 8]
            offset += 8
        elif wire_type == WIRE_FIXED32:
            if offset + 4 > len(data):
                raise WireFormatError("truncated fixed32 field")
            yield number, wire_type, data[offset : offset + 4]
            offset += 4
        else:
            raise WireFormatError(f"unsupported wire type {wire_type}")


def decode_packed_floats(payload: bytes) -> tuple[float, ...]:
    if len(payload) % 4:
        raise WireFormatError("packed floats must have a length divisible by four")
    return struct.unpack(f"<{len(payload) // 4}f", payload)
