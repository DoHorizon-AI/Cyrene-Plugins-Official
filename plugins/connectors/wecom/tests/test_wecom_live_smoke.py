"""Live smoke test executing real wecom-cli commands if installed on the host.

中文:如果宿主机已安装 wecom-cli 则执行真实命令的连通性烟测。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from wecom_connector import (
    WeComCliClient,
    WeComToolProvider,
)
from wecom_connector._generated import tool_provider_pb2 as tool_contract


@pytest.fixture
def cli_available() -> bool:
    return bool(shutil.which("wecom-cli") or shutil.which("wecom"))


@pytest.fixture
def live_cli(cli_available: bool) -> WeComCliClient:
    if not cli_available:
        pytest.skip("wecom-cli is not installed on this host")
    return WeComCliClient(config_dir=os.environ.get("WECOM_LIVE_CLI_CONFIG_DIR"))


def test_real_wecom_cli_live_smoke(live_cli: WeComCliClient) -> None:
    cli = live_cli
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


def test_real_wecom_cli_tool_provider_smoke(live_cli: WeComCliClient) -> None:
    # Validate the CLI capability without fabricating a bot login.
    # 中文:只验证 CLI 能力,不使用虚构机器人凭据代替真实登录。
    provider = WeComToolProvider(
        binding_id="wecom.live.test", cli_client=live_cli, cli_enabled=True
    )
    tools_res = provider.list_tools()
    assert len(tools_res["catalog"]["tools"]) >= 8
    request = tool_contract.CallToolRequest(
        binding_id="wecom.live.test",
        provider_tool_id="wecom_auth_status",
        arguments_json="{}",
    )
    result = tool_contract.CallToolResponse.FromString(
        provider.call_tool_proto(request.SerializeToString())
    )
    assert result.outcome.is_error is False
    content = json.loads(result.outcome.content[0].json.json)
    assert content["auth_status"] in {"authorized", "unauthorized"}


def test_real_wecom_cli_empty_profile_does_not_use_host_credentials(
    live_cli: WeComCliClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_path = live_cli.get_status().executable_path
    monkeypatch.setenv("WECOM_CLI_ACCESS_TOKEN", "not-a-real-profile-token")
    client = WeComCliClient(
        executable_path=executable_path,
        config_dir=tmp_path / "empty-profile",
    )
    assert client.get_auth_status() == "unauthorized"
