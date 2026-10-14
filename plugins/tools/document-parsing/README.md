# Document Parsing / 文档解析

`cyrene.tools.document-parsing` implements stateless `document.parsing.v1` for
PDF, DOCX, PPTX, XLSX, CSV, Markdown, TXT, PNG, and JPEG source revisions. It
leaves source bytes unchanged and writes a format payload, a stable located block
index, and a conversion report with SHA-256 receipts. PDF/DOCX payloads preserve
the complete DoclingDocument export; other formats retain adapter-native JSON.
Catalyst owns source identities, content revisions, review state, permissions,
and publication.

`parse` uses `DirectPluginRuntime` with typed JSON at
`type.cyrene.io/document.parsing.v1.parse.request` and
`type.cyrene.io/document.parsing.v1.parse.response`. Its four absolute paths are
executor-local staging paths and must never be exposed through a Product API.
The block envelope is `cyrene.document.blocks.v1`; source page/slide numbers are
one-based. DOCX page numbers remain empty when Docling does not provide them.
PPTX, XLSX, and OCR-specific source structure is preserved in each block's
`locator.provenance`. `diagnostics[]` reports located parser/OCR warnings and
`unsupported_content[]` lists content that remains unresolved; `warnings[]` stays
available for older clients. Any unresolved content or warning diagnostic makes
the source `partial_success` without stopping other source revisions.

PDF parsing defaults to Docling's model-free native PDF pipeline, which extracts
the embedded text layer and does not download models during parsing. Textless PDF
pages are rendered locally with `pypdfium2` and passed to the independent
Tesseract adapter; unavailable OCR engines or language data produce explicit
page diagnostics and unsupported-content records. The parser does not download
OCR language data. Set `CYRENE_DOCUMENT_PARSING_OCR_ENGINE`,
`CYRENE_DOCUMENT_PARSING_OCR_LANGUAGES`, `CYRENE_DOCUMENT_PARSING_OCR_DPI`, and
`CYRENE_DOCUMENT_PARSING_OCR_MINIMUM_CONFIDENCE` to configure OCR. To use an
isolated Tesseract installation, also set
`CYRENE_DOCUMENT_PARSING_TESSERACT_CMD` and
`CYRENE_DOCUMENT_PARSING_TESSDATA_PREFIX`; the launcher must expose any required
shared libraries through `LD_LIBRARY_PATH`. Provision Tesseract and language
files such as `eng.traineddata`/`chi_sim.traineddata` separately from parser
installation.

The Plugin declares `rtree` directly because Docling's native PDF pipeline
imports it even though the slim PDF extras do not include it. The baseline keeps
Torch and model downloads out of the parser install. When Docling layout models
are needed, install the optional model dependencies, prefetch model files as a
separate operator step, and set
`CYRENE_DOCUMENT_PARSING_ARTIFACTS_PATH` for the Plugin:

```bash
python -m pip install '.[models]'
docling-tools models download --output-dir /var/lib/cyrene/docling-models
export CYRENE_DOCUMENT_PARSING_ARTIFACTS_PATH=/var/lib/cyrene/docling-models
```

The configured directory is passed as Docling's `artifacts_path`, which keeps
model acquisition separate from conversion. Remote model services are disabled.
DOCX uses Docling's simple local pipeline and does not require PDF models.
If Docling emits an empty DOCX table grid, the block index uses a narrow
`python-docx` cell-text fallback and records that recovery in the report; the
full DoclingDocument JSON remains the original Docling export.
PPTX parsing preserves slide titles, notes, shape text, geometry, and detected
unsupported media metadata. XLSX parsing preserves sheets, tables, cell
positions, formula expressions, and cached values where available; formulas are
not recalculated. CSV, Markdown, TXT, PNG, and JPEG use stateless local adapters,
with image text processed through the same configured OCR engine.

Install and run the focused module tests with the repository's normal Plugin
runtime environment:

```bash
python -m pip install -e '.[test]'
pytest -q
```

## Files / 文件

| File | Responsibility |
| --- | --- |
| `document_parsing.py` | Typed DirectPluginRuntime adapter, format routing, Docling/PDF OCR, receipts. |
| `parsing_types.py` | Shared adapter context, result, diagnostic, and stable block types. |
| `office_parsing.py` | PPTX/XLSX structure, location, provenance, and unsupported-content reporting. |
| `simple_parsing.py` | CSV, Markdown, TXT, PNG, and JPEG adapters. |
| `ocr_parsing.py` | Local Tesseract configuration and TSV-to-region conversion. |
| `contracts/v1/schema.json` | Strict request, response, block-index, diagnostic, and report schemas. |
| `plugin.manifest.json` | Capability, method, runtime entrypoint, and package metadata. |
| `tests/` | Targeted format integration tests and input validation. |
