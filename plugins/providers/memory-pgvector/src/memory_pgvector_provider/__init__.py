"""Tenant-isolated PostgreSQL storage for the canonical memory capability.

中文：为规范记忆能力提供租户隔离的 PostgreSQL 存储。
"""

from .plugin import MemoryPgvectorProvider

__all__ = ["MemoryPgvectorProvider"]
