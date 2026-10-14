# Provider Package

This directory implements the GitHub Issues `tool.provider.v1` binding. Read `settings.py` first for operator-scoped identities, `trust.py` for signed actor context, `account_center.py` for the resolved lease port, then `github_api.py` and `plugin.py` for transport and dispatch.

| File | Responsibility |
| --- | --- |
| `__init__.py` | Public provider class export. |
| `account_center.py` | Typed Account Center credential lease port. |
| `github_api.py` | Fixed-host GitHub REST requests and sanitized status mapping. |
| `plugin.py` | Tool catalog, calls, and webhook normalization. |
| `settings.py` | Binding and allowlist validation. |
| `trust.py` | Trusted caller signature verification and re-signing. |
| `_generated/` | Canonical tool provider protobuf binding. |
