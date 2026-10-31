# Provider package source

This directory contains the Azure DevOps provider implementation and its
generated `tool.provider.v1` protocol binding.

| Directory | Responsibility |
| --- | --- |
| `azure_devops_observation_provider/` | Activation, trust, lease, REST, cursor, and tool dispatch code. |

Suggested reading order: `azure_devops_observation_provider/README.md`, then
`settings.py`, `trust.py`, `azure_devops_api.py`, and `plugin.py`.
