# Account Center package

This package implements the private `account.center.v1` DirectPluginRuntime capability.

| File | Responsibility |
| --- | --- |
| `plugin.py` | Fixed method routing, caller roles, owner-scoped links, leases, and refresh lifecycle |
| `store.py` | Private SQLite migrations, state, replay protection, and one-use lease transactions |
| `security.py` | Canonical HMAC context checks and authenticated encryption |
| `github.py` | Fixed official GitHub App OAuth, PKCE, user verification, refresh, and revocation |
| `models.py` | Internal owner, credential, and sanitized error types |

Read `security.py` and `store.py` before changing a state transition; then follow the caller through `plugin.py` to the fixed provider client.
