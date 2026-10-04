"""Live smoke test executing real wecom-cli commands if installed on the host.

中文:如果宿主机已安装 wecom-cli 则执行真实命令的连通性烟测。"""

from __future__ import annotations

import shutil

import pytest

from wecom_connector import (
    WeComCliClient,
    WeComConnector,
    WeComInstanceConfig,
)


@pytest.fixture
def cli_available() -> bool:
    return bool(shutil.which("wecom-cli") or shutil.which("wecom"))


def test_real_wecom_cli_live_smoke(cli_available: bool) -> None:
    if not cli_available:
        pytest.skip("wecom-cli is not installed on this host")

    cli = WeComCliClient()
    status = cli.get_status()
    assert status.available is True
    assert status.version != ""
    assert status.auth_status in {"authorized", "unauthorized"}

    # Query schema of standard services
    contact_schema = cli.get_service_schema("contact")
    assert contact_schema.get("name") == "contact"
    assert "methods" in contact_schema

    todo_schema = cli.get_service_schema("todo")
    assert todo_schema.get("name") == "todo"

    calendar_schema = cli.get_service_schema("calendar")
    assert calendar_schema.get("name") == "calendar"


def test_real_wecom_connector_tool_provider_smoke(cli_available: bool) -> None:
    if not cli_available:
        pytest.skip("wecom-cli is not installed on this host")

    config = WeComInstanceConfig(
        binding_id="wecom.live.test",
        mode="bot",
        bot_id="b-live",
        bot_secret="s-live",
        cli_enabled=True,
    )
    connector = WeComConnector(config)

    # 1. List tools
    tools_res = connector.tool_provider.list_tools()
    assert len(tools_res["catalog"]["tools"]) >= 8

    # 2. Invoke auth status check tool
    auth_call = connector.tool_provider.call_tool(
        {"provider_tool_id": "wecom_auth_status", "arguments_json": "{}"}
    )
    assert auth_call["is_error"] is False
    assert "auth_status" in auth_call["content_json"]
