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
import hmac
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

# Microsoft Teams SDK loads dotenv during import. Production configuration must
# come only from the host's injected environment and secret mounts.
os.environ["PYTHON_DOTENV_DISABLED"] = "true"

from fastapi import FastAPI, HTTPException, Request, Response  # noqa: E402
from microsoft_teams.api import MessageActivity  # noqa: E402
from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter  # noqa: E402
from microsoft_teams.common import ClientOptions  # noqa: E402

from .activity import ActivityRejected, normalize_activity  # noqa: E402
from .config import IngressSettings  # noqa: E402
from .delivery_spool import (  # noqa: E402
    DeliveryAdminConflict,
    DeliveryIdentityConflict,
    DeliveryPauseTimeout,
    DeliverySpool,
    DeliverySpoolError,
    NavigatorDeliveryWorker,
)
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
    delivery_spool: DeliverySpool | None = None
    delivery_worker: NavigatorDeliveryWorker | None = None

    async def start(self) -> None:
        """Run the SDK-managed FastAPI server on the configured port.

        中文：在配置端口运行 SDK 管理的 FastAPI 服务。
        """

        await self.sdk_app.start(self.port)

    async def close(self) -> None:
        """Close the Navigator client after the listener stops.

        中文：监听器停止后关闭 Navigator client。
        """

        if self.delivery_worker is not None:
            await self.delivery_worker.close()
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
    navigator_client = navigator or NavigatorEventClient(
        base_url=settings.navigator_base_url,
        key_id=settings.navigator_hmac_key_id,
        hmac_key=settings.navigator_hmac_key,
        timeout_seconds=settings.navigator_timeout_seconds,
    )
    delivery_spool = (
        DeliverySpool(settings.delivery_state_dir)
        if settings.delivery_state_dir is not None
        else None
    )
    delivery_worker = (
        NavigatorDeliveryWorker(
            delivery_spool,
            navigator_client.submit_body,
            navigator_timeout_seconds=settings.navigator_timeout_seconds,
        )
        if delivery_spool is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_api: FastAPI):
        """Start and drain the optional delivery worker with the app lifetime.

        中文：随应用生命周期启动可选投递 worker，并在关闭时有界排空。
        """

        if delivery_worker is not None:
            await delivery_worker.start()
        try:
            yield
        finally:
            if delivery_worker is not None:
                await delivery_worker.close()

    api = FastAPI(lifespan=lifespan)
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

    if delivery_spool is not None and delivery_worker is not None:
        admin_token = settings.delivery_admin_token
        if admin_token is None:
            raise ValueError("durable delivery requires its independent admin token")

        def require_delivery_admin(request: Request) -> None:
            """Compare the in-memory token without logging or persisting headers.

            中文：在内存中常量时间比较 token，不记录或持久化请求 header。
            """

            authorization = request.headers.get("authorization", "")
            scheme, separator, value = authorization.partition(" ")
            try:
                candidate = value.encode("ascii")
            except UnicodeEncodeError:
                candidate = b""
            if (
                scheme.casefold() != "bearer"
                or not separator
                or not hmac.compare_digest(candidate, admin_token)
            ):
                raise HTTPException(
                    status_code=401,
                    detail="delivery admin authentication required",
                    headers={"WWW-Authenticate": "Bearer"},
                )

        async def read_admin_cas(request: Request) -> tuple[str, int]:
            """Read holder/revision fields without echoing request values.

            中文：只读取 holder/revision，不回显请求字段。
            """

            try:
                body = await request.json()
            except (ValueError, UnicodeDecodeError):
                raise HTTPException(400, "invalid delivery admin request") from None
            if (
                not isinstance(body, dict)
                or set(body) != {"holder", "expected_revision"}
                or not isinstance(body.get("holder"), str)
                or type(body.get("expected_revision")) is not int
            ):
                raise HTTPException(400, "invalid delivery admin request")
            return body["holder"], body["expected_revision"]

        @api.get("/_internal/teams/delivery/status")
        async def delivery_status(request: Request) -> dict[str, Any]:
            """Return queue metadata only; never expose bodies or holders.

            中文：只返回队列元数据，不暴露正文或 holder。
            """

            require_delivery_admin(request)
            try:
                return (await delivery_spool.status()).as_dict()
            except DeliverySpoolError:
                raise HTTPException(503, "delivery status is unavailable") from None

        @api.post("/_internal/teams/delivery/pause")
        async def pause_delivery(request: Request) -> dict[str, Any]:
            """CAS-pause claims while continuing durable webhook admission.

            中文：以 CAS 暂停后台领取；webhook 仍须持久入队后 ACK。
            """

            require_delivery_admin(request)
            holder, revision = await read_admin_cas(request)
            try:
                return (
                    await delivery_worker.pause_and_wait(
                        holder=holder, expected_revision=revision
                    )
                ).as_dict()
            except DeliveryAdminConflict:
                raise HTTPException(
                    409, "delivery pause revision or holder conflict"
                ) from None
            except DeliveryPauseTimeout:
                raise HTTPException(503, "active delivery did not drain") from None
            except DeliverySpoolError:
                raise HTTPException(
                    503, "delivery pause could not be persisted"
                ) from None

        @api.post("/_internal/teams/delivery/resume")
        async def resume_delivery(request: Request) -> dict[str, Any]:
            """CAS-resume only the pause owned by the matching holder.

            中文：只允许匹配 holder 以 CAS 恢复后台领取。
            """

            require_delivery_admin(request)
            holder, revision = await read_admin_cas(request)
            try:
                return (
                    await delivery_worker.resume(
                        holder=holder, expected_revision=revision
                    )
                ).as_dict()
            except DeliveryAdminConflict:
                raise HTTPException(
                    409, "delivery pause revision or holder conflict"
                ) from None
            except DeliverySpoolError:
                raise HTTPException(
                    503, "delivery resume could not be persisted"
                ) from None

        @api.post("/_internal/teams/delivery/retry-dead")
        async def retry_dead_delivery(request: Request) -> dict[str, Any]:
            """CAS-requeue dead letters only while the matching pause is drained.

            中文：仅在匹配的 pause 已排空时，以 CAS 恢复 dead-letter。
            """

            require_delivery_admin(request)
            holder, revision = await read_admin_cas(request)
            try:
                retried_count, status = await delivery_spool.retry_dead(
                    holder=holder, expected_revision=revision
                )
                await delivery_worker.notify()
                return {"retried_count": retried_count, **status.as_dict()}
            except DeliveryAdminConflict:
                raise HTTPException(
                    409, "dead delivery recovery requires the current paused holder"
                ) from None
            except DeliverySpoolError:
                raise HTTPException(
                    503, "dead delivery recovery could not be persisted"
                ) from None

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

        if delivery_spool is not None and delivery_worker is not None:
            try:
                await delivery_spool.enqueue(normalized.as_event())
            except DeliveryIdentityConflict:
                raise HTTPException(
                    409, "Teams activity identity conflicts with queued body"
                ) from None
            except DeliverySpoolError:
                # Disk-full and commit failures must remain visible to Teams retries.
                raise HTTPException(
                    503, "Teams event could not be durably queued"
                ) from None
            await delivery_worker.notify()
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
        delivery_spool=delivery_spool,
        delivery_worker=delivery_worker,
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
