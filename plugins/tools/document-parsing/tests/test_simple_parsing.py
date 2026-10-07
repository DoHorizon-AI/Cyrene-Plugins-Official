"""Exercise generated UTF-8, Markdown, CSV, PNG, and JPEG sources.

中文：使用生成的 UTF-8、Markdown、CSV、PNG 与 JPEG 来源进行验证。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest
import simple_parsing
from ocr_parsing import ocr_config_from_env
from parsing_types import OcrRegion, OcrResult, ParseContext, ParserDiagnostic
from PIL import Image, ImageDraw, ImageFont
from simple_parsing import parse_simple

_OCR_PREFIX = "CYRENE_DOCUMENT_PARSING_"


def test_text_keeps_original_line_endings_and_line_locations(tmp_path: Path) -> None:
    """Keep CRLF and content exactly while indexing one-based physical lines."""

    source = tmp_path / "source.txt"
    source.write_bytes("first line\r\n第二行\nlast".encode())
    context = _context(source, "text/plain")

    result = parse_simple(source, context)

    assert result.payload["raw_text"] == "first line\r\n第二行\nlast"
    assert result.payload["line_count"] == 3
    assert [block["locator"]["item_ref"] for block in result.blocks] == [
        "line:1",
        "line:2",
        "line:3",
    ]
    assert [block["text"] for block in result.blocks] == [
        "first line\r\n",
        "第二行\n",
        "last",
    ]


def test_rejects_invalid_utf8_without_replacement(tmp_path: Path) -> None:
    """Never guess a source encoding or silently replace invalid bytes."""

    source = tmp_path / "invalid.txt"
    source.write_bytes(b"valid\xfftail")

    with pytest.raises(ValueError, match="no encoding fallback"):
        parse_simple(source, _context(source, "text/plain"))


def test_markdown_preserves_headings_lines_code_tables_and_references(
    tmp_path: Path,
) -> None:
    """Preserve source Markdown while marking links/images as unextracted references."""

    raw_text = (
        "# Dataset\r\n"
        "intro [guide](https://example.invalid/guide) and ![diagram](diagram.png)\r\n"
        "\r\n"
        "| name | value |\r\n"
        "| --- | --- |\r\n"
        "| alpha | beta |\r\n"
        "\r\n"
        "```md\r\n"
        "![code example](ignored.png)\r\n"
        "```\r\n"
        "Heading underlined\n"
        "------------------\n"
    )
    source = tmp_path / "source.md"
    source.write_bytes(raw_text.encode("utf-8"))

    result = parse_simple(source, _context(source, "text/markdown"))

    assert result.payload["raw_text"] == raw_text
    assert "# Dataset\r\n" in [block["text"] for block in result.blocks]
    assert any(
        block["kind"] == "table" and "| alpha | beta |\r\n" in block["text"]
        for block in result.blocks
    )
    code = next(block for block in result.blocks if block["kind"] == "code")
    assert "ignored.png" in code["text"]
    assert len(result.unsupported_content) == 2
    assert {item["raw_syntax"] for item in result.unsupported_content} == {
        "[guide](https://example.invalid/guide)",
        "![diagram](diagram.png)",
    }
    assert all(
        "not fetched or extracted" in item["message"]
        for item in result.unsupported_content
    )
    assert result.status == "partial_success"
    assert any(
        block["kind"] == "heading" and block["locator"]["tree_level"] == 2
        for block in result.blocks
    )


def test_csv_keeps_quoted_comma_multiline_fields_and_header(tmp_path: Path) -> None:
    """Use the standard CSV reader for quoted delimiters and multiline records."""

    raw_text = 'name,notes,count\r\n"Ada, Lovelace","first line\r\nsecond line",2\r\nGrace,plain,3\r\n'
    source = tmp_path / "people.csv"
    source.write_bytes(raw_text.encode("utf-8"))

    result = parse_simple(source, _context(source, "text/csv"))

    assert result.payload["raw_text"] == raw_text
    assert result.payload["header"] == ["name", "notes", "count"]
    assert [record["values"] for record in result.payload["records"]] == [
        ["Ada, Lovelace", "first line\r\nsecond line", "2"],
        ["Grace", "plain", "3"],
    ]
    assert result.payload["records"][0]["locator"] == {"line_start": 2, "line_end": 3}
    assert (
        result.blocks[1]["text"] == '"Ada, Lovelace","first line\r\nsecond line",2\r\n'
    )
    assert result.status == "success"


def test_csv_field_count_mismatch_is_diagnostic_and_raw_record_is_kept(
    tmp_path: Path,
) -> None:
    """Report malformed field counts without discarding their parsed values."""

    source = tmp_path / "uneven.csv"
    source.write_text("a,b\n1,2,3\n", encoding="utf-8")

    result = parse_simple(source, _context(source, "text/csv"))

    assert result.payload["records"][0]["values"] == ["1", "2", "3"]
    assert result.diagnostics[0].code == "csv.field_count_mismatch"
    assert result.unsupported_content[0]["raw_text"] == "1,2,3\n"
    assert result.status == "partial_success"


def test_csv_unclosed_quote_keeps_malformed_source_and_reports_tail(
    tmp_path: Path,
) -> None:
    """Keep an unterminated quoted record and any following bytes visible."""

    raw_text = 'name,notes\nAda,"unfinished\nnext bytes,still quoted\n'
    source = tmp_path / "malformed.csv"
    source.write_bytes(raw_text.encode("utf-8"))

    result = parse_simple(source, _context(source, "text/csv"))

    assert result.payload["raw_text"] == raw_text
    assert result.diagnostics[0].code == "csv.malformed_record"
    assert (
        result.unsupported_content[0]["raw_text"]
        == 'Ada,"unfinished\nnext bytes,still quoted\n'
    )
    assert result.status == "partial_success"


@pytest.mark.parametrize(
    ("image_format", "media_type", "extension"),
    [("PNG", "image/png", ".png"), ("JPEG", "image/jpeg", ".jpg")],
)
def test_images_keep_ocr_bbox_confidence_and_source_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    image_format: str,
    media_type: str,
    extension: str,
) -> None:
    """Retain the actual OCR evidence for locally decoded PNG and JPEG fixtures."""

    source = tmp_path / f"sample{extension}"
    image = Image.new("RGB", (120, 50), "white")
    image.save(source, format=image_format)

    def fake_ocr(
        image: Image.Image, *, context: ParseContext, page_number: int, config: object
    ) -> OcrResult:
        return OcrResult(
            text="visible words",
            engine="tesseract-cli",
            languages=("eng",),
            confidence=0.87,
            regions=(
                OcrRegion(
                    text="visible words",
                    bbox={"x": 4.0, "y": 8.0, "width": 80.0, "height": 15.0},
                    confidence=0.87,
                ),
            ),
            diagnostics=(
                ParserDiagnostic(
                    code="ocr.low_confidence",
                    message="OCR confidence is low.",
                    kind="ocr",
                    locator={"page_number": page_number, "item_ref": "image:line:1"},
                    confidence=0.87,
                ),
            ),
        )

    monkeypatch.setattr(simple_parsing, "ocr_image", fake_ocr)
    result = parse_simple(source, _context(source, media_type))

    assert result.payload["image"]["format"] == image_format
    assert result.payload["image"]["width"] == 120
    assert result.payload["ocr"]["text"] == "visible words"
    assert result.payload["ocr"]["regions"][0]["bbox"] == {
        "x": 4.0,
        "y": 8.0,
        "width": 80.0,
        "height": 15.0,
    }
    assert result.blocks[0]["locator"]["provenance"][0]["confidence"] == 0.87
    assert result.diagnostics[0].code == "ocr.low_confidence"
    assert result.status == "partial_success"


def test_empty_image_ocr_is_partial_and_marks_graphic_content_unparsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not report graphics-only or unprocessed image data as an empty success."""

    source = tmp_path / "graphic.png"
    Image.new("RGB", (60, 60), "white").save(source, format="PNG")

    def fake_ocr(
        image: Image.Image, *, context: ParseContext, page_number: int, config: object
    ) -> OcrResult:
        return OcrResult(
            text="",
            engine="unavailable",
            languages=("eng",),
            diagnostics=(
                ParserDiagnostic(
                    code="ocr.engine_unavailable",
                    message="No local Tesseract executable is configured.",
                    kind="ocr",
                    locator={"page_number": page_number},
                ),
            ),
        )

    monkeypatch.setattr(simple_parsing, "ocr_image", fake_ocr)
    result = parse_simple(source, _context(source, "image/png"))

    assert result.status == "partial_success"
    assert result.blocks == []
    assert result.unsupported_content[0]["code"] == "image.no_extracted_text"
    assert "graphics only" in result.unsupported_content[0]["message"]
    assert {item.code for item in result.diagnostics} == {
        "ocr.engine_unavailable",
        "image.no_extracted_text",
    }


def test_image_media_type_must_match_real_bytes(tmp_path: Path) -> None:
    """Reject a PNG byte stream falsely labeled as JPEG."""

    source = tmp_path / "mislabelled.jpg"
    Image.new("RGB", (10, 10), "white").save(source, format="PNG")

    with pytest.raises(ValueError, match="does not match detected PNG"):
        parse_simple(source, _context(source, "image/jpeg"))


def test_simple_image_adapter_runs_real_local_tesseract_when_available(
    tmp_path: Path,
) -> None:
    """Exercise the full PNG adapter against a generated image and local engine."""

    command = os.environ.get(f"{_OCR_PREFIX}TESSERACT_CMD") or shutil.which("tesseract")
    if not command:
        pytest.skip("SKIP: no local Tesseract executable is configured or available")

    source = tmp_path / "generated-ocr.png"
    image = Image.new("RGB", (900, 180), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48
        )
    except OSError:
        font = ImageFont.load_default(size=48)
    draw.text((30, 45), "CYRENE OCR 123", fill="black", font=font)
    image.save(source, format="PNG")

    result = parse_simple(source, _context(source, "image/png"), ocr_config_from_env())
    missing_prerequisites = {
        "ocr.engine_unavailable",
        "ocr.language_data_unavailable",
        "ocr.language_unavailable",
    }
    if any(item.code in missing_prerequisites for item in result.diagnostics):
        pytest.skip(
            "SKIP: local Tesseract or configured language data is unavailable "
            f"({result.diagnostics[0].code})"
        )

    assert result.payload["ocr"]["engine"] == "tesseract-cli"
    assert "OCR" in result.payload["ocr"]["text"].upper()
    assert result.blocks
    assert result.blocks[0]["locator"]["provenance"][0]["bbox"]["width"] > 0
    assert result.page_count == 1


def _context(source: Path, media_type: str) -> ParseContext:
    """Create a context whose digest matches the generated source file."""

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return ParseContext(
        source_ref=f"source:{source.name}",
        source_digest=f"sha256:{digest}",
        filename=source.name,
        media_type=media_type,
    )
