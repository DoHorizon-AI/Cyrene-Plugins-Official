"""Serve one configured Plugin instance over its direct data-plane endpoint."""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import queue
import signal
import threading
import uuid
from collections.abc import Iterable, Sequence
from concurrent import futures
from typing import Any

import grpc

from ._generated import direct_plugin_runtime_pb2 as wire
from ._generated import direct_plugin_runtime_pb2_grpc as wire_grpc

_MAX_ID_BYTES = 256
_MAX_TYPE_URL_BYTES = 512
_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024


class _ContextCancellation:
    """Expose gRPC termination as a thread-safe Plugin cancellation signal."""

    def __init__(self, context: grpc.ServicerContext) -> None:
        self._context = context
        self._cancelled = threading.Event()
        if not context.add_callback(self._cancelled.set):
            self._cancelled.set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set() or not self._context.is_active()

    def is_set(self) -> bool:
        return self.is_cancelled()


class _SubscriptionEmitter:
    def __init__(self, events: queue.Queue[wire.DirectPayload]) -> None:
        self._events = events

    def emit(self, event_type: str, payload: bytes, type_url: str = "") -> bool:
        try:
            self._events.put_nowait(
                wire.DirectPayload(
                    event_type=event_type,
                    type_url=type_url,
                    value=payload,
                )
            )
        except queue.Full:
            return False
        return True


class DirectPluginService(wire_grpc.DirectPluginRuntimeServicer):
    """A direct endpoint bound to exactly one Plugin implementation."""

    def __init__(
        self,
        plugin: Any,
        capability: str,
        interface_versions: str | Sequence[str],
    ) -> None:
        self._plugin = plugin
        self._capability = _required_text(capability, "capability", _MAX_ID_BYTES)
        versions = (
            (interface_versions,)
            if isinstance(interface_versions, str)
            else tuple(interface_versions)
        )
        if not versions:
            raise ValueError("interface_versions must not be empty")
        self._interface_versions = frozenset(
            _required_text(version, "interface_version", _MAX_ID_BYTES)
            for version in versions
        )
        capabilities = tuple(getattr(plugin, "capabilities", ()))
        if capabilities and self._capability not in capabilities:
            raise ValueError(
                f"plugin does not declare configured capability {self._capability!r}"
            )

    def Invoke(
        self,
        request: wire.DirectInvocationRequest,
        context: grpc.ServicerContext,
    ) -> wire.DirectInvocationResponse:
        error = self._validate_request(request)
        if error is not None:
            return wire.DirectInvocationResponse(error=error)
        if request.stream_mode == wire.DIRECT_STREAM_MODE_SUBSCRIPTION:
            return wire.DirectInvocationResponse(
                error=_wire_error("INVALID_REQUEST: subscription requires InvokeStream")
            )
        success, result = self._dispatch(request, context, stream_results=False)
        if not success:
            return wire.DirectInvocationResponse(error=_wire_error(result))
        try:
            return wire.DirectInvocationResponse(payload=_wire_payload(result))
        except (TypeError, ValueError) as exc:
            return wire.DirectInvocationResponse(
                error=_wire_error(f"EXECUTION_FAILED: {exc}")
            )

    def InvokeStream(
        self,
        request: wire.DirectInvocationRequest,
        context: grpc.ServicerContext,
    ) -> Iterable[wire.DirectStreamItem]:
        error = self._validate_request(request)
        if error is not None:
            yield wire.DirectStreamItem(error=error)
            return
        if request.stream_mode == wire.DIRECT_STREAM_MODE_SUBSCRIPTION:
            yield from self._subscribe(request, context)
            return
        success, result = self._dispatch(request, context, stream_results=True)
        if not success:
            yield wire.DirectStreamItem(error=_wire_error(result))
            return
        items = getattr(result, "items", None)
        if items is None:
            items = (result,)
        try:
            for item in items:
                if not context.is_active():
                    yield wire.DirectStreamItem(
                        error=_wire_error("CANCELLED: direct request was cancelled")
                    )
                    return
                yield wire.DirectStreamItem(payload=_wire_payload(item))
        except Exception as exc:  # noqa: BLE001 - Plugin boundary must not tear down gRPC.
            yield wire.DirectStreamItem(error=_wire_error(f"EXECUTION_FAILED: {exc}"))
            return
        yield wire.DirectStreamItem(end=wire.DirectStreamEnd())

    def _subscribe(
        self,
        request: wire.DirectInvocationRequest,
        context: grpc.ServicerContext,
    ) -> Iterable[wire.DirectStreamItem]:
        subscribe = getattr(self._plugin, "on_subscribe", None)
        if not callable(subscribe):
            yield wire.DirectStreamItem(
                error=_wire_error(
                    "METHOD_NOT_FOUND: plugin does not support subscriptions"
                )
            )
            return
        events: queue.Queue[wire.DirectPayload] = queue.Queue(maxsize=256)
        available = inspect.signature(subscribe).parameters
        optional: dict[str, Any] = {"emitter": _SubscriptionEmitter(events)}
        kwargs = {name: value for name, value in optional.items() if name in available}
        try:
            failure = subscribe(
                request.request_id,
                request.capability,
                bytes(request.payload),
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - Plugin boundary returns structured failure.
            yield wire.DirectStreamItem(error=_wire_error(f"EXECUTION_FAILED: {exc}"))
            return
        if failure is not None:
            yield wire.DirectStreamItem(error=_wire_error(failure))
            return
        try:
            while context.is_active():
                try:
                    payload = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                yield wire.DirectStreamItem(payload=payload)
        finally:
            unsubscribe = getattr(self._plugin, "on_unsubscribe", None)
            if callable(unsubscribe):
                unsubscribe(request.request_id, "direct stream closed")

    def Health(
        self,
        request: wire.HealthRequest,
        context: grpc.ServicerContext,
    ) -> wire.HealthResponse:
        del request, context
        return wire.HealthResponse(
            status=wire.HealthResponse.STATUS_SERVING,
            plugin_id=str(getattr(self._plugin, "plugin_id", "unknown")),
            plugin_version=str(getattr(self._plugin, "version", "unknown")),
            capabilities=[self._capability],
        )

    def _validate_request(
        self, request: wire.DirectInvocationRequest
    ) -> wire.DirectInvocationError | None:
        fields = (
            (request.capability, "capability", _MAX_ID_BYTES),
            (request.interface_version, "interface_version", _MAX_ID_BYTES),
            (request.method, "method", _MAX_ID_BYTES),
            (request.payload_type_url, "payload_type_url", _MAX_TYPE_URL_BYTES),
            (request.request_id, "request_id", _MAX_ID_BYTES),
        )
        try:
            for value, name, maximum in fields:
                _required_text(value, name, maximum)
        except ValueError as exc:
            return _wire_error(f"INVALID_REQUEST: {exc}")
        if request.capability != self._capability:
            return _wire_error(
                f"INVALID_REQUEST: endpoint is bound to {self._capability!r}"
            )
        if request.interface_version not in self._interface_versions:
            return _wire_error(
                "INVALID_REQUEST: endpoint interface version does not match"
            )
        if len(request.payload) > _MAX_PAYLOAD_BYTES:
            return _wire_error("INVALID_REQUEST: payload exceeds 64 MiB")
        return None

    def _dispatch(
        self,
        request: wire.DirectInvocationRequest,
        context: grpc.ServicerContext,
        *,
        stream_results: bool,
    ) -> tuple[bool, Any]:
        invoke = getattr(self._plugin, "on_invoke", None)
        if not callable(invoke):
            return False, "METHOD_NOT_FOUND: plugin has no direct invocation adapter"
        available = inspect.signature(invoke).parameters
        cancel = getattr(self._plugin, "on_cancel", None)
        if callable(cancel):

            def cancel_plugin() -> None:
                cancel(request.request_id, "direct gRPC invocation ended")

            context.add_callback(cancel_plugin)
        optional: dict[str, Any] = {
            "cancellation": _ContextCancellation(context),
            "request_id": request.request_id,
            "request_type_url": request.payload_type_url,
            "stream_results": stream_results,
        }
        kwargs = {name: value for name, value in optional.items() if name in available}
        try:
            outcome = invoke(
                request.capability,
                request.method,
                bytes(request.payload),
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - Plugin boundary returns structured failure.
            return False, f"EXECUTION_FAILED: {exc}"
        if not isinstance(outcome, tuple) or len(outcome) != 2:
            return (
                False,
                "EXECUTION_FAILED: plugin returned an invalid invocation result",
            )
        return bool(outcome[0]), outcome[1]


def _required_text(value: str, name: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{name} exceeds {maximum_bytes} bytes")
    return value


def _wire_payload(value: Any) -> wire.DirectPayload:
    payload = getattr(value, "value", None)
    type_url = getattr(value, "type_url", None)
    if not isinstance(payload, bytes):
        raise TypeError("plugin result must expose bytes in value")
    if not isinstance(type_url, str) or not type_url.strip():
        raise TypeError("plugin result must expose a non-empty type_url")
    if len(payload) > _MAX_PAYLOAD_BYTES:
        raise ValueError("plugin result exceeds 64 MiB")
    _required_text(type_url, "result type_url", _MAX_TYPE_URL_BYTES)
    return wire.DirectPayload(type_url=type_url, value=payload)


def _wire_error(value: Any) -> wire.DirectInvocationError:
    text = str(value).strip() or "EXECUTION_FAILED: plugin invocation failed"
    prefix, separator, detail = text.partition(":")
    code_name = prefix.strip().upper() if separator else "EXECUTION_FAILED"
    message = detail.strip() if separator else text
    mapping = {
        "INVALID_INPUT": wire.DirectInvocationError.CODE_INVALID_REQUEST,
        "UNSUPPORTED_INPUT": wire.DirectInvocationError.CODE_INVALID_REQUEST,
        "INVALID_REQUEST": wire.DirectInvocationError.CODE_INVALID_REQUEST,
        "UNKNOWN_OPERATION": wire.DirectInvocationError.CODE_METHOD_NOT_FOUND,
        "METHOD_NOT_FOUND": wire.DirectInvocationError.CODE_METHOD_NOT_FOUND,
        "METHOD_NOT_SUPPORTED": wire.DirectInvocationError.CODE_METHOD_NOT_FOUND,
        "CANCELLED": wire.DirectInvocationError.CODE_CANCELLED,
        "TIMEOUT": wire.DirectInvocationError.CODE_DEADLINE_EXCEEDED,
        "DEADLINE_EXCEEDED": wire.DirectInvocationError.CODE_DEADLINE_EXCEEDED,
        "CAPABILITY_UNAVAILABLE": wire.DirectInvocationError.CODE_UNAVAILABLE,
        "UNAVAILABLE": wire.DirectInvocationError.CODE_UNAVAILABLE,
        "EXECUTION_FAILED": wire.DirectInvocationError.CODE_EXECUTION_FAILED,
    }
    code = mapping.get(code_name, wire.DirectInvocationError.CODE_EXECUTION_FAILED)
    return wire.DirectInvocationError(
        code=code,
        message=message[:4096],
        domain_code=code_name[:256],
        retryable=code
        in {
            wire.DirectInvocationError.CODE_DEADLINE_EXCEEDED,
            wire.DirectInvocationError.CODE_UNAVAILABLE,
        },
    )


def load_entrypoint(value: str) -> Any:
    module_name, separator, attribute_name = value.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("entrypoint must use module:attribute")
    attribute = getattr(importlib.import_module(module_name), attribute_name)
    return attribute() if inspect.isclass(attribute) else attribute


def serve(
    plugin: Any,
    capability: str,
    interface_versions: str | Sequence[str],
    listen: str,
    *,
    workers: int = 8,
) -> tuple[grpc.Server, str]:
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max(1, workers)),
        options=(
            ("grpc.max_receive_message_length", _MAX_PAYLOAD_BYTES),
            ("grpc.max_send_message_length", _MAX_PAYLOAD_BYTES),
        ),
    )
    wire_grpc.add_DirectPluginRuntimeServicer_to_server(
        DirectPluginService(plugin, capability, interface_versions), server
    )
    bound_port = server.add_insecure_port(listen)
    if bound_port == 0:
        raise RuntimeError(f"could not bind direct Plugin endpoint {listen!r}")
    host, separator, configured_port = listen.rpartition(":")
    connection = (
        f"grpc://{host}:{bound_port}"
        if separator and configured_port == "0"
        else f"grpc://{listen}"
    )
    server.start()
    return server, connection


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--capability", required=True)
    parser.add_argument("--interface-version", required=True, action="append")
    parser.add_argument("--listen", default="127.0.0.1:0")
    parser.add_argument("--workers", type=int, default=8)
    options = parser.parse_args(arguments)

    plugin = load_entrypoint(options.entrypoint)
    server, connection = serve(
        plugin,
        options.capability,
        options.interface_version,
        options.listen,
        workers=options.workers,
    )
    print(
        json.dumps(
            {
                "event": "direct_plugin_ready",
                "connection_ref": connection,
                "capability": options.capability,
                "interface_version": options.interface_version[0],
                "interface_versions": options.interface_version,
                "runtime_id": str(uuid.uuid4()),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    stopped = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    stopped.wait()
    server.stop(grace=2).wait()
    shutdown = getattr(plugin, "on_shutdown", None)
    if callable(shutdown):
        shutdown(2_000)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
