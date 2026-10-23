# Account Center package

This package implements the private `account.center.v1` DirectPluginRuntime capability.

| File | Responsibility |
| --- | --- |
| `plugin.py` | Fixed method routing, provider profiles, caller roles, leases, and token refresh lifecycle |
| `store.py` | Private SQLite migrations, owner-scoped profiles, replay protection, and one-use lease transactions |
| `security.py` | Canonical HMAC context checks and authenticated encryption |
| `github.py` | Legacy fixed GitHub OAuth, PKCE, refresh, and revocation |
| `github_app.py` | GitHub App JWT verification and repository-scoped token issuance |
| `huggingface_auth.py` | Fixed-host Hugging Face token identity verification |
| `models.py` | Internal owner, credential, and sanitized error types |

Read `security.py` and `store.py` before changing a state transition; then follow the caller through `plugin.py` to the fixed provider client.

## 账号中心源码

此 package 实现私有的 `account.center.v1` DirectPluginRuntime capability。先看 `security.py`
和 `store.py` 理解签名、加密和持久化，再沿着 `plugin.py` 查看固定 provider 客户端调用。
