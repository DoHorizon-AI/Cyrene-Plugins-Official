"""Closed activation settings for the SQLite knowledge graph provider.

中文：校验图谱提供方的封闭配置对象。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cyrene_plugin_runtime.configuration import read_environment_settings


class ConfigurationError(ValueError):
    """The plugin activation configuration is absent or invalid."""


@dataclass(frozen=True, slots=True)
class GraphSettings:
    """Validated local storage and resource bounds for one plugin binding."""

    database_path: Path
    max_nodes_per_namespace: int = 1_000_000
    max_edges_per_namespace: int = 5_000_000
    max_batch_items: int = 1_000
    max_page_size: int = 200
    max_expand_depth: int = 4
    max_expand_nodes: int = 200
    max_expand_edges: int = 1_000
    max_expand_response_bytes: int = 1_048_576

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> GraphSettings:
        """Validate a JSON configuration object without reading process state."""

        allowed = {
            "database_path",
            "max_nodes_per_namespace",
            "max_edges_per_namespace",
            "max_batch_items",
            "max_page_size",
            "max_expand_depth",
            "max_expand_nodes",
            "max_expand_edges",
            "max_expand_response_bytes",
        }
        if set(value) - allowed:
            raise ConfigurationError("configuration contains unsupported fields")
        raw_path = value.get("database_path")
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise ConfigurationError("database_path must be a non-empty absolute path")
        database_path = Path(raw_path)
        if not database_path.is_absolute() or database_path.name in {"", ".", ".."}:
            raise ConfigurationError("database_path must be a file path under an absolute path")

        defaults = {
            "max_nodes_per_namespace": (1_000_000, 1, 10_000_000),
            "max_edges_per_namespace": (5_000_000, 1, 50_000_000),
            "max_batch_items": (1_000, 1, 10_000),
            "max_page_size": (200, 1, 1_000),
            "max_expand_depth": (4, 1, 8),
            "max_expand_nodes": (200, 1, 500),
            "max_expand_edges": (1_000, 1, 10_000),
            "max_expand_response_bytes": (1_048_576, 256, 16_777_216),
        }
        limits: dict[str, int] = {}
        for name, (default, minimum, maximum) in defaults.items():
            item = value.get(name, default)
            if type(item) is not int or not minimum <= item <= maximum:
                raise ConfigurationError(f"{name} must be an integer from {minimum} to {maximum}")
            limits[name] = item

        return cls(database_path=database_path, **limits)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> GraphSettings:
        """Read the standard plugin activation object and parse its JSON body."""

        source = os.environ if environ is None else environ
        if environ is not None:
            binding_id = source.get("CYRENE_CAPABILITY_BINDING_ID")
            encoded = source.get("CYRENE_CAPABILITY_CONFIGURATION_JSON")
            if not binding_id:
                raise ConfigurationError("plugin binding ID is required")
            encoded = encoded or "{}"
        else:
            try:
                environment = read_environment_settings()
            except (TypeError, ValueError) as error:
                raise ConfigurationError("plugin activation configuration is invalid") from error
            if environment is None:
                raise ConfigurationError("plugin activation configuration is required")
            encoded = environment["config"]
        try:
            decoded = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise ConfigurationError("plugin configuration must be valid JSON") from error
        if not isinstance(decoded, Mapping):
            raise ConfigurationError("plugin configuration must be an object")
        return cls.from_mapping(decoded)
