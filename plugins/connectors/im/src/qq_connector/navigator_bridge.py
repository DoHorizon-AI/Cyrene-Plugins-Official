"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 navigator_bridge.py                                             │
│  Module: qq_connector.navigator_bridge                              │
│  Role: Navigator work API adapter for the existing QQ connector.    │
│                                                                     │
│  模块职责：将 Navigator work API 接到既有 QQ connector 与 stdio Host。  │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .qqnt_direct import QQNTDirectConnector
from .qqnt_direct_host import QQHostClient
from .support import ApplicationEventEmitter


class NavigatorQQBridge:
    """Expose owner-scoped QQ actions over an existing connector instance.

    The bridge does not start a second worker or transport. It delegates to
    `QQNTDirectConnector`, which owns the existing binding-local stdio Host.

    将 owner-scoped QQ 操作连接到既有 connector 实例。
    此 bridge 不会启动第二个 worker 或传输；具体调用由现有的
    `QQNTDirectConnector` 和 binding 本地 stdio Host 处理。
    """

    def __init__(
        self,
        config: Mapping[str, Any] | None,
        *,
        host: QQHostClient | Any | None = None,
    ) -> None:
        self._connector = QQNTDirectConnector(config, host=host)

    @property
    def connector(self) -> QQNTDirectConnector:
        """Return the existing connector for event subscription wiring.

        返回既有 connector，供事件订阅接线使用。
        """

        return self._connector

    def health(self) -> dict[str, Any]:
        """Return a live Host/API probe without starting an absent runtime.

        返回 Host/API 实际探测状态，不会启动缺失的 runtime。
        """

        return self._connector.health()

    def request_qr(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Start one short-lived QR login for the confirmed binding account.

        为已确认的 binding 账号发起一次短时 QR 登录。
        """

        return self._connector.request_qr(params)

    def poll_login(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Poll one QR attempt by its binding-local login identifier.

        按 binding 本地 login id 轮询一次 QR 登录尝试。
        """

        return self._connector.poll_login(params)

    def subscribe_login_events(
        self,
        subscription_id: str,
        emitter: ApplicationEventEmitter,
    ) -> str | None:
        """Subscribe to the binding-local QR/login state stream.

        订阅 binding 本地 QR／登录状态事件流。
        """

        payload = b'{"event_type":"qq_login_state"}'
        return self._connector.on_subscribe(
            subscription_id,
            "qq.client.v1",
            payload,
            emitter,
        )

    def unsubscribe(self, subscription_id: str, reason: str = "") -> None:
        """Release one event subscription without affecting another binding.

        释放一个事件订阅，不影响其他 binding。
        """

        self._connector.on_unsubscribe(subscription_id, reason)

    def close(self) -> None:
        """Close the binding-local Host process and event subscriptions.

        关闭 binding 本地 Host 进程和事件订阅。
        """

        self._connector.close()


def create_navigator_qq_bridge(
    config: Mapping[str, Any] | None = None,
    *,
    host: QQHostClient | Any | None = None,
) -> NavigatorQQBridge:
    """Create the Navigator adapter from an optional protected binding config.

    An absent config returns a bridge whose health is `NOT_CONFIGURED`; it does
    not discover a QQ client or fabricate a Host artifact.

    从可选的受保护 binding 配置创建 Navigator adapter。没有配置时返回
    `NOT_CONFIGURED` 健康状态，不扫描 QQ 客户端，也不伪造 Host 制品。
    """

    return NavigatorQQBridge(config, host=host)


__all__ = ["NavigatorQQBridge", "create_navigator_qq_bridge"]
