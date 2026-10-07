# Document Parsing / 文档解析

`cyrene.tools.document-parsing` implements stateless `document.parsing.v1` for
PDF and DOCX source revisions. It leaves source bytes unchanged, writes the full
DoclingDocument JSON, a stable block index with source locators, and a conversion
report with SHA-256 receipts. Catalyst owns source identities, content revisions,
review state, permissions, and publication.

`parse` uses `DirectPluginRuntime` with typed JSON at
`type.cyrene.io/document.parsing.v1.parse.request` and
`type.cyrene.io/document.parsing.v1.parse.response`. Its four absolute paths are
executor-local staging paths and must never be exposed through a Product API.
The block envelope is `cyrene.document.blocks.v1`; PDF source page numbers are
one-based. DOCX page numbers remain empty when Docling does not provide them.

PDF parsing defaults to Docling's model-free native PDF pipeline, which extracts
the embedded text layer and does not download models during parsing. Scan-only
pages may therefore have empty text and receive explicit warnings. Its report
also states that OCR, reading-order inference, and table-structure inference did
not run. The Plugin declares `rtree` directly because Docling's native PDF
pipeline imports it even though the slim PDF extras do not include it. This
keeps the baseline free of Torch and model downloads. When layout and OCR models
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

Install and run the focused module tests with the repository's normal Plugin
runtime environment:

```bash
python -m pip install -e '.[test]'
pytest -q
```

## Files / 文件

| File | Responsibility |
| --- | --- |
| `document_parsing.py` | Typed DirectPluginRuntime adapter, Docling conversion, block index, receipts. |
| `contracts/v1/schema.json` | Strict request, response, block-index, and report schemas. |
| `plugin.manifest.json` | Capability, method, runtime entrypoint, and package metadata. |
| `tests/test_document_parsing.py` | Generated PDF/DOCX integration examples and input validation. |
