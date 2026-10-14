# Independent SMTP Email Notification Connector / 独立 SMTP 邮件通知连接器

This Apache-2.0 Plugin implements two fixed `tool.provider.v1` tools:
`email_notification_send` and `email_connection_health`. Calls use the shared
`cyrene_integration_contracts` signed envelope. The provider checks the exact
tool, operation, binding, caller workload, audience, organization, workspace,
payload digest and short TTL before contacting SMTP.

The binding owns the SMTP hostname, port, TLS mode, sender, credential
environment-variable references, signing-key reference and named recipient
allowlist. Callers supply only a `recipient_ref`, bounded subject and text, and
an opaque `request_ref`; they cannot choose an address, endpoint, URL or shell
command. SMTP authentication is independent of WeCom credentials.

SMTP implicit TLS is the default. STARTTLS is available when explicitly
configured. Plain SMTP is accepted only for an explicitly enabled loopback test
binding. Subject line breaks and control characters are rejected. The provider
does not return credentials, raw SMTP errors, arbitrary vendor text or a
fabricated message ID.

`ACCEPTED` means the configured SMTP server returned a successful final response
after `DATA`; it does not prove delivery or reading. An explicit SMTP rejection
returns `REJECTED`. A disconnect or timeout after `DATA` begins returns
`UNKNOWN`; the connector never resends. Pre-send connection failures use the
Tool Provider retryable error field so an Exchange outbox can distinguish safe
retries from uncertain submissions. Exchange remains responsible for durable
business outbox and workflow idempotency; this private ledger closes the
provider transport replay boundary.

The provider also keeps a private SQLite transport ledger keyed by a scoped
signed `request_ref`. It commits a `SENDING` intent before opening the SMTP
session, returns the recorded result for an exact replay, and rejects reuse of a
request reference with changed content. A `SENDING` row found after restart
becomes `UNKNOWN` and is never submitted again. The ledger contains scoped
identity and content digests only; it omits request references, message text,
sender and recipient addresses. Its configured parent directory must be owner-only and
the database file is created with owner-only permissions.

Offline tests use only a synthetic loopback SMTP fixture. They demonstrate
simulated SMTP behavior and are not real vendor delivery evidence. Real SMTP
configuration and email sends remain `NOT_RUN` until separately configured and
authorized.

离线测试仅使用本机 loopback 的合成 SMTP fixture，用于模拟 SMTP 行为，不代表真实邮件送达。
真实 SMTP 配置和邮件发送在单独配置并授权前均为 `NOT_RUN`。

## Binding configuration / 绑定配置

The full JSON Schema is in `configuration.schema.json`. `smtp_username_env_ref`
and `smtp_password_env_ref` are optional as a pair; their values are read only
from the named environment variables. `context_hmac_key_env_ref` is required
and names an environment variable containing a 32-byte key encoded as 64
hexadecimal characters. The configured key is never included in diagnostics.

This activation example uses reserved `.invalid` mailbox names and placeholder
environment-variable names. It contains no live endpoint or credential values:

```json
{
  "smtp_host": "smtp.example.invalid",
  "smtp_port": 465,
  "tls_mode": "implicit",
  "timeout_seconds": 15,
  "sender": "notifications@example.invalid",
  "recipients": {
    "operator": "operator@example.invalid"
  },
  "state_db_path": "/var/lib/cyrene-private/email-notification/submissions.sqlite3",
  "smtp_username_env_ref": "SMTP_USERNAME",
  "smtp_password_env_ref": "SMTP_PASSWORD",
  "context_hmac_key_env_ref": "EMAIL_NOTIFICATION_CONTEXT_HMAC_KEY",
  "caller_workload": "cyrene-exchange",
  "audience": "cyrene.connectors.email-notification",
  "organization_id": "example-organization",
  "workspace_id": "example-workspace"
}
```

Create the private ledger directory with owner-only permissions before
activation. Set the three named environment variables through the host's secret
store; do not place their values in this configuration JSON.

Runtime activation uses `CYRENE_CAPABILITY_BINDING_ID` and
`CYRENE_CAPABILITY_CONFIGURATION_JSON`. The signed caller context must match
the configured workload, audience, binding, organization and workspace. Issuer
and subject can be pinned when the binding requires them.

`state_db_path` must point to a durable SQLite file inside a directory with no
group or other access. The directory must exist before activation. Preserve
this private database when updating the Plugin so replay protection survives a
restart.

The only recipient value accepted in a tool call is an alias key from the
binding's `recipients` object. SMTP endpoint and recipient maps are fixed for
the lifetime of that activation.

## Evidence / 验证状态

The local SMTP fixture tests exercise tool listing, signed calls, allowlist
boundaries, SMTP acceptance/rejection, health checks, and an ambiguous outcome
after `DATA`. They do not send real email and do not claim production readiness.
