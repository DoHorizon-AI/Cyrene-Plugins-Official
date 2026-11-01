"""Exercise Product event subscriptions through a real local gRPC endpoint.

中文：验证公开客户端的订阅模式、原生取消及关闭迭代器后的资源释放。
"""

from threading import Event

import pytest
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, serve
from cyrene_plugin_runtime.client import (
    DirectPluginDeadlineExceeded,
    DirectPluginInvocationCancelled,
)


class EventPlugin:
    """A provider fixture whose event stream differs from its invoke path."""

    plugin_id = "test.subscription.provider"
    version = "1.0.0"
    capabilities = ("test.events.v1",)

    def __init__(self) -> None:
        self.closed = Event()
        self.subscription_ids: list[str] = []

    def on_invoke(self, *_args: object, **_kwargs: object) -> tuple[bool, str]:
        return False, "METHOD_NOT_FOUND: events require subscription mode"

    def on_subscribe(
        self,
        subscription_id: str,
        capability: str,
        filter_payload: bytes,
        *,
        emitter: object,
    ) -> None:
        """Emit one event, then leave cancellation to the native RPC."""

        assert capability == "test.events.v1"
        self.subscription_ids.append(subscription_id)
        emitter.emit(  # type: ignore[attr-defined]
            "inbound_message",
            filter_payload,
            "type.googleapis.com/test.events.v1.Event",
        )

    def on_unsubscribe(self, _subscription_id: str, reason: str) -> None:
        assert reason
        self.closed.set()


@pytest.mark.parametrize("termination", ["close", "cancel", "deadline"])
def test_subscription_preserves_event_type_and_releases_provider(
    termination: str,
) -> None:
    """Each termination path unsubscribes without turning events into invokes."""

    plugin = EventPlugin()
    server, connection = serve(plugin, "test.events.v1", "1", "127.0.0.1:0")
    client = DirectPluginClient.for_local_connection_ref(connection)
    cancelled = Event()
    events = client.subscribe(
        capability="test.events.v1",
        interface_version="1",
        request=DirectPayload(
            "type.googleapis.com/test.events.v1.Filter", b"owner-filter"
        ),
        request_id="subscription-1",
        cancel_event=cancelled,
        deadline_seconds=0.2 if termination == "deadline" else 3,
    )
    try:
        assert next(events) == DirectPayload(
            "type.googleapis.com/test.events.v1.Event",
            b"owner-filter",
            "inbound_message",
        )
        if termination == "close":
            events.close()
        elif termination == "cancel":
            cancelled.set()
            with pytest.raises(DirectPluginInvocationCancelled):
                next(events)
        else:
            with pytest.raises(DirectPluginDeadlineExceeded):
                next(events)
        assert plugin.closed.wait(2), "provider did not release its subscription"
        assert plugin.subscription_ids == ["subscription-1"]
    finally:
        events.close()
        client.close()
        server.stop(grace=None).wait()


def test_pre_cancelled_subscription_never_contacts_provider() -> None:
    """Cancellation before iteration prevents subscription side effects."""

    plugin = EventPlugin()
    server, connection = serve(plugin, "test.events.v1", "1", "127.0.0.1:0")
    cancelled = Event()
    cancelled.set()
    try:
        with DirectPluginClient.for_local_connection_ref(connection) as client:
            events = client.subscribe(
                capability="test.events.v1",
                interface_version="1",
                request=DirectPayload("type.googleapis.com/test.events.v1.Filter", b""),
                cancel_event=cancelled,
            )
            with pytest.raises(DirectPluginInvocationCancelled):
                next(events)
        assert not plugin.subscription_ids
    finally:
        server.stop(grace=None).wait()
