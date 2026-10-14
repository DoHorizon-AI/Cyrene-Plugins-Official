"""Simple text, Markdown, CSV, and image parser adapters.

中文：处理纯文本、Markdown、CSV 与图片来源，并保留原始文本和定位信息。
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path
from typing import Any

from ocr_parsing import ocr_image
from parsing_types import (
    OcrConfig,
    ParseContext,
    ParserDiagnostic,
    ParseResult,
    make_block,
)
from PIL import Image, UnidentifiedImageError

MAX_SOURCE_BYTES = 128 * 1024 * 1024
_SUPPORTED_MEDIA_TYPES = {
    "text/markdown": "MARKDOWN",
    "text/plain": "TXT",
    "text/csv": "CSV",
    "image/png": "PNG",
    "image/jpeg": "JPEG",
}
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:\s+|$)(.*?)\s*(?:\r?\n)?$")
_SETEXT_HEADING = re.compile(r"^ {0,3}(?:={1,}|-{1,})\s*(?:\r?\n)?$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_INLINE_LINK = re.compile(r"!?\[[^\]]*\]\((?:\\.|[^)])*\)")
_REFERENCE_LINK = re.compile(r"(?<!!)\[[^\]]+\]\[[^\]]*\]")
_REFERENCE_IMAGE = re.compile(r"!\[[^\]]*\]\[[^\]]*\]")
_REFERENCE_DEFINITION = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S.*$")
_AUTOLINK = re.compile(r"<(?:(?:https?|mailto):)[^>]+>", re.IGNORECASE)
_HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def parse_simple(
    source_path: Path,
    context: ParseContext,
    ocr_config: OcrConfig | None = None,
) -> ParseResult:
    """Parse one UTF-8 text source or locally OCR one PNG/JPEG source.

    Text payloads retain the exact decoded source, while blocks identify their
    physical line or CSV record locations. Image sources call the local OCR
    adapter and retain its text, confidence, boxes, and diagnostics.
    中文：保留文本原文和物理行定位；图片调用本机 OCR 并保留置信度、坐标与诊断。
    """

    source_path = Path(source_path)
    source_format = _SUPPORTED_MEDIA_TYPES.get(context.media_type)
    if source_format is None:
        raise ValueError(f"unsupported simple-parser media type: {context.media_type}")
    source_bytes = _read_source(source_path, context)
    if source_format in {"PNG", "JPEG"}:
        return _parse_image(
            source_bytes, source_format, context, ocr_config or OcrConfig()
        )

    try:
        raw_text = source_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{source_format} source is not valid UTF-8 at byte offset {exc.start}; no encoding fallback was applied"
        ) from exc

    if source_format == "CSV":
        return _parse_csv(raw_text, context)
    if source_format == "MARKDOWN":
        return _parse_markdown(raw_text, context)
    return _parse_text(raw_text, context)


def _parse_text(raw_text: str, context: ParseContext) -> ParseResult:
    """Index each physical text line without normalizing its contents."""

    lines = raw_text.splitlines(keepends=True)
    blocks = [
        make_block(
            context,
            ordinal=index - 1,
            item_ref=f"line:{index}",
            kind="line",
            text=line,
            provenance=({"adapter": "utf8-line-v1"},),
        )
        for index, line in enumerate(lines, start=1)
    ]
    return ParseResult(
        payload={
            "schema_name": "cyrene.document.simple-text.v1",
            "source_format": "TXT",
            "source_digest": context.source_digest,
            "raw_text": raw_text,
            "line_count": len(lines),
        },
        blocks=blocks,
        source_format="TXT",
        conversion_profile="utf8-lines-v1",
        status="success",
    )


def _parse_markdown(raw_text: str, context: ParseContext) -> ParseResult:
    """Preserve Markdown lines, headings, code fences, and pipe tables."""

    lines = raw_text.splitlines(keepends=True)
    blocks: list[dict[str, Any]] = []
    diagnostics: list[ParserDiagnostic] = []
    unsupported: list[dict[str, Any]] = []
    section_titles: list[str] = []
    line_index = 0
    ordinal = 0
    in_fence = False
    fence_marker = ""

    while line_index < len(lines):
        line = lines[line_index]
        start = line_index
        kind = "line"
        level = 0
        heading_match = _ATX_HEADING.match(line)
        if heading_match and not in_fence:
            kind = "heading"
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            section_titles = section_titles[: level - 1]
            section_titles.append(title)
            line_index += 1
        elif _is_fence_line(line) and not in_fence:
            fence_marker = _fence_marker(line)
            in_fence = True
            kind = "code"
            line_index += 1
            while line_index < len(lines):
                current = lines[line_index]
                line_index += 1
                if _is_closing_fence(current, fence_marker):
                    in_fence = False
                    fence_marker = ""
                    break
            else:
                message = f"Markdown code fence starting at line {start + 1} has no closing fence."
                diagnostics.append(
                    ParserDiagnostic(
                        code="markdown.unclosed_code_fence",
                        message=message,
                        kind="parser",
                        locator={"line_start": start + 1, "line_end": len(lines)},
                    )
                )
        elif _is_table_start(lines, line_index) and not in_fence:
            kind = "table"
            line_index = _consume_table(lines, line_index)
        elif (
            line_index + 1 < len(lines)
            and _SETEXT_HEADING.match(lines[line_index + 1])
            and line.strip()
            and not in_fence
        ):
            kind = "heading"
            level = 1 if lines[line_index + 1].lstrip().startswith("=") else 2
            title = line.strip()
            section_titles = section_titles[: level - 1]
            section_titles.append(title)
            line_index += 2
        else:
            line_index += 1

        block_text = "".join(lines[start:line_index])
        end = line_index
        blocks.append(
            make_block(
                context,
                ordinal=ordinal,
                item_ref=f"line:{start + 1}"
                if end == start + 1
                else f"lines:{start + 1}-{end}",
                kind=kind,
                text=block_text,
                section_path=tuple(section_titles),
                tree_level=level,
                provenance=({"adapter": "markdown-source-preserving-v1"},),
            )
        )
        ordinal += 1

        if kind != "code":
            diagnostics_for_line, unsupported_for_line = _markdown_references(
                block_text,
                start_line=start + 1,
                end_line=end,
                ordinal=ordinal,
            )
            diagnostics.extend(diagnostics_for_line)
            unsupported.extend(unsupported_for_line)

    return ParseResult(
        payload={
            "schema_name": "cyrene.document.markdown.v1",
            "source_format": "MARKDOWN",
            "source_digest": context.source_digest,
            "raw_text": raw_text,
            "line_count": len(lines),
        },
        blocks=blocks,
        source_format="MARKDOWN",
        conversion_profile="markdown-source-preserving-v1",
        status="partial_success" if diagnostics else "success",
        warnings=list(dict.fromkeys(item.message for item in diagnostics)),
        diagnostics=diagnostics,
        unsupported_content=unsupported,
    )


def _markdown_references(
    block_text: str,
    *,
    start_line: int,
    end_line: int,
    ordinal: int,
) -> tuple[list[ParserDiagnostic], list[dict[str, Any]]]:
    """Report link and image syntax while preserving it verbatim in blocks."""

    diagnostics: list[ParserDiagnostic] = []
    unsupported: list[dict[str, Any]] = []
    matches: list[tuple[str, str]] = []
    for pattern, label in (
        (_INLINE_LINK, "Markdown link or image"),
        (_REFERENCE_LINK, "Markdown reference link"),
        (_REFERENCE_IMAGE, "Markdown reference image"),
        (_REFERENCE_DEFINITION, "Markdown link definition"),
        (_AUTOLINK, "Markdown autolink"),
        (_HTML_IMAGE, "HTML image in Markdown"),
    ):
        for match in pattern.finditer(block_text):
            matches.append((match.group(0), label))

    for match_index, (raw_syntax, label) in enumerate(matches, start=1):
        message = f"{label} content was preserved as source text; its target was not fetched or extracted."
        locator = {
            "line_start": start_line,
            "line_end": end_line,
            "item_ref": f"markdown-reference:{ordinal}:{match_index}",
        }
        diagnostics.append(
            ParserDiagnostic(
                code="markdown.reference_not_extracted",
                message=message,
                kind="parser",
                locator=locator,
            )
        )
        unsupported.append(
            {
                "kind": "markdown_reference",
                "code": "markdown.reference_not_extracted",
                "raw_syntax": raw_syntax,
                "message": message,
                "locator": locator,
            }
        )
    return diagnostics, unsupported


def _parse_csv(raw_text: str, context: ParseContext) -> ParseResult:
    """Parse CSV records with Python's quoted/multiline-aware CSV reader."""

    source_lines = raw_text.splitlines(keepends=True)
    stream = io.StringIO(raw_text, newline="")
    reader = csv.reader(stream, strict=True)
    parsed_rows: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    diagnostics: list[ParserDiagnostic] = []
    unsupported: list[dict[str, Any]] = []
    previous_line = 0
    header: list[str] | None = None
    failed = False

    while True:
        row_start = previous_line + 1
        try:
            values = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            failed = True
            row_end = max(reader.line_num, row_start)
            raw_record = "".join(source_lines[previous_line:row_end])
            locator = {
                "line_start": row_start,
                "line_end": min(row_end, len(source_lines)),
            }
            message = f"Malformed CSV record at physical line {row_start} ({type(exc).__name__})."
            diagnostics.append(
                ParserDiagnostic(
                    code="csv.malformed_record",
                    message=message,
                    kind="parser",
                    locator=locator,
                )
            )
            unsupported.append(
                {
                    "kind": "csv_record",
                    "code": "csv.malformed_record",
                    "raw_text": raw_record,
                    "message": message,
                    "locator": locator,
                }
            )
            previous_line = min(row_end, len(source_lines))
            break

        row_end = reader.line_num
        raw_record = "".join(source_lines[previous_line:row_end])
        locator = {"line_start": row_start, "line_end": row_end}
        row_index = len(parsed_rows)

        if header is None:
            header = list(values)
            row_kind = "csv_header"
            fields = [
                {"name": name, "value": value}
                for name, value in zip(header, values, strict=False)
            ]
        else:
            row_kind = "csv_record"
            fields = [
                {"name": name, "value": value}
                for name, value in zip(header, values, strict=False)
            ]
            if len(values) != len(header):
                message = (
                    f"CSV record at physical line {row_start} has {len(values)} fields; "
                    f"the header has {len(header)}."
                )
                diagnostics.append(
                    ParserDiagnostic(
                        code="csv.field_count_mismatch",
                        message=message,
                        kind="parser",
                        locator=locator,
                    )
                )
                unsupported.append(
                    {
                        "kind": "csv_record",
                        "code": "csv.field_count_mismatch",
                        "raw_text": raw_record,
                        "message": message,
                        "locator": locator,
                        "expected_fields": len(header),
                        "actual_fields": len(values),
                    }
                )

        parsed_rows.append(
            {
                "row_number": row_index + 1,
                "values": list(values),
                "fields": fields,
                "locator": locator,
                "raw_text": raw_record,
            }
        )
        blocks.append(
            make_block(
                context,
                ordinal=len(blocks),
                item_ref=f"csv-record:{row_index + 1}:lines:{row_start}-{row_end}",
                kind=row_kind,
                text=raw_record,
                provenance=({"adapter": "python-csv-strict-v1"},),
            )
        )
        previous_line = row_end

    unparsed_tail = "".join(source_lines[previous_line:]) if failed else ""
    if unparsed_tail:
        locator = {"line_start": previous_line + 1, "line_end": len(source_lines)}
        message = "CSV content after the malformed record remains unparsed and is preserved verbatim."
        unsupported.append(
            {
                "kind": "unparsed_csv_content",
                "code": "csv.unparsed_tail",
                "raw_text": unparsed_tail,
                "message": message,
                "locator": locator,
            }
        )
        diagnostics.append(
            ParserDiagnostic(
                code="csv.unparsed_tail",
                message=message,
                kind="parser",
                locator=locator,
            )
        )

    if not parsed_rows and not raw_text:
        header = []
    records = parsed_rows[1:] if parsed_rows else []
    return ParseResult(
        payload={
            "schema_name": "cyrene.document.csv.v1",
            "source_format": "CSV",
            "source_digest": context.source_digest,
            "raw_text": raw_text,
            "header": header or [],
            "records": records,
            "record_count": len(records),
        },
        blocks=blocks,
        source_format="CSV",
        conversion_profile="python-csv-strict-v1",
        status="partial_success" if diagnostics else "success",
        warnings=list(dict.fromkeys(item.message for item in diagnostics)),
        diagnostics=diagnostics,
        unsupported_content=unsupported,
    )


def _parse_image(
    source_bytes: bytes,
    source_format: str,
    context: ParseContext,
    ocr_config: OcrConfig,
) -> ParseResult:
    """Decode PNG/JPEG metadata and preserve local OCR line evidence."""

    expected_format = source_format
    try:
        image = Image.open(io.BytesIO(source_bytes))
        image.load()
    except (OSError, UnidentifiedImageError) as exc:
        message = f"{source_format} image could not be decoded ({type(exc).__name__}); source bytes remain unchanged."
        diagnostic = ParserDiagnostic(
            code="image.decode_failed", message=message, kind="parser"
        )
        return ParseResult(
            payload={
                "schema_name": "cyrene.document.image.v1",
                "source_format": source_format,
                "source_digest": context.source_digest,
                "decode_status": "failed",
            },
            blocks=[],
            source_format=source_format,
            conversion_profile="local-tesseract-tsv-v1",
            status="partial_success",
            page_count=None,
            warnings=[message],
            diagnostics=[diagnostic],
            unsupported_content=[
                {
                    "kind": "image_content",
                    "code": "image.decode_failed",
                    "message": message,
                    "locator": {
                        "item_ref": "image",
                        "source_basename": _safe_basename(context.filename),
                    },
                }
            ],
        )

    if image.format != expected_format:
        image.close()
        raise ValueError(
            f"image media type {context.media_type} does not match detected {image.format or 'unknown'} format"
        )

    width, height = image.size
    image_format = image.format
    image_mode = image.mode
    try:
        ocr_result = ocr_image(image, context=context, page_number=1, config=ocr_config)
    finally:
        image.close()

    blocks: list[dict[str, Any]] = []
    for index, region in enumerate(ocr_result.regions, start=1):
        blocks.append(
            make_block(
                context,
                ordinal=index - 1,
                item_ref=f"image:line:{index}",
                kind="ocr_text",
                text=region.text,
                source_pages=(1,),
                provenance=(
                    {
                        "adapter": "tesseract-tsv-v1",
                        "engine": ocr_result.engine,
                        "languages": list(ocr_result.languages),
                        "bbox": dict(region.bbox),
                        "confidence": region.confidence,
                    },
                ),
            )
        )

    diagnostics = list(ocr_result.diagnostics)
    unsupported: list[dict[str, Any]] = []
    if not ocr_result.text.strip():
        message = (
            "The image has no extracted text; OCR may be unavailable or the image may contain graphics only. "
            "The source image content was not interpreted."
        )
        if not any(item.code == "ocr.no_text_detected" for item in diagnostics):
            diagnostics.append(
                ParserDiagnostic(
                    code="image.no_extracted_text",
                    message=message,
                    kind="ocr",
                    locator={
                        "page_number": 1,
                        "item_ref": "image",
                        "source_basename": _safe_basename(context.filename),
                    },
                )
            )
        unsupported.append(
            {
                "kind": "image_content",
                "code": "image.no_extracted_text",
                "message": message,
                "locator": {
                    "page_number": 1,
                    "item_ref": "image",
                    "source_basename": _safe_basename(context.filename),
                },
            }
        )

    return ParseResult(
        payload={
            "schema_name": "cyrene.document.image.v1",
            "source_format": source_format,
            "source_digest": context.source_digest,
            "image": {
                "format": image_format,
                "width": width,
                "height": height,
                "mode": image_mode,
            },
            "ocr": {
                "engine": ocr_result.engine,
                "languages": list(ocr_result.languages),
                "text": ocr_result.text,
                "confidence": ocr_result.confidence,
                "regions": [
                    {
                        "text": region.text,
                        "bbox": dict(region.bbox),
                        "confidence": region.confidence,
                    }
                    for region in ocr_result.regions
                ],
            },
        },
        blocks=blocks,
        source_format=source_format,
        conversion_profile="local-tesseract-tsv-v1",
        status="partial_success" if diagnostics or unsupported else "success",
        page_count=1,
        warnings=list(dict.fromkeys(item.message for item in diagnostics)),
        diagnostics=diagnostics,
        unsupported_content=unsupported,
    )


def _read_source(source_path: Path, context: ParseContext) -> bytes:
    """Read a bounded regular file and verify its staged source digest."""

    if not source_path.is_file():
        raise ValueError("source_path must identify a regular file")
    size = source_path.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise ValueError(f"source exceeds the {MAX_SOURCE_BYTES}-byte parser limit")
    source_bytes = source_path.read_bytes()
    digest = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
    if digest != context.source_digest:
        raise ValueError("source_digest does not match the staged source bytes")
    return source_bytes


def _safe_basename(filename: str) -> str:
    """Return only the final path component for public diagnostic locators."""

    return filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]


def _is_fence_line(line: str) -> bool:
    return bool(re.match(r"^ {0,3}(`{3,}|~{3,})", line))


def _fence_marker(line: str) -> str:
    match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
    return match.group(1) if match else "````"


def _is_closing_fence(line: str, marker: str) -> bool:
    character = marker[0]
    minimum_length = len(marker)
    return bool(
        re.match(
            rf"^ {{0,3}}{re.escape(character)}{{{minimum_length},}}\s*(?:\r?\n)?$", line
        )
    )


def _is_table_start(lines: list[str], index: int) -> bool:
    if index >= len(lines):
        return False
    if _TABLE_SEPARATOR.match(lines[index]):
        return True
    return (
        index + 1 < len(lines) and _TABLE_SEPARATOR.match(lines[index + 1]) is not None
    )


def _consume_table(lines: list[str], index: int) -> int:
    """Return the exclusive end of a contiguous pipe table."""

    end = index
    while end < len(lines):
        line = lines[end]
        if not line.strip() or "|" not in line:
            break
        end += 1
    return max(index + 1, end)
