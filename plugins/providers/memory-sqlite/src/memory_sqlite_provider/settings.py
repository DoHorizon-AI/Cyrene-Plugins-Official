"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 settings.py                                                      │
│  Module: memory_sqlite_provider                                      │
│  Role: Validate the host-owned activation and durable state path.    │
│                                                                      │
│  模块职责：校验宿主提供的激活配置和持久状态路径。                      │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cyrene_plugin_runtime.configuration import read_environment_settings

_MAX_CONFIGURATION_BYTES = 64 * 1024
_LOCAL_GRPC_ENDPOINT = re.compile(r"grpc://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}\Z")


class ConfigurationError(ValueError):
    """Activation settings are missing or outside the supported bounds."""


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Validated SQLite path, embedding profile, and bounded runtime settings."""

    database_path: Path
    embedding_model_id: str
    embedding_dimensions: int
    embedding_endpoint: str | None
    embedding_timeout_seconds: float
    recall_batch_size: int
    max_top_k: int

    @classmethod
    def from_environment(cls) -> ProviderSettings:
        """Read one standard DirectPluginRuntime activation configuration."""

        environment = read_environment_settings()
        if environment is None:
            raise ConfigurationError("memory provider activation configuration is unavailable")
        raw = environment["config"]
        if len(raw.encode("utf-8")) > _MAX_CONFIGURATION_BYTES:
            raise ConfigurationError("activation configuration exceeds its size limit")
        try:
            values = json.loads(raw, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as error:
            raise ConfigurationError("activation configuration must be valid JSON") from error
        if not isinstance(values, dict):
            raise ConfigurationError("activation configuration must be an object")
        allowed = {
            "database_path",
            "embedding_model_id",
            "embedding_dimensions",
            "embedding_endpoint",
            "embedding_timeout_seconds",
            "recall_batch_size",
            "max_top_k",
        }
        if set(values) - allowed:
            raise ConfigurationError("activation contains unsupported fields")
        try:
            path = _database_path(values.get("database_path"))
            model_id = _required_text(values.get("embedding_model_id"), "embedding_model_id", 256)
            dimensions = _bounded_integer(
                values.get("embedding_dimensions"), "embedding_dimensions", 1, 4096
            )
            endpoint = _embedding_endpoint(values.get("embedding_endpoint"))
            timeout = _bounded_number(
                values.get("embedding_timeout_seconds", 30), "embedding_timeout_seconds", 0.1, 120
            )
            batch_size = _bounded_integer(
                values.get("recall_batch_size", 256), "recall_batch_size", 16, 4096
            )
            max_top_k = _bounded_integer(values.get("max_top_k", 100), "max_top_k", 1, 1000)
        except (TypeError, ValueError) as error:
            raise ConfigurationError(str(error)) from error
        return cls(
            database_path=path,
            embedding_model_id=model_id,
            embedding_dimensions=dimensions,
            embedding_endpoint=endpoint,
            embedding_timeout_seconds=timeout,
            recall_batch_size=batch_size,
            max_top_k=max_top_k,
        )


def _database_path(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("database_path must be an absolute file path")
    path = Path(value)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise ValueError("database_path must be an absolute file path")
    return path


def _embedding_endpoint(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _LOCAL_GRPC_ENDPOINT.fullmatch(value) is None:
        raise ValueError("embedding_endpoint must be a local DirectPluginRuntime gRPC endpoint")
    port = int(value.rsplit(":", 1)[1])
    if not 1 <= port <= 65535:
        raise ValueError("embedding_endpoint port must be between 1 and 65535")
    return value


def _required_text(value: Any, name: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{name} exceeds its byte limit")
    return value.strip()


def _bounded_integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _bounded_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number from {minimum} through {maximum}")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{name} must be a number from {minimum} through {maximum}")
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("activation configuration contains duplicate fields")
        result[key] = value
    return result
