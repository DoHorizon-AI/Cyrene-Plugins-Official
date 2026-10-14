"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_document_parsing.py                                        │
│  Module: tests.test_document_parsing                                │
│  Role: Verify real local Docling PDF and DOCX parsing.              │
│                                                                     │
│  模块职责：使用生成的公开样例验证真实 Docling PDF 与 DOCX 解析。        │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import ocr_parsing
import pytest
from document_parsing import DocumentParsingPlugin, TypedPayload
from parsing_types import OcrRegion, OcrResult, ParseContext, ParserDiagnostic
from PIL import Image, ImageDraw

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
TEXT_MEDIA_TYPE = "text/plain"


def test_parses_generated_pdf_with_real_docling(tmp_path: Path) -> None:
    """Parse a local text PDF and verify its full document, locators, and receipts."""

    source = tmp_path / "sample.pdf"
    _write_text_pdf(source, "Catalyst parser sample PDF")
    result = _parse(source, tmp_path, "source:pdf-sample", PDF_MEDIA_TYPE)

    assert result["source_format"] == "PDF"
    assert result["conversion_profile"] == "pdf-native-no-model-download"
    assert result["page_count"] == 1
    payload = json.loads((tmp_path / "docling.json").read_text(encoding="utf-8"))
    blocks = json.loads((tmp_path / "blocks.json").read_text(encoding="utf-8"))
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert payload["schema_name"]
    assert any(
        "Catalyst parser sample PDF" in block["text"] for block in blocks["blocks"]
    )
    assert blocks["schema_version"] == "cyrene.document.blocks.v1"
    assert blocks["blocks"][0]["locator"]["source_pages"] == [1]
    assert report["receipts"]["payload"]["digest"] == result["payload_digest"]
    assert report["receipts"]["blocks"]["digest"] == result["blocks_digest"]


def test_parses_generated_docx_and_does_not_invent_page_numbers(tmp_path: Path) -> None:
    """Parse a minimal OOXML Word file and leave absent page provenance empty."""

    source = tmp_path / "sample.docx"
    _write_minimal_docx(source, "Catalyst parser sample DOCX")
    result = _parse(source, tmp_path, "source:docx-sample", DOCX_MEDIA_TYPE)

    assert result["source_format"] == "DOCX"
    assert result["conversion_profile"] == "docx-simple"
    blocks = json.loads((tmp_path / "blocks.json").read_text(encoding="utf-8"))
    assert any(
        "Catalyst parser sample DOCX" in block["text"] for block in blocks["blocks"]
    )
    table_blocks = [block for block in blocks["blocks"] if block["kind"] == "table"]
    assert len(table_blocks) == 1
    assert "DOCX table fallback cell" in table_blocks[0]["text"]
    assert all(block["locator"]["source_pages"] == [] for block in blocks["blocks"])
    assert any(
        "python-docx table fallback used" in warning for warning in result["warnings"]
    )


def test_scanned_pdf_page_uses_local_ocr_and_keeps_page_locator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Render one image-only PDF page and retain OCR text with its page and box."""

    source = tmp_path / "scanned.pdf"
    _write_image_pdf(source)
    calls: list[int] = []

    def fake_ocr(
        image: Image.Image,
        *,
        context: ParseContext,
        page_number: int,
        config: object,
    ) -> OcrResult:
        assert image.width > 0 and image.height > 0
        calls.append(page_number)
        return OcrResult(
            text="scanned page text",
            engine="tesseract-cli",
            languages=("eng",),
            confidence=0.94,
            regions=(
                OcrRegion(
                    text="scanned page text",
                    bbox={"x": 12.0, "y": 18.0, "width": 240.0, "height": 40.0},
                    confidence=0.94,
                ),
            ),
        )

    monkeypatch.setattr(ocr_parsing, "ocr_image", fake_ocr)
    result = _parse(source, tmp_path, "source:scanned-pdf", PDF_MEDIA_TYPE)

    blocks = json.loads((tmp_path / "blocks.json").read_text(encoding="utf-8"))
    ocr_block = next(block for block in blocks["blocks"] if block["kind"] == "text")
    assert calls == [1]
    assert ocr_block["text"] == "scanned page text"
    assert ocr_block["locator"]["source_pages"] == [1]
    assert ocr_block["locator"]["provenance"][0]["type"] == "ocr"
    assert ocr_block["locator"]["provenance"][0]["bbox"]["width"] == 240.0
    assert result["conversion_profile"].endswith("+local-page-ocr")


def test_scanned_pdf_without_ocr_text_is_explicitly_unparsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never turn an image-only page into an empty successful conversion."""

    source = tmp_path / "unreadable-scan.pdf"
    _write_image_pdf(source)

    def disabled_ocr(
        image: Image.Image,
        *,
        context: ParseContext,
        page_number: int,
        config: object,
    ) -> OcrResult:
        return OcrResult(
            text="",
            engine="unavailable",
            languages=("eng",),
            diagnostics=(
                ParserDiagnostic(
                    code="ocr.engine_unavailable",
                    message="No local OCR engine is configured.",
                    kind="ocr",
                    locator={"page_number": page_number},
                ),
            ),
        )

    monkeypatch.setattr(ocr_parsing, "ocr_image", disabled_ocr)
    result = _parse(source, tmp_path, "source:unreadable-scan", PDF_MEDIA_TYPE)

    assert result["status"] == "partial_success"
    assert any(
        item["code"] == "ocr.engine_unavailable"
        and item["locator"]["source_pages"] == [1]
        for item in result["diagnostics"]
    )
    assert any(
        item["kind"] == "scanned_page" and item["locator"]["source_pages"] == [1]
        for item in result["unsupported_content"]
    )


def test_direct_runtime_requires_digest_and_exact_payload_fields(
    tmp_path: Path,
) -> None:
    """Reject unknown payload fields before invoking the parser."""

    source = tmp_path / "sample.pdf"
    _write_text_pdf(source, "Digest check")
    payload = {
        "source_path": str(source),
        "payload_path": str(tmp_path / "docling.json"),
        "blocks_path": str(tmp_path / "blocks.json"),
        "result_path": str(tmp_path / "result.json"),
        "source_ref": "source:sample",
        "source_digest": "sha256:" + "0" * 64,
        "filename": source.name,
        "media_type": PDF_MEDIA_TYPE,
        "unexpected": "must be rejected",
    }
    ok, response = DocumentParsingPlugin().on_invoke(
        "document.parsing.v1",
        "parse",
        json.dumps(payload).encode("utf-8"),
        request_type_url="type.cyrene.io/document.parsing.v1.parse.request",
    )

    assert not ok
    assert isinstance(response, str)
    assert "unknown request fields" in response


def test_routes_plain_text_through_shared_payload_and_receipt_envelope(
    tmp_path: Path,
) -> None:
    """Persist a simple adapter result with the common report and digest receipts."""

    source = tmp_path / "notes.txt"
    source.write_text("first line\nsecond line\n", encoding="utf-8")
    result = _parse(source, tmp_path, "source:text-sample", TEXT_MEDIA_TYPE)

    payload = json.loads((tmp_path / "docling.json").read_text(encoding="utf-8"))
    blocks = json.loads((tmp_path / "blocks.json").read_text(encoding="utf-8"))
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["source_format"] == "TXT"
    assert payload["raw_text"] == "first line\nsecond line\n"
    assert [block["locator"]["item_ref"] for block in blocks["blocks"]] == [
        "line:1",
        "line:2",
    ]
    assert report["payload_format"] == "adapter-json"
    assert report["docling_document_digest"] is None
    assert report["receipts"]["payload"]["digest"] == result["payload_digest"]


def test_rejects_source_digest_mismatch(tmp_path: Path) -> None:
    """Reject staged bytes whose digest differs from the SourceRevision receipt."""

    source = tmp_path / "sample.pdf"
    _write_text_pdf(source, "Digest check")
    with pytest.raises(ValueError, match="does not match"):
        DocumentParsingPlugin().parse_document(
            source_path=source,
            payload_path=tmp_path / "docling.json",
            blocks_path=tmp_path / "blocks.json",
            result_path=tmp_path / "result.json",
            source_ref="source:sample",
            source_digest="sha256:" + "0" * 64,
            filename=source.name,
            media_type=PDF_MEDIA_TYPE,
        )


def _parse(
    source_path: Path,
    output_directory: Path,
    source_ref: str,
    media_type: str,
) -> dict[str, object]:
    """Invoke the Plugin's typed implementation for a generated local fixture."""

    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    request = {
        "source_path": str(source_path),
        "payload_path": str(output_directory / "docling.json"),
        "blocks_path": str(output_directory / "blocks.json"),
        "result_path": str(output_directory / "result.json"),
        "source_ref": source_ref,
        "source_digest": f"sha256:{digest}",
        "filename": source_path.name,
        "media_type": media_type,
    }
    ok, response = DocumentParsingPlugin().on_invoke(
        "document.parsing.v1",
        "parse",
        json.dumps(request).encode("utf-8"),
        request_type_url="type.cyrene.io/document.parsing.v1.parse.request",
    )
    assert ok
    assert isinstance(response, TypedPayload)
    assert response.type_url == "type.cyrene.io/document.parsing.v1.parse.response"
    return json.loads(response.value)


def _write_text_pdf(path: Path, text: str) -> None:
    """Write a tiny one-page PDF with an embedded Helvetica text layer."""

    encoded_text = text.encode("ascii")
    stream = b"BT /F1 18 Tf 72 720 Td (" + encoded_text + b") Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"endstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode("ascii"))
        pdf.extend(value + b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "ascii"
        )
    )
    path.write_bytes(pdf)


def _write_image_pdf(path: Path) -> None:
    """Create a one-page raster-only PDF with Pillow's local PDF writer."""

    image = Image.new("RGB", (1200, 500), "white")
    ImageDraw.Draw(image).text((40, 160), "SCANNED PAGE OCR", fill="black")
    image.save(path, format="PDF", resolution=200)


def _write_minimal_docx(path: Path, text: str) -> None:
    """Write a minimal valid OOXML Word package using only the standard library."""

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "word/document.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
    <w:tbl><w:tblPr/><w:tblGrid/>
      <w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>DOCX table fallback cell</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>""",
        )
