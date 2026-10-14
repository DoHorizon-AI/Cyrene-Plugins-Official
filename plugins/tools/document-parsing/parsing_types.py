"""Shared return types for stateless document parser adapters.

中文：无状态文档解析 adapter 共用的返回类型。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ParseStatus = Literal["success", "partial_success"]
DiagnosticKind = Literal["parser", "ocr"]
OcrEngine = Literal["auto", "tesseract-cli", "rapidocr", "none"]


@dataclass(frozen=True, slots=True)
class ParseContext:
    """Stable source identity shared with each per-format adapter.

    中文：传递给各格式 adapter 的稳定来源身份。
    """

    source_ref: str
    source_digest: str
    filename: str
    media_type: str


@dataclass(frozen=True, slots=True)
class ParserDiagnostic:
    """One non-fatal parser or OCR warning for a source or located item.

    Confidence is normalized to the inclusive range 0.0–1.0 when available.
    中文：来源级或定位到内容项的非致命解析/OCR 警告；可用置信度归一化到 0–1。
    """

    code: str
    message: str
    kind: DiagnosticKind
    severity: Literal["warning"] = "warning"
    locator: dict[str, Any] | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class OcrConfig:
    """Operator-selected local OCR settings; adapters must not fetch models.

    中文：由运行环境选择的本地 OCR 设置；adapter 不负责下载模型。
    """

    engine: OcrEngine = "auto"
    languages: tuple[str, ...] = ("eng",)
    dpi: int = 200
    minimum_confidence: float = 0.8
    tesseract_cmd: str | None = None
    tessdata_prefix: Path | None = None
    artifacts_path: Path | None = None


@dataclass(frozen=True, slots=True)
class OcrRegion:
    """OCR text and geometry for one recognized page region.

    中文：一个识别区域的文字、归一化置信度与页内坐标。
    """

    text: str
    bbox: dict[str, float]
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class OcrResult:
    """Local OCR output returned to a source-format adapter.

    中文：返回给来源格式 adapter 的本地 OCR 结果。
    """

    text: str
    engine: str
    languages: tuple[str, ...]
    confidence: float | None = None
    regions: tuple[OcrRegion, ...] = ()
    diagnostics: tuple[ParserDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Normalized result returned by a format-specific adapter.

    The caller persists the JSON payload and blocks and computes their receipts.
    中文：格式 adapter 返回规范结果；调用方负责写制品并计算摘要回执。
    """

    payload: dict[str, Any]
    blocks: list[dict[str, Any]]
    source_format: str
    conversion_profile: str
    status: ParseStatus
    page_count: int | None = None
    slide_count: int | None = None
    sheet_count: int | None = None
    warnings: list[str] = field(default_factory=list)
    diagnostics: list[ParserDiagnostic] = field(default_factory=list)
    unsupported_content: list[dict[str, Any]] = field(default_factory=list)


def make_block(
    context: ParseContext,
    *,
    ordinal: int,
    item_ref: str,
    kind: str,
    text: str,
    source_pages: Sequence[int] = (),
    section_path: Sequence[str] = (),
    tree_level: int = 0,
    table_index: int | None = None,
    provenance: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build the shared public block shape and deterministic source-scoped ID.

    Block identity includes both the logical source reference and the byte digest,
    so equal bytes attached to distinct source records do not collapse together.
    中文：生成统一公开 block 与稳定身份；相同字节的不同逻辑来源不会合并。
    """

    block_hash = hashlib.sha256(
        f"{context.source_ref}\0{context.source_digest}\0{item_ref}".encode()
    ).hexdigest()
    locator: dict[str, Any] = {
        "source_pages": list(source_pages),
        "section_path": list(section_path),
        "item_ref": item_ref,
        "tree_level": tree_level,
        "provenance": [dict(item) for item in provenance],
    }
    if table_index is not None:
        locator["table_index"] = table_index
    return {
        "id": f"sha256:{block_hash}",
        "ordinal": ordinal,
        "kind": kind,
        "text": text,
        "locator": locator,
    }
