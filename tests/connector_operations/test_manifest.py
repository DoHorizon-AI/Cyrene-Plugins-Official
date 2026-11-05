"""The rules that merge the source dumps, and the committed connector operation manifest.

The comparison with the real source repositories runs in the connector-operations CI job.
中文：合并来源数据的规则，以及已提交的清单。和真实来源仓库的比对在 CI 任务中运行。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import jsonschema
import pytest

from tooling.connector_operations import manifest as tool
from tooling.connector_operations.manifest import SourceError, build_manifest

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / tool.MANIFEST_PATH
SCHEMA = COMMITTED.with_name("connector-operations.schema.json")
PINS = {"exchange": "a" * 40, "navigator": "b" * 40, "platform": "c" * 40}
ROLES = {
    "operator": "operator.role",
    "automationViewer": "viewer.role",
    "officeTaskWriter": "writer.role",
}
PROVIDERS = ["azure", "github", "microsoft_graph", "msteams", "not_a_provider"]
GRAPH_PROJECTIONS = (
    "microsoft_graph.calendar.events.list",
    "microsoft_graph.todo.tasks.list",
)


def _pair(provider: str, operation: str) -> dict:
    """What Platform's real functions would report for one provider and operation."""

    allowed_providers = {
        "cloud.resource.get": {"azure"},
        "github.branch.create": {"github"},
        "github.issue.get": {"github"},
        "microsoft_graph.calendar.events.list": {"microsoft_graph"},
        "microsoft_graph.todo.tasks.list": {"microsoft_graph"},
        "msteams.message.reply": {"msteams"},
    }
    supported = provider in allowed_providers.get(operation, set())
    transport = supported and operation == "msteams.message.reply"
    viewer = supported and operation == "github.issue.get"
    writer = supported and operation in {"github.issue.get", "github.branch.create"}
    return {
        "provider": provider,
        "operation": operation,
        "supported": supported,
        "viewerCatalog": viewer,
        "writerCatalog": writer,
        "transportCatalog": transport,
        "allowed": {
            "operator": True,
            "automationViewer": viewer or transport,
            "officeTaskWriter": writer or transport,
            "noRole": False,
        },
    }


def _dumps() -> dict:
    supported = [
        "cloud.resource.get",
        "github.branch.create",
        "github.issue.get",
        *GRAPH_PROJECTIONS,
        "msteams.message.reply",
    ]
    operations = [*supported, "github.commits.compare"]
    pairs = [
        _pair(provider, operation) for provider in PROVIDERS for operation in operations
    ]
    row = lambda operation, providers: {
        "operationId": operation,
        "providerIds": providers,
    }
    policy = {
        "operations": [
            row("cloud.resource.get", ["azure"]),
            row("github.branch.create", ["github"]),
            row("github.issue.get", ["github"]),
            row(GRAPH_PROJECTIONS[0], ["microsoft_graph"]),
            row(GRAPH_PROJECTIONS[1], ["microsoft_graph"]),
            row("msteams.message.reply", ["msteams"]),
        ],
        "readOnlyOperations": [row("github.issue.get", ["github"])],
        "taskWriterOperations": [
            row("github.issue.get", ["github"]),
            row("github.branch.create", ["github"]),
        ],
        "automationTransportOperations": [row("msteams.message.reply", ["msteams"])],
    }
    tool_ids = {
        "azure": {"cloud.resource.get": "cloud_resource_get"},
        "github": {
            "github.branch.create": "github.branch.create",
            "github.issue.get": "github.issue.get",
            "github.commits.compare": "github.commits.compare",
        },
        "microsoft_graph": {
            GRAPH_PROJECTIONS[0]: GRAPH_PROJECTIONS[0],
            GRAPH_PROJECTIONS[1]: GRAPH_PROJECTIONS[1],
        },
    }
    binding = lambda operation, tool_id, projection, mutating: {
        "toolId": tool_id,
        "operationId": operation,
        "resourceProjection": projection,
        "mutating": mutating,
    }
    exchange = {
        "toolIds": tool_ids,
        "mutating": ["github.branch.create"],
        "bindingTools": {
            "azure": [
                binding(
                    "cloud.resource.get", "cloud_resource_get", "cloud_target", False
                )
            ],
            "github": [
                binding(
                    "github.branch.create",
                    "github.branch.create",
                    "github_branch",
                    True,
                ),
                binding("github.issue.get", "github.issue.get", "github_issue", False),
                binding(
                    "github.commits.compare",
                    "github.commits.compare",
                    "github_commit",
                    False,
                ),
            ],
            "microsoft_graph": [
                binding(
                    GRAPH_PROJECTIONS[0],
                    GRAPH_PROJECTIONS[0],
                    "microsoft_graph_event",
                    False,
                ),
                binding(
                    GRAPH_PROJECTIONS[1],
                    GRAPH_PROJECTIONS[1],
                    "microsoft_graph_todo_task",
                    False,
                ),
            ],
        },
    }
    navigator_table = {
        "cloud.resource.get": ("azure", "cloud_resource_get", False, "cloud_target"),
        "github.branch.create": (
            "github",
            "github.branch.create",
            True,
            "github_branch",
        ),
        "github.issue.get": ("github", "github.issue.get", False, "github_issue"),
        GRAPH_PROJECTIONS[0]: (
            "microsoft_graph",
            GRAPH_PROJECTIONS[0],
            False,
            "microsoft_graph_calendar",
        ),
        GRAPH_PROJECTIONS[1]: (
            "microsoft_graph",
            GRAPH_PROJECTIONS[1],
            False,
            "microsoft_graph_todo_list",
        ),
    }
    policies: dict = {provider: dict.fromkeys(operations) for provider in PROVIDERS}
    for operation, (provider, tool_id, mutating, projection) in navigator_table.items():
        policies[provider][operation] = {
            "toolId": tool_id,
            "mutating": mutating,
            "resourceProjection": projection,
        }
    navigator = {
        "policies": policies,
        "scopeLocks": {**dict.fromkeys(operations), "github.branch.create": "branch"},
        "scopeLocksOutsideGithub": [],
        "outerClaim": {
            "azure": False,
            "github": True,
            "microsoft_graph": True,
            "msteams": False,
            "not_a_provider": False,
        },
    }
    platform = {"supported": supported, "roles": ROLES, "pairs": pairs}
    return {
        "platform": platform,
        "policy": policy,
        "exchange": exchange,
        "navigator": navigator,
    }


def _build(dumps: dict) -> dict:
    return build_manifest(
        dumps["platform"], dumps["policy"], dumps["exchange"], dumps["navigator"], PINS
    )


def _changed_pair(dumps: dict, provider: str, operation: str) -> dict:
    return next(
        pair
        for pair in dumps["platform"]["pairs"]
        if pair["provider"] == provider and pair["operation"] == operation
    )


def test_merge_records_each_source() -> None:
    manifest = _build(_dumps())
    rows = {row["operationId"]: row for row in manifest["operations"]}
    assert rows["github.branch.create"] == {
        "operationId": "github.branch.create",
        "providerIds": ["github"],
        "roles": ["officeTaskWriter", "operator"],
        "automationTransport": False,
        "mutating": True,
        "exchangeToolIds": {"github": "github.branch.create"},
        "exchangeResourceProjections": {"github": "github_branch"},
        "navigatorToolId": "github.branch.create",
        "navigatorResourceProjection": "github_branch",
        "scopeLockBranchField": "branch",
    }
    reply = rows["msteams.message.reply"]
    assert reply["mutating"] is None
    assert reply["automationTransport"] is True
    assert reply["roles"] == ["automationViewer", "officeTaskWriter", "operator"]
    calendar = rows[GRAPH_PROJECTIONS[0]]
    assert calendar["exchangeResourceProjections"] == {
        "microsoft_graph": "microsoft_graph_event"
    }
    assert calendar["navigatorResourceProjection"] == "microsoft_graph_calendar"
    assert [row["operationId"] for row in manifest["exchangeOnlyOperations"]] == [
        "github.commits.compare"
    ]
    assert {
        row["providerId"]: row["profileIsOuterClaim"] for row in manifest["providers"]
    } == {
        "azure": False,
        "github": True,
        "microsoft_graph": True,
        "msteams": None,
    }


def _drop_viewer_catalog(dumps: dict) -> None:
    dumps["policy"]["readOnlyOperations"] = []


def _policy_extra_provider(dumps: dict) -> None:
    dumps["policy"]["operations"][1]["providerIds"].append("azure")


def _policy_missing_operation(dumps: dict) -> None:
    dumps["policy"]["operations"].pop()


def _policy_repeats_operation(dumps: dict) -> None:
    dumps["policy"]["operations"].append(dumps["policy"]["operations"][0])


def _grant_without_role(dumps: dict) -> None:
    _changed_pair(dumps, "github", "github.issue.get")["allowed"]["noRole"] = True


def _operator_denied(dumps: dict) -> None:
    _changed_pair(dumps, "github", "github.issue.get")["allowed"]["operator"] = False


def _exchange_disallowed_provider(dumps: dict) -> None:
    dumps["exchange"]["toolIds"]["azure"]["github.issue.get"] = "github.issue.get"


def _tool_disagrees(dumps: dict) -> None:
    dumps["exchange"]["toolIds"]["github"]["github.issue.get"] = "other"
    dumps["exchange"]["bindingTools"]["github"][1]["toolId"] = "other"


def _two_exchange_tables_disagree(dumps: dict) -> None:
    dumps["exchange"]["bindingTools"]["github"][1]["toolId"] = "other"


def _mutating_disagrees(dumps: dict) -> None:
    dumps["exchange"]["mutating"] = []
    dumps["exchange"]["bindingTools"]["github"][0]["mutating"] = False


def _launcher_mutating_disagrees(dumps: dict) -> None:
    dumps["exchange"]["bindingTools"]["github"][1]["mutating"] = True


def _new_projection_disagreement(dumps: dict) -> None:
    dumps["exchange"]["bindingTools"]["github"][1]["resourceProjection"] = "profile"


def _stale_projection_exception(dumps: dict) -> None:
    dumps["exchange"]["bindingTools"]["microsoft_graph"][0]["resourceProjection"] = (
        "microsoft_graph_calendar"
    )


def _new_unknown_mutating(dumps: dict) -> None:
    navigator = dumps["navigator"]["policies"]["azure"]
    dumps["exchange"]["toolIds"]["azure"].pop("cloud.resource.get")
    dumps["exchange"]["bindingTools"]["azure"] = []
    navigator["cloud.resource.get"] = None


def _stale_mutating_exception(dumps: dict) -> None:
    dumps["exchange"]["toolIds"]["msteams"] = {"msteams.message.reply": "reply"}
    dumps["exchange"]["bindingTools"]["msteams"] = [
        {
            "toolId": "reply",
            "operationId": "msteams.message.reply",
            "resourceProjection": "profile",
            "mutating": True,
        }
    ]
    dumps["exchange"]["mutating"].append("msteams.message.reply")


def _navigator_other_provider(dumps: dict) -> None:
    dumps["navigator"]["policies"]["azure"]["github.issue.get"] = dumps["navigator"][
        "policies"
    ]["github"]["github.issue.get"]


def _navigator_differs_by_provider(dumps: dict) -> None:
    row = dumps["navigator"]["policies"]["github"]["github.issue.get"]
    dumps["policy"]["operations"][3]["providerIds"] = ["microsoft_graph"]
    dumps["navigator"]["policies"]["github"]["github.issue.get"] = {
        **row,
        "mutating": True,
    }


def _lock_on_read(dumps: dict) -> None:
    dumps["navigator"]["scopeLocks"]["github.issue.get"] = "branch"


def _lock_outside_github(dumps: dict) -> None:
    dumps["navigator"]["scopeLocksOutsideGithub"] = [["azure", "cloud.resource.get"]]


def _exchange_only_is_grantable(dumps: dict) -> None:
    _changed_pair(dumps, "github", "github.commits.compare")["supported"] = True


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_drop_viewer_catalog, "readOnlyOperations and code disagree"),
        (_policy_extra_provider, "disagree on the providers"),
        (_policy_missing_operation, "different operations"),
        (_policy_repeats_operation, "repeats an operation"),
        (_grant_without_role, "with no role"),
        (_operator_denied, "denies .* to the operator"),
        (_exchange_disallowed_provider, "provider Platform does not allow"),
        (_tool_disagrees, "disagree on the tool"),
        (_two_exchange_tables_disagree, "two tables disagree"),
        (_mutating_disagrees, "disagree on whether"),
        (_launcher_mutating_disagrees, "launcher map disagrees"),
        (_new_projection_disagreement, "disagree on the projection"),
        (_stale_projection_exception, "Stale exception"),
        (_new_unknown_mutating, "No source states"),
        (_stale_mutating_exception, "Stale exception"),
        (_navigator_other_provider, "disagree on the providers"),
        (_navigator_differs_by_provider, "depends on the provider|disagree on whether"),
        (_lock_on_read, "not marked mutating"),
        (_lock_outside_github, "outside GitHub"),
        (_exchange_only_is_grantable, "does not list it"),
    ],
)
def test_disagreement_is_refused(change: Callable[[dict], None], message: str) -> None:
    dumps = copy.deepcopy(_dumps())
    change(dumps)
    with pytest.raises(SourceError, match=message):
        _build(dumps)


def _committed() -> dict:
    return json.loads(COMMITTED.read_text(encoding="utf-8"))


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def test_committed_manifest_matches_its_schema() -> None:
    _validator().validate(_committed())


def test_schema_refuses_an_operation_without_the_operator_role() -> None:
    manifest = _committed()
    manifest["operations"][0]["roles"] = ["automationViewer"]
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(manifest)


def test_committed_manifest_is_complete_and_stable() -> None:
    manifest = _committed()
    identifiers = [row["operationId"] for row in manifest["operations"]]
    assert len(identifiers) == 94
    assert identifiers == sorted(set(identifiers))
    known = {row["providerId"] for row in manifest["providers"]}
    assert {
        provider for row in manifest["operations"] for provider in row["providerIds"]
    } == known
    assert tool.UNRESOLVED_MUTATING == {
        row["operationId"] for row in manifest["operations"] if row["mutating"] is None
    }
    # Rendering is byte-stable, so the CI comparison means the content is equal.
    assert COMMITTED.read_text(encoding="utf-8") == tool.render(manifest)
