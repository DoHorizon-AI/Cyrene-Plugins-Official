"""Typed tool.provider.v1 catalog adapter tests.

These tests validate registration and protocol envelopes, not live task bridging.
中文：测试类型化目录适配器，不代表真实Task bridge验收。
"""

from __future__ import annotations

from pathlib import Path

from cyrene_tool_provider_contracts import (
    METHOD_CALL_TOOL,
    METHOD_LIST_TOOLS,
    REQUEST_TYPE_URLS,
    RESPONSE_TYPE_URLS,
    decode_response,
    encode_request,
)
from cyrene_tool_provider_contracts import (
    tool_provider_pb2 as wire,
)

from cyrene_media import MediaLibrary
from cyrene_media.plugin import MediaProvider
from cyrene_media.settings import MediaSettings, TrustedCaller
from cyrene_media.tool_catalog import PLUGIN_VERSION


class _NoopResolver:
    """Resolver placeholder that must not be called by catalog requests."""

    def read(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("catalog call attempted artifact access")

    def write(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("catalog call attempted artifact access")


def _provider(tmp_path: Path) -> MediaProvider:
    """Construct one plugin instance against local actor-scoped state."""

    caller = TrustedCaller(
        caller_workload="cyrene-navigator",
        audience="media-library",
        organization_id="org-1",
        workspace_id="workspace-1",
        issuer="https://identity.local",
        verification_key=b"x" * 32,
    )
    settings = MediaSettings(
        binding_id="media-binding-1",
        state_dir=tmp_path / "state",
        timeout_seconds=5,
        trusted_callers=(caller,),
        artifact_resolver=_NoopResolver(),  # type: ignore[arg-type]
    )
    return MediaProvider(settings=settings, library=MediaLibrary(tmp_path / "library"))


def test_typed_catalog_contains_fixed_media_operations(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    request = wire.ListToolsRequest(binding_id="media-binding-1")
    encoded = encode_request(METHOD_LIST_TOOLS, request)

    accepted, response_payload = provider.on_invoke(
        "tool.provider.v1",
        METHOD_LIST_TOOLS,
        encoded.value,
        request_type_url=encoded.type_url,
    )
    response = decode_response(METHOD_LIST_TOOLS, response_payload)

    assert accepted is True
    assert response_payload.type_url == RESPONSE_TYPE_URLS[METHOD_LIST_TOOLS]
    assert response.catalog.catalog_version == PLUGIN_VERSION
    assert {item.provider_tool_id for item in response.catalog.tools} == {
        "media.asset.import",
        "media.asset.search",
        "media.asset.get",
        "media.asset.delete",
        "media.asset.restore",
        "media.card.render",
    }


def test_unknown_tool_returns_typed_error(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    request = wire.CallToolRequest(
        binding_id="media-binding-1",
        provider_tool_id="unknown.tool",
        arguments_json="{}",
        catalog_version=PLUGIN_VERSION,
    )
    encoded = encode_request(METHOD_CALL_TOOL, request)

    accepted, response_payload = provider.on_invoke(
        "tool.provider.v1",
        METHOD_CALL_TOOL,
        encoded.value,
        request_type_url=REQUEST_TYPE_URLS[METHOD_CALL_TOOL],
    )
    response = decode_response(METHOD_CALL_TOOL, response_payload)

    assert accepted is True
    assert response.outcome.is_error is True
    assert response_payload.type_url == RESPONSE_TYPE_URLS[METHOD_CALL_TOOL]
