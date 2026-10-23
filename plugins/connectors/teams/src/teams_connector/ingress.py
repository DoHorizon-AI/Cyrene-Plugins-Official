"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 ingress.py                                                       │
│  Module: teams_connector.ingress                                    │
│  Role: Host verified Teams ingress and Navigator admission callback.  │
│                                                                      │
│  模块职责：托管已验证 Teams 入站与 Navigator 持久接纳回调               │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

# Microsoft Teams SDK loads dotenv during import. Production configuration must
# come only from the host's injected environment and secret mounts.
os.environ["PYTHON_DOTENV_DISABLED"] = "true"

from fastapi import FastAPI, Response  # noqa: E402
from microsoft_teams.api import MessageActivity  # noqa: E402
from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter  # noqa: E402
from microsoft_teams.common import ClientOptions  # noqa: E402

from .activity import ActivityRejected, normalize_activity  # noqa: E402
from .config import IngressSettings  # noqa: E402
from .logging_utils import install_sensitive_log_filters  # noqa: E402
from .navigator import (  # noqa: E402
    NavigatorAdmissionUnavailable,
    NavigatorEventClient,
)


@dataclass
class TeamsIngressService:
    """Initialized SDK app and its Navigator event client.

    中文：初始化后的 SDK app 与 Navigator event client。
    """

    sdk_app: App
    http_app: FastAPI
    navigator: NavigatorEventClient
    port: int

    async def start(self) -> None:
        """Run the SDK-managed FastAPI server on the configured port.

        中文：在配置端口运行 SDK 管理的 FastAPI 服务。
        """

        await self.sdk_app.start(self.port)

    async def close(self) -> None:
        """Close the Navigator client after the listener stops.

        中文：监听器停止后关闭 Navigator client。
        """

        await self.navigator.aclose()


async def build_ingress_service(
    settings: IngressSettings,
    *,
    navigator: NavigatorEventClient | None = None,
) -> TeamsIngressService:
    """Build the official Teams SDK app and register safe health routes.

    The SDK validates the Bot Framework JWT before invoking ``handle_message``.
    The handler waits for Navigator's durable-admission response before the SDK
    returns an HTTP success acknowledgement to Teams.

    中文：构建微软官方 Teams SDK app 并注册健康检查路由。SDK 会在调用消息 handler 前
    校验 Bot Framework JWT；handler 等待 Navigator 持久接纳后，SDK 才向 Teams 返回成功。
    """

    install_sensitive_log_filters()
    api = FastAPI()
    ready = False

    @api.get("/health/live")
    async def liveness() -> dict[str, str]:
        """Expose process liveness without returning runtime configuration.

        中文：提供进程存活状态，不返回运行时配置。
        """

        return {"status": "live"}

    @api.get("/health/ready")
    async def readiness(response: Response) -> dict[str, str]:
        """Expose readiness only after SDK authentication is initialized.

        中文：仅在 SDK 鉴权初始化后报告 ready。
        """

        if not ready:
            response.status_code = 503
            return {"status": "not_ready"}
        return {"status": "ready"}

    navigator_client = navigator or NavigatorEventClient(
        base_url=settings.navigator_base_url,
        key_id=settings.navigator_hmac_key_id,
        hmac_key=settings.navigator_hmac_key,
        timeout_seconds=settings.navigator_timeout_seconds,
    )
    sdk_app = App(
        client_id=settings.bot_app_id,
        tenant_id=settings.tenant_id,
        client=ClientOptions(timeout=settings.navigator_timeout_seconds),
        dangerously_allow_unauthenticated_requests=False,
        telemetry={"agent365": False},
        http_server_adapter=FastAPIAdapter(app=api),
    )

    @sdk_app.on_message
    async def handle_message(ctx: ActivityContext[MessageActivity]) -> None:
        """Forward one authenticated channel activity and await durable admission.

        中文：转发一个经过鉴权的频道 activity，并等待 Navigator 持久接纳。
        """

        activity_model = ctx.activity
        activity_data: Any = activity_model.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        try:
            normalized = normalize_activity(
                activity_data,
                bot_app_id=settings.bot_app_id,
                tenant_id=settings.tenant_id,
                allowed_team_ids=settings.allowed_team_ids,
            )
        except ActivityRejected:
            # Authenticated messages outside the configured tenant/team or bot
            # echoes are terminally ignored; they must not create Navigator work.
            return

        result = await navigator_client.submit(normalized.as_event())
        if result.accepted:
            return
        if result.terminal_rejection:
            # Only a code-allowlisted business decision after verified HMAC is
            # terminal; transport authentication failures remain retryable.
            return
        raise NavigatorAdmissionUnavailable(
            "Navigator did not confirm durable Teams event admission"
        )

    await sdk_app.initialize()
    ready = True
    return TeamsIngressService(
        sdk_app=sdk_app,
        http_app=api,
        navigator=navigator_client,
        port=settings.port,
    )


async def _run() -> None:
    """Load host configuration and run the sidecar until shutdown.

    中文：加载主机配置并运行侧车，直到服务停止。
    """

    settings = IngressSettings.from_environment()
    service = await build_ingress_service(settings)
    try:
        await service.start()
    finally:
        await service.close()


def main() -> None:
    """Run the Teams ingress sidecar as the project script entrypoint.

    中文：作为项目脚本入口运行 Teams 入站侧车。
    """

    asyncio.run(_run())


if __name__ == "__main__":
    main()
