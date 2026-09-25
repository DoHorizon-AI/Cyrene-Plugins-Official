"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 qqnt_direct_protocol.py                                         │
│  Module: qq_connector.qqnt_direct_protocol                           │
│  Role: Cyrene-owned length-delimited stdio protocol primitives.      │
│                                                                     │
│  模块职责：实现 QQNT direct Host 的长度分帧与 JSON 协议基础。          │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from typing import Any, BinaryIO

MAX_FRAME_BYTES = 8 * 1024 * 1024
FRAME_HEADER_BYTES = 4


class QQHostProtocolError(ValueError):
    """Raised when a QQ Host frame is malformed or exceeds its limits.

        中文:当 QQ Host 帧格式错误或超出限制时引发。"""


def encode_frame(message: dict[str, Any]) -> bytes:
    """Encode one JSON object as a big-endian length-prefixed frame.

    Args:
        message: Protocol object that will be encoded as UTF-8 JSON.
    Returns:
        A four-byte length header followed by the JSON payload.
    Raises:
        QQHostProtocolError: If the value is not an object or is too large.

        中文:将一个 JSON 对象编码为带大端长度前缀的帧。

        参数:
            message:将编码为 UTF-8 JSON 的协议对象。
        返回:
            四字节长度标头,后接 JSON 载荷。
        引发:
            QQHostProtocolError:当值不是对象或数据过大时。
    """

    if not isinstance(message, dict):
        raise QQHostProtocolError("protocol message must be an object")
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise QQHostProtocolError("protocol message is not JSON serializable") from exc
    if len(payload) > MAX_FRAME_BYTES:
        raise QQHostProtocolError("protocol message exceeds the frame limit")
    return len(payload).to_bytes(FRAME_HEADER_BYTES, "big") + payload


def write_frame(stream: BinaryIO, message: dict[str, Any]) -> None:
    """Write one complete frame to a binary stdio stream and flush it.

        中文:将一个完整帧写入二进制标准输入输出流并刷新。"""

    frame = encode_frame(message)
    remaining = memoryview(frame)
    while remaining:
        written = stream.write(remaining)
        if written is None or written <= 0:
            raise QQHostProtocolError("stdio stream did not accept the frame")
        remaining = remaining[written:]
    stream.flush()


def _read_exact(stream: BinaryIO, size: int) -> bytes | None:
    """Read exactly ``size`` bytes, distinguishing clean EOF from truncation.

        中文:恰好读取 ``size`` 个字节,并区分正常 EOF 与数据截断。"""

    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if not chunks:
                return None
            raise QQHostProtocolError("truncated stdio frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    """Read and validate one length-prefixed JSON object from stdio.

    A clean EOF returns ``None``. Partial headers, invalid UTF-8, malformed JSON,
    and non-object JSON values fail closed with :class:`QQHostProtocolError`.

        中文:从标准输入输出流读取并验证一个带长度前缀的 JSON 对象。

        中文：正常 EOF 会返回 ``None``。部分标头、无效 UTF-8、格式错误的 JSON,
        以及非对象 JSON 值都会触发 :class:`QQHostProtocolError`,并按失败即拒绝处理。
    """

    raw_length = _read_exact(stream, FRAME_HEADER_BYTES)
    if raw_length is None:
        return None
    frame_length = int.from_bytes(raw_length, "big")
    if frame_length <= 0 or frame_length > MAX_FRAME_BYTES:
        raise QQHostProtocolError("invalid or oversized frame length")
    payload = _read_exact(stream, frame_length)
    if payload is None:
        raise QQHostProtocolError("missing frame payload")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QQHostProtocolError("frame payload is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise QQHostProtocolError("frame payload must be a JSON object")
    return decoded
