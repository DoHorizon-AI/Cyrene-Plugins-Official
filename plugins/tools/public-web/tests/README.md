# Public web tests

The suite uses loopback HTTP peers and the real shared ToolEnvelope and provider
contracts. It verifies HMAC scope, DNS and redirect guards, bounded responses,
search/open/document pagination, total HTTP deadlines, parser timeout and
cancellation, source digests, and task-authorized artifact resolution. Parser
fixtures exercise searchable PDF text, Office cells and paragraphs, plain text,
CSV, and Markdown in the isolated worker. Image inputs remain explicitly
unrecognized when OCR is not configured. These tests do not count as external
search-engine or public-site acceptance.
