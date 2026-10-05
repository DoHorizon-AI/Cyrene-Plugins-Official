"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 navigator_workbridge.py                                         │
│  Package: wecom_connector                                           │
│  Role: Expose canonical WeCom tools to a same-host Navigator bridge. │
│                                                                     │
│  模块职责：通过标准 tool.provider.v1 protobuf 将 WeCom 工具接入同宿主   │
│  Navigator Workbridge。                                             │
│  · 不复制 CLI 命令映射或执行逻辑。                                    │
│  · 固定当前 binding，并复用规范 catalog、approval 与调用检查。         │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ._generated import tool_provider_pb2 as tool_contract
from .connector import WeComConnector, WeComInstanceConfig
from .errors import ConnectorError
from .tool_provider import WRITE_TOOL_IDS


class NavigatorWeComBridge:
    """Adapt one configured connector to a small Navigator Workbridge surface.

    中文:将一个已配置 connector 适配到精简的 Navigator Workbridge 接口。
    """

    def __init__(self, connector: WeComConnector) -> None:
        binding_id = connector.configured_binding_id
        if not binding_id:
            raise ConnectorError(
                "UNAVAILABLE", "WeCom bridge requires a configured binding"
            )
        self._connector = connector
        self._binding_id = binding_id

    @property
    def binding_id(self) -> str:
        """Return the fixed connector binding handled by this bridge."""
        return self._binding_id

    def health(self) -> dict[str, Any]:
        """Return configured connector health without starting external sessions."""
        return self._connector.connection_health()

    def request_qr(self, params: Mapping[str, Any] | None = None) -> None:
        """Reject the QQ-only login operation on a WeCom bridge."""
        del params
        raise ConnectorError(
            "UNSUPPORTED", "WeCom bridge does not expose QQ QR login"
        )

    def poll_login(self, params: Mapping[str, Any] | None = None) -> None:
        """Reject the QQ-only login operation on a WeCom bridge."""
        del params
        raise ConnectorError(
            "UNSUPPORTED", "WeCom bridge does not expose QQ QR login"
        )

    def list_tools(self) -> dict[str, Any]:
        """Return the canonical tool catalog in Workbridge JSON shape."""
        request = tool_contract.ListToolsRequest(binding_id=self._binding_id)
        raw = self._connector.tool_provider.list_tools_proto(
            request.SerializeToString()
        )
        response = tool_contract.ListToolsResponse()
        response.ParseFromString(raw)
        if response.WhichOneof("result") != "catalog":
            error = response.error
            raise ConnectorError(
                "UNAVAILABLE", error.message or "WeCom tool catalog is unavailable"
            )
        tools: list[dict[str, Any]] = []
        for descriptor in response.catalog.tools:
            if descriptor.binding_id != self._binding_id:
                continue
            try:
                input_schema = json.loads(descriptor.input_schema_json or "{}")
            except json.JSONDecodeError as exc:
                raise ConnectorError(
                    "UNAVAILABLE", "WeCom tool catalog has invalid input schema"
                ) from exc
            tools.append(
                {
                    "id": descriptor.provider_tool_id,
                    "name": descriptor.display_name,
                    "description": descriptor.description,
                    "inputSchema": input_schema,
                    "readOnly": descriptor.provider_tool_id not in WRITE_TOOL_IDS,
                }
            )
        return {"tools": tools}

    def call_tool(
        self, tool_id: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Call one catalog tool via the canonical protobuf provider operations."""
        if not isinstance(tool_id, str) or not tool_id:
            raise ConnectorError("INVALID_REQUEST", "tool_id must be non-empty text")
        if not isinstance(arguments, Mapping):
            raise ConnectorError("INVALID_REQUEST", "tool arguments must be an object")
        try:
            arguments_json = json.dumps(
                dict(arguments), ensure_ascii=False, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ConnectorError(
                "INVALID_REQUEST", "tool arguments are not JSON"
            ) from exc
        request = tool_contract.CallToolRequest(
            binding_id=self._binding_id,
            provider_tool_id=tool_id,
            arguments_json=arguments_json,
        )
        raw = self._connector.tool_provider.call_tool_proto(
            request.SerializeToString()
        )
        response = tool_contract.CallToolResponse()
        response.ParseFromString(raw)
        if response.WhichOneof("result") == "error":
            error = response.error
            return {
                "content": [{"type": "text", "text": error.message}],
                "isError": True,
                "structuredContent": {
                    "code": tool_contract.ToolProviderErrorCode.Name(error.code),
                    "message": error.message,
                    "retryable": error.retryable,
                },
            }
        outcome = response.outcome
        content: list[dict[str, str]] = []
        structured_content: Any | None = None
        for part in outcome.content:
            kind = part.WhichOneof("content")
            if kind == "json":
                text = part.json.json
                content.append({"type": "text", "text": text})
                try:
                    structured_content = json.loads(text)
                except json.JSONDecodeError:
                    structured_content = None
            elif kind == "text":
                content.append({"type": "text", "text": part.text.text})
        result: dict[str, Any] = {"content": content, "isError": outcome.is_error}
        if structured_content is not None:
            result["structuredContent"] = structured_content
        return result

    def start(self) -> None:
        """Start an explicitly configured bot/Navigator background connection."""
        self._connector.start_background_services()

    def close(self) -> None:
        """Stop background services owned by this same-host bridge."""
        self._connector.close()


def create_navigator_wecom_bridge(
    config: WeComInstanceConfig | Mapping[str, Any] | None = None,
    *,
    connector: WeComConnector | None = None,
) -> NavigatorWeComBridge:
    """Create an optional local Workbridge using the configured connector.

    中文:使用已配置 connector 创建可选的本地 Workbridge。
    """
    if connector is None:
        if config is None:
            raise ConnectorError(
                "INVALID_REQUEST", "WeCom bridge requires connector config"
            )
        resolved = (
            config
            if isinstance(config, WeComInstanceConfig)
            else WeComInstanceConfig.from_mapping(config)
        )
        connector = WeComConnector(resolved)
    return NavigatorWeComBridge(connector)
