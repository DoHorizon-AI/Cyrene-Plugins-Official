# Azure DevOps observation provider source

This package validates signed calls, checks configured source scope, redeems
one-use Account Center credential leases, and reads fixed Azure DevOps REST
endpoints.

| File | Responsibility |
| --- | --- |
| `settings.py` | Host activation, key references, and exact source allowlists. |
| `trust.py` | Exchange actor/workspace/argument verification and lease signing. |
| `account_center.py` | Direct Account Center lease redemption. |
| `azure_devops_api.py` | Fixed HTTPS v7.1 read operations and event projections. |
| `plugin.py` | Canonical `tool.provider.v1` catalog and dispatch. |
| `_generated/` | Generated provider protocol messages. |

Suggested reading order: settings and trust, then the REST client, then tool
dispatch.
