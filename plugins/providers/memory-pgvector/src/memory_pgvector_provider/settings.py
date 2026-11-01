"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 settings.py                                                      │
│  Module: memory_pgvector_provider                                    │
│  Role: Resolve secret-backed PostgreSQL activation configuration.   │
│                                                                      │
│  模块职责：解析通过 secret ref 注入的 PostgreSQL 激活配置。          │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from psycopg.conninfo import conninfo_to_dict

_CONFIGURATION_ENV = "CYRENE_CAPABILITY_CONFIGURATION_JSON"
_BINDING_ENV = "CYRENE_CAPABILITY_BINDING_ID"
_DSN_REF = "memory-pgvector/database-dsn"
_MAX_CONFIG_BYTES = 64 * 1024
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_MODEL_ENDPOINT = re.compile(
    r"grpc://(?:127\.0\.0\.1|localhost|\[::1\]):"
    r"(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])\Z"
)


class ConfigurationError(ValueError):
    """The host supplied activation or secret-reference mapping is invalid."""


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Validated settings with the database credential hidden from repr output."""

    database_dsn: str = field(repr=False)
    embedding_model_id: str
    embedding_dimensions: int
    embedding_endpoint: str | None
    embedding_timeout_seconds: float
    pool_min_size: int
    pool_max_size: int
    connection_timeout_seconds: float
    recall_batch_size: int
    max_top_k: int
    embedding_profile_id: str | None = None
    embedding_document_prefix: str = ""
    embedding_query_prefix: str = ""
    statement_timeout_seconds: float = 30

    @property
    def vector_profile_id(self) -> str:
        """Return the stable identity of model plus input preprocessing."""

        return self.embedding_profile_id or self.embedding_model_id

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ProviderSettings:
        """Resolve only the configured stable database DSN reference."""

        source = os.environ if environ is None else environ
        if not _required_text(source.get(_BINDING_ENV), "capability binding"):
            raise ConfigurationError("capability binding is unavailable")
        raw = source.get(_CONFIGURATION_ENV, "{}")
        if len(raw.encode("utf-8")) > _MAX_CONFIG_BYTES:
            raise ConfigurationError("activation configuration exceeds its size limit")
        try:
            values = json.loads(raw, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as error:
            raise ConfigurationError("activation configuration must be valid JSON") from error
        if not isinstance(values, dict):
            raise ConfigurationError("activation configuration must be an object")
        allowed = {
            "database_dsn_ref",
            "embedding_model_id",
            "embedding_profile_id",
            "embedding_document_prefix",
            "embedding_query_prefix",
            "embedding_dimensions",
            "embedding_endpoint",
            "embedding_timeout_seconds",
            "pool_min_size",
            "pool_max_size",
            "connection_timeout_seconds",
            "statement_timeout_seconds",
            "recall_batch_size",
            "max_top_k",
            "hostSecretRefs",
        }
        if set(values) - allowed:
            raise ConfigurationError("activation contains unsupported fields")
        if values.get("database_dsn_ref") != _DSN_REF:
            raise ConfigurationError("database_dsn_ref must name the configured database secret")
        references = values.get("hostSecretRefs")
        if not isinstance(references, dict) or set(references) != {_DSN_REF}:
            raise ConfigurationError("hostSecretRefs must contain only the database DSN ref")
        environment_name = references[_DSN_REF]
        if not isinstance(environment_name, str) or _ENV_NAME.fullmatch(environment_name) is None:
            raise ConfigurationError("database DSN environment reference is invalid")
        dsn = source.get(environment_name)
        if not isinstance(dsn, str) or not dsn.strip() or len(dsn.encode("utf-8")) > 8192:
            raise ConfigurationError("database DSN secret is unavailable")
        try:
            parsed_dsn = conninfo_to_dict(dsn)
        except (TypeError, ValueError) as error:
            raise ConfigurationError("database DSN is invalid") from error
        if not parsed_dsn:
            raise ConfigurationError("database DSN is invalid")

        try:
            model_id = _required_text(values.get("embedding_model_id"), "embedding_model_id")
            profile_id = values.get("embedding_profile_id")
            document_prefix = _optional_text(
                values.get("embedding_document_prefix", ""),
                "embedding_document_prefix",
            )
            query_prefix = _optional_text(
                values.get("embedding_query_prefix", ""), "embedding_query_prefix"
            )
            if (document_prefix or query_prefix) and profile_id is None:
                raise ValueError(
                    "embedding_profile_id is required when an embedding prefix is configured"
                )
            vector_profile_id = (
                model_id
                if profile_id is None
                else _required_text(profile_id, "embedding_profile_id")
            )
            dimensions = _bounded_integer(
                values.get("embedding_dimensions"), "embedding_dimensions", 1, 4096
            )
            endpoint = _model_endpoint(values.get("embedding_endpoint"))
            embedding_timeout = _bounded_number(
                values.get("embedding_timeout_seconds", 30),
                "embedding_timeout_seconds",
                0.1,
                120,
            )
            min_pool = _bounded_integer(values.get("pool_min_size", 1), "pool_min_size", 1, 8)
            max_pool = _bounded_integer(values.get("pool_max_size", 8), "pool_max_size", 1, 32)
            connection_timeout = _bounded_number(
                values.get("connection_timeout_seconds", 10),
                "connection_timeout_seconds",
                0.1,
                120,
            )
            statement_timeout = _bounded_number(
                values.get("statement_timeout_seconds", 30),
                "statement_timeout_seconds",
                0.1,
                600,
            )
            batch_size = _bounded_integer(
                values.get("recall_batch_size", 256), "recall_batch_size", 16, 4096
            )
            top_k = _bounded_integer(values.get("max_top_k", 100), "max_top_k", 1, 1000)
        except (TypeError, ValueError) as error:
            raise ConfigurationError(str(error)) from error
        if min_pool > max_pool:
            raise ConfigurationError("pool_min_size must not exceed pool_max_size")
        return cls(
            database_dsn=dsn,
            embedding_model_id=model_id,
            embedding_dimensions=dimensions,
            embedding_endpoint=endpoint,
            embedding_timeout_seconds=embedding_timeout,
            pool_min_size=min_pool,
            pool_max_size=max_pool,
            connection_timeout_seconds=connection_timeout,
            recall_batch_size=batch_size,
            max_top_k=top_k,
            embedding_profile_id=vector_profile_id,
            embedding_document_prefix=document_prefix,
            embedding_query_prefix=query_prefix,
            statement_timeout_seconds=statement_timeout,
        )


def _model_endpoint(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _MODEL_ENDPOINT.fullmatch(value) is None:
        raise ValueError("embedding_endpoint must be a loopback DirectPluginRuntime endpoint")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 256:
        raise ValueError(f"{name} must be non-empty text within 256 bytes")
    return value.strip()


def _optional_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 512:
        raise ValueError(f"{name} must be text within 512 bytes")
    return value


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
