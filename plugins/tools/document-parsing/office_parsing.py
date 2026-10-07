"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 office_parsing.py                                               │
│  Module: office_parsing                                             │
│  Role: Preserve located text and structure from PPTX and XLSX files.│
│                                                                     │
│  模块职责：提取 PPTX/XLSX 文字与结构，并显式报告无法语义解析的内容。    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from parsing_types import ParseContext, ParserDiagnostic, ParseResult, make_block

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PPTX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_XLSX_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_SUPPORTED_PURPOSES = {"pptx": _PPTX_MEDIA_TYPE, "xlsx": _XLSX_MEDIA_TYPE}


def parse_office(source_path: Path, context: ParseContext) -> ParseResult:
    """Extract one PPTX or XLSX source without writing artifacts.

    The caller owns staging, persistence, and receipts. This adapter validates the
    source identity, then returns a JSON-safe payload and located text blocks.
    中文：调用方负责 staging 与持久化；本适配器只返回可序列化解析结果。

    Args:
        source_path: Private staged path to a source file.
        context: Frozen source reference, digest, filename, and media type.
    Returns:
        A shared ``ParseResult`` containing the complete Office projection.
    Raises:
        ValueError: If the extension, media type, path, or source digest is invalid.
    """

    source_path = Path(source_path)
    if not source_path.is_file():
        raise ValueError("source_path must point to a regular file")
    extension = source_path.suffix.lower().lstrip(".")
    expected_media_type = _SUPPORTED_PURPOSES.get(extension)
    if expected_media_type is None:
        raise ValueError("only .pptx and .xlsx Office files are supported")
    if context.media_type != expected_media_type:
        raise ValueError(f"media_type does not match .{extension} source format")
    _validate_source_identity(source_path, context)

    if extension == "pptx":
        return _parse_pptx(source_path, context)
    return _parse_xlsx(source_path, context)


def _validate_source_identity(source_path: Path, context: ParseContext) -> None:
    """Check the caller's immutable source identity against the staged bytes."""

    if not context.source_ref.strip() or not context.filename.strip():
        raise ValueError("source_ref and filename must be non-empty")
    if not _SHA256_RE.fullmatch(context.source_digest):
        raise ValueError("source_digest must use sha256:<64 lowercase hex> format")
    actual_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if context.source_digest != f"sha256:{actual_digest}":
        raise ValueError("source_digest does not match the staged source bytes")


def _parse_pptx(source_path: Path, context: ParseContext) -> ParseResult:
    """Extract slide text, notes, tables, shape geometry, and unsupported parts."""

    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    try:
        presentation = Presentation(str(source_path))
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        raise ValueError(f"invalid PPTX source: {exc}") from exc

    slide_size = {
        "width": int(presentation.slide_width),
        "height": int(presentation.slide_height),
        "unit": "EMU",
    }
    slide_records: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    diagnostics: list[ParserDiagnostic] = []
    unsupported: list[dict[str, Any]] = []
    warnings: list[str] = []

    for slide_ordinal, slide in enumerate(presentation.slides, start=1):
        slide_name = _pptx_slide_name(slide, slide_ordinal)
        title = _pptx_title(slide)
        notes = _pptx_notes(slide)
        shapes: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = [
            {
                "type": "slide",
                "slideOrdinal": slide_ordinal,
                "slideName": slide_name,
                "title": title,
                "notes": notes,
                "slideSizeEMU": dict(slide_size),
            }
        ]
        text_segments: list[str] = []
        has_table_text = False
        has_non_table_text = False

        for shape in slide.shapes:
            record, shape_text, shape_unsupported = _pptx_shape_record(
                shape,
                slide_ordinal=slide_ordinal,
                shape_types=MSO_SHAPE_TYPE,
                parent_ref=None,
            )
            shapes.append(record)
            provenance.extend(_shape_provenance(record, slide_ordinal))
            provenance.extend(
                {"type": "unsupportedContent", **entry} for entry in shape_unsupported
            )
            text_segments.extend(shape_text)
            has_table_text = has_table_text or bool(record.get("table"))
            has_non_table_text = (
                has_non_table_text or bool(shape_text) and not bool(record.get("table"))
            )
            for entry in shape_unsupported:
                unsupported.append(entry)
                diagnostic = _unsupported_diagnostic(entry)
                diagnostics.append(diagnostic)
                warnings.append(diagnostic.message)

        slide_record = {
            "ordinal": slide_ordinal,
            "name": slide_name,
            "title": title,
            "notes": notes,
            "slideSizeEMU": dict(slide_size),
            "shapes": shapes,
        }
        slide_records.append(slide_record)

        for unsupported_kind in _pptx_unsupported_slide_parts(source_path, slide):
            if unsupported_kind:
                entry = {
                    "kind": unsupported_kind,
                    "message": (
                        "Slide animation/timing metadata is retained in the source "
                        "but is not interpreted by this text parser."
                        if unsupported_kind == "animation"
                        else "Slide transition metadata is retained in the source "
                        "but is not interpreted by this text parser."
                    ),
                    "locator": {
                        "slideOrdinal": slide_ordinal,
                        "slideName": slide_name,
                    },
                }
                unsupported.append(entry)
                provenance.append({"type": "unsupportedContent", **entry})
                diagnostic = _unsupported_diagnostic(entry)
                diagnostics.append(diagnostic)
                warnings.append(diagnostic.message)

        if notes:
            text_segments.append(f"Notes: {notes}")
        slide_text = "\n".join(text for text in text_segments if text.strip())
        kind = (
            "text"
            if has_non_table_text or notes
            else ("table" if has_table_text else "other")
        )
        blocks.append(
            make_block(
                context,
                ordinal=len(blocks),
                item_ref=f"pptx:slide:{slide_ordinal}",
                kind=kind,
                text=slide_text,
                section_path=(slide_name,),
                provenance=provenance,
            )
        )

    payload = {
        "schemaVersion": 1,
        "sourceRef": context.source_ref,
        "sourceDigest": context.source_digest,
        "filename": Path(context.filename).name,
        "mediaType": context.media_type,
        "sourceFormat": "PPTX",
        "conversionProfile": "python-pptx-structure-v1",
        "slideSizeEMU": slide_size,
        "slides": slide_records,
        "unsupportedContent": unsupported,
    }
    return ParseResult(
        payload=payload,
        blocks=blocks,
        source_format="PPTX",
        conversion_profile="python-pptx-structure-v1",
        status="partial_success" if unsupported else "success",
        page_count=None,
        slide_count=len(slide_records),
        sheet_count=None,
        warnings=warnings,
        diagnostics=diagnostics,
        unsupported_content=unsupported,
    )


def _pptx_unsupported_slide_parts(source_path: Path, slide: Any) -> list[str]:
    """Inspect original slide XML for animation and transition metadata.

    python-pptx does not preserve unsupported XML extensions in its object tree,
    so these two known tags are read directly from the ZIP package.
    """

    part_name = str(slide.part.partname).lstrip("/")
    try:
        with zipfile.ZipFile(source_path, "r") as package:
            root = ElementTree.fromstring(package.read(part_name))
    except (KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError):
        return []
    found = {element.tag.rsplit("}", 1)[-1] for element in root.iter()}
    unsupported: list[str] = []
    if "timing" in found:
        unsupported.append("animation")
    if "transition" in found:
        unsupported.append("slide_transition")
    return unsupported


def _pptx_slide_name(slide: Any, ordinal: int) -> str:
    """Return the source slide name or a stable display fallback."""

    raw_name = slide._element.cSld.get("name")
    if isinstance(raw_name, str) and raw_name.strip():
        return raw_name.strip()
    return f"Slide {ordinal}"


def _pptx_title(slide: Any) -> str | None:
    """Return actual text from the slide title placeholder, when present."""

    title_shape = slide.shapes.title
    if title_shape is None:
        return None
    title = _plain_text(getattr(title_shape, "text", ""))
    return title or None


def _pptx_notes(slide: Any) -> str | None:
    """Return notes text without including generated slide-number placeholders."""

    try:
        notes_frame = slide.notes_slide.notes_text_frame
    except (AttributeError, KeyError, ValueError):
        return None
    if notes_frame is None:
        return None
    notes = _plain_text(notes_frame.text)
    return notes or None


def _pptx_shape_record(
    shape: Any,
    *,
    slide_ordinal: int,
    shape_types: Any,
    parent_ref: str | None,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """Serialize one shape recursively and report media the parser cannot read."""

    shape_id = int(shape.shape_id)
    shape_ref = f"slide:{slide_ordinal}:shape:{shape_id}"
    position = {
        "left": int(getattr(shape, "left", 0)),
        "top": int(getattr(shape, "top", 0)),
        "width": int(getattr(shape, "width", 0)),
        "height": int(getattr(shape, "height", 0)),
        "unit": "EMU",
    }
    shape_type_value = getattr(shape, "shape_type", None)
    shape_type_name = getattr(shape_type_value, "name", str(shape_type_value))
    record: dict[str, Any] = {
        "shapeId": shape_id,
        "shapeType": shape_type_name,
        "name": str(getattr(shape, "name", "")),
        "positionEMU": position,
        "parentRef": parent_ref,
    }
    text_segments: list[str] = []
    unsupported: list[dict[str, Any]] = []

    if bool(getattr(shape, "has_table", False)):
        table, table_text = _pptx_table(shape.table)
        record["contentState"] = "table"
        record["table"] = table
        if table_text:
            text_segments.append(table_text)
    elif bool(getattr(shape, "has_text_frame", False)):
        text = _plain_text(shape.text)
        record["text"] = text
        record["contentState"] = "text" if text.strip() else "empty"
        if text.strip():
            text_segments.append(text)
    else:
        record["contentState"] = "non_text"

    is_group = shape_type_value == shape_types.GROUP
    if is_group and hasattr(shape, "shapes"):
        child_records: list[dict[str, Any]] = []
        for child in shape.shapes:
            child_record, child_text, child_unsupported = _pptx_shape_record(
                child,
                slide_ordinal=slide_ordinal,
                shape_types=shape_types,
                parent_ref=shape_ref,
            )
            child_records.append(child_record)
            text_segments.extend(child_text)
            unsupported.extend(child_unsupported)
        record["children"] = child_records

    unsupported_kind: str | None = None
    if (
        bool(getattr(shape, "has_chart", False))
        or shape_type_value == shape_types.CHART
    ):
        unsupported_kind = "chart"
    elif shape_type_value in {shape_types.PICTURE, shape_types.LINKED_PICTURE}:
        unsupported_kind = "picture"
    elif shape_type_value in {
        shape_types.EMBEDDED_OLE_OBJECT,
        shape_types.LINKED_OLE_OBJECT,
    }:
        unsupported_kind = "embedded_object"
    elif shape_type_value == shape_types.MEDIA:
        unsupported_kind = "media"
    elif shape_type_name in {"DIAGRAM", "IGX_GRAPHIC", "WEB_VIDEO"}:
        unsupported_kind = "embedded_graphic"

    if unsupported_kind:
        record["unsupportedKind"] = unsupported_kind
        unsupported.append(
            {
                "kind": unsupported_kind,
                "message": (
                    f"PPTX {unsupported_kind.replace('_', ' ')} content is preserved "
                    "as a located reference but is not semantically extracted."
                ),
                "locator": {
                    "slideOrdinal": slide_ordinal,
                    "shapeId": shape_id,
                    "shapeType": shape_type_name,
                    "positionEMU": position,
                    "itemRef": shape_ref,
                },
            }
        )
    return record, text_segments, unsupported


def _pptx_table(table: Any) -> tuple[dict[str, Any], str]:
    """Serialize row/cell text and merge semantics from a PowerPoint table."""

    rows: list[dict[str, Any]] = []
    text_rows: list[str] = []
    for row_index, row in enumerate(table.rows, start=1):
        cells: list[dict[str, Any]] = []
        values: list[str] = []
        for column_index, cell in enumerate(row.cells, start=1):
            cell_text = _plain_text(cell.text)
            cells.append(
                {
                    "row": row_index,
                    "column": column_index,
                    "text": cell_text,
                    "isMergeOrigin": bool(getattr(cell, "is_merge_origin", False)),
                    "isSpanned": bool(getattr(cell, "is_spanned", False)),
                    "rowSpan": int(getattr(cell, "span_height", 1)),
                    "columnSpan": int(getattr(cell, "span_width", 1)),
                }
            )
            values.append(cell_text)
        rows.append({"row": row_index, "cells": cells})
        text_rows.append(" | ".join(values))
    serialized = {
        "rowCount": len(rows),
        "columnCount": len(table.columns),
        "rows": rows,
    }
    return serialized, "\n".join(text_rows)


def _shape_provenance(
    shape_record: dict[str, Any], slide_ordinal: int
) -> list[dict[str, Any]]:
    """Flatten nested shapes into individually located provenance objects."""

    own_record = {
        key: value for key, value in shape_record.items() if key != "children"
    }
    provenance = [{"type": "shape", "slideOrdinal": slide_ordinal, **own_record}]
    for child in shape_record.get("children", []):
        provenance.extend(_shape_provenance(child, slide_ordinal))
    return provenance


def _parse_xlsx(source_path: Path, context: ParseContext) -> ParseResult:
    """Extract worksheet cells and workbook metadata using formula/cache views."""

    from openpyxl import load_workbook

    try:
        formula_book = load_workbook(source_path, data_only=False, read_only=False)
        cached_book = load_workbook(source_path, data_only=True, read_only=False)
    except (OSError, zipfile.BadZipFile, ValueError, KeyError) as exc:
        raise ValueError(f"invalid XLSX source: {exc}") from exc

    diagnostics: list[ParserDiagnostic] = []
    unsupported: list[dict[str, Any]] = []
    warnings: list[str] = []
    sheet_records: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    workbook_metadata = {
        "sheetOrder": list(formula_book.sheetnames),
        "activeSheet": formula_book.active.title if formula_book.active else None,
        "properties": _xlsx_properties(formula_book.properties),
        "externalLinkCount": len(getattr(formula_book, "_external_links", [])),
    }

    for sheet_ordinal, formula_sheet in enumerate(formula_book.worksheets, start=1):
        cached_sheet = cached_book[formula_sheet.title]
        item_ref = f"xlsx:sheet:{sheet_ordinal}:{formula_sheet.title}"
        cells: list[dict[str, Any]] = []
        cell_text: list[str] = []
        provenance: list[dict[str, Any]] = []

        for cell in sorted(
            formula_sheet._cells.values(), key=lambda item: (item.row, item.column)
        ):
            formula = cell.value if cell.data_type == "f" else None
            stored_value = None if formula is not None else cell.value
            if formula is None and stored_value is None:
                continue
            cached_cell = cached_sheet[cell.coordinate]
            cached_value = cached_cell.value if formula is not None else None
            cached_data_type = cached_cell.data_type if formula is not None else None
            cell_record = {
                "coordinate": cell.coordinate,
                "row": int(cell.row),
                "column": int(cell.column),
                "formula": formula,
                "storedValue": _json_value(stored_value),
                "storedDataType": cell.data_type,
                "cachedValue": _json_value(cached_value),
                "cachedDataType": cached_data_type,
                "numberFormat": cell.number_format,
            }
            cells.append(cell_record)
            provenance.append(
                {
                    "type": "cell",
                    "sheetOrdinal": sheet_ordinal,
                    "sheetName": formula_sheet.title,
                    **cell_record,
                }
            )
            if formula is not None:
                if cached_value is None:
                    message = (
                        f"Formula cache is missing for {formula_sheet.title}!"
                        f"{cell.coordinate}; formulas are preserved without calculation."
                    )
                    warnings.append(message)
                    diagnostics.append(
                        ParserDiagnostic(
                            code="XLSX_FORMULA_CACHE_MISSING",
                            message=message,
                            kind="parser",
                            locator={
                                "sheetOrdinal": sheet_ordinal,
                                "sheetName": formula_sheet.title,
                                "cell": cell.coordinate,
                                "itemRef": item_ref,
                            },
                        )
                    )
                    displayed_value = "cached value unavailable"
                else:
                    displayed_value = f"cached value {_display_value(cached_value)}"
                cell_text.append(
                    f"{cell.coordinate}: formula {formula}; {displayed_value}"
                )
            else:
                cell_text.append(f"{cell.coordinate}: {_display_value(stored_value)}")

        tables = _xlsx_tables(formula_sheet)
        merged_ranges = sorted(str(rng) for rng in formula_sheet.merged_cells.ranges)
        hidden_rows = [
            {"row": int(index), "hidden": True}
            for index, dimension in sorted(formula_sheet.row_dimensions.items())
            if dimension.hidden
        ]
        hidden_columns = [
            {
                "key": str(key),
                "min": int(dimension.min) if dimension.min is not None else None,
                "max": int(dimension.max) if dimension.max is not None else None,
                "hidden": True,
            }
            for key, dimension in sorted(formula_sheet.column_dimensions.items())
            if dimension.hidden
        ]
        sheet_record = {
            "ordinal": sheet_ordinal,
            "name": formula_sheet.title,
            "state": formula_sheet.sheet_state,
            "tables": tables,
            "cells": cells,
            "mergedRanges": merged_ranges,
            "hiddenRows": hidden_rows,
            "hiddenColumns": hidden_columns,
            "freezePanes": str(formula_sheet.freeze_panes)
            if formula_sheet.freeze_panes is not None
            else None,
            "autoFilterRef": formula_sheet.auto_filter.ref,
            "showGridLines": _xlsx_show_grid_lines(formula_sheet),
        }
        sheet_records.append(sheet_record)
        provenance.insert(
            0,
            {
                "type": "worksheet",
                "sheetOrdinal": sheet_ordinal,
                **{key: value for key, value in sheet_record.items() if key != "cells"},
            },
        )

        sheet_unsupported = _xlsx_unsupported_objects(formula_sheet, sheet_ordinal)
        for entry in sheet_unsupported:
            unsupported.append(entry)
            diagnostic = _unsupported_diagnostic(entry)
            diagnostics.append(diagnostic)
            warnings.append(diagnostic.message)
            provenance.append({"type": "unsupportedContent", **entry})

        sheet_header = (
            f"Sheet: {formula_sheet.title} (state={formula_sheet.sheet_state})"
        )
        text = "\n".join([sheet_header, *cell_text])
        blocks.append(
            make_block(
                context,
                ordinal=len(blocks),
                item_ref=item_ref,
                kind="table" if cells or tables else "other",
                text=text,
                section_path=(formula_sheet.title,),
                provenance=provenance,
            )
        )

    if workbook_metadata["externalLinkCount"]:
        message = (
            "Workbook contains external links; their targets are retained only as "
            "workbook metadata and are not resolved."
        )
        warnings.append(message)
        diagnostics.append(
            ParserDiagnostic(
                code="XLSX_EXTERNAL_LINKS_UNRESOLVED",
                message=message,
                kind="parser",
                locator={"itemRef": "xlsx:workbook"},
            )
        )

    payload = {
        "schemaVersion": 1,
        "sourceRef": context.source_ref,
        "sourceDigest": context.source_digest,
        "filename": Path(context.filename).name,
        "mediaType": context.media_type,
        "sourceFormat": "XLSX",
        "conversionProfile": "openpyxl-cells-v1",
        "workbook": workbook_metadata,
        "sheets": sheet_records,
        "unsupportedContent": unsupported,
    }
    status = "partial_success" if diagnostics or unsupported else "success"
    return ParseResult(
        payload=payload,
        blocks=blocks,
        source_format="XLSX",
        conversion_profile="openpyxl-cells-v1",
        status=status,
        page_count=None,
        slide_count=None,
        sheet_count=len(sheet_records),
        warnings=warnings,
        diagnostics=diagnostics,
        unsupported_content=unsupported,
    )


def _xlsx_properties(properties: Any) -> dict[str, Any]:
    """Keep common workbook identity fields as JSON-safe values."""

    names = (
        "title",
        "subject",
        "creator",
        "keywords",
        "description",
        "lastModifiedBy",
        "created",
        "modified",
        "category",
        "identifier",
        "language",
        "revision",
    )
    return {name: _json_value(getattr(properties, name, None)) for name in names}


def _xlsx_tables(worksheet: Any) -> list[dict[str, Any]]:
    """Serialize worksheet table names, references, and column headings."""

    result: list[dict[str, Any]] = []
    for table in worksheet.tables.values():
        columns = []
        for column in getattr(table, "tableColumns", []):
            columns.append(
                {
                    "id": int(column.id),
                    "name": str(column.name),
                    "totalsRowFunction": column.totalsRowFunction,
                }
            )
        result.append(
            {
                "name": str(table.name),
                "displayName": str(table.displayName),
                "ref": str(table.ref),
                "columns": columns,
                "headerRowCount": int(table.headerRowCount or 0),
                "totalsRowCount": int(table.totalsRowCount or 0),
            }
        )
    return result


def _xlsx_show_grid_lines(worksheet: Any) -> bool | None:
    """Return the explicit worksheet grid-line setting, if it exists."""

    sheet_view = getattr(worksheet, "sheet_view", None)
    if sheet_view is None:
        return None
    value = getattr(sheet_view, "showGridLines", None)
    return bool(value) if value is not None else None


def _xlsx_unsupported_objects(
    worksheet: Any, sheet_ordinal: int
) -> list[dict[str, Any]]:
    """Report charts and pictures by their anchor cells without rendering them."""

    unsupported: list[dict[str, Any]] = []
    for attribute, kind in (("_charts", "chart"), ("_images", "picture")):
        objects = getattr(worksheet, attribute, [])
        for index, item in enumerate(objects, start=1):
            anchor = getattr(item, "anchor", None)
            from_marker = getattr(anchor, "_from", None)
            to_marker = getattr(anchor, "to", None)
            locator = {
                "sheetOrdinal": sheet_ordinal,
                "sheetName": worksheet.title,
                "objectOrdinal": index,
                "fromCell": _xlsx_anchor_cell(from_marker),
                "toCell": _xlsx_anchor_cell(to_marker),
                "itemRef": f"xlsx:sheet:{sheet_ordinal}:{kind}:{index}",
            }
            unsupported.append(
                {
                    "kind": kind,
                    "message": (
                        f"XLSX {kind} content is preserved as a located reference "
                        "but is not semantically extracted."
                    ),
                    "locator": locator,
                }
            )
    return unsupported


def _xlsx_anchor_cell(marker: Any) -> str | None:
    """Convert an openpyxl zero-based drawing anchor into an A1 cell reference."""

    if marker is None:
        return None
    try:
        from openpyxl.utils.cell import get_column_letter

        return f"{get_column_letter(int(marker.col) + 1)}{int(marker.row) + 1}"
    except (AttributeError, TypeError, ValueError):
        return None


def _unsupported_diagnostic(entry: dict[str, Any]) -> ParserDiagnostic:
    """Convert one unsupported-content record into the shared diagnostic type."""

    return ParserDiagnostic(
        code=f"OFFICE_UNSUPPORTED_{str(entry['kind']).upper()}",
        message=str(entry["message"]),
        kind="parser",
        locator=dict(entry.get("locator", {})),
    )


def _plain_text(value: Any) -> str:
    """Normalize line endings while preserving user-authored Unicode text."""

    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _json_value(value: Any) -> Any:
    """Convert standard Office value types into deterministic JSON primitives."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if hasattr(value, "__float__"):
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            pass
    return str(value)


def _display_value(value: Any) -> str:
    """Render a workbook value for the companion text block."""

    normalized = _json_value(value)
    if isinstance(normalized, (dict, list)):
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    return "" if normalized is None else str(normalized)
