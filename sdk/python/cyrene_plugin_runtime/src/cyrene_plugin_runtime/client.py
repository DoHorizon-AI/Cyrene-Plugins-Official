"""Product client for one Plugins-owned direct endpoint.

The selected endpoint is already bound to one configured Plugin instance. This
client transports capability-owned bytes and preserves cancellation, deadlines,
and typed Plugin failures without adding routing or retry policy.

面向单个 Plugins 直连端点的 Product 客户端。端点已绑定到一个配置好的 Plugin
实例；本客户端只传输能力自有字节并保留取消、超时和类型化错误语义，不增加路由
或重试策略。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any, Self

import grpc

from ._generated import direct_plugin_runtime_pb2 as wire
from ._generated import direct_plugin_runtime_pb2_grpc as wire_grpc


@dataclass(frozen=True, slots=True)
class DirectPayload:
    """One capability-owned typed payload."""

    type_url: str
    value: bytes
    event_type: str = ""


class DirectPluginError(RuntimeError):
    """Base error for a direct Plugin invocation."""


class DirectPluginProtocolError(DirectPluginError):
    """The endpoint violated the direct runtime protocol."""


class DirectPluginFailure(DirectPluginError):
    """The Plugin endpoint returned a typed invocation failure."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        domain_code: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.domain_code = domain_code
        self.retryable = retryable

    @property
    def is_cancelled(self) -> bool:
        """Whether the endpoint classified the outcome as cancellation."""

        return self.code == wire.DirectInvocationError.CODE_CANCELLED


class DirectPluginTransportError(DirectPluginError):
    """gRPC failed before the Plugin endpoint returned a typed result."""

    def __init__(self, status: grpc.StatusCode, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class DirectPluginInvocationCancelled(DirectPluginTransportError):
    """The Product cancelled the direct invocation."""


class DirectPluginDeadlineExceeded(DirectPluginTransportError):
    """The direct invocation exceeded its native gRPC deadline."""


class DirectPluginClient:
    """Call one resolved Plugin endpoint without a Platform data-plane hop."""

    def __init__(self, channel: grpc.Channel, *, owns_channel: bool = False) -> None:
        self._channel = channel
        self._owns_channel = owns_channel
        self._stub = wire_grpc.DirectPluginRuntimeStub(channel)

    @classmethod
    def for_local_connection_ref(cls, connection_ref: str) -> DirectPluginClient:
        """Create an insecure client for a loopback or Unix connection ref."""

        target = connection_ref.removeprefix("grpc://")
        if not _is_local_target(target):
            raise ValueError(
                "insecure direct Plugin endpoints must be loopback or Unix-domain sockets"
            )
        return cls(grpc.insecure_channel(target), owns_channel=True)

    def close(self) -> None:
        """Close a channel created by :meth:`for_local_connection_ref`."""

        if self._owns_channel:
            self._channel.close()

    def invoke(
        self,
        *,
        capability: str,
        interface_version: str,
        method: str,
        request: DirectPayload,
        deadline_seconds: float | None = None,
        cancel_event: Event | None = None,
        request_id: str | None = None,
    ) -> DirectPayload:
        """Invoke one method and return its typed response."""

        invocation = _invocation(
            capability=capability,
            interface_version=interface_version,
            method=method,
            request=request,
            request_id=request_id,
        )
        _validate_deadline(deadline_seconds)
        _raise_if_pre_cancelled(cancel_event)
        rpc = self._stub.Invoke.future(invocation, timeout=deadline_seconds)
        completed = Event()
        watcher = _start_cancellation_watcher(rpc, cancel_event, completed)
        try:
            response = rpc.result()
        except grpc.FutureCancelledError as error:
            raise DirectPluginInvocationCancelled(
                grpc.StatusCode.CANCELLED, "direct Plugin invocation cancelled"
            ) from error
        except grpc.RpcError as error:
            _raise_transport_error(error)
            raise AssertionError("unreachable")
        finally:
            completed.set()
            if watcher is not None:
                watcher.join(timeout=0.2)

        result = response.WhichOneof("result")
        if result == "payload":
            return _payload(response.payload)
        if result == "error":
            raise _failure(response.error)
        raise DirectPluginProtocolError("direct Plugin endpoint returned no result")

    def invoke_stream(
        self,
        *,
        capability: str,
        interface_version: str,
        method: str,
        request: DirectPayload,
        deadline_seconds: float | None = None,
        cancel_event: Event | None = None,
        request_id: str | None = None,
    ) -> Iterator[DirectPayload]:
        """Yield ordered typed results until the endpoint sends its end marker."""

        invocation = _invocation(
            capability=capability,
            interface_version=interface_version,
            method=method,
            request=request,
            request_id=request_id,
        )
        invocation.stream_mode = wire.DIRECT_STREAM_MODE_INVOCATION
        _validate_deadline(deadline_seconds)
        _raise_if_pre_cancelled(cancel_event)
        rpc = self._stub.InvokeStream(invocation, timeout=deadline_seconds)
        completed = Event()
        watcher = _start_cancellation_watcher(rpc, cancel_event, completed)
        saw_end = False
        try:
            for item in rpc:
                event = item.WhichOneof("event")
                if event == "payload":
                    yield _payload(item.payload)
                    continue
                if event == "error":
                    raise _failure(item.error)
                if event == "end":
                    saw_end = True
                    return
                raise DirectPluginProtocolError(
                    "direct Plugin endpoint returned an empty stream item"
                )
            if not saw_end:
                raise DirectPluginProtocolError(
                    "direct Plugin endpoint closed without an end marker"
                )
        except grpc.FutureCancelledError as error:
            raise DirectPluginInvocationCancelled(
                grpc.StatusCode.CANCELLED, "direct Plugin invocation cancelled"
            ) from error
        except grpc.RpcError as error:
            _raise_transport_error(error)
            raise AssertionError("unreachable")
        finally:
            completed.set()
            if watcher is not None:
                watcher.join(timeout=0.2)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _invocation(
    *,
    capability: str,
    interface_version: str,
    method: str,
    request: DirectPayload,
    request_id: str | None,
) -> Any:
    for name, value in (
        ("capability", capability),
        ("interface_version", interface_version),
        ("method", method),
        ("request.type_url", request.type_url),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must not be empty")
    resolved_request_id = request_id or str(uuid.uuid4())
    if not resolved_request_id.strip():
        raise ValueError("request_id must not be empty when provided")
    return wire.DirectInvocationRequest(
        capability=capability,
        interface_version=interface_version,
        method=method,
        payload_type_url=request.type_url,
        payload=request.value,
        request_id=resolved_request_id,
    )


def _payload(value: Any) -> DirectPayload:
    if not value.type_url:
        raise DirectPluginProtocolError(
            "direct Plugin endpoint returned a payload without type URL"
        )
    return DirectPayload(
        type_url=value.type_url,
        value=bytes(value.value),
        event_type=value.event_type,
    )


def _failure(value: Any) -> DirectPluginFailure:
    return DirectPluginFailure(
        value.code,
        value.message,
        domain_code=value.domain_code,
        retryable=value.retryable,
    )


def _raise_if_pre_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DirectPluginInvocationCancelled(
            grpc.StatusCode.CANCELLED, "direct Plugin invocation cancelled"
        )


def _validate_deadline(deadline_seconds: float | None) -> None:
    if deadline_seconds is not None and deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be positive when provided")


def _raise_transport_error(error: grpc.RpcError) -> None:
    status = error.code()
    message = error.details() or str(error)
    if status is grpc.StatusCode.CANCELLED:
        raise DirectPluginInvocationCancelled(status, message) from error
    if status is grpc.StatusCode.DEADLINE_EXCEEDED:
        raise DirectPluginDeadlineExceeded(status, message) from error
    raise DirectPluginTransportError(status, message) from error


def _start_cancellation_watcher(
    rpc: Any,
    cancel_event: Event | None,
    completed: Event,
) -> Thread | None:
    if cancel_event is None:
        return None

    def cancel_when_requested() -> None:
        while not completed.wait(0.01):
            if cancel_event.is_set():
                rpc.cancel()
                return

    watcher = Thread(
        target=cancel_when_requested,
        name="cyrene-direct-plugin-cancel",
        daemon=True,
    )
    watcher.start()
    return watcher


def _is_local_target(target: str) -> bool:
    if target.startswith("unix:"):
        return True
    host, separator, port = target.rpartition(":")
    if not separator or not port.isdigit():
        return False
    return host.strip("[]").lower() in {"127.0.0.1", "localhost", "::1"}


__all__ = [
    "DirectPayload",
    "DirectPluginClient",
    "DirectPluginDeadlineExceeded",
    "DirectPluginError",
    "DirectPluginFailure",
    "DirectPluginInvocationCancelled",
    "DirectPluginProtocolError",
    "DirectPluginTransportError",
]
