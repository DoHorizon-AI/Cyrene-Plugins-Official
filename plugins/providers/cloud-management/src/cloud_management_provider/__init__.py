"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud_management_provider                                  │
│  Role: Signed, profile-scoped cloud management tools.               │
│                                                                      │
│  模块职责：提供签名校验、profile 限定的云管理工具。                   │
└─────────────────────────────────────────────────────────────────────┘
"""

from .plugin import CloudManagementProvider

__all__ = ["CloudManagementProvider"]
