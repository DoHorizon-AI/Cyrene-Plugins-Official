"""The connector operation manifest and the checks that keep it equal to its sources.

中文：操作清单及其与来源仓库保持一致的校验。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from tooling.connector_operations import sources
from tooling.connector_operations.sources import (
    SourceError,
    SourceTexts,
    build_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / sources.MANIFEST_PATH
SCHEMA = MANIFEST.with_name("connector-operations.schema.json")
PINS = {"exchange": "a" * 40, "navigator": "b" * 40, "platform": "c" * 40}

GRANT = """
pub const NAVIGATOR_CONNECTOR_OPERATOR_ROLE: &str = "operator.role";
pub const NAVIGATOR_AUTOMATION_VIEWER_ROLE: &str = "viewer.role";
pub const NAVIGATOR_OFFICE_TASK_WRITER_ROLE: &str = "writer.role";
pub const SUPPORTED_CONNECTOR_OPERATION_IDS: [&str; 5] = [
    "cloud.resource.get",
    "cloud.resource.restart",
    "github.branch.create",
    "github.issue.get",
    "msteams.message.reply",
];
pub const AUTOMATION_VIEWER_CONNECTOR_OPERATION_IDS: [&str; 1] = ["github.issue.get"];
pub const OFFICE_TASK_WRITER_CONNECTOR_OPERATION_IDS: [&str; 2] = [
    "github.issue.get",
    "github.branch.create",
];
pub const AUTOMATION_TRANSPORT_CONNECTOR_OPERATION_IDS: [&str; 1] = ["msteams.message.reply"];
pub fn is_supported_connector_operation(provider_id: &str, operation_id: &str) -> bool {
    match operation_id.split_once('.') {
        Some(("github", _)) => provider_id == "github",
        Some(("msteams", _)) => provider_id == "msteams",
        Some(("cloud", _)) if operation_id == "cloud.resource.restart" => provider_id == "runpod",
        Some(("cloud", _)) => matches!(provider_id, "azure" | "runpod"),
        _ => false,
    }
}
/// Return whether the exact provider/operation pair is in the viewer's read-only catalog.
"""
POLICY = {
    "operations": [
        {"operationId": "cloud.resource.get", "providerIds": ["azure", "runpod"]},
        {"operationId": "cloud.resource.restart", "providerIds": ["runpod"]},
        {"operationId": "github.branch.create", "providerIds": ["github"]},
        {"operationId": "github.issue.get", "providerIds": ["github"]},
        {"operationId": "msteams.message.reply", "providerIds": ["msteams"]},
    ]
}
GRAPH = 'GRAPH_OPERATIONS = frozenset({"github.issue.get"})\n'
EXCHANGE = """
_TOOL_IDS_BY_PROVIDER: Mapping[str, Mapping[str, str]] = {
    "github": {"github.branch.create": "github.branch.create", "github.commits.compare": "compare"},
    "azure": {"cloud.resource.get": "cloud_resource_get"},
    "runpod": {"cloud.resource.get": "cloud_resource_get", "cloud.resource.restart": "cloud_restart"},
}
_MUTATING_OPERATIONS = frozenset({"github.branch.create", "cloud.resource.restart"}) | frozenset()
"""
SERVE = """
function connectorOperationPolicy(providerId: string, operationId: string) {
  const policies = {
    'github.branch.create': { toolId: 'github.branch.create', mutating: true, resourceProjection: 'github_branch' },
    'github.issue.get': {
      toolId: 'github.issue.get', mutating: false, resourceProjection: 'github_issue',
    },
  };
  const policy = policies[operationId];
}
"""
TOOL_PROVIDERS = """
  const profileIsOuterClaim = hostBinding.providerId === 'github'
    || hostBinding.providerId === 'azure';
function connectorScopeLock(providerId: string) {
  if (providerId !== 'github') return undefined;
  const branchField = operationId === 'github.branch.create' ? 'branch' : undefined;
}
"""


def texts(**changes: str) -> SourceTexts:
    base = SourceTexts(
        platform_grant=GRANT,
        platform_service=sources.ROLE_RULE,
        platform_policy=json.dumps(POLICY),
        exchange_modules=(GRAPH, EXCHANGE),
        navigator_serve=SERVE,
        navigator_tool_providers=TOOL_PROVIDERS,
    )
    return replace(base, **changes)


def rows(manifest: dict) -> dict:
    return {row["operationId"]: row for row in manifest["operations"]}


def test_build_records_each_source_and_leaves_unknown_facts_null() -> None:
    manifest = build_manifest(texts(), PINS)
    table = rows(manifest)
    assert table["github.branch.create"] == {
        "operationId": "github.branch.create",
        "providerIds": ["github"],
        "roles": ["officeTaskWriter", "operator"],
        "mutating": True,
        "exchangeToolIds": {"github": "github.branch.create"},
        "navigatorToolId": "github.branch.create",
        "resourceProjection": "github_branch",
        "scopeLockBranchField": "branch",
    }
    # No source says whether the Teams reply mutates, and the manifest does not guess.
    assert table["msteams.message.reply"]["mutating"] is None
    assert table["msteams.message.reply"]["roles"] == [
        "automationViewer",
        "officeTaskWriter",
        "operator",
    ]
    assert table["cloud.resource.get"]["exchangeToolIds"] == {
        "azure": "cloud_resource_get",
        "runpod": "cloud_resource_get",
    }
    assert table["cloud.resource.restart"]["providerIds"] == ["runpod"]
    assert [row["operationId"] for row in manifest["exchangeOnlyOperations"]] == [
        "github.commits.compare"
    ]
    assert {
        row["providerId"]: row["profileIsOuterClaim"] for row in manifest["providers"]
    } == {
        "azure": True,
        "github": True,
        "msteams": False,
        "runpod": False,
    }
    assert manifest["roles"] == {
        "automationViewer": "viewer.role",
        "officeTaskWriter": "writer.role",
        "operator": "operator.role",
    }


def test_exchange_table_may_derive_a_provider_from_a_named_set() -> None:
    constants = sources._Constants(
        (GRAPH, "TABLE = {op: op for op in sorted(GRAPH_OPERATIONS)}\n")
    )
    assert constants.get("TABLE") == {"github.issue.get": "github.issue.get"}


def policy_with(change) -> str:
    policy = json.loads(json.dumps(POLICY))
    change(policy["operations"])
    return json.dumps(policy)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        pytest.param(
            {"platform_policy": policy_with(lambda rows: rows.pop())},
            "different operations",
            id="policy-misses-an-operation",
        ),
        pytest.param(
            {
                "platform_policy": policy_with(
                    lambda rows: rows[0]["providerIds"].append("gcp")
                )
            },
            "disagree on the providers",
            id="policy-and-rule-disagree-on-providers",
        ),
        pytest.param(
            {
                "platform_grant": GRANT.replace(
                    '[&str; 1] = ["github.issue.get"]',
                    '[&str; 1] = ["github.issue.gone"]',
                )
            },
            "viewer list",
            id="role-list-names-unknown-operation",
        ),
        pytest.param(
            {"platform_grant": GRANT.replace("[&str; 5]", "[&str; 6]")},
            "declared size",
            id="array-size-wrong",
        ),
        pytest.param(
            {
                "platform_grant": GRANT.replace(
                    'provider_id == "github"', 'provider_id.starts_with("git")'
                )
            },
            "unrecognized result",
            id="rule-result-unknown",
        ),
        pytest.param(
            {
                "platform_grant": GRANT.replace(
                    'Some(("github", _)) =>',
                    'Some(("github", tail)) if tail.len() > 1 =>',
                )
            },
            "unrecognized arm",
            id="rule-arm-unknown",
        ),
        pytest.param(
            {
                "platform_grant": GRANT.replace(
                    'Some(("msteams", _)) => provider_id == "msteams",',
                    'Some(("msteams", _)) => {\n provider_id == "msteams"\n}',
                )
            },
            "cannot parse",
            id="rule-arm-multiline",
        ),
        pytest.param(
            {
                "platform_grant": GRANT.replace(
                    'Some(("msteams", _)) => provider_id == "msteams",\n', ""
                )
            },
            "does not cover",
            id="rule-misses-a-prefix",
        ),
        pytest.param(
            {"platform_service": "if roles.contains(ANYONE)"},
            "role rule changed",
            id="role-rule-changed",
        ),
        pytest.param(
            {"exchange_modules": (GRAPH, EXCHANGE.replace('"azure": {', '"gcp": {'))},
            "provider Platform does not allow",
            id="exchange-maps-disallowed-provider",
        ),
        pytest.param(
            {
                "exchange_modules": (
                    GRAPH,
                    EXCHANGE.replace(
                        '"github.branch.create": "github.branch.create"',
                        '"github.branch.create": "other"',
                    ),
                )
            },
            "disagree on the tool",
            id="tool-id-disagrees",
        ),
        pytest.param(
            {
                "exchange_modules": (
                    GRAPH,
                    EXCHANGE.replace(
                        'frozenset({"github.branch.create", "cloud.resource.restart"})',
                        'frozenset({"cloud.resource.restart"})',
                    ),
                )
            },
            "disagree on whether",
            id="mutating-disagrees",
        ),
        pytest.param(
            {
                "exchange_modules": (
                    GRAPH,
                    EXCHANGE.replace("| frozenset()", "| compute()"),
                )
            },
            "unsupported expression",
            id="exchange-constant-not-literal",
        ),
        pytest.param(
            {"exchange_modules": (GRAPH, "_MUTATING_OPERATIONS = frozenset()\n")},
            "_TOOL_IDS_BY_PROVIDER is missing",
            id="exchange-table-missing",
        ),
        pytest.param(
            {
                "navigator_serve": SERVE.replace(
                    "'github.issue.get': {", "'github.issue.other': {"
                )
            },
            "does not support",
            id="navigator-names-unknown-operation",
        ),
        pytest.param(
            {
                "navigator_serve": SERVE.replace(
                    "mutating: false, resourceProjection",
                    "mutating: isWrite, resourceProjection",
                )
            },
            "cannot parse",
            id="navigator-row-not-literal",
        ),
        pytest.param(
            {
                "navigator_serve": SERVE.replace(
                    "    'github.issue.get': {",
                    "    'github.branch.create': {\n toolId: 'github.branch.create', mutating: true, resourceProjection: 'x',\n },\n    'github.issue.get': {",
                )
            },
            "repeats",
            id="navigator-repeats-an-operation",
        ),
        pytest.param(
            {
                "navigator_tool_providers": TOOL_PROVIDERS.replace(
                    "|| hostBinding.providerId === 'azure'", "|| isTrusted(hostBinding)"
                )
            },
            "unrecognized term",
            id="outer-claim-term-unknown",
        ),
        pytest.param(
            {
                "navigator_tool_providers": TOOL_PROVIDERS.replace(
                    "|| hostBinding.providerId === 'azure'",
                    "|| hostBinding.providerId === 'gcp'",
                )
            },
            "unknown provider",
            id="outer-claim-unknown-provider",
        ),
        pytest.param(
            {
                "navigator_tool_providers": TOOL_PROVIDERS.replace(
                    "operationId === 'github.branch.create'",
                    "operationId === 'github.issue.get'",
                )
            },
            "not marked mutating",
            id="scope-lock-on-a-read",
        ),
        pytest.param(
            {
                "navigator_tool_providers": TOOL_PROVIDERS.replace(
                    "operationId === 'github.branch.create'",
                    "operationId === 'github.branch.gone'",
                )
            },
            "not marked mutating|does not support",
            id="scope-lock-on-unknown-operation",
        ),
        pytest.param(
            {
                "navigator_tool_providers": TOOL_PROVIDERS.replace(
                    "operationId === 'github.branch.create' ?",
                    "startsWith(operationId) ?",
                )
            },
            "cannot parse",
            id="scope-lock-branch-unknown",
        ),
        pytest.param(
            {
                "navigator_tool_providers": TOOL_PROVIDERS.replace(
                    "if (providerId !== 'github') return undefined;", ""
                )
            },
            "unrecognized shape",
            id="scope-lock-provider-guard-removed",
        ),
    ],
)
def test_disagreement_or_unreadable_source_is_refused(
    changes: dict, message: str
) -> None:
    with pytest.raises(SourceError, match=message):
        build_manifest(texts(**changes), PINS)


def committed() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_committed_manifest_matches_its_schema() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(committed())


def test_schema_refuses_an_operation_without_the_operator_role() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    manifest = committed()
    manifest["operations"][0]["roles"] = ["automationViewer"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(manifest)


def test_committed_manifest_is_complete_and_stable() -> None:
    manifest = committed()
    identifiers = [row["operationId"] for row in manifest["operations"]]
    assert len(identifiers) == 94
    assert identifiers == sorted(set(identifiers))
    known = {row["providerId"] for row in manifest["providers"]}
    assert {
        provider for row in manifest["operations"] for provider in row["providerIds"]
    } == known
    # Rendering is byte-stable, so the CI comparison means the content is equal.
    assert MANIFEST.read_text(encoding="utf-8") == sources.render(manifest)


def test_check_command_compares_with_the_real_sources(tmp_path: Path) -> None:
    """CI sets the checkouts; without them this test is reported as skipped, not as passed."""

    checkouts = os.environ.get("CYRENE_CONNECTOR_SOURCE_CHECKOUTS")
    if not checkouts:
        pytest.skip("CYRENE_CONNECTOR_SOURCE_CHECKOUTS is not set")
    base = Path(checkouts)
    command = [
        sys.executable,
        str(ROOT / "tooling/connector_operations/sources.py"),
        "check",
        "--platform",
        str(base / "platform"),
        "--exchange",
        str(base / "exchange"),
        "--navigator",
        str(base / "navigator"),
    ]
    good = subprocess.run(
        [*command, "--root", str(ROOT)], capture_output=True, text=True, check=False
    )
    assert good.returncode == 0, good.stderr

    # The same check must fail on a manifest with one changed fact.
    altered = committed()
    altered["operations"][0]["mutating"] = not altered["operations"][0]["mutating"]
    target = tmp_path / sources.MANIFEST_PATH
    target.parent.mkdir(parents=True)
    target.write_text(sources.render(altered), encoding="utf-8")
    bad = subprocess.run(
        [*command, "--root", str(tmp_path)], capture_output=True, text=True, check=False
    )
    assert bad.returncode == 1
    assert "differs from its sources" in bad.stderr
