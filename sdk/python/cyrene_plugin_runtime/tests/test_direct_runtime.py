"""Direct data-plane TCK for one Plugin process."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import grpc
import pytest
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, serve
from cyrene_plugin_runtime._generated import direct_plugin_runtime_pb2 as wire
from cyrene_plugin_runtime._generated import direct_plugin_runtime_pb2_grpc as wire_grpc
from cyrene_plugin_runtime.client import DirectPluginFailure
from onebot_v11_connector import OneBotV11Connector
from onebot_v11_connector._generated import message_connector_pb2 as message_wire


@dataclass(frozen=True)
class TypedPayload:
    value: bytes
    type_url: str


@dataclass(frozen=True)
class TypedStream:
    items: tuple[TypedPayload, ...]


class EchoPlugin:
    plugin_id = "test.direct.echo"
    version = "1.0.0"
    capabilities = ("test.echo.v1",)

    def __init__(self) -> None:
        self.unsubscribed: list[str] = []

    def on_invoke(
        self,
        capability: str,
        method: str,
        payload: bytes,
        *,
        cancellation: object,
        request_id: str,
        request_type_url: str,
        stream_results: bool,
    ) -> tuple[bool, object]:
        assert capability == "test.echo.v1"
        assert request_id == "request-1"
        assert request_type_url == "type.googleapis.com/test.echo.v1.Request"
        assert not cancellation.is_cancelled()  # type: ignore[attr-defined]
        if method == "fail":
            return False, "INVALID_INPUT: rejected by fixture"
        response_type = "type.googleapis.com/test.echo.v1.Response"
        if stream_results:
            return True, TypedStream(
                (
                    TypedPayload(payload + b"-1", response_type),
                    TypedPayload(payload + b"-2", response_type),
                )
            )
        return True, TypedPayload(payload, response_type)

    def on_subscribe(
        self,
        subscription_id: str,
        capability: str,
        filter_payload: bytes,
        *,
        emitter: object,
    ) -> None:
        assert capability == "test.echo.v1"
        emitter.emit(  # type: ignore[attr-defined]
            "fixture_event",
            filter_payload + b"-event",
            "type.googleapis.com/test.echo.v1.Event",
        )

    def on_unsubscribe(self, subscription_id: str, reason: str) -> None:
        assert reason
        self.unsubscribed.append(subscription_id)


class MultiCapabilityPlugin:
    """Minimal plugin fixture for a shared direct endpoint."""

    plugin_id = "test.direct.multi"
    version = "1.0.0"
    capabilities = ("test.echo.v1", "test.other.v1")

    def on_invoke(
        self,
        capability: str,
        method: str,
        payload: bytes,
        *,
        cancellation: object,
        request_id: str,
        request_type_url: str,
        stream_results: bool,
    ) -> tuple[bool, object]:
        del method, cancellation, request_id, request_type_url, stream_results
        return True, TypedPayload(payload, f"type.googleapis.com/{capability}.Response")


class _OneBotTransport:
    def call(
        self,
        action: str,
        params: object,
        *,
        timeout_seconds: float,
        cancellation: object,
    ) -> dict[str, object]:
        assert action == "send_private_msg"
        assert timeout_seconds > 0
        assert cancellation is not None
        assert params
        return {"message_id": "vendor-message-1"}

    def close(self) -> None:
        return


def request(method: str = "echo") -> wire.DirectInvocationRequest:
    return wire.DirectInvocationRequest(
        capability="test.echo.v1",
        interface_version="1",
        method=method,
        payload_type_url="type.googleapis.com/test.echo.v1.Request",
        payload=b"hello",
        request_id="request-1",
    )


def test_product_calls_plugin_endpoint_without_platform_process() -> None:
    server, connection_ref = serve(EchoPlugin(), "test.echo.v1", "1", "127.0.0.1:0")
    target = connection_ref.removeprefix("grpc://")
    channel = grpc.insecure_channel(target)
    client = wire_grpc.DirectPluginRuntimeStub(channel)
    try:
        response = client.Invoke(request(), timeout=2)
        assert response.WhichOneof("result") == "payload"
        assert response.payload.value == b"hello"
        assert response.payload.type_url.endswith("test.echo.v1.Response")

        items = list(client.InvokeStream(request(), timeout=2))
        assert [item.WhichOneof("event") for item in items] == [
            "payload",
            "payload",
            "end",
        ]
        assert [item.payload.value for item in items[:-1]] == [b"hello-1", b"hello-2"]

        health = client.Health(wire.HealthRequest(), timeout=2)
        assert health.status == wire.HealthResponse.STATUS_SERVING
        assert health.plugin_id == "test.direct.echo"
        assert list(health.capabilities) == ["test.echo.v1"]
    finally:
        channel.close()
        server.stop(grace=None).wait()


def test_product_client_preserves_direct_payloads_streaming_and_failures() -> None:
    """Exercise the Product facade without a Platform process.

    在没有 Platform 进程的情况下验证 Product 直连 facade。
    """

    server, connection_ref = serve(EchoPlugin(), "test.echo.v1", "1", "127.0.0.1:0")
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    payload = DirectPayload("type.googleapis.com/test.echo.v1.Request", b"hello")
    try:
        response = client.invoke(
            capability="test.echo.v1",
            interface_version="1",
            method="echo",
            request=payload,
            request_id="request-1",
            deadline_seconds=2,
        )
        assert response == DirectPayload(
            "type.googleapis.com/test.echo.v1.Response", b"hello"
        )

        streamed = list(
            client.invoke_stream(
                capability="test.echo.v1",
                interface_version="1",
                method="echo",
                request=payload,
                request_id="request-1",
                deadline_seconds=2,
            )
        )
        assert [item.value for item in streamed] == [b"hello-1", b"hello-2"]

        with pytest.raises(DirectPluginFailure) as captured:
            client.invoke(
                capability="test.echo.v1",
                interface_version="1",
                method="fail",
                request=payload,
                request_id="request-1",
                deadline_seconds=2,
            )
        assert captured.value.domain_code == "INVALID_INPUT"
        assert captured.value.message == "rejected by fixture"
    finally:
        client.close()
        server.stop(grace=None).wait()


def test_endpoint_rejects_wrong_binding_contract_and_returns_typed_error() -> None:
    server, connection_ref = serve(EchoPlugin(), "test.echo.v1", "1", "127.0.0.1:0")
    channel = grpc.insecure_channel(connection_ref.removeprefix("grpc://"))
    client = wire_grpc.DirectPluginRuntimeStub(channel)
    try:
        wrong = request()
        wrong.capability = "other.capability.v1"
        response = client.Invoke(wrong, timeout=2)
        assert response.error.code == wire.DirectInvocationError.CODE_INVALID_REQUEST

        failure = client.Invoke(request("fail"), timeout=2)
        assert failure.error.code == wire.DirectInvocationError.CODE_INVALID_REQUEST
        assert failure.error.domain_code == "INVALID_INPUT"
        assert failure.error.message == "rejected by fixture"

        wrong_version = request()
        wrong_version.interface_version = "2"
        rejected = client.Invoke(wrong_version, timeout=2)
        assert rejected.error.code == wire.DirectInvocationError.CODE_INVALID_REQUEST
    finally:
        channel.close()
        server.stop(grace=None).wait()


def test_endpoint_accepts_each_explicitly_bound_interface_version() -> None:
    server, connection_ref = serve(
        EchoPlugin(), "test.echo.v1", ("1", "2"), "127.0.0.1:0"
    )
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    try:
        response = client.invoke(
            capability="test.echo.v1",
            interface_version="2",
            method="echo",
            request=DirectPayload("type.googleapis.com/test.echo.v1.Request", b"hello"),
            request_id="request-1",
            deadline_seconds=2,
        )
        assert response.value == b"hello"
    finally:
        client.close()
        server.stop(grace=None).wait()


def test_endpoint_can_bind_multiple_capabilities_without_cross_routing() -> None:
    server, connection_ref = serve(
        MultiCapabilityPlugin(),
        ("test.echo.v1", "test.other.v1"),
        {"test.echo.v1": "1", "test.other.v1": "1"},
        "127.0.0.1:0",
    )
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    try:
        response = client.invoke(
            capability="test.other.v1",
            interface_version="1",
            method="echo",
            request=DirectPayload(
                "type.googleapis.com/test.other.v1.Request", b"other"
            ),
            request_id="multi-1",
            deadline_seconds=2,
        )
        assert response == DirectPayload(
            "type.googleapis.com/test.other.v1.Response", b"other"
        )
    finally:
        client.close()
        server.stop(grace=None).wait()


def test_subscription_stream_stays_between_product_and_plugin() -> None:
    plugin = EchoPlugin()
    server, connection_ref = serve(plugin, "test.echo.v1", "1", "127.0.0.1:0")
    channel = grpc.insecure_channel(connection_ref.removeprefix("grpc://"))
    client = wire_grpc.DirectPluginRuntimeStub(channel)
    subscription = request("events")
    subscription.stream_mode = wire.DIRECT_STREAM_MODE_SUBSCRIPTION
    stream = client.InvokeStream(subscription, timeout=2)
    try:
        first = next(stream)
        assert first.payload.event_type == "fixture_event"
        assert first.payload.value == b"hello-event"
        stream.cancel()
    finally:
        channel.close()
        server.stop(grace=None).wait()


def test_real_onebot_plugin_runs_on_direct_endpoint() -> None:
    request_payload = message_wire.SendMessageRequest(
        conversation=message_wire.ConversationScope(
            vendor="onebot.v11",
            account_id="10000",
            conversation_id="20000",
            kind=message_wire.CONVERSATION_KIND_PRIVATE,
        ),
        content=[
            message_wire.MessageContentPart(text=message_wire.TextContent(text="hello"))
        ],
    )
    plugin = OneBotV11Connector(
        {
            "binding_id": "onebot-main",
            "http_base_url": "http://127.0.0.1:1",
            "self_account_id": "10000",
        },
        transport=_OneBotTransport(),
    )
    server, connection_ref = serve(plugin, "message.connector.v1", "1", "127.0.0.1:0")
    channel = grpc.insecure_channel(connection_ref.removeprefix("grpc://"))
    client = wire_grpc.DirectPluginRuntimeStub(channel)
    try:
        response = client.Invoke(
            wire.DirectInvocationRequest(
                capability="message.connector.v1",
                interface_version="1",
                method="send_message",
                payload_type_url=(
                    "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest"
                ),
                payload=request_payload.SerializeToString(),
                request_id="onebot-direct-1",
            ),
            timeout=2,
        )
        assert response.WhichOneof("result") == "payload", response.error
        result = message_wire.DeliveryResult.FromString(response.payload.value)
        assert result.status == message_wire.DELIVERY_STATUS_ACCEPTED
        assert result.vendor_message_id == "vendor-message-1"
    finally:
        channel.close()
        server.stop(grace=None).wait()
        plugin.close()


def test_runtime_source_has_no_platform_import_or_proxy_client() -> None:
    import cyrene_plugin_runtime.server as runtime_server

    source = Path(runtime_server.__file__).read_text(encoding="utf-8")
    assert "cyrene_capability_client" not in source
    assert "cy_platform" not in source
    assert "CapabilityExecutionService" not in source


def test_running_the_bootstrap_by_path_does_not_shadow_stdlib_modules() -> None:
    """A vendored package must not hide standard-library modules.

    The packaged launcher executes ``src/cyrene_plugin_runtime/bootstrap.py`` by
    path, so the package directory becomes ``sys.path[0]``; without normalizing
    it, ``cyrene_plugin_runtime/logging.py`` shadows ``logging`` and importing the
    server fails with ``AttributeError: module 'logging' has no attribute
    'getLogger'``.
    """

    bootstrap = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "cyrene_plugin_runtime"
        / "bootstrap.py"
    )
    result = subprocess.run(
        [sys.executable, "-B", str(bootstrap), "--help"],
        capture_output=True,
        text=True,
        cwd=bootstrap.parent,
    )

    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()
