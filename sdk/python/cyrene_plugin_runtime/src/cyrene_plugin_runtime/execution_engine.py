"""Shared execution.engine.v1 adapter for Plugin implementations.

┌─────────────────────────────────────────────────────────────────────┐
│  📄 execution_engine.py                                             │
│  Module: cyrene_plugin_runtime                                      │
│  Role: Canonical execution.engine.v1 JSON direct-runtime adapter.   │
│                                                                     │
│  模块职责：统一 execution.engine.v1 JSON 直连运行时适配行为。             │
└─────────────────────────────────────────────────────────────────────┘

The adapter owns only request validation, typed JSON framing, cancellation
correlation, and failure classification. It does not add a transport: the
canonical ``DirectPluginRuntime`` server remains the sole data-plane protocol.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Event, Lock
from typing import Any

CAPABILITY_ID = "execution.engine.v1"
INTERFACE_VERSION = "1"
METHODS = frozenset(
    {"load_model", "execute_inference", "unload_model", "get_engine_info"}
)
EMPTY_REQUEST_METHODS = frozenset({"unload_model", "get_engine_info"})
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
LOAD_MODEL_FIELDS = frozenset(
    {
        "model_path",
        "adapter_path",
        "adapter_name",
        "max_lora_rank",
        "max_output_len",
        "max_model_len",
        "max_batch_size",
        "tensor_parallel_size",
        "gpu_memory_utilization",
        "quantization",
        "enable_prefix_caching",
        "kv_cache_dtype",
    }
)
EXECUTE_INFERENCE_FIELDS = frozenset(
    {"prompt", "max_new_tokens", "temperature", "top_p", "cancel_requested"}
)


@dataclass(frozen=True, slots=True)
class DirectTypedPayload:
    """One typed JSON response returned through DirectPluginRuntime.

        中文:通过 DirectPluginRuntime 返回的一项类型化 JSON 响应。
    """

    value: bytes
    type_url: str


class ExecutionEngineRequestError(ValueError):
    """A request failed execution.engine.v1 JSON validation.

        中文:请求未通过 execution.engine.v1 JSON 校验。
    """


def is_cancelled(*signals: Any) -> bool:
    """Return whether any supplied runtime cancellation signal is set.

        中文:返回任意一个已提供 Runtime 取消信号是否处于设置状态。
    """

    for signal in signals:
        if signal is None:
            continue
        checker = getattr(signal, "is_cancelled", None)
        if callable(checker) and checker():
            return True
        checker = getattr(signal, "is_set", None)
        if callable(checker) and checker():
            return True
    return False


def _positive_int(value: Any, field: str) -> None:
    """Validate one positive integer JSON field without accepting booleans.

        中文:校验一个正整数 JSON 字段,并拒绝布尔值。
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExecutionEngineRequestError(f"{field} must be a positive integer")


def _decode_request(payload: bytes, action: str) -> dict[str, Any]:
    """Decode and validate one canonical execution.engine.v1 request.

        中文:解码并校验一条规范的 execution.engine.v1 请求。
    """

    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ExecutionEngineRequestError("request exceeds 64 MiB")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecutionEngineRequestError(
            f"request must be valid JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ExecutionEngineRequestError("request must be a JSON object")
    if action in EMPTY_REQUEST_METHODS:
        if value:
            raise ExecutionEngineRequestError("request must be an empty object")
        return value

    required_name = "model_path" if action == "load_model" else "prompt"
    required_value = value.get(required_name)
    if not isinstance(required_value, str) or not required_value.strip():
        raise ExecutionEngineRequestError(
            f"{required_name} must be non-empty text"
        )
    if action == "load_model":
        unknown = set(value) - LOAD_MODEL_FIELDS
        if unknown:
            raise ExecutionEngineRequestError(
                f"unsupported request fields: {', '.join(sorted(unknown))}"
            )
        for field in (
            "max_output_len",
            "max_model_len",
            "max_batch_size",
            "max_lora_rank",
            "tensor_parallel_size",
        ):
            if field in value:
                _positive_int(value[field], field)
        for field in (
            "adapter_path",
            "adapter_name",
            "quantization",
            "kv_cache_dtype",
        ):
            if field in value and (
                not isinstance(value[field], str) or not value[field].strip()
            ):
                raise ExecutionEngineRequestError(f"{field} must be non-empty text")
        utilization = value.get("gpu_memory_utilization")
        if utilization is not None and (
            isinstance(utilization, bool)
            or not isinstance(utilization, (int, float))
            or not math.isfinite(utilization)
            or not 0 < utilization <= 1
        ):
            raise ExecutionEngineRequestError(
                "gpu_memory_utilization must be a finite number in (0, 1]"
            )
        prefix_caching = value.get("enable_prefix_caching")
        if prefix_caching is not None and not isinstance(prefix_caching, bool):
            raise ExecutionEngineRequestError(
                "enable_prefix_caching must be boolean"
            )
    else:
        unknown = set(value) - EXECUTE_INFERENCE_FIELDS
        if unknown:
            raise ExecutionEngineRequestError(
                f"unsupported request fields: {', '.join(sorted(unknown))}"
            )
        if "max_new_tokens" in value:
            _positive_int(value["max_new_tokens"], "max_new_tokens")
        for field in ("temperature", "top_p"):
            if field not in value:
                continue
            number = value[field]
            try:
                finite = math.isfinite(number)
            except (OverflowError, TypeError):
                finite = False
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not finite:
                raise ExecutionEngineRequestError(
                    f"{field} must be a finite number"
                )
        if "cancel_requested" in value and not isinstance(
            value["cancel_requested"], bool
        ):
            raise ExecutionEngineRequestError("cancel_requested must be boolean")
    return value


class ExecutionEngineDirectAdapter:
    """Mixin implementing one canonical execution.engine.v1 direct endpoint.

        中文:实现一个规范 execution.engine.v1 direct Endpoint 的 mixin。
    """

    plugin_id = ""
    version = "0.1.0"
    capabilities = (CAPABILITY_ID,)

    def __init__(self) -> None:
        self._active_cancellations: dict[str, Event] = {}
        self._cancellation_lock = Lock()

    def on_cancel(self, request_id: str, reason: str) -> None:
        """Propagate DirectPluginRuntime cancellation to an active request.

            中文:将 DirectPluginRuntime 的取消信号传播给正在处理的请求。
        """

        del reason
        with self._cancellation_lock:
            cancellation = self._active_cancellations.get(request_id)
        if cancellation is not None:
            cancellation.set()

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: Any = None,
        request_id: str | None = None,
        request_type_url: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, DirectTypedPayload | str]:
        """Adapt one JSON request to the canonical direct runtime endpoint.

            中文:将 JSON 请求适配到规范 direct runtime Endpoint。
        """

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action not in METHODS:
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        expected_type_url = f"type.cyrene.io/{CAPABILITY_ID}.{action}.request"
        if request_type_url != expected_type_url:
            return False, f"INVALID_REQUEST: request_type_url must be {expected_type_url}"
        if stream_results:
            return False, (
                "METHOD_NOT_SUPPORTED: execution engine methods are not streaming"
            )

        local_cancellation = Event()
        if request_id is not None:
            with self._cancellation_lock:
                if request_id in self._active_cancellations:
                    return False, f"INVALID_REQUEST: duplicate request id {request_id}"
                self._active_cancellations[request_id] = local_cancellation
        try:
            if is_cancelled(cancellation, local_cancellation):
                return False, "CANCELLED: operation cancelled"
            request = _decode_request(payload, action)
            if request.get("cancel_requested") is True:
                return False, "CANCELLED: operation cancelled"
            arguments = dict(request)
            if action in {"load_model", "execute_inference"}:
                arguments["cancellation"] = local_cancellation
            result = getattr(self, action)(**arguments)
            if is_cancelled(cancellation, local_cancellation):
                return False, "CANCELLED: operation cancelled"
        except ExecutionEngineRequestError as error:
            return False, f"INVALID_REQUEST: {error}"
        except FileNotFoundError as error:
            return False, f"INVALID_REQUEST: {error}"
        except RuntimeError as error:
            text = str(error)
            lowered = text.lower()
            if "cancelled" in lowered or "canceled" in lowered:
                code = "CANCELLED"
            elif "not installed" in lowered or "unavailable" in lowered:
                code = "CAPABILITY_UNAVAILABLE"
            else:
                code = "EXECUTION_FAILED"
            return False, f"{code}: {error}"
        except Exception as error:  # noqa: BLE001 - isolate plugin failures at boundary
            return False, f"EXECUTION_FAILED: {error}"
        finally:
            if request_id is not None:
                with self._cancellation_lock:
                    self._active_cancellations.pop(request_id, None)
        if not isinstance(result, Mapping):
            return False, "EXECUTION_FAILED: engine result must be a JSON object"
        try:
            encoded = json.dumps(
                result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            return False, f"EXECUTION_FAILED: engine result is not JSON: {error}"
        return True, DirectTypedPayload(
            encoded, f"type.cyrene.io/{CAPABILITY_ID}.{action}.response"
        )


__all__ = [
    "CAPABILITY_ID",
    "INTERFACE_VERSION",
    "DirectTypedPayload",
    "ExecutionEngineDirectAdapter",
    "is_cancelled",
]
