"""Comprehensive stress and edge-case tests for document parsing (Phase 3)."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

from document_parsing import (
    DocumentParsingPlugin,
    TypedPayload,
)

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _run_plugin(
    tmp_path: Path,
    source_file: Path,
    media_type: str,
    *,
    source_digest: str | None = None,
    filename: str | None = None,
) -> tuple[bool, Any]:
    content = source_file.read_bytes()
    digest = source_digest or _digest(content)
    name = filename or source_file.name

    payload = {
        "source_path": str(source_file),
        "payload_path": str(tmp_path / "docling.json"),
        "blocks_path": str(tmp_path / "blocks.json"),
        "result_path": str(tmp_path / "result.json"),
        "source_ref": f"source:{name}",
        "source_digest": digest,
        "filename": name,
        "media_type": media_type,
    }
    plugin = DocumentParsingPlugin()
    ok, response = plugin.on_invoke(
        "document.parsing.v1",
        "parse",
        json.dumps(payload).encode("utf-8"),
        request_type_url="type.cyrene.io/document.parsing.v1.parse.request",
    )
    if ok and isinstance(response, TypedPayload):
        return True, json.loads(response.value)
    return False, response


def test_corrupt_empty_file(tmp_path: Path) -> None:
    """Empty files must be explicitly rejected."""
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    ok, res = _run_plugin(tmp_path, empty, PDF_MEDIA_TYPE)
    assert not ok
    assert "source file is empty" in str(res)


def test_corrupt_bad_magic_pdf(tmp_path: Path) -> None:
    """Files claiming to be PDF without %PDF- magic bytes must be explicitly rejected."""
    bad_magic = tmp_path / "not_a_pdf.pdf"
    bad_magic.write_bytes(b"HELLO WORLD NOT A PDF")
    ok, res = _run_plugin(tmp_path, bad_magic, PDF_MEDIA_TYPE)
    assert not ok
    assert "source bytes do not contain a PDF header" in str(res)


def test_corrupt_truncated_pdf(tmp_path: Path) -> None:
    """Truncated PDFs must fail conversion or produce explicit failure, never silent success."""
    truncated = tmp_path / "truncated.pdf"
    truncated.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n")
    ok, res = _run_plugin(tmp_path, truncated, PDF_MEDIA_TYPE)
    # Either returns ok=False or raises ValueError / failure status
    assert not ok or res.get("status") in {"failure", "partial_success"}
    if ok:
        assert len(res.get("warnings", [])) > 0


def test_scanned_raster_pdf_no_silent_success(tmp_path: Path) -> None:
    """A pure image/scanned PDF without text layer must explicitly report unparsed/limitation warning."""
    # Create minimal 1-page PDF without text (only an image object or empty page)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
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
    scanned = tmp_path / "scanned.pdf"
    scanned.write_bytes(pdf)

    ok, res = _run_plugin(tmp_path, scanned, PDF_MEDIA_TYPE)
    assert ok
    # Must report limitation or warnings that no text was found - NO silent fake success
    warnings = res.get("warnings", [])
    assert any(
        "Native PDF parsing does not run OCR" in w
        or "no addressable content blocks" in w
        for w in warnings
    )
    assert res.get("block_count", 0) == 0


def test_unicode_and_emojis_preserved(tmp_path: Path) -> None:
    """Complex Unicode, CJK, and emojis must be preserved and normalized without crash."""
    test_text = "中文测试 🎯🚀 日本語 한국어 \u202eRTL\u202c \u00e9\u00e0\u00ee"
    docx_file = tmp_path / "unicode.docx"
    with zipfile.ZipFile(docx_file, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        )
        z.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        )
        z.writestr(
            "word/document.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{test_text}</w:t></w:r></w:p>
  </w:body>
</w:document>""",
        )

    ok, _res = _run_plugin(tmp_path, docx_file, DOCX_MEDIA_TYPE)
    assert ok
    blocks_data = json.loads((tmp_path / "blocks.json").read_text(encoding="utf-8"))
    all_text = " ".join(b["text"] for b in blocks_data["blocks"])
    assert "中文测试" in all_text
    assert "🎯🚀" in all_text


def test_formula_and_math_symbols(tmp_path: Path) -> None:
    """Formula and mathematical symbols are retained in extracted text."""
    math_text = "E = mc^2 and \\int_0^\\infty e^{-x^2} dx = \\frac{\\sqrt{\\pi}}{2}"
    docx_file = tmp_path / "math.docx"
    with zipfile.ZipFile(docx_file, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        )
        z.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        )
        z.writestr(
            "word/document.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{math_text}</w:t></w:r></w:p>
  </w:body>
</w:document>""",
        )

    ok, _res = _run_plugin(tmp_path, docx_file, DOCX_MEDIA_TYPE)
    assert ok
    blocks_data = json.loads((tmp_path / "blocks.json").read_text(encoding="utf-8"))
    all_text = " ".join(b["text"] for b in blocks_data["blocks"])
    assert "mc^2" in all_text


def test_complex_table_extraction(tmp_path: Path) -> None:
    """Tables must be extracted as kind 'table' with structured cells."""
    docx_file = tmp_path / "table.docx"
    with zipfile.ZipFile(docx_file, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        )
        z.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        )
        z.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Introduction</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr/><w:tblGrid/>
      <w:tr>
        <w:tc><w:tcPr/><w:p><w:r><w:t>Header A</w:t></w:r></w:p></w:tc>
        <w:tc><w:tcPr/><w:p><w:r><w:t>Header B</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:tcPr/><w:p><w:r><w:t>Val 1</w:t></w:r></w:p></w:tc>
        <w:tc><w:tcPr/><w:p><w:r><w:t>Val 2</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>""",
        )

    ok, _res = _run_plugin(tmp_path, docx_file, DOCX_MEDIA_TYPE)
    assert ok
    blocks_data = json.loads((tmp_path / "blocks.json").read_text(encoding="utf-8"))
    table_blocks = [b for b in blocks_data["blocks"] if b["kind"] == "table"]
    assert len(table_blocks) == 1
    assert (
        "Header A" in table_blocks[0]["text"]
        or "DOCX table" in table_blocks[0]["text"]
        or "Val" in table_blocks[0]["text"]
    )


def test_filename_directory_traversal_rejected(tmp_path: Path) -> None:
    """Filename containing directory traversal sequences must be rejected."""
    good_file = tmp_path / "good.pdf"
    good_file.write_bytes(b"%PDF-1.4\n%%EOF\n")
    ok, res = _run_plugin(
        tmp_path, good_file, PDF_MEDIA_TYPE, filename="../../etc/shadow"
    )
    assert not ok
    assert "filename must be a basename" in str(res)


def test_output_path_overwriting_source_rejected(tmp_path: Path) -> None:
    """Output paths attempting to overwrite the staged source file must be rejected."""
    good_file = tmp_path / "good.pdf"
    good_file.write_bytes(b"%PDF-1.4\n%%EOF\n")
    payload = {
        "source_path": str(good_file),
        "payload_path": str(good_file),  # Malicious: overwrite source!
        "blocks_path": str(tmp_path / "blocks.json"),
        "result_path": str(tmp_path / "result.json"),
        "source_ref": "source:good",
        "source_digest": _digest(good_file.read_bytes()),
        "filename": "good.pdf",
        "media_type": PDF_MEDIA_TYPE,
    }
    plugin = DocumentParsingPlugin()
    ok, res = plugin.on_invoke(
        "document.parsing.v1",
        "parse",
        json.dumps(payload).encode("utf-8"),
        request_type_url="type.cyrene.io/document.parsing.v1.parse.request",
    )
    assert not ok
    assert "output paths must not overwrite source_path" in str(res)
