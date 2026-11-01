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
accepts a file path. PDF and Office files use the existing `document.parsing.v1`
library; HTML, text, Markdown, CSV, and supported images use its existing
format-specific adapters. Results are returned as source-bound blocks with
bounded cursors, content digests, and parser diagnostics.

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
