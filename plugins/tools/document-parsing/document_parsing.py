"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 document_parsing.py                                             │
│  Module: document_parsing                                           │
│  Role: Stateless document.parsing.v1 PDF/DOCX conversion.           │
│                                                                     │
│  模块职责：无状态 PDF/DOCX 解析，输出完整 Docling 文档与定位索引。       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

CAPABILITY_ID = "document.parsing.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"
BLOCKS_SCHEMA = "cyrene.document.blocks.v1"
RESULT_SCHEMA = "cyrene.document.parsing.result.v1"
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_DOCX_ENTRIES = 20_000
MAX_DOCX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_REPORTED_ERRORS = 500
SOURCE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SUPPORTED_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """Typed response consumed by DirectPluginRuntime.

    中文：DirectPluginRuntime 使用的类型化 JSON 响应。
    """

    value: bytes
    type_url: str


class DocumentParsingPlugin:
    """Convert one staged PDF or DOCX without owning Product state.

    中文：在 staging 路径上转换单个 PDF 或 DOCX，不持有 Product 状态。
    """

    plugin_id = "cyrene.tools.document-parsing"
    version = "0.1.0"
    capabilities = (CAPABILITY_ID,)

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: Any | None = None,
        request_type_url: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, TypedPayload | str]:
        """Validate and dispatch one typed parse request.

        中文：校验并分派一个类型化解析请求。
        """

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action != "parse":
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        expected_type_url = f"{TYPE_PREFIX}.{action}.request"
        if request_type_url != expected_type_url:
            return (
                False,
                f"INVALID_REQUEST: request_type_url must be {expected_type_url}",
            )
        if stream_results:
            return False, "METHOD_NOT_SUPPORTED: document parsing is not streaming"
        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"

        try:
            request = json.loads(payload.decode("utf-8"))
            if not isinstance(request, dict):
                raise TypeError("request must be an object")
            expected_fields = {
                "source_path",
                "payload_path",
                "blocks_path",
                "result_path",
                "source_ref",
                "source_digest",
                "filename",
                "media_type",
            }
            unknown_fields = set(request) - expected_fields
            missing_fields = expected_fields - set(request)
            if unknown_fields:
                raise ValueError(
                    f"unknown request fields: {', '.join(sorted(unknown_fields))}"
                )
            if missing_fields:
                raise ValueError(
                    f"missing request fields: {', '.join(sorted(missing_fields))}"
                )
            result = self.parse_document(
                source_path=_absolute_path(request["source_path"], "source_path"),
                payload_path=_absolute_path(request["payload_path"], "payload_path"),
                blocks_path=_absolute_path(request["blocks_path"], "blocks_path"),
                result_path=_absolute_path(request["result_path"], "result_path"),
                source_ref=_required_text(request["source_ref"], "source_ref"),
                source_digest=_required_text(request["source_digest"], "source_digest"),
                filename=_required_text(request["filename"], "filename"),
                media_type=_required_text(request["media_type"], "media_type"),
            )
        except (
            TypeError,
            ValueError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
        ) as exc:
            return False, f"INVALID_INPUT: {exc}"
        except ImportError as exc:
            return False, f"DEPENDENCY_MISSING: {exc}"

        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"
        return True, TypedPayload(
            value=_canonical_json(result),
            type_url=f"{TYPE_PREFIX}.{action}.response",
        )

    def parse_document(
        self,
        *,
        source_path: Path,
        payload_path: Path,
        blocks_path: Path,
        result_path: Path,
        source_ref: str,
        source_digest: str,
        filename: str,
        media_type: str,
    ) -> dict[str, Any]:
        """Convert a staged source and persist the full document and block index.

        The source path is read only. PDF page numbers in locators are one-based;
        the adapter leaves them absent for formats where Docling has no page data.

        中文：只读转换 staging 源文件，并写入完整文档、block 索引和报告；可用页码
        固定为从 1 开始，Docling 未提供的页码保持缺失。
        """

        source_path = _absolute_path(source_path, "source_path")
        payload_path = _absolute_path(payload_path, "payload_path")
        blocks_path = _absolute_path(blocks_path, "blocks_path")
        result_path = _absolute_path(result_path, "result_path")
        _validate_request_identity(source_ref, source_digest, filename)
        source_format = _validate_source(source_path, media_type)
        actual_source_digest = _sha256_file(source_path)
        if actual_source_digest != source_digest:
            raise ValueError("source_digest does not match the staged source bytes")
        _validate_output_paths(source_path, (payload_path, blocks_path, result_path))

        from docling.datamodel.base_models import InputFormat

        converter, conversion_profile = _build_converter(InputFormat, source_format)
        conversion = converter.convert(str(source_path))
        document = getattr(conversion, "document", None)
        if document is None:
            errors = _conversion_errors(conversion, source_path)
            summary = errors[0]["message"] if errors else "Docling returned no document"
            raise ValueError(f"Docling conversion failed: {summary}")

        # Preserve Docling's full structured export; only the companion block index is normalized.
        # 完整保留 Docling 结构化导出；仅规范化配套的公开 block 索引。
        docling_payload = document.export_to_dict()
        payload_bytes = _canonical_json(docling_payload)
        payload_receipt = _write_bytes(payload_path, payload_bytes)

        blocks, extraction_warnings = _extract_blocks(
            document,
            source_ref=source_ref,
            source_digest=source_digest,
            source_path=source_path,
            source_format=source_format,
        )
        blocks_document = {
            "schema_version": BLOCKS_SCHEMA,
            "source_ref": source_ref,
            "source_digest": source_digest,
            "blocks": blocks,
        }
        blocks_receipt = _write_json(blocks_path, blocks_document)

        conversion_errors = _conversion_errors(conversion, source_path)
        conversion_limitations: list[str] = []
        if conversion_profile == "pdf-native-no-model-download":
            conversion_limitations.append(
                "Native PDF parsing does not run OCR or infer reading order/table structure; scanned or layout-only content may remain unparsed."
            )
        warnings = [entry["message"] for entry in conversion_errors]
        warnings.extend(extraction_warnings)
        warnings.extend(conversion_limitations)
        status = _status_text(getattr(conversion, "status", "success"))
        if status == "failure":
            summary = (
                warnings[0] if warnings else "Docling marked the conversion as failed."
            )
            raise ValueError(f"Docling conversion failed: {summary}")
        if status == "partial_success" and not warnings:
            warnings.append("Docling marked the conversion as partial success.")
        pages = getattr(document, "pages", None)
        page_count = len(pages) if pages else None
        if source_format == "PDF" and not page_count:
            page_count = None
            warnings.append("Docling returned no PDF page records.")
        if not blocks:
            warnings.append("Docling returned no addressable content blocks.")

        conversion_report: dict[str, Any] = {
            "schema_version": RESULT_SCHEMA,
            "capability": CAPABILITY_ID,
            "source_ref": source_ref,
            "source_digest": source_digest,
            "filename": Path(filename).name,
            "media_type": media_type,
            "source_format": source_format,
            "status": status,
            "conversion_profile": conversion_profile,
            "docling_version": _docling_version(),
            "docling_document_digest": payload_receipt["digest"],
            "page_count": page_count,
            "block_count": len(blocks),
            "conversion_errors": conversion_errors,
            "conversion_limitations": conversion_limitations,
            "unparsed_content": _unparsed_content(
                conversion_errors, extraction_warnings
            ),
            "warnings": warnings,
            "locator_policy": {
                "source_pages": "Docling page_no values, normalized to one-based integers; [] when absent.",
                "docx_pagination": "No synthetic source page numbers are assigned.",
                "coordinates": "Docling provenance bounding boxes retain their coordinate origin.",
            },
            "receipts": {
                "source": {
                    "digest": source_digest,
                    "size": source_path.stat().st_size,
                },
                "payload": payload_receipt,
                "blocks": blocks_receipt,
            },
        }
        result_receipt = _write_json(result_path, conversion_report)
        return {
            "source_digest": source_digest,
            "source_format": source_format,
            "status": status,
            "conversion_profile": conversion_profile,
            "page_count": page_count,
            "block_count": len(blocks),
            "warning_count": len(warnings),
            "warnings": warnings,
            "payload_digest": payload_receipt["digest"],
            "payload_size": payload_receipt["size"],
            "blocks_digest": blocks_receipt["digest"],
            "blocks_size": blocks_receipt["size"],
            "result_digest": result_receipt["digest"],
            "result_size": result_receipt["size"],
        }


def _build_converter(input_format: Any, source_format: str) -> tuple[Any, str]:
    """Build an offline converter, selecting model-backed PDF only when preloaded.

    中文：创建离线转换器；只有显式预下载并配置模型目录时才启用 PDF 模型管线。
    """

    from docling.document_converter import DocumentConverter

    if source_format == "DOCX":
        return (
            DocumentConverter(allowed_formats=[input_format.DOCX]),
            "docx-simple",
        )

    artifacts_value = os.environ.get("CYRENE_DOCUMENT_PARSING_ARTIFACTS_PATH")
    if artifacts_value:
        artifacts_path = Path(artifacts_value).expanduser().resolve()
        if not artifacts_path.is_dir():
            raise ValueError(
                "CYRENE_DOCUMENT_PARSING_ARTIFACTS_PATH must point to a preloaded Docling model directory"
            )
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,
            RapidOcrOptions,
        )
        from docling.document_converter import PdfFormatOption

        options = PdfPipelineOptions(artifacts_path=artifacts_path)
        options.enable_remote_services = False
        options.ocr_options = RapidOcrOptions()
        return (
            DocumentConverter(
                allowed_formats=[input_format.PDF],
                format_options={
                    input_format.PDF: PdfFormatOption(pipeline_options=options)
                },
            ),
            "pdf-preloaded-models",
        )

    from docling.datamodel.pipeline_options import NativePdfPipelineOptions
    from docling.document_converter import NativePdfFormatOption

    return (
        DocumentConverter(
            allowed_formats=[input_format.PDF],
            format_options={
                input_format.PDF: NativePdfFormatOption(
                    pipeline_options=NativePdfPipelineOptions()
                )
            },
        ),
        "pdf-native-no-model-download",
    )


def _extract_blocks(
    document: Any,
    *,
    source_ref: str,
    source_digest: str,
    source_path: Path,
    source_format: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Project Docling reading-order items into stable public block records.

    中文：按 Docling 阅读顺序投影为稳定的公开 block 记录。
    """

    from docling_core.types.doc.document import ContentLayer

    blocks: list[dict[str, Any]] = []
    warnings: list[str] = []
    section_stack: list[str] = []
    seen_refs: set[str] = set()
    items: Iterable[tuple[Any, int]] = document.iterate_items(
        traverse_pictures=True,
        included_content_layers={ContentLayer.BODY, ContentLayer.FURNITURE},
    )

    for item, tree_level in items:
        item_ref = str(getattr(item, "self_ref", "") or f"item:{len(blocks)}")
        if item_ref in seen_refs:
            continue
        seen_refs.add(item_ref)

        label_value = getattr(getattr(item, "label", None), "value", None)
        label = str(label_value or type(item).__name__).lower()
        text, text_warning = _item_text(
            item,
            document,
            label,
            source_path=source_path,
            source_format=source_format,
            item_ref=item_ref,
        )
        if text_warning:
            warnings.append(f"{item_ref}: {text_warning}")

        if "section_header" in label or "heading" in type(item).__name__.lower():
            depth_value = getattr(item, "level", None)
            depth = (
                depth_value if isinstance(depth_value, int) and depth_value > 0 else 1
            )
            section_stack = section_stack[: depth - 1]
            if text:
                section_stack.append(text)

        provenance = _provenance(item)
        source_pages = sorted(
            {
                entry["page_no"]
                for entry in provenance
                if isinstance(entry.get("page_no"), int)
                and not isinstance(entry.get("page_no"), bool)
                and entry["page_no"] > 0
            }
        )
        kind = _block_kind(label)
        if not text and kind in {"picture", "table", "form"}:
            warnings.append(
                f"{item_ref}: Docling supplied no public text for this {kind} item."
            )
        block_id = hashlib.sha256(
            f"{source_ref}\0{source_digest}\0{item_ref}".encode()
        ).hexdigest()
        locator: dict[str, Any] = {
            "source_pages": source_pages,
            "section_path": list(section_stack),
            "item_ref": item_ref,
            "tree_level": tree_level,
            "provenance": provenance,
        }
        if kind == "table":
            table_index = _table_index(item_ref)
            if table_index is not None:
                locator["table_index"] = table_index
        blocks.append(
            {
                "id": f"sha256:{block_id}",
                "ordinal": len(blocks),
                "kind": kind,
                "text": text,
                "locator": locator,
            }
        )

    return blocks, warnings


def _item_text(
    item: Any,
    document: Any,
    label: str,
    *,
    source_path: Path,
    source_format: str,
    item_ref: str,
) -> tuple[str, str | None]:
    """Extract visible text without inventing descriptions for non-text items.

    中文：提取实际可见文本；不为非文本内容编造描述。
    """

    if label == "table":
        markdown_warning: str | None = None
        try:
            text = item.export_to_markdown(doc=document)
            if isinstance(text, str) and text.strip():
                return _normalize_text(text), None
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            markdown_warning = f"table Markdown export failed ({type(exc).__name__})."
        fallback = _table_cells_text(item)
        if fallback:
            warning = markdown_warning or "table Markdown export was empty."
            return fallback, f"{warning} Docling cell text fallback used."
        if source_format == "DOCX":
            docx_fallback = _docx_table_text(source_path, item_ref)
            if docx_fallback:
                warning = markdown_warning or "Docling table export was empty."
                return docx_fallback, f"{warning} python-docx table fallback used."
        return "", markdown_warning or "table had no public cell text."

    value = getattr(item, "text", None)
    if isinstance(value, str) and value.strip():
        return _normalize_text(value), None

    captions: list[str] = []
    for reference in getattr(item, "captions", ()) or ():
        try:
            resolved = reference.resolve(document)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            continue
        caption = getattr(resolved, "text", None)
        if isinstance(caption, str) and caption.strip():
            captions.append(_normalize_text(caption))
    if captions:
        return "\n".join(captions), None
    return "", None


def _table_cells_text(item: Any) -> str:
    """Preserve available table cell text when Docling Markdown export fails.

    中文：Docling Markdown 导出失败时，保留已提供的单元格文本。
    """

    data = getattr(item, "data", None)
    grid = getattr(data, "grid", None)
    if not isinstance(grid, list):
        return ""
    rows = [
        [str(getattr(cell, "text", "") or "").strip() for cell in row]
        for row in grid
        if isinstance(row, list)
    ]
    return _normalize_text("\n".join(" | ".join(row) for row in rows if any(row)))


def _docx_table_text(source_path: Path, item_ref: str) -> str:
    """Recover a DOCX table's visible cells when Docling emitted an empty grid.

    中文：Docling 输出空表格网格时，从 DOCX 中恢复可见单元格文本。
    """

    table_index = _table_index(item_ref)
    if table_index is None:
        return ""
    from docx import Document

    document = Document(str(source_path))
    if table_index >= len(document.tables):
        return ""
    rows = [
        [_normalize_text(cell.text) for cell in row.cells]
        for row in document.tables[table_index].rows
    ]
    return _normalize_text("\n".join(" | ".join(cells) for cells in rows if any(cells)))


def _provenance(item: Any) -> list[dict[str, Any]]:
    """Convert item provenance to JSON while preserving Docling coordinate metadata.

    中文：将来源定位信息转换为 JSON，并保留 Docling 坐标元数据。
    """

    entries = getattr(item, "prov", ()) or ()
    result: list[dict[str, Any]] = []
    for entry in entries:
        value = _to_json_value(entry)
        if isinstance(value, dict):
            result.append(value)
    return result


def _block_kind(label: str) -> str:
    """Group Docling labels into stable block kinds without losing the source label.

    中文：将 Docling 标签归入稳定 block 类别，同时保留原标签。
    """

    if "table" in label:
        return "table"
    if "picture" in label or "figure" in label:
        return "picture"
    if "form" in label or "key_value" in label:
        return "form"
    if any(
        token in label
        for token in (
            "text",
            "paragraph",
            "title",
            "section",
            "list",
            "code",
            "formula",
            "caption",
            "footnote",
            "header",
            "footer",
        )
    ):
        return "text"
    return label.replace("_", "-")


def _table_index(item_ref: str) -> int | None:
    """Read Docling's stable table-array index from a table item reference.

    中文：从 table item reference 中提取 Docling table 数组索引。
    """

    match = re.fullmatch(r"#/tables/(\d+)", item_ref)
    return int(match.group(1)) if match is not None else None


def _conversion_errors(conversion: Any, source_path: Path) -> list[dict[str, Any]]:
    """Return bounded Docling conversion diagnostics with local paths redacted.

    中文：返回有界 Docling 诊断信息，并隐藏执行器本地路径。
    """

    raw_errors = getattr(conversion, "errors", ()) or ()
    errors: list[dict[str, Any]] = []
    for raw in raw_errors[:MAX_REPORTED_ERRORS]:
        value = _redact_source_path(_to_json_value(raw), str(source_path))
        item = value if isinstance(value, dict) else {"message": str(value)}
        message = str(item.get("error_message") or item.get("message") or item)
        item["message"] = message.replace(str(source_path), "<staged-source>")
        item.pop("error_message", None)
        errors.append(item)
    if len(raw_errors) > MAX_REPORTED_ERRORS:
        errors.append(
            {
                "message": f"Docling returned more than {MAX_REPORTED_ERRORS} errors; remaining entries were omitted.",
                "truncated": True,
            }
        )
    return errors


def _unparsed_content(
    errors: list[dict[str, Any]], extraction_warnings: list[str]
) -> list[dict[str, Any]]:
    """Summarize content that Docling reported or the adapter could not project.

    中文：汇总 Docling 明确报告失败或 adapter 无法投影的内容。
    """

    unparsed = [{"kind": "conversion_error", **error} for error in errors]
    unparsed.extend(
        {"kind": "block_projection_warning", "message": message}
        for message in extraction_warnings
    )
    return unparsed


def _redact_source_path(value: Any, source_path: str) -> Any:
    """Remove executor-local source paths from nested diagnostic metadata.

    中文：从嵌套诊断元数据中移除执行器本地来源路径。
    """

    if isinstance(value, str):
        return value.replace(source_path, "<staged-source>")
    if isinstance(value, dict):
        return {
            key: _redact_source_path(item, source_path) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_source_path(item, source_path) for item in value]
    return value


def _validate_source(source_path: Path, media_type: str) -> str:
    """Check source bytes, declared media type, and actual PDF/DOCX structure.

    中文：校验来源字节数、声明类型以及实际 PDF/DOCX 结构。
    """

    if not source_path.is_file():
        raise ValueError("source_path must name an existing regular file")
    source_size = source_path.stat().st_size
    if source_size <= 0:
        raise ValueError("source file is empty")
    if source_size > MAX_SOURCE_BYTES:
        raise ValueError(f"source file exceeds {MAX_SOURCE_BYTES} bytes")

    normalized_media_type = media_type.lower()
    if normalized_media_type == SUPPORTED_MEDIA_TYPES["pdf"]:
        with source_path.open("rb") as stream:
            if not stream.read(8).startswith(b"%PDF-"):
                raise ValueError("source bytes do not contain a PDF header")
        return "PDF"
    if normalized_media_type == SUPPORTED_MEDIA_TYPES["docx"]:
        try:
            with zipfile.ZipFile(source_path) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_DOCX_ENTRIES:
                    raise ValueError("DOCX archive contains too many entries")
                expanded_size = sum(entry.file_size for entry in entries)
                if expanded_size > MAX_DOCX_EXPANDED_BYTES:
                    raise ValueError("DOCX expanded size exceeds the safe limit")
                names = {entry.filename for entry in entries}
                if (
                    "[Content_Types].xml" not in names
                    or "word/document.xml" not in names
                ):
                    raise ValueError("DOCX archive is missing its document parts")
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise ValueError("source bytes are not a readable DOCX package") from exc
        return "DOCX"
    raise ValueError("media_type must identify application/pdf or OOXML Word DOCX")


def _validate_request_identity(
    source_ref: str, source_digest: str, filename: str
) -> None:
    """Validate stable logical identity and exact source-byte digest.

    中文：校验逻辑来源身份及来源字节摘要。
    """

    _required_text(source_ref, "source_ref")
    if not SOURCE_DIGEST_PATTERN.fullmatch(source_digest):
        raise ValueError("source_digest must use sha256:<64 lowercase hex>")
    if not Path(filename).name or Path(filename).name != filename:
        raise ValueError("filename must be a basename without directory components")


def _validate_output_paths(source_path: Path, output_paths: tuple[Path, ...]) -> None:
    """Require distinct output files that never overwrite the staged source.

    中文：要求输出文件彼此独立，且不会覆盖 staging 来源文件。
    """

    resolved_source = source_path.resolve()
    resolved_outputs = [path.resolve() for path in output_paths]
    if len(set(resolved_outputs)) != len(resolved_outputs):
        raise ValueError("payload_path, blocks_path, and result_path must be distinct")
    if resolved_source in resolved_outputs:
        raise ValueError("output paths must not overwrite source_path")


def _absolute_path(value: Any, field: str) -> Path:
    """Validate an absolute executor-local path.

    中文：校验执行器本地绝对路径。
    """

    path = Path(value) if isinstance(value, str | Path) else None
    if path is None or not path.is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    return path


def _required_text(value: Any, field: str) -> str:
    """Normalize one required request string.

    中文：规范化一个必填请求字符串。
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def _normalize_text(value: str) -> str:
    """Apply only Unicode and newline normalization, preserving source wording.

    中文：仅规范化 Unicode 与换行，保留来源文本内容。
    """

    return unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    ).strip()


def _to_json_value(value: Any) -> Any:
    """Convert Pydantic/dataclass values into JSON-native structures.

    中文：将 Pydantic 或 dataclass 对象转换为 JSON 原生结构。
    """

    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_to_json_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _to_json_value(model_dump(mode="json", exclude_none=True))
    as_dict = getattr(value, "dict", None)
    if callable(as_dict):
        return _to_json_value(as_dict(exclude_none=True))
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


def _status_text(value: Any) -> str:
    """Normalize Docling's conversion status enum.

    中文：规范化 Docling 转换状态枚举。
    """

    raw = getattr(value, "value", value)
    text = str(raw).lower().replace("-", "_")
    if text.endswith("partial_success"):
        return "partial_success"
    if text.endswith("failure"):
        return "failure"
    return "success"


def _package_version(package: str) -> str | None:
    """Read an installed package version without making it an import-time dependency.

    中文：读取已安装组件版本，不在模块加载时强制导入其运行代码。
    """

    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _docling_version() -> str | None:
    """Return the version for either the full or slim Docling distribution.

    中文：返回 full 或 slim Docling 分发包的版本。
    """

    return _package_version("docling") or _package_version("docling-slim")


def _sha256_file(path: Path) -> str:
    """Hash staged bytes in bounded memory.

    中文：以有界内存计算 staging 字节摘要。
    """

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _write_json(path: Path, value: Any) -> dict[str, Any]:
    """Atomically write canonical UTF-8 JSON and return its bounded digest receipt.

    中文：原子写入规范化 UTF-8 JSON，并返回摘要与大小回执。
    """

    return _write_bytes(path, _canonical_json(value))


def _write_bytes(path: Path, data: bytes) -> dict[str, Any]:
    """Atomically persist bytes and report SHA-256 digest plus byte size.

    中文：原子持久化字节，并报告 SHA-256 摘要与字节数。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_bytes(data)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {
        "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
        "size": len(data),
    }


def _canonical_json(value: Any) -> bytes:
    """Serialize deterministic UTF-8 JSON without lossy fallback encoders.

    中文：以确定性方式序列化 UTF-8 JSON，不使用有损 fallback 编码。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = ["CAPABILITY_ID", "INTERFACE_VERSION", "DocumentParsingPlugin"]
