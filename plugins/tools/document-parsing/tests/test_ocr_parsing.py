"""Verify local OCR configuration, TSV geometry, and safe failure behavior.

中文：验证本机 OCR 配置、TSV 坐标与安全失败路径。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import ocr_parsing
import pytest
from ocr_parsing import ocr_config_from_env, ocr_image
from parsing_types import OcrConfig, ParseContext
from PIL import Image, ImageDraw, ImageFont

_PREFIX = "CYRENE_DOCUMENT_PARSING_"
_TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


def test_parses_tesseract_tsv_and_reports_low_confidence_regions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group word rows into lines, union their boxes, and retain confidence."""

    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(command)
        if command[-1] == "--list-langs":
            return SimpleNamespace(
                returncode=0,
                stdout="List of available languages (2):\neng\nchi_sim\n",
                stderr="",
            )
        output = _TSV_HEADER + (
            "5\t1\t1\t1\t1\t1\t5\t10\t35\t24\t97\tCyrene\n"
            "5\t1\t1\t1\t1\t2\t48\t10\t47\t24\t70\tParser\n"
            "5\t1\t1\t1\t2\t1\t5\t50\t90\t22\t95\tSecond\n"
        )
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(ocr_parsing.subprocess, "run", fake_run)
    image = Image.new("RGB", (200, 100), "white")
    result = ocr_image(
        image,
        context=_context("ocr.png"),
        page_number=3,
        config=OcrConfig(
            engine="tesseract-cli", languages=("eng",), tesseract_cmd="tesseract"
        ),
    )

    assert result.text == "Cyrene Parser\nSecond"
    assert len(result.regions) == 2
    assert result.regions[0].bbox == {
        "x": 5.0,
        "y": 10.0,
        "width": 90.0,
        "height": 24.0,
    }
    assert result.regions[0].confidence == pytest.approx(0.7)
    assert result.diagnostics[0].code == "ocr.low_confidence"
    assert result.diagnostics[0].locator == {
        "page_number": 3,
        "source_basename": "ocr.png",
        "item_ref": "page:3:line:1",
        "bbox": result.regions[0].bbox,
    }
    assert result.diagnostics[0].confidence == pytest.approx(0.7)
    assert calls[1][-1] == "tsv"
    assert "--dpi" in calls[1]


def test_reports_missing_language_without_leaking_paths_or_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make missing language data actionable without exposing host paths."""

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0, stdout="eng\n", stderr="/private/path is not available"
        )

    monkeypatch.setattr(ocr_parsing.subprocess, "run", fake_run)
    result = ocr_image(
        Image.new("RGB", (20, 20), "white"),
        context=_context("scan.png"),
        page_number=1,
        config=OcrConfig(
            engine="tesseract-cli",
            languages=("chi_sim",),
            tesseract_cmd="/private/path/tesseract",
            tessdata_prefix=Path("/private/tessdata"),
        ),
    )

    assert result.diagnostics[0].code == "ocr.language_unavailable"
    assert "chi_sim" in result.diagnostics[0].message
    assert "/private" not in result.diagnostics[0].message


def test_does_not_expose_subprocess_stderr_on_engine_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep command output private when the local engine exits unsuccessfully."""

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        if command[-1] == "--list-langs":
            return SimpleNamespace(returncode=0, stdout="eng\n", stderr="")
        return SimpleNamespace(
            returncode=2, stdout="", stderr="SECRET /root/private/token"
        )

    monkeypatch.setattr(ocr_parsing.subprocess, "run", fake_run)
    result = ocr_image(
        Image.new("RGB", (20, 20), "white"),
        context=_context("scan.png"),
        page_number=2,
        config=OcrConfig(engine="tesseract-cli", tesseract_cmd="tesseract"),
    )

    assert result.diagnostics[0].code == "ocr.engine_failure"
    assert "SECRET" not in result.diagnostics[0].message
    assert "/root" not in result.diagnostics[0].message


def test_reports_no_text_after_successful_empty_tsv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An image with no OCR rows remains explicitly unresolved."""

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        output = "eng\n" if command[-1] == "--list-langs" else _TSV_HEADER
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(ocr_parsing.subprocess, "run", fake_run)
    result = ocr_image(
        Image.new("RGB", (20, 20), "white"),
        context=_context("graphic.png"),
        page_number=1,
        config=OcrConfig(engine="tesseract-cli", tesseract_cmd="tesseract"),
    )

    assert result.text == ""
    assert result.regions == ()
    assert result.diagnostics[0].code == "ocr.no_text_detected"


def test_rapidocr_is_explicitly_unavailable_and_never_downloaded() -> None:
    """An unimplemented optional engine returns a warning instead of fetching a model."""

    result = ocr_image(
        Image.new("RGB", (10, 10), "white"),
        context=_context("scan.png"),
        page_number=1,
        config=OcrConfig(engine="rapidocr"),
    )

    assert result.engine == "rapidocr"
    assert result.diagnostics[0].code == "ocr.engine_unavailable"
    assert "no model was downloaded" in result.diagnostics[0].message


def test_none_engine_reports_disabled_ocr() -> None:
    """An operator can disable OCR without receiving a success-empty result."""

    result = ocr_image(
        Image.new("RGB", (10, 10), "white"),
        context=_context("scan.png"),
        page_number=1,
        config=OcrConfig(engine="none"),
    )

    assert result.diagnostics[0].code == "ocr.engine_disabled"


def test_missing_tesseract_returns_unavailable_diagnostic_without_throwing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return a basename-scoped operator warning when no local engine exists."""

    monkeypatch.delenv(f"{_PREFIX}TESSERACT_CMD", raising=False)
    monkeypatch.setattr(ocr_parsing.shutil, "which", lambda _: None)
    context = ParseContext(
        source_ref="source:opaque-id",
        source_digest="sha256:" + "a" * 64,
        filename="/private/input/scan.png",
        media_type="image/png",
    )

    result = ocr_image(
        Image.new("RGB", (10, 10), "white"),
        context=context,
        page_number=4,
        config=OcrConfig(engine="auto"),
    )

    assert result.engine == "unavailable"
    assert result.diagnostics[0].code == "ocr.engine_unavailable"
    assert result.diagnostics[0].locator == {
        "page_number": 4,
        "source_basename": "scan.png",
    }
    assert "/private" not in result.diagnostics[0].message


def test_ocr_config_reads_operator_variables_and_standard_tessdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read the documented environment settings and prefer the parser override."""

    monkeypatch.setenv(f"{_PREFIX}OCR_ENGINE", "tesseract-cli")
    monkeypatch.setenv(f"{_PREFIX}OCR_LANGUAGES", "eng, chi_sim")
    monkeypatch.setenv(f"{_PREFIX}OCR_DPI", "300")
    monkeypatch.setenv(f"{_PREFIX}OCR_MINIMUM_CONFIDENCE", "0.65")
    monkeypatch.setenv(f"{_PREFIX}TESSERACT_CMD", "/opt/tesseract")
    monkeypatch.setenv("TESSDATA_PREFIX", "/opt/tessdata-standard")
    monkeypatch.setenv(f"{_PREFIX}TESSDATA_PREFIX", "/opt/tessdata-override")

    config = ocr_config_from_env()

    assert config.engine == "tesseract-cli"
    assert config.languages == ("eng", "chi_sim")
    assert config.dpi == 300
    assert config.minimum_confidence == pytest.approx(0.65)
    assert config.tesseract_cmd == "/opt/tesseract"
    assert config.tessdata_prefix == Path("/opt/tessdata-override")
    assert config.artifacts_path is None


def test_ocr_config_rejects_invalid_threshold_with_environment_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid operator confidence values fail before starting the OCR process."""

    monkeypatch.setenv(f"{_PREFIX}OCR_MINIMUM_CONFIDENCE", "1.2")
    with pytest.raises(ValueError, match="OCR_MINIMUM_CONFIDENCE"):
        ocr_config_from_env()


def test_real_local_tesseract_ocr_on_generated_image() -> None:
    """Run a real local OCR smoke test, skipping only when the engine/data is absent."""

    command = os.environ.get(f"{_PREFIX}TESSERACT_CMD") or shutil.which("tesseract")
    if not command:
        pytest.skip("SKIP: no local Tesseract executable is configured or available")

    image = Image.new("RGB", (900, 180), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48
        )
    except OSError:
        font = ImageFont.load_default(size=48)
    draw.text((30, 45), "CYRENE OCR 123", fill="black", font=font, stroke_width=0)
    result = ocr_image(
        image,
        context=_context("generated-real-ocr.png"),
        page_number=1,
        config=ocr_config_from_env(),
    )

    availability_codes = {
        "ocr.engine_unavailable",
        "ocr.language_data_unavailable",
        "ocr.language_unavailable",
    }
    if any(item.code in availability_codes for item in result.diagnostics):
        pytest.skip(
            f"SKIP: local OCR prerequisites are missing ({result.diagnostics[0].code})"
        )
    assert result.regions, "real Tesseract should recognize the generated text image"
    assert "OCR" in result.text.upper()
    assert result.regions[0].bbox["width"] > 0
    assert result.regions[0].confidence is not None


def _context(filename: str) -> ParseContext:
    """Build a stable image context for an in-memory OCR fixture."""

    return ParseContext(
        source_ref=f"source:{filename}",
        source_digest="sha256:" + "a" * 64,
        filename=filename,
        media_type="image/png",
    )
