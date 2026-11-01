"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 library.py                                                       │
│  Module: cyrene_media.library                                       │
│  Role: Persist, validate, and render content-addressed media assets. │
│                                                                      │
│  模块职责：持久化、校验并渲染按内容寻址的媒体制品。                   │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_ASSET_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_PAGE_SIZE = 100
MAX_TAGS = 32
_ALLOWED_FORMATS = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}
_ASSET_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_THEMES = {
    "light": ("#f7f5ef", "#1e293b", "#b45309"),
    "dark": ("#111827", "#f9fafb", "#38bdf8"),
    "warm": ("#fff7ed", "#431407", "#c2410c"),
}
_CJK_FONT_PATHS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/ubuntu/UbuntuSans[wdth,wght].ttf",
    "C:/Windows/Fonts/arial.ttf",
)


class MediaError(RuntimeError):
    """Sanitized media operation failure with a stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """Public metadata for one catalog asset; content is returned separately."""

    asset_id: str
    digest: str
    mime_type: str
    byte_size: int
    title: str
    alt_text: str
    tags: tuple[str, ...]
    width: int
    height: int
    created_at: int
    deleted_at: int | None

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-compatible metadata without filesystem paths."""

        return {
            "asset_id": self.asset_id,
            "digest": self.digest,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "title": self.title,
            "alt_text": self.alt_text,
            "tags": list(self.tags),
            "width": self.width,
            "height": self.height,
            "created_at": self.created_at,
            "deleted_at": self.deleted_at,
        }


class MediaLibrary:
    """Own a bounded SQLite catalog and hash-verified image blob directory.

    The root is selected by trusted service configuration, never request data.
    A content digest names one immutable blob while each catalog row retains its
    own metadata and deletion lifecycle.
    中文：状态目录只来自受信任配置；不同资产可共享不可变内容 blob。
    """

    def __init__(self, root: str | Path, *, max_asset_bytes: int = MAX_ASSET_BYTES):
        if not 1 <= max_asset_bytes <= MAX_ASSET_BYTES:
            raise ValueError("max_asset_bytes is outside the supported range")
        self.root = Path(root).expanduser().resolve()
        self.max_asset_bytes = max_asset_bytes
        self.blob_root = self.root / "blobs"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blob_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root / "media.sqlite3"
        self._initialize()

    def put(
        self,
        data: bytes,
        *,
        title: str,
        alt_text: str = "",
        tags: tuple[str, ...] | list[str] = (),
        expected_mime_type: str | None = None,
    ) -> MediaAsset:
        """Validate and persist one image, reusing an existing live blob entry."""

        if not isinstance(data, bytes) or not data:
            raise MediaError("INVALID_INPUT")
        if len(data) > self.max_asset_bytes:
            raise MediaError("QUOTA_EXCEEDED")
        clean_title = _clean_text(title, max_length=160, required=True)
        clean_alt = _clean_text(alt_text, max_length=1000, required=False)
        clean_tags = _clean_tags(tags)
        mime_type, width, height = _validate_image(data, self.max_asset_bytes)
        if expected_mime_type and expected_mime_type.strip().lower() != mime_type:
            raise MediaError("UNSUPPORTED_MEDIA")

        digest = hashlib.sha256(data).hexdigest()
        created_at = int(time.time())
        asset_id = str(uuid.uuid4())
        blob_path = self._blob_path(digest)
        self._write_blob_if_missing(blob_path, data)
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    """SELECT a.* FROM assets a
                       WHERE a.digest = ? AND a.deleted_at IS NULL
                       ORDER BY a.created_at, a.asset_id LIMIT 1""",
                    (digest,),
                ).fetchone()
                if existing is not None:
                    self._verify_blob(digest, blob_path, expected_size=len(data))
                    return _asset_from_row(existing)
                connection.execute(
                    """INSERT INTO assets (
                         asset_id, digest, mime_type, byte_size, title, alt_text,
                         tags_json, width, height, created_at, deleted_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (
                        asset_id,
                        digest,
                        mime_type,
                        len(data),
                        clean_title,
                        clean_alt,
                        json.dumps(
                            clean_tags, ensure_ascii=False, separators=(",", ":")
                        ),
                        width,
                        height,
                        created_at,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
                ).fetchone()
                if row is None:
                    raise MediaError("STORAGE_FAILURE")
                return _asset_from_row(row)
        except sqlite3.Error as error:
            raise MediaError("STORAGE_FAILURE") from error

    def get(self, asset_id: str) -> tuple[MediaAsset, bytes]:
        """Return one live asset only after its blob digest and image are verified."""

        row = self._get_row(asset_id, include_deleted=False)
        asset = _asset_from_row(row)
        content = self._read_verified_blob(asset)
        mime_type, width, height = _validate_image(content, self.max_asset_bytes)
        if (
            mime_type != asset.mime_type
            or width != asset.width
            or height != asset.height
        ):
            raise MediaError("INTEGRITY_FAILURE")
        return asset, content

    def validate(self, asset_id: str) -> dict[str, Any]:
        """Recompute content integrity and report image metadata validation."""

        asset, content = self.get(asset_id)
        mime_type, width, height = _validate_image(content, self.max_asset_bytes)
        return {
            "valid": True,
            "asset_id": asset.asset_id,
            "digest": asset.digest,
            "mime_type": mime_type,
            "width": width,
            "height": height,
        }

    def delete(self, asset_id: str) -> bool:
        """Soft-delete one live catalog row while retaining the shared blob."""

        _require_asset_id(asset_id)
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """UPDATE assets SET deleted_at = ?
                       WHERE asset_id = ? AND deleted_at IS NULL""",
                    (int(time.time()), asset_id),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as error:
            raise MediaError("STORAGE_FAILURE") from error

    def restore(self, asset_id: str) -> MediaAsset:
        """Restore a soft-deleted row only after its original blob verifies."""

        row = self._get_row(asset_id, include_deleted=True)
        asset = _asset_from_row(row)
        blob_path = self._blob_path(asset.digest)
        self._verify_blob(asset.digest, blob_path, expected_size=asset.byte_size)
        try:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE assets SET deleted_at = NULL WHERE asset_id = ?",
                    (asset_id,),
                )
                restored = connection.execute(
                    "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
                ).fetchone()
                if restored is None:
                    raise MediaError("NOT_FOUND")
                return _asset_from_row(restored)
        except sqlite3.Error as error:
            raise MediaError("STORAGE_FAILURE") from error

    def list_assets(
        self,
        *,
        query: str = "",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one bounded page of live assets using an opaque stable cursor."""

        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise MediaError("INVALID_INPUT")
        clean_query = _clean_text(query, max_length=128, required=False)
        cursor_value = _decode_cursor(cursor) if cursor else None
        clauses = ["deleted_at IS NULL"]
        values: list[Any] = []
        if clean_query:
            escaped = (
                clean_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            term = f"%{escaped}%"
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR alt_text LIKE ? ESCAPE '\\' "
                "OR tags_json LIKE ? ESCAPE '\\')"
            )
            values.extend((term, term, term))
        if cursor_value:
            clauses.append("(created_at < ? OR (created_at = ? AND asset_id < ?))")
            values.extend((cursor_value[0], cursor_value[0], cursor_value[1]))
        sql = (
            "SELECT * FROM assets WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, asset_id DESC LIMIT ?"
        )
        values.append(limit + 1)
        try:
            with self._connect() as connection:
                rows = connection.execute(sql, values).fetchall()
        except sqlite3.Error as error:
            raise MediaError("STORAGE_FAILURE") from error
        has_more = len(rows) > limit
        selected = rows[:limit]
        next_cursor = None
        if has_more and selected:
            last = selected[-1]
            next_cursor = _encode_cursor(int(last["created_at"]), str(last["asset_id"]))
        return {
            "items": [_asset_from_row(row).as_dict() for row in selected],
            "next_cursor": next_cursor,
        }

    def render_card(
        self,
        *,
        title: str,
        body: str,
        theme: str = "light",
    ) -> tuple[MediaAsset, bytes]:
        """Render a deterministic bounded PNG card and store it as a catalog asset."""

        clean_title = _clean_text(title, max_length=120, required=True)
        clean_body = _clean_text(body, max_length=1200, required=True)
        colors = _THEMES.get(theme)
        if colors is None:
            raise MediaError("INVALID_INPUT")
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as error:
            raise MediaError("RENDERER_UNAVAILABLE") from error

        font_path = _font_path(require_cjk=_contains_cjk(clean_title + clean_body))
        try:
            title_font = ImageFont.truetype(font_path, 44)
            body_font = ImageFont.truetype(font_path, 27)
        except (OSError, UnboundLocalError) as error:
            raise MediaError("FONT_UNAVAILABLE") from error
        image = Image.new("RGB", (1200, 630), colors[0])
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((54, 54, 1146, 576), radius=34, fill=colors[0])
        draw.rounded_rectangle((54, 54, 70, 576), radius=8, fill=colors[2])
        try:
            title_lines = _wrap_text(draw, clean_title, title_font, 1000, 2)
            body_lines = _wrap_text(draw, clean_body, body_font, 1000, 7)
            y = 118
            for line in title_lines:
                draw.text((110, y), line, font=title_font, fill=colors[1])
                y += 58
            y += 24
            for line in body_lines:
                draw.text((110, y), line, font=body_font, fill=colors[1])
                y += 42
            image_bytes = io.BytesIO()
            image.save(image_bytes, format="PNG", optimize=True)
        except (UnicodeEncodeError, OSError, ValueError) as error:
            raise MediaError("FONT_UNAVAILABLE") from error
        data = image_bytes.getvalue()
        if len(data) > self.max_asset_bytes:
            raise MediaError("QUOTA_EXCEEDED")
        return (
            self.put(
                data,
                title=clean_title,
                alt_text=clean_title + ": " + clean_body,
                tags=("generated", "card", theme),
                expected_mime_type="image/png",
            ),
            data,
        )

    def _initialize(self) -> None:
        """Create the versioned local schema without migrating unrelated state."""

        try:
            with self._connect() as connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS assets (
                         asset_id TEXT PRIMARY KEY,
                         digest TEXT NOT NULL,
                         mime_type TEXT NOT NULL,
                         byte_size INTEGER NOT NULL,
                         title TEXT NOT NULL,
                         alt_text TEXT NOT NULL,
                         tags_json TEXT NOT NULL,
                         width INTEGER NOT NULL,
                         height INTEGER NOT NULL,
                         created_at INTEGER NOT NULL,
                         deleted_at INTEGER NULL
                       )"""
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS assets_live_created "
                    "ON assets(deleted_at, created_at DESC, asset_id DESC)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS assets_digest "
                    "ON assets(digest, deleted_at)"
                )
        except sqlite3.Error as error:
            raise MediaError("STORAGE_FAILURE") from error

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived SQLite connection with bounded lock waiting."""

        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _get_row(self, asset_id: str, *, include_deleted: bool) -> sqlite3.Row:
        """Load one row after validating the opaque UUID identifier."""

        _require_asset_id(asset_id)
        try:
            with self._connect() as connection:
                query = "SELECT * FROM assets WHERE asset_id = ?"
                if not include_deleted:
                    query += " AND deleted_at IS NULL"
                row = connection.execute(query, (asset_id,)).fetchone()
        except sqlite3.Error as error:
            raise MediaError("STORAGE_FAILURE") from error
        if row is None:
            raise MediaError("NOT_FOUND")
        return row

    def _blob_path(self, digest: str) -> Path:
        """Derive a private path only from a validated SHA-256 digest."""

        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MediaError("INTEGRITY_FAILURE")
        path = self.blob_root / digest[:2] / (digest + ".blob")
        if not path.parent.resolve().is_relative_to(self.root):
            raise MediaError("STORAGE_FAILURE")
        return path

    def _write_blob_if_missing(self, path: Path, data: bytes) -> None:
        """Atomically materialize an immutable content-addressed blob."""

        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists():
            self._verify_blob(path.stem, path, expected_size=len(data))
            return
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=".media-", delete=False
            ) as stream:
                temporary_name = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
            temporary_name = None
        except OSError as error:
            raise MediaError("STORAGE_FAILURE") from error
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    def _read_verified_blob(self, asset: MediaAsset) -> bytes:
        """Read the internally derived file and verify exact size and SHA-256."""

        path = self._blob_path(asset.digest)
        content = _read_blob_limited(
            path,
            asset.byte_size,
            self.max_asset_bytes,
        )
        self._verify_blob(
            asset.digest, path, expected_size=asset.byte_size, content=content
        )
        return content

    def _verify_blob(
        self,
        digest: str,
        path: Path,
        *,
        expected_size: int,
        content: bytes | None = None,
    ) -> None:
        """Reject missing, truncated, or tampered content-addressed files."""

        if not 0 <= expected_size <= self.max_asset_bytes:
            raise MediaError("INTEGRITY_FAILURE")
        data = (
            content
            if content is not None
            else _read_blob_limited(path, expected_size, self.max_asset_bytes)
        )
        if (
            len(data) != expected_size
            or len(data) > self.max_asset_bytes
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise MediaError("INTEGRITY_FAILURE")


def _validate_image(data: bytes, max_bytes: int) -> tuple[str, int, int]:
    """Decode supported raster formats with pixel and byte limits."""

    if len(data) > max_bytes:
        raise MediaError("QUOTA_EXCEEDED")
    try:
        from PIL import Image
    except ImportError as error:
        raise MediaError("RENDERER_UNAVAILABLE") from error
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                format_name = str(image.format or "").upper()
                if format_name not in _ALLOWED_FORMATS or getattr(
                    image, "is_animated", False
                ):
                    raise MediaError("UNSUPPORTED_MEDIA")
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                    raise MediaError("QUOTA_EXCEEDED")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except MediaError:
        raise
    except Exception as error:
        raise MediaError("UNSUPPORTED_MEDIA") from error
    return _ALLOWED_FORMATS[format_name], width, height


def _read_blob_limited(path: Path, expected_size: int, maximum_size: int) -> bytes:
    """Read one regular blob with stat checks and a hard byte-count ceiling."""

    if not 0 <= expected_size <= maximum_size <= MAX_ASSET_BYTES:
        raise MediaError("INTEGRITY_FAILURE")
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_size != expected_size
        ):
            raise MediaError("INTEGRITY_FAILURE")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size != expected_size
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise MediaError("INTEGRITY_FAILURE")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            content = stream.read(maximum_size + 1)
        if len(content) != expected_size or len(content) > maximum_size:
            raise MediaError("INTEGRITY_FAILURE")
        return content
    except MediaError:
        raise
    except OSError as error:
        raise MediaError("INTEGRITY_FAILURE") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _clean_text(value: str, *, max_length: int, required: bool) -> str:
    """Normalize bounded display text and reject invalid control characters."""

    if not isinstance(value, str):
        raise MediaError("INVALID_INPUT")
    cleaned = " ".join(value.split())
    if (required and not cleaned) or len(cleaned) > max_length:
        raise MediaError("INVALID_INPUT")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise MediaError("INVALID_INPUT")
    return cleaned


def _clean_tags(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Normalize a bounded set of descriptive tags."""

    if not isinstance(values, (tuple, list)) or len(values) > MAX_TAGS:
        raise MediaError("INVALID_INPUT")
    tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = _clean_text(value, max_length=48, required=True)
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    return tuple(tags)


def _asset_from_row(row: sqlite3.Row) -> MediaAsset:
    """Decode the canonical database row into a public metadata value."""

    try:
        raw_tags = json.loads(str(row["tags_json"]))
        if not isinstance(raw_tags, list) or not all(
            isinstance(tag, str) for tag in raw_tags
        ):
            raise ValueError("invalid tags")
        return MediaAsset(
            asset_id=str(row["asset_id"]),
            digest=str(row["digest"]),
            mime_type=str(row["mime_type"]),
            byte_size=int(row["byte_size"]),
            title=str(row["title"]),
            alt_text=str(row["alt_text"]),
            tags=tuple(raw_tags),
            width=int(row["width"]),
            height=int(row["height"]),
            created_at=int(row["created_at"]),
            deleted_at=(
                int(row["deleted_at"]) if row["deleted_at"] is not None else None
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise MediaError("STORAGE_FAILURE") from error


def _require_asset_id(asset_id: str) -> None:
    """Reject malformed identifiers before any catalog lookup."""

    if not isinstance(asset_id, str) or not _ASSET_ID.fullmatch(asset_id):
        raise MediaError("INVALID_INPUT")


def _encode_cursor(created_at: int, asset_id: str) -> str:
    """Encode stable pagination state without exposing local paths."""

    value = f"{created_at}:{asset_id}".encode("ascii")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[int, str]:
    """Parse and validate an opaque bounded page cursor."""

    if not isinstance(cursor, str) or len(cursor) > 256:
        raise MediaError("INVALID_INPUT")
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(cursor + padding).decode("ascii")
        created_text, asset_id = decoded.split(":", maxsplit=1)
        created_at = int(created_text)
        _require_asset_id(asset_id)
        if created_at < 0:
            raise ValueError("negative cursor")
        return created_at, asset_id
    except (ValueError, UnicodeError) as error:
        raise MediaError("INVALID_INPUT") from error


def _font_path(*, require_cjk: bool = False) -> str:
    """Resolve a font only from trusted process configuration or fixed OS paths."""

    configured = os.getenv("CYRENE_MEDIA_FONT")
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path)
        raise MediaError("FONT_UNAVAILABLE")
    candidates = _CJK_FONT_PATHS if require_cjk else _FONT_PATHS
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise MediaError("FONT_UNAVAILABLE")


def _contains_cjk(value: str) -> bool:
    """Detect scripts requiring a CJK-capable operator font."""

    return any(
        0x2E80 <= ord(character) <= 0xA4CF
        or 0xAC00 <= ord(character) <= 0xD7AF
        or 0xF900 <= ord(character) <= 0xFAFF
        or 0xFF00 <= ord(character) <= 0xFFEF
        for character in value
    )


def _wrap_text(
    draw: Any, text: str, font: Any, width: int, max_lines: int
) -> list[str]:
    """Wrap text by glyph width and truncate overlong card content visibly."""

    lines: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        for character in paragraph:
            candidate = current + character
            if current and draw.textbbox((0, 0), candidate, font=font)[2] > width:
                lines.append(current)
                current = character
                if len(lines) >= max_lines:
                    lines[-1] = lines[-1].rstrip() + "…"
                    return lines
            else:
                current = candidate
        if current:
            lines.append(current)
            current = ""
        if len(lines) >= max_lines:
            lines[-1] = lines[-1].rstrip() + "…"
            return lines
    if current:
        lines.append(current)
    return lines[:max_lines]
