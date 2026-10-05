"""WeCom CLI wrapper and tool.provider.v1 integration tests.

中文:WeCom CLI 封装器与 tool.provider.v1 集成测试。"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from wecom_connector import (
    CALL_TOOL_METHOD,
    LIST_TOOLS_METHOD,
    TOOL_PROVIDER_CAPABILITY_ID,
    ConnectorError,
    WeComCliClient,
    WeComCliError,
    WeComConnector,
    WeComInstanceConfig,
    WeComToolProvider,
    create_navigator_wecom_bridge,
)
from wecom_connector._generated import tool_provider_pb2 as tool_contract


def test_wecom_cli_version_parsing() -> None:
    client = WeComCliClient()
    with patch.object(
        client,
        "_run_raw",
        return_value=subprocess.CompletedProcess(
            args=["wecom-cli", "--version"],
            returncode=0,
            stdout="wecom-cli 1.3.4 (wecom 2026-09-23T11:45:45Z f9b2815)\n",
            stderr="",
        ),
    ):
        version = client.get_version()
        assert version == "1.3.4"


def test_wecom_cli_auth_status_authorized() -> None:
    client = WeComCliClient()
    with patch.object(
        client,
        "_run_raw",
        return_value=subprocess.CompletedProcess(
            args=["wecom-cli", "auth", "show", "--status"],
            returncode=0,
            stdout="authorized\n",
            stderr="",
        ),
    ):
        status = client.get_auth_status()
        assert status == "authorized"


def test_wecom_cli_execute_json_output() -> None:
    client = WeComCliClient()
    fake_json = {"items": [{"title": "Review PR"}], "total": 1}
    with patch.object(
        client,
        "_run_raw",
        return_value=subprocess.CompletedProcess(
            args=["wecom-cli", "todo", "list"],
            returncode=0,
            stdout=json.dumps(fake_json),
            stderr="",
        ),
    ):
        res = client.execute("todo", "list")
        assert res == fake_json


def test_wecom_cli_execute_error_handling() -> None:
    client = WeComCliClient()
    with patch.object(
        subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            args=["wecom-cli", "calendar", "invalid"],
            returncode=1,
            stdout="",
            stderr="Unknown subcommand 'invalid'",
        ),
    ):
        with pytest.raises(WeComCliError, match="Unknown subcommand 'invalid'") as exc:
            client.execute("calendar", "invalid")
        assert exc.value.returncode == 1


def test_tool_provider_list_tools() -> None:
    provider = WeComToolProvider(binding_id="wecom.binding")
    res = provider.list_tools()
    assert "catalog" in res
    catalog = res["catalog"]
    assert catalog["catalog_version"] == "v1.4.0"
    tool_ids = [t["provider_tool_id"] for t in catalog["tools"]]
    assert "wecom_auth_status" in tool_ids
    assert "wecom_contact_search" in tool_ids
    assert "wecom_calendar_list" in tool_ids
    assert "wecom_todo_list" in tool_ids
    assert "wecom_todo_create" in tool_ids
    assert "wecom_todo_finish" in tool_ids
    assert "wecom_doc_search" in tool_ids
    assert "wecom_operation_catalog" in tool_ids
    assert "wecom_connection_health" in tool_ids
    assert "wecom_cli_exec" not in tool_ids


def test_tool_provider_call_auth_status() -> None:
    cli = WeComCliClient()
    with (
        patch.object(
            cli,
            "_run_raw",
            return_value=subprocess.CompletedProcess(
                args=["wecom-cli", "auth", "show", "--status"],
                returncode=0,
                stdout="authorized\n",
                stderr="",
            ),
        ),
        patch.object(
            cli,
            "get_version",
            return_value="1.3.4",
        ),
    ):
        provider = WeComToolProvider(binding_id="wecom.binding", cli_client=cli)
        call_res = provider.call_tool(
            {
                "provider_tool_id": "wecom_auth_status",
                "arguments_json": "{}",
            }
        )
        assert call_res["is_error"] is False
        content = json.loads(call_res["content_json"])
        assert content["auth_status"] == "authorized"
        assert content["version"] == "1.3.4"


def test_tool_provider_call_todo_list() -> None:
    cli = WeComCliClient()
    sample_todos = {"todos": [{"id": "td-1", "title": "Deploy model"}]}
    with patch.object(
        cli,
        "execute",
        return_value=sample_todos,
    ) as mock_exec:
        provider = WeComToolProvider(binding_id="wecom.binding", cli_client=cli)
        call_res = provider.call_tool(
            {
                "provider_tool_id": "wecom_todo_list",
                "arguments_json": "{}",
            }
        )
        assert call_res["is_error"] is False
        mock_exec.assert_called_once_with("todo", "list", [])
        content = json.loads(call_res["content_json"])
        assert content == sample_todos


def test_tool_provider_write_requires_binding_and_call_approval() -> None:
    cli = WeComCliClient()
    provider = WeComToolProvider(
        binding_id="wecom.binding", cli_client=cli, cli_enabled=True
    )
    with patch.object(cli, "execute") as execute:
        denied = provider.call_tool(
            {
                "provider_tool_id": "wecom_todo_create",
                "arguments_json": json.dumps(
                    {"title": "Draft", "write_approval": "todo.create"}
                ),
            }
        )
        assert denied["is_error"] is True
        assert "operator approval is required" in denied["content_json"]
        execute.assert_not_called()

        approved = WeComToolProvider(
            binding_id="wecom.binding",
            cli_client=cli,
            cli_enabled=True,
            approved_write_operations=("todo.create",),
        )
        missing_invocation_approval = approved.call_tool(
            {
                "provider_tool_id": "wecom_todo_create",
                "arguments_json": json.dumps({"title": "Draft"}),
            }
        )
        assert missing_invocation_approval["is_error"] is True
        assert (
            "write_approval must exactly match"
            in missing_invocation_approval["content_json"]
        )
        execute.assert_not_called()

        with patch.object(cli, "execute", return_value={"id": "todo-1"}) as execute:
            accepted = approved.call_tool(
                {
                    "provider_tool_id": "wecom_todo_create",
                    "arguments_json": json.dumps(
                        {"title": "Draft", "write_approval": "todo.create"}
                    ),
                }
            )
            assert accepted["is_error"] is False
            execute.assert_called_once_with("todo", "create", ["--title", "Draft"])


def test_tool_provider_has_no_arbitrary_cli_executor() -> None:
    cli = WeComCliClient()
    provider = WeComToolProvider(
        binding_id="wecom.binding", cli_client=cli, cli_enabled=True
    )
    with patch.object(cli, "execute") as execute:
        result = provider.call_tool(
            {
                "provider_tool_id": "wecom_cli_exec",
                "arguments_json": json.dumps(
                    {"service": "shell", "subcommand": "-c", "args": ["whoami"]}
                ),
            }
        )
    assert result["is_error"] is True
    execute.assert_not_called()


def test_human_cli_requires_an_isolated_config_dir() -> None:
    with pytest.raises(ConnectorError, match="cli.config_dir is required"):
        WeComInstanceConfig.from_mapping(
            {
                "binding_id": "wecom.cli",
                "mode": "bot",
                "bot_id": "bot-1",
                "bot_secret": "secret-1",
                "cli": {"enabled": True},
            }
        )


def test_connection_health_keeps_app_bot_and_human_cli_identities_separate() -> None:
    config = WeComInstanceConfig(
        binding_id="wecom.identity-health",
        mode="hybrid",
        corp_id="ww-corp",
        corp_secret="application-secret",
        agent_id=42,
        bot_id="bot-7",
        bot_secret="bot-secret",
        cli_enabled=True,
        cli_config_dir="/tmp/wecom-human-identity",
    )
    connector = WeComConnector(config)
    with patch.object(
        connector.cli_client,
        "get_status",
        return_value=MagicMock(
            available=True,
            version="1.3.4",
            auth_status="unauthorized",
            executable_path="wecom-cli",
        ),
    ):
        health = connector.connection_health()
    assert health["application"] == {
        "configured": True,
        "account_id": "agent:42",
        "status": "configured",
    }
    assert health["bot"]["configured"] is True
    assert health["bot"]["account_id"] == "bot:bot-7"
    assert health["bot"]["status"] == "disconnected"
    assert health["human_cli"]["enabled"] is True
    assert health["human_cli"]["config_dir_configured"] is True
    assert health["human_cli"]["auth_status"] == "unauthorized"
    assert "application-secret" not in json.dumps(health)
    assert "bot-secret" not in json.dumps(health)
    connector.close()


def test_app_and_bot_secrets_resolve_from_distinct_environment_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WECOM_CORP_SECRET", "application-secret")
    monkeypatch.setenv("WECOM_BOT_SECRET", "robot-secret")
    config = WeComInstanceConfig.from_mapping(
        {
            "binding_id": "wecom.hybrid",
            "mode": "hybrid",
            "corp_id": "ww-corp",
            "agent_id": 42,
            "bot_id": "bot-1",
        }
    )
    assert config.corp_secret == "application-secret"
    assert config.bot_secret == "robot-secret"
    assert config.corp_secret != config.bot_secret


def test_tool_provider_proto_roundtrip() -> None:
    cli = WeComCliClient()
    with patch.object(
        cli,
        "get_status",
        return_value=MagicMock(
            available=True,
            version="1.3.4",
            auth_status="authorized",
            executable_path="/usr/bin/wecom-cli",
        ),
    ):
        provider = WeComToolProvider(binding_id="wecom.test", cli_client=cli)

        # 1. list_tools_proto
        req_proto = tool_contract.ListToolsRequest(binding_id="wecom.test")
        resp_bytes = provider.list_tools_proto(req_proto.SerializeToString())
        resp_proto = tool_contract.ListToolsResponse()
        resp_proto.ParseFromString(resp_bytes)
        assert resp_proto.catalog.catalog_version == "v1.4.0"
        assert len(resp_proto.catalog.tools) >= 8

        # 2. call_tool_proto
        call_req_proto = tool_contract.CallToolRequest(
            binding_id="wecom.test",
            provider_tool_id="wecom_auth_status",
            arguments_json="{}",
        )
        call_resp_bytes = provider.call_tool_proto(call_req_proto.SerializeToString())
        call_resp_proto = tool_contract.CallToolResponse()
        call_resp_proto.ParseFromString(call_resp_bytes)
        assert call_resp_proto.outcome.is_error is False
        parsed = json.loads(call_resp_proto.outcome.content[0].json.json)
        assert parsed["auth_status"] == "authorized"


def test_connector_on_invoke_tool_provider() -> None:
    config = WeComInstanceConfig(
        binding_id="wecom.tool.test",
        mode="bot",
        bot_id="b1",
        bot_secret="s1",
        cli_enabled=True,
        cli_config_dir="/tmp/wecom-test-profile",
    )
    connector = WeComConnector(config)
    with patch.object(
        connector.cli_client,
        "get_status",
        return_value=MagicMock(
            available=True,
            version="1.3.4",
            auth_status="authorized",
            executable_path="wecom-cli",
        ),
    ):
        # on_invoke list_tools
        req_proto = tool_contract.ListToolsRequest(binding_id="wecom.tool.test")
        ok, typed = connector.on_invoke(
            capability=TOOL_PROVIDER_CAPABILITY_ID,
            action=LIST_TOOLS_METHOD,
            payload=req_proto.SerializeToString(),
        )
        assert ok is True
        resp_proto = tool_contract.ListToolsResponse()
        resp_proto.ParseFromString(typed.value)
        assert len(resp_proto.catalog.tools) >= 8

        # on_invoke call_tool
        call_req = tool_contract.CallToolRequest(
            binding_id="wecom.tool.test",
            provider_tool_id="wecom_auth_status",
            arguments_json="{}",
        )
        ok, typed = connector.on_invoke(
            capability=TOOL_PROVIDER_CAPABILITY_ID,
            action=CALL_TOOL_METHOD,
            payload=call_req.SerializeToString(),
        )
        assert ok is True
        call_resp = tool_contract.CallToolResponse()
        call_resp.ParseFromString(typed.value)
        assert call_resp.outcome.is_error is False


def test_navigator_workbridge_uses_canonical_tool_provider_operations() -> None:
    config = WeComInstanceConfig(
        binding_id="wecom.workbridge",
        mode="bot",
        bot_id="bot-1",
        bot_secret="secret-1",
        cli_enabled=True,
        cli_config_dir="/tmp/wecom-workbridge-human-profile",
    )
    connector = WeComConnector(config)
    with patch.object(
        connector.cli_client,
        "get_status",
        return_value=MagicMock(
            available=True,
            version="1.3.4",
            auth_status="authorized",
            executable_path="wecom-cli",
        ),
    ), patch.object(
        connector.cli_client,
        "execute",
        return_value={"users": [{"userid": "member-1"}]},
    ):
        bridge = create_navigator_wecom_bridge(connector=connector)
        catalog = bridge.list_tools()
        tools = {tool["id"]: tool for tool in catalog["tools"]}
        assert tools["wecom_contact_search"]["readOnly"] is True
        assert tools["wecom_todo_create"]["readOnly"] is False
        assert tools["wecom_contact_search"]["inputSchema"]["required"] == [
            "keywords"
        ]

        result = bridge.call_tool("wecom_contact_search", {"keywords": "Alex"})
        assert result["isError"] is False
        assert result["structuredContent"] == {
            "users": [{"userid": "member-1"}]
        }
        denied = bridge.call_tool("wecom_todo_create", {"title": "Follow up"})
        assert denied["isError"] is True
        assert "operator approval" in denied["structuredContent"]["error"]
        with pytest.raises(ConnectorError) as unsupported:
            bridge.request_qr({})
        assert unsupported.value.code == "UNSUPPORTED"
        with pytest.raises(ConnectorError) as unsupported:
            bridge.poll_login({})
        assert unsupported.value.code == "UNSUPPORTED"
        assert bridge.health()["human_cli"]["auth_status"] == "authorized"
        connector.cli_client.execute.assert_called_once_with(
            "contact", "users", ["search", "--keywords", "Alex"]
        )
        bridge.close()
