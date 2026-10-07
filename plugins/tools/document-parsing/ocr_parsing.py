"""Local Tesseract OCR adapter for images and rendered document pages.

中文：为图片和已渲染文档页提供本机 Tesseract OCR，不下载模型。
"""

from __future__ import annotations

import csv
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections import OrderedDict
from io import StringIO
from pathlib import Path
from typing import Any

from parsing_types import (
    OcrConfig,
    OcrRegion,
    OcrResult,
    ParseContext,
    ParserDiagnostic,
)
from PIL import Image

_ENV_PREFIX = "CYRENE_DOCUMENT_PARSING_"
_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9_+-]+$")


def ocr_config_from_env() -> OcrConfig:
    """Build a validated, local-only OCR configuration from process settings.

    The parser-specific tessdata setting takes precedence over the standard
    ``TESSDATA_PREFIX`` variable. Paths are consumed by the subprocess and are
    never copied into public diagnostics.
    中文：从进程环境读取并校验本机 OCR 配置；模型目录不会写入公开诊断。
    """

    engine = os.environ.get(f"{_ENV_PREFIX}OCR_ENGINE", "auto").strip().lower()
    if engine not in {"auto", "tesseract-cli", "rapidocr", "none"}:
        raise ValueError(
            f"{_ENV_PREFIX}OCR_ENGINE must be auto, tesseract-cli, rapidocr, or none"
        )

    raw_languages = os.environ.get(f"{_ENV_PREFIX}OCR_LANGUAGES", "eng")
    languages = tuple(part.strip() for part in raw_languages.split(",") if part.strip())
    if not languages or any(
        not _LANGUAGE_PATTERN.fullmatch(item) for item in languages
    ):
        raise ValueError(
            f"{_ENV_PREFIX}OCR_LANGUAGES must be a comma-separated list of language codes"
        )

    dpi = _positive_integer(
        os.environ.get(f"{_ENV_PREFIX}OCR_DPI", "200"), f"{_ENV_PREFIX}OCR_DPI"
    )
    minimum_confidence = _unit_interval(
        os.environ.get(f"{_ENV_PREFIX}OCR_MINIMUM_CONFIDENCE", "0.8"),
        f"{_ENV_PREFIX}OCR_MINIMUM_CONFIDENCE",
    )

    tesseract_cmd = _optional_environment_value(f"{_ENV_PREFIX}TESSERACT_CMD")
    tessdata_value = _optional_environment_value(
        f"{_ENV_PREFIX}TESSDATA_PREFIX"
    ) or _optional_environment_value("TESSDATA_PREFIX")
    return OcrConfig(
        engine=engine,  # type: ignore[arg-type] -- validated above
        languages=languages,
        dpi=dpi,
        minimum_confidence=minimum_confidence,
        tesseract_cmd=tesseract_cmd,
        tessdata_prefix=Path(tessdata_value) if tessdata_value else None,
    )


def ocr_image(
    image: Image.Image,
    *,
    context: ParseContext,
    page_number: int,
    config: OcrConfig,
) -> OcrResult:
    """Recognize text with an already installed local Tesseract executable.

    Returned boxes use source-image pixel coordinates. Every recognized line
    retains a conservative minimum word confidence so downstream review can
    identify low-confidence evidence. Unsupported engines never trigger a
    download or model initialization.
    中文：只调用已安装的 Tesseract；坐标采用源图像像素，低置信区域保留供复核。
    """

    if page_number < 1:
        raise ValueError("page_number must be one-based")
    if config.engine not in {"auto", "tesseract-cli", "rapidocr", "none"}:
        raise ValueError("engine must be auto, tesseract-cli, rapidocr, or none")
    if config.dpi < 1:
        raise ValueError("dpi must be a positive integer")
    if (
        not math.isfinite(config.minimum_confidence)
        or not 0.0 <= config.minimum_confidence <= 1.0
    ):
        raise ValueError("minimum_confidence must be between 0 and 1")

    base = {
        "text": "",
        "engine": "unavailable"
        if config.engine in {"auto", "tesseract-cli"}
        else config.engine,
        "languages": config.languages,
    }
    if config.engine == "none":
        diagnostic = _diagnostic(
            "ocr.engine_disabled",
            "OCR is disabled by the local parser configuration.",
            context,
            page_number,
        )
        return OcrResult(**base, diagnostics=(diagnostic,))
    if config.engine == "rapidocr":
        diagnostic = _diagnostic(
            "ocr.engine_unavailable",
            "The requested RapidOCR adapter is not available; no model was downloaded.",
            context,
            page_number,
        )
        return OcrResult(**base, diagnostics=(diagnostic,))

    command = config.tesseract_cmd or os.environ.get(f"{_ENV_PREFIX}TESSERACT_CMD")
    if not command:
        command = shutil.which("tesseract")
    if not command:
        diagnostic = _diagnostic(
            "ocr.engine_unavailable",
            "No local Tesseract executable is configured or available on PATH.",
            context,
            page_number,
        )
        return OcrResult(**base, diagnostics=(diagnostic,))

    if not config.languages or any(
        not _LANGUAGE_PATTERN.fullmatch(lang) for lang in config.languages
    ):
        diagnostic = _diagnostic(
            "ocr.language_invalid",
            "The configured OCR language list contains an invalid language code.",
            context,
            page_number,
        )
        return OcrResult(**base, diagnostics=(diagnostic,))

    process_env = os.environ.copy()
    tessdata_prefix = config.tessdata_prefix or _optional_environment_value(
        f"{_ENV_PREFIX}TESSDATA_PREFIX"
    )
    if tessdata_prefix:
        process_env["TESSDATA_PREFIX"] = str(tessdata_prefix)

    try:
        language_result = subprocess.run(
            [command, "--list-langs"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            env=process_env,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        diagnostic = _execution_diagnostic(
            exc, context, page_number, "query local language data"
        )
        return OcrResult(**base, diagnostics=(diagnostic,))

    if language_result.returncode != 0:
        diagnostic = _diagnostic(
            "ocr.language_data_unavailable",
            "Local Tesseract could not list installed language data; check its language-data installation.",
            context,
            page_number,
        )
        return OcrResult(**base, diagnostics=(diagnostic,))

    available_languages = {
        line.strip()
        for line in language_result.stdout.splitlines()
        if _LANGUAGE_PATTERN.fullmatch(line.strip())
    }
    missing_languages = tuple(
        lang for lang in config.languages if lang not in available_languages
    )
    if missing_languages:
        languages = ", ".join(missing_languages)
        diagnostic = _diagnostic(
            "ocr.language_unavailable",
            f"Local Tesseract language data is missing for: {languages}.",
            context,
            page_number,
        )
        return OcrResult(**base, diagnostics=(diagnostic,))

    try:
        with tempfile.TemporaryDirectory(
            prefix="cyrene-document-ocr-"
        ) as temporary_directory:
            image_path = Path(temporary_directory) / "source.png"
            with image.convert("RGB") as normalized_image:
                normalized_image.save(image_path, format="PNG")
            language_argument = "+".join(config.languages)
            result = subprocess.run(
                [
                    command,
                    str(image_path),
                    "stdout",
                    "-l",
                    language_argument,
                    "--psm",
                    "3",
                    "--dpi",
                    str(config.dpi),
                    "tsv",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                env=process_env,
            )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        diagnostic = _execution_diagnostic(exc, context, page_number, "run local OCR")
        return OcrResult(**base, diagnostics=(diagnostic,))

    if result.returncode != 0:
        diagnostic = _diagnostic(
            "ocr.engine_failure",
            "Local Tesseract could not process the image; check the image and configured language data.",
            context,
            page_number,
        )
        return OcrResult(**base, diagnostics=(diagnostic,))

    try:
        regions = _parse_tesseract_tsv(
            result.stdout,
        )
    except (csv.Error, KeyError, TypeError, ValueError) as exc:
        diagnostic = _diagnostic(
            "ocr.tsv_invalid",
            f"Local Tesseract returned unreadable TSV output ({type(exc).__name__}).",
            context,
            page_number,
        )
        return OcrResult(**base, diagnostics=(diagnostic,))

    text = "\n".join(region.text for region in regions)
    confidences = [
        region.confidence for region in regions if region.confidence is not None
    ]
    confidence = sum(confidences) / len(confidences) if confidences else None
    diagnostics = tuple(
        diagnostic
        for index, region in enumerate(regions, start=1)
        if region.confidence is not None
        and region.confidence < config.minimum_confidence
        for diagnostic in (
            ParserDiagnostic(
                code="ocr.low_confidence",
                message=(
                    f"OCR confidence {region.confidence:.3f} is below the configured "
                    f"minimum {config.minimum_confidence:.3f}."
                ),
                kind="ocr",
                locator={
                    **_page_locator(context, page_number),
                    "item_ref": f"page:{page_number}:line:{index}",
                    "bbox": dict(region.bbox),
                },
                confidence=region.confidence,
            ),
        )
    )
    if not regions:
        diagnostics += (
            _diagnostic(
                "ocr.no_text_detected",
                "OCR completed but found no readable text; the image may contain graphics only.",
                context,
                page_number,
            ),
        )

    return OcrResult(
        text=text,
        engine="tesseract-cli",
        languages=config.languages,
        confidence=confidence,
        regions=regions,
        diagnostics=diagnostics,
    )


def _parse_tesseract_tsv(
    tsv: str,
) -> tuple[OcrRegion, ...]:
    """Convert Tesseract word rows into ordered line regions and diagnostics."""

    reader = csv.DictReader(StringIO(tsv, newline=""), delimiter="\t", strict=True)
    required = {
        "level",
        "block_num",
        "par_num",
        "line_num",
        "left",
        "top",
        "width",
        "height",
        "conf",
        "text",
    }
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise ValueError("required TSV columns are missing")

    lines: OrderedDict[tuple[int, int, int], list[dict[str, Any]]] = OrderedDict()
    for row in reader:
        if row.get("level") != "5":
            continue
        word = (row.get("text") or "").strip()
        if not word:
            continue
        try:
            confidence_value = float(row["conf"])
            if confidence_value < 0 or not math.isfinite(confidence_value):
                continue
            confidence = min(1.0, max(0.0, confidence_value / 100.0))
            left = int(row["left"])
            top = int(row["top"])
            width = int(row["width"])
            height = int(row["height"])
            key = (int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid word row value") from exc
        if width < 0 or height < 0:
            raise ValueError("negative word box size")
        lines.setdefault(key, []).append(
            {
                "text": word,
                "confidence": confidence,
                "left": left,
                "top": top,
                "right": left + width,
                "bottom": top + height,
            }
        )

    regions: list[OcrRegion] = []
    for words in lines.values():
        confidences = [word["confidence"] for word in words]
        left = min(word["left"] for word in words)
        top = min(word["top"] for word in words)
        right = max(word["right"] for word in words)
        bottom = max(word["bottom"] for word in words)
        regions.append(
            OcrRegion(
                text=" ".join(word["text"] for word in words),
                bbox={
                    "x": float(left),
                    "y": float(top),
                    "width": float(right - left),
                    "height": float(bottom - top),
                },
                confidence=min(confidences),
            )
        )
    return tuple(regions)


def _execution_diagnostic(
    exception: BaseException,
    context: ParseContext,
    page_number: int,
    operation: str,
) -> ParserDiagnostic:
    """Describe a subprocess failure without exposing its path or stderr."""

    if isinstance(exception, FileNotFoundError):
        reason = "the configured local Tesseract executable is unavailable"
    elif isinstance(exception, subprocess.TimeoutExpired):
        reason = "local Tesseract exceeded its time limit"
    elif isinstance(exception, PermissionError):
        reason = "the local Tesseract executable could not be started"
    else:
        reason = "local Tesseract could not be started"
    return _diagnostic(
        "ocr.engine_unavailable"
        if isinstance(exception, FileNotFoundError)
        else "ocr.engine_failure",
        f"Could not {operation}: {reason} ({type(exception).__name__}).",
        context,
        page_number,
    )


def _diagnostic(
    code: str,
    message: str,
    context: ParseContext,
    page_number: int,
) -> ParserDiagnostic:
    return ParserDiagnostic(
        code=code,
        message=message,
        kind="ocr",
        locator=_page_locator(context, page_number),
    )


def _page_locator(context: ParseContext, page_number: int) -> dict[str, Any]:
    """Return page identity with a basename only, never a host path."""

    locator: dict[str, Any] = {"page_number": page_number}
    basename = context.filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    if basename:
        locator["source_basename"] = basename
    return locator


def _positive_integer(raw_value: str, variable_name: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{variable_name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{variable_name} must be a positive integer")
    return value


def _unit_interval(raw_value: str, variable_name: str) -> float:
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{variable_name} must be between 0 and 1") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{variable_name} must be between 0 and 1")
    return value


def _optional_environment_value(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None
