# Public Web Tools

This stateful-boundary-free `tool.provider.v1` package exposes three operations:
`web.search`, `web.open`, and `document.read`. Search uses a single operator-set
backend URL and optional host-held bearer token; the model supplies only a query
and a bounded page cursor. Web reads accept HTTPS URLs, resolve DNS once, reject
non-public addresses, connect to the resolved IP while preserving TLS SNI, and
refuse redirects and ambient proxy configuration.

`document.read` accepts a public URL or an opaque attachment ID. Attachment
resolution goes through the current task's authenticated Work bridge, which
authorizes the original message/source before returning bytes. The plugin never
accepts a file path. Its isolated parser uses pinned local PDFium, Word, PowerPoint,
and spreadsheet libraries plus the Python standard library. It extracts searchable
PDF text and Office/text content without loading a model. Images and PDF pages with
no text layer return an explicit OCR-unavailable diagnostic; the parser never
returns fabricated OCR text. Results are returned as source-bound blocks with
bounded cursors, content digests, and parser diagnostics.

Document parsing runs in at most two isolated workers, each with a 90-second
wall-clock limit. Cancellation and timeout terminate the worker process group;
the source, request, and parser outputs live only in a private temporary
directory. The child receives no caller, search, or artifact bridge secrets.
The package lock contains the complete external wheel closure; the parser does not
resolve an unpublished Cyrene distribution from a package registry.

The plugin does not invent search results when no backend is configured. Search
and document results are bounded and cite a stable `source_ref`; changing the
URL, query, actor, workspace, or source bytes invalidates the signed pagination
cursor. Cancellation is checked before and after network/parser calls, and the
host runtime drops any late result from a cancelled task.

The optional `loopback_test_origins` setting is intended only for a private WSL
acceptance peer. It allows exact loopback origins with HTTP; all other web reads
must use public HTTPS origins. No local test peer is considered external search
acceptance.

Run focused tests with `uv run --project plugins/tools/public-web pytest`.
