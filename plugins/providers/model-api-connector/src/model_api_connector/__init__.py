"""Plugins-owned OpenAI-compatible implementation of ``model.provider.v1``.

The connector owns the typed chat transport to one operator-configured upstream
endpoint. Routing, credentials, quota and lifecycle stay with the Product that
resolved the binding. | 只负责类型化传输，不拥有路由与凭据生命周期。
"""

from .mapping import ProviderMappingError, chunk_from_event, response_chunks, to_upstream_body
from .plugin import ModelApiConnector, TypedPayload
from .settings import ConfigurationError, ProviderSettings
from .upstream import (
    OpenAICompatibleUpstream,
    ProviderCancelled,
    UpstreamCall,
    UpstreamFailure,
    parse_sse_line,
)

__all__ = [
    "ConfigurationError",
    "ModelApiConnector",
    "OpenAICompatibleUpstream",
    "ProviderCancelled",
    "ProviderMappingError",
    "ProviderSettings",
    "TypedPayload",
    "UpstreamCall",
    "UpstreamFailure",
    "chunk_from_event",
    "parse_sse_line",
    "response_chunks",
    "to_upstream_body",
]
