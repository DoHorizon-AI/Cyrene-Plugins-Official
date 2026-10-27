# Microsoft Graph Provider Source

This directory contains the stateless provider implementation. The plugin
validates Exchange identity context, reads the actor-owned Account Center
profile, redeems a one-use lease, and verifies the signed-in Graph user before
calling a fixed v1.0 route.

| File | Responsibility |
| --- | --- |
| `settings.py` | Closed host activation settings and private secret resolution. |
| `account_center.py` | Profile lookup and one-use delegated credential redemption. |
| `trust.py` | Exchange context verification and Account Center signing. |
| `microsoft_graph_api.py` | Fixed Graph v1.0 transport, pagination, projections, and write reconciliation. |
| `plugin.py` | Tool catalog, argument checks, scope checks, and invocation routing. |

Execution order: `settings.py` → `trust.py` → `account_center.py` → `microsoft_graph_api.py` → `plugin.py`.
