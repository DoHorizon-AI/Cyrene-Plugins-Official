"""Persistent image media support for Cyrene.

Product permissions remain outside this package. 中文：本包不负责产品权限判断。
"""

from .library import MediaError, MediaLibrary

__all__ = ["MediaError", "MediaLibrary"]
