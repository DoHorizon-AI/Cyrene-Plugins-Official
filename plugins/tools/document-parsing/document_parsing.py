"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 document_parsing.py                                             │
│  Module: document_parsing                                           │
│  Role: Stateless document.parsing.v1 file conversion.              │
│                                                                     │
│  模块职责：无状态多格式解析，输出原生 JSON、定位索引与诊断。             │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from parsing_types import (
    OcrConfig,
    ParseContext,
    ParserDiagnostic,
    ParseResult,
    make_block,
)

CAPABILITY_ID = "document.parsing.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"
BLOCKS_SCHEMA = "cyrene.document.blocks.v1"
RESULT_SCHEMA = "cyrene.document.parsing.result.v1"
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_OOXML_ENTRIES = 20_000
MAX_OOXML_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_REPORTED_ERRORS = 500
SOURCE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SUPPORTED_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "markdown": "text/markdown",
    "txt": "text/plain",
    "png": "image/png",
    "jpeg": "image/jpeg",
}

MEDIA_TYPE_FORMATS = {
    media_type: source_format
    for source_format, media_type in {
        "PDF": SUPPORTED_MEDIA_TYPES["pdf"],
        "DOCX": SUPPORTED_MEDIA_TYPES["docx"],
        "PPTX": SUPPORTED_MEDIA_TYPES["pptx"],
        "XLSX": SUPPORTED_MEDIA_TYPES["xlsx"],
        "CSV": SUPPORTED_MEDIA_TYPES["csv"],
        "MARKDOWN": SUPPORTED_MEDIA_TYPES["markdown"],
        "TXT": SUPPORTED_MEDIA_TYPES["txt"],
        "PNG": SUPPORTED_MEDIA_TYPES["png"],
        "JPEG": SUPPORTED_MEDIA_TYPES["jpeg"],
    }.items()
}


class ParserExecutionError(RuntimeError):
    """Raised when a valid source cannot be converted by its selected parser.

    中文：有效来源无法由对应解析器转换时抛出。
    """


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """Typed response consumed by DirectPluginRuntime.

    中文：DirectPluginRuntime 使用的类型化 JSON 响应。
    """

    value: bytes
    type_url: str


class DocumentParsingPlugin:
    """Parse one staged source without owning Product state.

    中文：在 staging 路径上解析单个来源，不持有 Product 状态。
    """

    plugin_id = "cyrene.tools.document-parsing"
    version = "0.2.0"
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
        except ParserExecutionError as exc:
            return False, f"PARSER_FAILURE: {exc}"
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
        """Parse one staged source and persist its payload, blocks, and report.

        PDF/DOCX retain Docling's complete JSON export. PDF pages without native
        text use the separately configured local OCR helper. Source page numbers
        are one-based; Office and simple adapters own their native locators.
        中文：保存完整 Docling JSON、公开 blocks 与转换报告；扫描页 OCR 独立于模型下载。
        """

        source_path = _absolute_path(source_path, "source_path")
        payload_path = _absolute_path(payload_path, "payload_path")
        blocks_path = _absolute_path(blocks_path, "blocks_path")
        result_path = _absolute_path(result_path, "result_path")
        _validate_request_identity(source_ref, source_digest, filename)
        media_type = _required_text(media_type, "media_type").lower()
        source_format = _validate_source(source_path, media_type)
        if _sha256_file(source_path) != source_digest:
            raise ValueError("source_digest does not match the staged source bytes")
        _validate_output_paths(source_path, (payload_path, blocks_path, result_path))

        context = ParseContext(
            source_ref=source_ref,
            source_digest=source_digest,
            filename=filename,
            media_type=media_type,
        )
        docling_version: str | None = None
        conversion_errors: list[dict[str, Any]] = []
        conversion_limitations: list[str] = []
        if source_format in {"PDF", "DOCX"}:
            parsed, conversion_errors, conversion_limitations = _parse_docling(
                source_path, context, source_format
            )
            docling_version = _docling_version()
        elif source_format in {"PPTX", "XLSX"}:
            from office_parsing import parse_office

            parsed = parse_office(source_path, context)
        else:
            from ocr_parsing import ocr_config_from_env
            from simple_parsing import parse_simple

            try:
                ocr_config = ocr_config_from_env()
            except ValueError as exc:
                ocr_config = OcrConfig(engine="none")
                diagnostic = ParserDiagnostic(
                    code="ocr.config_invalid",
                    message=str(exc),
                    kind="ocr",
                )
                parsed = parse_simple(source_path, context, ocr_config=ocr_config)
                parsed = _with_diagnostic(parsed, diagnostic)
            else:
                parsed = parse_simple(source_path, context, ocr_config=ocr_config)

        if source_format == "PDF":
            from ocr_parsing import ocr_config_from_env, ocr_image

            try:
                ocr_config = ocr_config_from_env()
            except ValueError as exc:
                ocr_config = OcrConfig(engine="none")
                diagnostic = ParserDiagnostic(
                    code="ocr.config_invalid",
                    message=str(exc),
                    kind="ocr",
                )
                parsed = _with_diagnostic(parsed, diagnostic)
            parsed = _add_pdf_page_ocr(
                source_path, context, parsed, ocr_config, ocr_image
            )

        parsed = _finalize_parse_result(parsed)
        payload_receipt = _write_json(payload_path, parsed.payload)
        blocks_document = {
            "schema_version": BLOCKS_SCHEMA,
            "source_ref": source_ref,
            "source_digest": source_digest,
            "blocks": parsed.blocks,
        }
        blocks_receipt = _write_json(blocks_path, blocks_document)
        warnings = list(dict.fromkeys(parsed.warnings))
        if conversion_errors:
            warnings.extend(
                message
                for error in conversion_errors
                if (message := str(error.get("message", "")))
                and message not in warnings
            )
        warnings.extend(
            limitation
            for limitation in conversion_limitations
            if limitation not in warnings
        )
        diagnostics = [_diagnostic_json(item) for item in parsed.diagnostics]
        unsupported_content = list(parsed.unsupported_content)
        source_receipt = {
            "digest": source_digest,
            "size": source_path.stat().st_size,
        }
        report: dict[str, Any] = {
            "schema_version": RESULT_SCHEMA,
            "capability": CAPABILITY_ID,
            "source_ref": source_ref,
            "source_digest": source_digest,
            "filename": Path(filename).name,
            "media_type": media_type,
            "source_format": parsed.source_format,
            "status": parsed.status,
            "conversion_profile": parsed.conversion_profile,
            "docling_version": docling_version,
            "docling_document_digest": (
                payload_receipt["digest"] if docling_version is not None else None
            ),
            "payload_format": (
                "docling-document-json"
                if docling_version is not None
                else "adapter-json"
            ),
            "page_count": parsed.page_count,
            "slide_count": parsed.slide_count,
            "sheet_count": parsed.sheet_count,
            "block_count": len(parsed.blocks),
            "conversion_errors": conversion_errors,
            "conversion_limitations": conversion_limitations,
            "unparsed_content": _unparsed_content(
                conversion_errors, unsupported_content
            ),
            "warnings": warnings,
            "diagnostics": diagnostics,
            "unsupported_content": unsupported_content,
            "locator_policy": {
                "source_pages": "One-based source page/slide values; empty when a source format has no page locator.",
                "docx_pagination": "No synthetic source page numbers are assigned.",
                "coordinates": "Adapter provenance preserves source-native geometry and coordinate units.",
            },
            "receipts": {
                "source": source_receipt,
                "payload": payload_receipt,
                "blocks": blocks_receipt,
            },
        }
        result_receipt = _write_json(result_path, report)
        response: dict[str, Any] = {
            "source_digest": source_digest,
            "source_format": parsed.source_format,
            "status": parsed.status,
            "conversion_profile": parsed.conversion_profile,
            "page_count": parsed.page_count,
            "slide_count": parsed.slide_count,
            "sheet_count": parsed.sheet_count,
            "block_count": len(parsed.blocks),
            "warning_count": len(warnings),
            "warnings": warnings,
            "diagnostics": diagnostics,
            "unsupported_content": unsupported_content,
            "payload_digest": payload_receipt["digest"],
            "payload_size": payload_receipt["size"],
            "blocks_digest": blocks_receipt["digest"],
            "blocks_size": blocks_receipt["size"],
            "result_digest": result_receipt["digest"],
            "result_size": result_receipt["size"],
        }
        return response


def _parse_docling(
    source_path: Path,
    context: ParseContext,
    source_format: str,
) -> tuple[ParseResult, list[dict[str, Any]], list[str]]:
    """Run Docling and preserve its complete source-format JSON export.

    中文：调用 Docling 并保留完整原生 JSON；公开 blocks 另行规范化。
    """

    from docling.datamodel.base_models import InputFormat

    try:
        converter, conversion_profile = _build_converter(InputFormat, source_format)
        conversion = converter.convert(str(source_path))
    except ImportError:
        raise
    except Exception as exc:
        raise ParserExecutionError(
            f"Docling conversion raised {type(exc).__name__}: {exc}"
        ) from exc

    document = getattr(conversion, "document", None)
    conversion_errors = _conversion_errors(conversion, source_path)
    if document is None:
        summary = (
            conversion_errors[0]["message"]
            if conversion_errors
            else "Docling returned no document"
        )
        raise ParserExecutionError(f"Docling conversion failed: {summary}")

    try:
        # Keep the native export intact; normalized blocks are a separate public view.
        # 保留完整原生导出；规范化 blocks 是独立的公开视图。
        payload = document.export_to_dict()
    except Exception as exc:
        raise ParserExecutionError(
            f"Docling JSON export failed: {type(exc).__name__}: {exc}"
        ) from exc

    blocks, extraction_warnings = _extract_blocks(
        document,
        context=context,
        source_path=source_path,
        source_format=source_format,
    )
    limitations: list[str] = []
    if conversion_profile == "pdf-native-no-model-download":
        limitations.append(
            "Native PDF parsing uses embedded text only; image-only or layout-only pages need local OCR and may remain unparsed."
        )
    warnings = [str(entry["message"]) for entry in conversion_errors]
    warnings.extend(extraction_warnings)
    status = _status_text(getattr(conversion, "status", "success"))
    if status == "failure":
        summary = (
            warnings[0] if warnings else "Docling marked the conversion as failed."
        )
        raise ParserExecutionError(f"Docling conversion failed: {summary}")
    if status == "partial_success" and not warnings:
        warnings.append("Docling marked the conversion as partial success.")

    pages = getattr(document, "pages", None)
    page_count = len(pages) if pages else None
    diagnostics: list[ParserDiagnostic] = []
    unsupported: list[dict[str, Any]] = []
    diagnostics.extend(
        ParserDiagnostic(
            code="docling.conversion_warning",
            message=warning,
            kind="parser",
        )
        for warning in (str(entry["message"]) for entry in conversion_errors)
    )
    diagnostics.extend(
        ParserDiagnostic(
            code="docling.block_projection_warning",
            message=warning,
            kind="parser",
        )
        for warning in extraction_warnings
    )
    for warning in warnings:
        if (
            not diagnostics
            and warning == "Docling marked the conversion as partial success."
        ):
            diagnostics.append(
                ParserDiagnostic(
                    code="docling.partial_success",
                    message=warning,
                    kind="parser",
                )
            )
    if source_format == "PDF" and not page_count:
        warning = "Docling returned no PDF page records."
        warnings.append(warning)
        diagnostics.append(
            ParserDiagnostic("docling.page_records_missing", warning, "parser")
        )
    if not blocks:
        warning = "Docling returned no addressable content blocks."
        warnings.append(warning)
        unsupported.append(
            {
                "kind": "document_content",
                "code": "docling.no_addressable_blocks",
                "message": warning,
                "locator": {
                    "source_pages": [],
                    "section_path": [],
                    "item_ref": "document",
                    "tree_level": 0,
                    "provenance": [],
                },
            }
        )
        diagnostics.append(
            ParserDiagnostic("docling.no_addressable_blocks", warning, "parser")
        )
    result = ParseResult(
        payload=payload,
        blocks=blocks,
        source_format=source_format,
        conversion_profile=conversion_profile,
        status="partial_success" if status == "partial_success" else "success",
        page_count=page_count,
        warnings=list(dict.fromkeys([*warnings, *limitations])),
        diagnostics=diagnostics,
        unsupported_content=unsupported,
    )
    return result, conversion_errors, limitations


def _add_pdf_page_ocr(
    source_path: Path,
    context: ParseContext,
    parsed: ParseResult,
    config: OcrConfig,
    ocr_image: Callable[..., Any],
) -> ParseResult:
    """OCR PDF pages that have no native text and preserve page-level limits.

    PDF rendering uses pypdfium2 only when a textless page is found. OCR remains a
    separately configured local engine and never downloads models.
    中文：仅对无原生文本页逐页 OCR；渲染和 OCR 不触发模型下载。
    """

    import pypdfium2 as pdfium

    page_numbers_with_text = {
        page
        for block in parsed.blocks
        if str(block.get("text", "")).strip()
        for page in block.get("locator", {}).get("source_pages", [])
        if isinstance(page, int) and not isinstance(page, bool) and page > 0
    }
    blocks = list(parsed.blocks)
    initial_block_count = len(blocks)
    warnings = list(parsed.warnings)
    diagnostics = list(parsed.diagnostics)
    unsupported = list(parsed.unsupported_content)
    profile = parsed.conversion_profile

    try:
        pdf_document = pdfium.PdfDocument(str(source_path))
    except Exception as exc:
        raise ParserExecutionError(
            f"PDF page rendering could not open the source: {type(exc).__name__}: {exc}"
        ) from exc

    page_count = len(pdf_document)
    ocr_attempted = False
    try:
        for page_number in range(1, page_count + 1):
            if page_number in page_numbers_with_text:
                continue
            ocr_attempted = True
            page_locator = _page_locator(page_number, f"ocr:page:{page_number}")
            page = pdf_document[page_number - 1]
            bitmap = None
            image = None
            try:
                bitmap = page.render(scale=config.dpi / 72.0)
                image = bitmap.to_pil()
                result = ocr_image(
                    image,
                    context=context,
                    page_number=page_number,
                    config=config,
                )
            except Exception as exc:  # noqa: BLE001
                # One page-level OCR failure must not discard other pages.
                message = f"PDF page {page_number} OCR failed ({type(exc).__name__})."
                diagnostic = ParserDiagnostic(
                    code="ocr.page_failed",
                    message=message,
                    kind="ocr",
                    locator=page_locator,
                )
                diagnostics.append(diagnostic)
                warnings.append(message)
                unsupported.append(
                    {
                        "kind": "scanned_page",
                        "code": "ocr.page_unparsed",
                        "message": message,
                        "locator": page_locator,
                    }
                )
                continue
            finally:
                if image is not None:
                    image.close()
                if bitmap is not None:
                    bitmap.close()
                page.close()

            result_text = _normalize_text(result.text) if result.text.strip() else ""
            result_diagnostics = list(result.diagnostics)
            for item in result_diagnostics:
                diagnostic_locator = _ocr_diagnostic_locator(item.locator, page_number)
                normalized = replace(item, locator=diagnostic_locator)
                diagnostics.append(normalized)
                warnings.append(normalized.message)

            region_texts: list[str] = []
            for region_index, region in enumerate(result.regions):
                text = _normalize_text(region.text)
                if not text:
                    continue
                region_texts.append(text)
                item_ref = f"ocr:page:{page_number}:region:{region_index}"
                blocks.append(
                    make_block(
                        context,
                        ordinal=len(blocks),
                        item_ref=item_ref,
                        kind="text",
                        text=text,
                        source_pages=(page_number,),
                        provenance=(
                            {
                                "type": "ocr",
                                "engine": result.engine,
                                "languages": list(result.languages),
                                "confidence": region.confidence,
                                "bbox": dict(region.bbox),
                                "coordinate_unit": "rendered-image-pixels",
                            },
                        ),
                    )
                )
            if result_text and not region_texts:
                blocks.append(
                    make_block(
                        context,
                        ordinal=len(blocks),
                        item_ref=f"ocr:page:{page_number}:text",
                        kind="text",
                        text=result_text,
                        source_pages=(page_number,),
                        provenance=(
                            {
                                "type": "ocr",
                                "engine": result.engine,
                                "languages": list(result.languages),
                                "confidence": result.confidence,
                            },
                        ),
                    )
                )
            if not region_texts and not result_text:
                message = f"PDF page {page_number} has no native text and OCR returned no text."
                if not any(item.locator == page_locator for item in diagnostics):
                    diagnostic = ParserDiagnostic(
                        code="ocr.page_no_text",
                        message=message,
                        kind="ocr",
                        locator=page_locator,
                    )
                    diagnostics.append(diagnostic)
                    warnings.append(message)
                unsupported.append(
                    {
                        "kind": "scanned_page",
                        "code": "ocr.page_unparsed",
                        "message": message,
                        "locator": page_locator,
                    }
                )
    finally:
        pdf_document.close()

    if ocr_attempted:
        profile = f"{profile}+local-page-ocr"
    if len(blocks) > initial_block_count:
        unsupported = [
            item
            for item in unsupported
            if item.get("code") != "docling.no_addressable_blocks"
        ]
        diagnostics = [
            item for item in diagnostics if item.code != "docling.no_addressable_blocks"
        ]
        warnings = [
            warning
            for warning in warnings
            if warning != "Docling returned no addressable content blocks."
        ]
    return replace(
        parsed,
        blocks=blocks,
        conversion_profile=profile,
        page_count=page_count,
        warnings=list(dict.fromkeys(warnings)),
        diagnostics=diagnostics,
        unsupported_content=unsupported,
    )


def _page_locator(page_number: int, item_ref: str) -> dict[str, Any]:
    """Build the common locator shape for page-level OCR diagnostics."""

    return {
        "source_pages": [page_number],
        "section_path": [],
        "item_ref": item_ref,
        "tree_level": 0,
        "provenance": [],
    }


def _ocr_diagnostic_locator(
    locator: dict[str, Any] | None,
    default_page: int,
) -> dict[str, Any]:
    """Normalize adapter OCR locator details into the public block locator shape.

    中文：将 OCR helper 的页码和框坐标包装成统一来源定位结构。
    """

    if locator is None:
        return _page_locator(default_page, f"ocr:page:{default_page}")
    required = {"source_pages", "section_path", "item_ref", "tree_level", "provenance"}
    if required.issubset(locator):
        return _to_json_value(locator)
    page_number = locator.get("page_number", default_page)
    if (
        not isinstance(page_number, int)
        or isinstance(page_number, bool)
        or page_number < 1
    ):
        page_number = default_page
    item_ref = str(locator.get("item_ref") or f"ocr:page:{page_number}")
    provenance = {"type": "ocr_diagnostic", **_to_json_value(locator)}
    return {
        "source_pages": [page_number],
        "section_path": [],
        "item_ref": item_ref,
        "tree_level": 0,
        "provenance": [provenance],
    }


def _with_diagnostic(parsed: ParseResult, diagnostic: ParserDiagnostic) -> ParseResult:
    """Add one diagnostic while retaining an adapter's existing output."""

    return replace(
        parsed,
        diagnostics=[*parsed.diagnostics, diagnostic],
        warnings=list(dict.fromkeys([*parsed.warnings, diagnostic.message])),
    )


def _finalize_parse_result(parsed: ParseResult) -> ParseResult:
    """Mark unresolved content or warnings as partial success."""

    partial = bool(parsed.unsupported_content or parsed.diagnostics)
    if parsed.status == "partial_success" or partial:
        return replace(parsed, status="partial_success")
    return parsed


def _diagnostic_json(diagnostic: ParserDiagnostic) -> dict[str, Any]:
    """Serialize the stable diagnostic fields without null optional keys."""

    result: dict[str, Any] = {
        "code": diagnostic.code,
        "message": diagnostic.message,
        "kind": diagnostic.kind,
        "severity": diagnostic.severity,
    }
    if diagnostic.locator is not None:
        result["locator"] = _to_json_value(diagnostic.locator)
    if diagnostic.confidence is not None:
        result["confidence"] = diagnostic.confidence
    return result


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
    context: ParseContext,
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
        table_index = _table_index(item_ref) if kind == "table" else None
        blocks.append(
            make_block(
                context,
                ordinal=len(blocks),
                item_ref=item_ref,
                kind=kind,
                text=text,
                source_pages=source_pages,
                section_path=section_stack,
                tree_level=tree_level,
                table_index=table_index,
                provenance=provenance,
            )
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
    errors: list[dict[str, Any]],
    unsupported_content: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Summarize conversion issues and explicit adapter gaps.

    中文：汇总转换问题和 adapter 明确报告的未解析内容。
    """

    unparsed = [{"kind": "conversion_error", **error} for error in errors]
    unparsed.extend(unsupported_content)
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
    """Check source bytes, declared media type, and format signatures.

    中文：校验来源字节数、声明类型及 PDF、OOXML、文本和图片结构。
    """

    if not source_path.is_file():
        raise ValueError("source_path must name an existing regular file")
    source_size = source_path.stat().st_size
    if source_size <= 0:
        raise ValueError("source file is empty")
    if source_size > MAX_SOURCE_BYTES:
        raise ValueError(f"source file exceeds {MAX_SOURCE_BYTES} bytes")

    source_format = MEDIA_TYPE_FORMATS.get(media_type)
    if source_format is None:
        raise ValueError(f"unsupported media_type: {media_type}")
    if source_format == "PDF":
        with source_path.open("rb") as stream:
            if not stream.read(8).startswith(b"%PDF-"):
                raise ValueError("source bytes do not contain a PDF header")
        return "PDF"
    expected_root = {
        "DOCX": "word/document.xml",
        "PPTX": "ppt/presentation.xml",
        "XLSX": "xl/workbook.xml",
    }.get(source_format)
    if expected_root is not None:
        _validate_ooxml_package(source_path, expected_root, source_format)
        return source_format
    if source_format in {"CSV", "MARKDOWN", "TXT"}:
        try:
            source_path.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            raise ValueError(f"{source_format} source must be UTF-8 text") from exc
        return source_format
    with source_path.open("rb") as stream:
        header = stream.read(8)
    if source_format == "PNG" and header != b"\x89PNG\r\n\x1a\n":
        raise ValueError("source bytes do not contain a PNG signature")
    if source_format == "JPEG" and not header.startswith(b"\xff\xd8\xff"):
        raise ValueError("source bytes do not contain a JPEG signature")
    return source_format


def _validate_ooxml_package(
    source_path: Path, required_root: str, source_format: str
) -> None:
    """Apply bounded ZIP checks before delegating an OOXML source.

    中文：在委派 Office 适配器之前限制压缩包条目数和展开大小。
    """

    try:
        with zipfile.ZipFile(source_path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_OOXML_ENTRIES:
                raise ValueError("OOXML archive contains too many entries")
            expanded_size = sum(entry.file_size for entry in entries)
            if expanded_size > MAX_OOXML_EXPANDED_BYTES:
                raise ValueError("OOXML expanded size exceeds the safe limit")
            names = {entry.filename for entry in entries}
            if "[Content_Types].xml" not in names or required_root not in names:
                raise ValueError(f"{source_format} archive is missing required parts")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError(
            f"source bytes are not a readable {source_format} package"
        ) from exc


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
