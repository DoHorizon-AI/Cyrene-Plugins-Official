"""Isolated worker for the bundled public-web document parser adapter."""

from __future__ import annotations

import json
import os
import stat
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

_FIELDS = frozenset(
    {
        "source_path",
        "payload_path",
        "blocks_path",
        "result_path",
        "source_ref",
        "source_digest",
        "filename",
        "media_type",
    }
)


def main() -> int:
    """Run one closed parser request and write a bounded summary receipt."""

    if len(sys.argv) != 2:
        return 2
    request_path = Path(sys.argv[1])
    if not request_path.is_absolute() or request_path.is_symlink():
        return 2
    try:
        request = _read_request(request_path)
        from document_parser import parse_document

        with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink):
            summary = parse_document(
                source_path=Path(request["source_path"]),
                payload_path=Path(request["payload_path"]),
                blocks_path=Path(request["blocks_path"]),
                result_path=Path(request["result_path"]),
                source_ref=request["source_ref"],
                source_digest=request["source_digest"],
                filename=request["filename"],
                media_type=request["media_type"],
            )
        if not isinstance(summary, dict):
            return 3
        summary_path = request_path.parent / "summary.json"
        descriptor = os.open(
            summary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(summary, stream, ensure_ascii=False, separators=(",", ":"))
        return 0
    except Exception:
        return 1


def _read_request(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024:
            raise ValueError("parser request is not a bounded regular file")
        raw = stream.read(16 * 1024 + 1)
    if len(raw.encode("utf-8")) > 16 * 1024:
        raise ValueError("parser request exceeds its size limit")
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("parser request shape is invalid")
    parent = path.parent.resolve()
    for name in ("source_path", "payload_path", "blocks_path", "result_path"):
        candidate = Path(value[name])
        if not candidate.is_absolute() or candidate.parent.resolve() != parent:
            raise ValueError("parser path is outside the private request directory")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate parser request field")
        result[key] = value
    return result


if __name__ == "__main__":
    raise SystemExit(main())
