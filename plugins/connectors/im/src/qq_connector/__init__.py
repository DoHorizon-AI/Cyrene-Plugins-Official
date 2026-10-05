###############################################################################
# 📄 File: plugins/connectors/im/src/qq_connector/__init__.py
# Module: Cyrene Plugins Official
# Role: Independent QQ/IM Python reference boundary.
#
# 模块：Cyrene Plugins Official
# 职责：独立 QQ/IM Python 参考实现边界。
###############################################################################
"""Independent QQNT reference implementation for the IM connector.

中文:IM 连接器的独立 QQNT 参考实现。"""

from .navigator_bridge import NavigatorQQBridge, create_navigator_qq_bridge
from .plugin import ConnectorPlugin
from .qq_official_packages import (
    PACKAGE_LOCK_SCHEMA,
    SUPPORTED_PACKAGE_TARGETS,
    OfficialQQPackage,
    QQOfficialPackageManager,
    QQPackageError,
    load_package_lock,
    target_for_current_platform,
)
from .qqnt_direct import (
    QQ_CALLBACK_TYPE_URL,
    QQ_CAPABILITY_ID,
    QQ_REQUEST_TYPE_URL,
    QQ_RESPONSE_TYPE_URL,
    QQNTDirectConfig,
    QQNTDirectConnector,
)
from .qqnt_direct_discovery import (
    INSTALLATION_MANIFEST_SCHEMA,
    SUPPORTED_PLATFORM,
    QQInstallation,
    QQInstallationError,
    discover_explicit,
    discover_manifest,
    discover_manifests,
)
from .qqnt_direct_host import (
    QQ_HOST_PROTOCOL,
    QQ_HOST_PROTOCOL_VERSION,
    QQHostClient,
    QQHostError,
    QQHostLaunchConfig,
)
from .qqnt_direct_operations import (
    CALLBACK_ONLY_OPERATION_NAMES,
    QQ_OPERATION_NAMES,
    QQ_OPERATIONS,
    QQOperation,
)
from .qqnt_direct_protocol import (
    QQHostProtocolError,
    encode_frame,
    read_frame,
    write_frame,
)
from .support import (
    ApplicationEventEmitter,
    CancellationToken,
    ConnectorError,
)

__all__ = [
    "ApplicationEventEmitter",
    "CALLBACK_ONLY_OPERATION_NAMES",
    "CancellationToken",
    "ConnectorError",
    "ConnectorPlugin",
    "NavigatorQQBridge",
    "INSTALLATION_MANIFEST_SCHEMA",
    "QQ_CALLBACK_TYPE_URL",
    "QQ_CAPABILITY_ID",
    "QQ_HOST_PROTOCOL",
    "QQ_HOST_PROTOCOL_VERSION",
    "QQ_OPERATION_NAMES",
    "QQ_OPERATIONS",
    "QQ_REQUEST_TYPE_URL",
    "QQ_RESPONSE_TYPE_URL",
    "QQHostClient",
    "QQHostError",
    "QQHostLaunchConfig",
    "QQHostProtocolError",
    "QQInstallation",
    "QQInstallationError",
    "QQNTDirectConfig",
    "QQNTDirectConnector",
    "QQOfficialPackageManager",
    "QQOperation",
    "QQPackageError",
    "OfficialQQPackage",
    "PACKAGE_LOCK_SCHEMA",
    "SUPPORTED_PLATFORM",
    "SUPPORTED_PACKAGE_TARGETS",
    "load_package_lock",
    "target_for_current_platform",
    "discover_explicit",
    "discover_manifest",
    "discover_manifests",
    "create_navigator_qq_bridge",
    "encode_frame",
    "read_frame",
    "write_frame",
]
