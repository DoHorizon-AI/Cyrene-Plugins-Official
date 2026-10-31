"""
┌────────────────────────────────────────────────────────────────────┐
│  Package manifest and tool catalog contract tests                    │
│  验证包清单、激活配置与工具 schema 为闭合结构                           │
└────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from microsoft_graph_provider._generated import tool_provider_pb2 as wire
from microsoft_graph_provider.plugin import MicrosoftGraphProvider
from microsoft_graph_provider.settings import (
    PROVIDER_CALLER_ID,
    ConfigurationError,
    ProviderSettings,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _valid_activation() -> dict[str, object]:
    return {
        "exchange_caller_id": "exchange-call-tool",
        "exchange_verification_key_ref": "microsoft-graph/exchange-verification-key",
        "account_center": {
            "binding_id": "host-account-center-binding",
            "connection_ref": "grpc://127.0.0.1:9000",
            "transport": "local",
            "caller_id": PROVIDER_CALLER_ID,
            "signing_key_ref": "microsoft-graph/account-center-provider-signing-key",
        },
        "hostSecretRefs": {
            "microsoft-graph/exchange-verification-key": "GRAPH_EXCHANGE_KEY",
            "microsoft-graph/account-center-provider-signing-key": "GRAPH_AC_KEY",
        },
    }


def _office_v2_scope(mail: dict[str, object]) -> dict[str, object]:
    """Return one synthetic closed Office v2 scope with caller-selected Mail flags."""

    return {
        "schema_version": 2,
        "tenant_id": "11111111-1111-4111-8111-111111111111",
        "workspace_id": "22222222-2222-4222-8222-222222222222",
        "planner": {"plan_ids": [], "tasks_read": False, "tasks_write": False},
        "sharepoint": {
            "enabled": False,
            "site_id": None,
            "list_ids": [],
            "drive_ids": [],
            "metadata_read": False,
            "content_read": False,
        },
        "todo": {
            "enabled": False,
            "task_list_ids": [],
            "tasks_read": False,
            "tasks_write": False,
        },
        "calendar": {
            "enabled": False,
            "calendar_ids": [],
            "events_read": False,
            "events_write": False,
            "freebusy_read": False,
            "freebusy_subjects": [],
            "attendees_write": False,
        },
        "mail": mail,
    }


def test_manifest_and_activation_schema_are_valid_closed_package_metadata() -> None:
    manifest = json.loads((PACKAGE_ROOT / "plugin.manifest.json").read_text())
    schema = json.loads((PACKAGE_ROOT / "configuration.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(_valid_activation())

    assert manifest["id"] == "cyrene.providers.microsoft-graph"
    assert manifest["licenseExpression"] == "Apache-2.0"
    assert manifest["runtime"]["entrypoint"] == (
        "microsoft_graph_provider.plugin:MicrosoftGraphProvider"
    )
    assert manifest["capabilities"] == ["tool.provider.v1"]


def test_activation_schema_rejects_token_fields_and_unreviewed_provider_scope() -> None:
    schema = json.loads((PACKAGE_ROOT / "configuration.schema.json").read_text())
    invalid = _valid_activation()
    invalid["access_token"] = "synthetic-token"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(invalid)


def test_office_v2_scope_schema_allows_send_only_and_rejects_body_or_unselected_folders() -> None:
    schema = json.loads(
        (PACKAGE_ROOT / "contracts/office-v2-target-scope.schema.json").read_text()
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    send_only = _office_v2_scope(
        {
            "enabled": True,
            "folder_ids": [],
            "metadata_read": False,
            "body_read": False,
            "drafts_write": False,
            "send_enabled": True,
        }
    )
    validator.validate(send_only)
    metadata = _office_v2_scope(
        {
            "enabled": True,
            "folder_ids": ["AAMkFolder_opaque=="],
            "metadata_read": True,
            "body_read": False,
            "drafts_write": False,
            "send_enabled": False,
        }
    )
    validator.validate(metadata)
    invalid_body = json.loads(json.dumps(metadata))
    invalid_body["mail"]["body_read"] = True
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid_body)
    invalid_folder = json.loads(json.dumps(send_only))
    invalid_folder["mail"]["folder_ids"] = ["AAMkFolder_opaque=="]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid_folder)


def test_host_sharepoint_allowlist_is_exact_optional_and_resolved_without_credentials() -> None:
    schema = json.loads((PACKAGE_ROOT / "configuration.schema.json").read_text())
    activation = _valid_activation()
    site_id = (
        "tenant.sharepoint.com,11111111-1111-4111-8111-111111111111,"
        "22222222-2222-4222-8222-222222222222"
    )
    activation["sharepointTargets"] = {
        "siteId": site_id,
        "listIds": ["33333333-3333-4333-8333-333333333333"],
    }
    jsonschema.Draft202012Validator(schema).validate(activation)
    settings = ProviderSettings.from_environment(
        {
            "CYRENE_CAPABILITY_BINDING_ID": "graph-binding",
            "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(activation),
            "GRAPH_EXCHANGE_KEY": "65" * 32,
            "GRAPH_AC_KEY": "70" * 32,
        }
    )

    assert settings.sharepoint_site_id == site_id
    assert settings.sharepoint_list_ids == ("33333333-3333-4333-8333-333333333333",)

    invalid = dict(activation)
    invalid["sharepointTargets"] = {"siteId": "https://tenant.example", "listIds": []}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(invalid)
    with pytest.raises(ConfigurationError, match="sharepointTargets"):
        ProviderSettings.from_environment(
            {
                "CYRENE_CAPABILITY_BINDING_ID": "graph-binding",
                "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(invalid),
                "GRAPH_EXCHANGE_KEY": "65" * 32,
                "GRAPH_AC_KEY": "70" * 32,
            }
        )


def test_mail_feature_flags_are_opt_in_independent_and_default_closed() -> None:
    schema = json.loads((PACKAGE_ROOT / "configuration.schema.json").read_text())
    default_settings = ProviderSettings.from_environment(
        {
            "CYRENE_CAPABILITY_BINDING_ID": "graph-binding",
            "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(_valid_activation()),
            "GRAPH_EXCHANGE_KEY": "65" * 32,
            "GRAPH_AC_KEY": "70" * 32,
        }
    )
    assert default_settings.office_v2_flags == {
        "calendar_enabled": False,
        "mail_drafts_enabled": False,
        "mail_metadata_enabled": False,
        "mail_send_enabled": False,
        "planner_enabled": False,
        "sharepoint_drive_metadata_enabled": False,
        "todo_enabled": False,
    }
    flags = {
        "planner_enabled": False,
        "todo_enabled": False,
        "calendar_enabled": False,
        "mail_metadata_enabled": False,
        "mail_drafts_enabled": False,
        "mail_send_enabled": True,
        "sharepoint_drive_metadata_enabled": False,
    }
    activation = {**_valid_activation(), "officeV2": flags}
    jsonschema.Draft202012Validator(schema).validate(activation)
    settings = ProviderSettings.from_environment(
        {
            "CYRENE_CAPABILITY_BINDING_ID": "graph-binding",
            "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(activation),
            "GRAPH_EXCHANGE_KEY": "65" * 32,
            "GRAPH_AC_KEY": "70" * 32,
        }
    )
    assert settings.office_v2_flags == flags
    with pytest.raises(ConfigurationError, match="officeV2 must contain"):
        ProviderSettings.from_environment(
            {
                "CYRENE_CAPABILITY_BINDING_ID": "graph-binding",
                "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(
                    {**activation, "officeV2": {**flags, "mail_body_read_enabled": True}}
                ),
                "GRAPH_EXCHANGE_KEY": "65" * 32,
                "GRAPH_AC_KEY": "70" * 32,
            }
        )


def test_tool_catalog_has_closed_fixed_argument_schemas_without_urls_or_tokens() -> None:
    settings = ProviderSettings(
        binding_id="graph-binding",
        exchange_caller_id="exchange-call-tool",
        exchange_verification_key=b"e" * 32,
        account_center_binding_id="account-center-binding",
        account_center_connection_ref="grpc://127.0.0.1:9000",
        account_center_transport="local",
        provider_caller_id=PROVIDER_CALLER_ID,
        provider_signing_key=b"p" * 32,
        account_center_root_cert_ref=None,
        account_center_client_cert_ref=None,
        account_center_client_key_ref=None,
        host_secret_names={},
        read_only=False,
        timeout_seconds=5,
        max_page_size=25,
    )
    provider = MicrosoftGraphProvider(settings=settings)
    request = wire.ListToolsRequest(binding_id=settings.binding_id)
    ok, payload = provider._list_tools(request.SerializeToString())
    assert ok is True
    response = wire.ListToolsResponse.FromString(payload.value)

    for descriptor in response.catalog.tools:
        input_schema = json.loads(descriptor.input_schema_json)
        jsonschema.Draft202012Validator.check_schema(input_schema)
        assert input_schema["additionalProperties"] is False
        assert "url" not in input_schema["properties"]
        assert "access_token" not in input_schema["properties"]
        assert "authorization" not in input_schema["properties"]
    tool_ids = {tool.provider_tool_id for tool in response.catalog.tools}
    assert len(tool_ids) == 8
    assert "microsoft_graph.planner.plans.list" not in tool_ids
    assert "microsoft_graph.todo.lists.list" not in tool_ids
    assert "microsoft_graph.calendar.calendars.list" not in tool_ids
    assert "microsoft_graph.sharepoint.drives.list" not in tool_ids
