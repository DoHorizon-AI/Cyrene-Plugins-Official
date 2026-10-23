"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 __init__.py                                                      │
│  Module: teams_connector                                             │
│  Role: Public entrypoints for the Cyrene Teams transport.            │
│                                                                      │
│  模块职责：公开 Cyrene Teams 传输层入口                                │
└─────────────────────────────────────────────────────────────────────┘
"""

from .activity import ActivityRejected, NormalizedTeamsActivity, normalize_activity
from .connector import TeamsConnector, TeamsConnectorError

__all__ = [
    "ActivityRejected",
    "NormalizedTeamsActivity",
    "TeamsConnector",
    "TeamsConnectorError",
    "normalize_activity",
]
