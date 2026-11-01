# Bilibili connector

This stateful `tool.provider.v1` plugin exposes bounded Bilibili reads and
explicit account-bound writes. Account Center owns encrypted credentials and
one-use operation leases; this plugin keeps credentials only in memory while
performing the current operation. QR link-session references and pending QR
keys remain in its private state directory and never appear in tool results.

The QR flow is: Navigator selects an owner-authorized Account Center profile,
then signs `bilibili.auth.qr.begin` with that `profile_ref` and a server-generated
idempotency key. The plugin creates the private Account Center link session,
obtains a real Bilibili QR ticket, and returns an actual PNG plus an opaque
`login_flow_id`. A later status call polls Bilibili, validates the returned
identity against the authenticated navigation endpoint, and sends the bounded
cookie jar directly to Account Center for encryption. The tool result contains
only linked-profile metadata.

Each mutation records a digest-bound receipt before contacting Bilibili. A
definite response is read back before it is reported as successful. A timeout,
connection loss, or incomplete read-back produces a durable
`UNKNOWN_RESULT`; the plugin does not replay that mutation. A repeat with the
same signed `operation_ref` (the host's `work-operation:<UUID>`) returns the
existing receipt. A verified write result includes `operation_ref`, the private
plugin `receipt_id`, an external ID, and a digest-only read-back proof; comment
and message bodies are not copied into the receipt store.

All Bilibili service hosts and paths are fixed in code. User arguments cannot
provide URLs, cookies, file paths, executables, or arbitrary request headers.
Network redirects and ambient proxy settings are disabled. Response bodies,
page sizes, timeouts, QR lifetime, and credential size are bounded. The
`bilibili.video.read` result distinguishes metadata and subtitles from full
video consumption; it does not claim the video was watched.

The package has no local credentials or account fixture. When Account Center,
the account profile, or an external test account is unavailable, that boundary
is reported as `NOT_CONFIGURED` or `NOT_RUN`; no fake success is returned.

Run focused checks with `uv run --project plugins/connectors/bilibili pytest`.
