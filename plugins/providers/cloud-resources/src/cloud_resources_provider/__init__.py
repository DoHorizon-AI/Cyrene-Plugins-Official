"""Read-only cloud resource observations for Cyrene.

中文:为 Cyrene 提供只读云资源观察。
"""

from .plugin import CloudResourcesProvider
from .settings import ProviderSettings

__all__ = ["CloudResourcesProvider", "ProviderSettings"]
