"""Persistent, tenant-scoped implementation of the graph provider capability.

中文：提供按租户与命名空间隔离的持久化知识图谱存储。
"""

from .plugin import KnowledgeGraphProvider, TypedPayload
from .settings import GraphSettings
from .sqlite_store import GraphStoreError, SQLiteGraphStore

__all__ = [
    "GraphSettings",
    "GraphStoreError",
    "KnowledgeGraphProvider",
    "SQLiteGraphStore",
    "TypedPayload",
]
