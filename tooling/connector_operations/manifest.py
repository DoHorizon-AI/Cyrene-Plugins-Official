"""Generate and check the connector operation manifest by running its source repositories' code.

Platform decides which operations can be granted, Exchange maps them to provider tools, and
Navigator holds their resource projections. Each probe runs that repository's real code and
dumps the result. This tool merges the dumps and fails when they disagree, except for the
exact disagreements listed below, which it records. Nothing reads the manifest yet.

中文：执行三个来源仓库的真实代码，生成并校验操作清单。来源不一致时报错，只有下面明确列出的分歧除外。
目前还没有代码读取这份清单。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "cyrene.connector-operations.v1"
MANIFEST_PATH = Path("contracts/connector-operations/v1/connector-operations.json")
TOOL_ROOT = Path(__file__).resolve().parent
PLATFORM_GRANT = (
    "framework/crates/cy-workspace-control-plane/src/authority/connector_grant.rs"
)
PLATFORM_POLICY = "contracts/policies/workspace-connector-operator-policy-v1.json"
PROBE_PROVIDER = "not_a_provider"
# sources.json is written last, so its presence means the whole probe finished.
DUMP_NAMES = (
    "sources",
    "platform",
    "policy",
    "exchange",
    "navigator",
    "navigator-input",
)

# Exchange's prepare path says this does not mutate and its ledger path says it does, so the
# manifest states neither. Neither path runs here, so this tool cannot notice if they are made to
# agree: remove the entry by hand when they do. An operation added here needs that same reason.
UNRESOLVED_MUTATING = frozenset({"msteams.message.reply"})
# Exchange's launcher map and Navigator's table give different projections, so Navigator would
# refuse these rows. Recorded in Exchange's and Navigator's own fields; see the README.
KNOWN_PROJECTION_DISAGREEMENTS = frozenset(
    {"microsoft_graph.calendar.events.list", "microsoft_graph.todo.tasks.list"}
)


class SourceError(ValueError):
    """A source could not be run, or the sources disagree."""


def _platform_pairs(
    platform: Mapping[str, Any],
) -> dict[str, dict[str, dict[str, Any]]]:
    by_operation: dict[str, dict[str, dict[str, Any]]] = {}
    for pair in platform["pairs"]:
        by_operation.setdefault(pair["operation"], {})[pair["provider"]] = pair
    return by_operation


def _operation_roles(
    operation_id: str, rows: Mapping[str, Mapping[str, Any]], providers: Sequence[str]
) -> list[str]:
    """Return the roles the real authorization check allows, identical across the operation's providers."""

    seen = set()
    for provider in providers:
        allowed = rows[provider]["allowed"]
        if allowed["noRole"]:
            raise SourceError(f"Platform allows {operation_id} with no role")
        seen.add(
            tuple(sorted(role for role, permitted in allowed.items() if permitted))
        )
    if len(seen) != 1:
        raise SourceError(
            f"Platform allows different roles for {operation_id} by provider"
        )
    roles = list(seen.pop())
    if "operator" not in roles:
        raise SourceError(f"Platform denies {operation_id} to the operator role")
    return roles


def _check_policy(
    policy: Mapping[str, Any],
    pairs: Mapping[str, Mapping[str, Mapping[str, Any]]],
    supported: Sequence[str],
    roles: Mapping[str, str],
) -> None:
    """Hold the policy file to what the real functions report."""

    role_fields = {
        "operatorRole": "operator",
        "readOnlyRole": "automationViewer",
        "taskWriterRole": "officeTaskWriter",
    }
    for field, role in role_fields.items():
        if policy[field] != roles[role]:
            raise SourceError(f"Platform policy {field} and code disagree")
    catalogs = {
        "readOnlyOperations": "viewerCatalog",
        "taskWriterOperations": "writerCatalog",
        "automationTransportOperations": "transportCatalog",
    }
    declared = {
        row["operationId"]: sorted(row["providerIds"]) for row in policy["operations"]
    }
    if len(declared) != len(policy["operations"]):
        raise SourceError("Platform policy repeats an operation")
    if set(declared) != set(supported):
        raise SourceError("Platform policy and code list different operations")
    for operation_id, providers in declared.items():
        actual = sorted(
            provider
            for provider, row in pairs[operation_id].items()
            if row["supported"]
        )
        if actual != providers:
            raise SourceError(
                f"Platform policy and code disagree on the providers of {operation_id}"
            )
    for section, field in catalogs.items():
        listed = {
            (row["operationId"], provider)
            for row in policy[section]
            for provider in row["providerIds"]
        }
        actual_pairs = {
            (operation_id, provider)
            for operation_id, rows in pairs.items()
            for provider, row in rows.items()
            if row[field]
        }
        if listed != actual_pairs:
            raise SourceError(f"Platform policy {section} and code disagree")


def build_manifest(
    platform: Mapping[str, Any],
    policy: Mapping[str, Any],
    exchange: Mapping[str, Any],
    navigator: Mapping[str, Any],
    sources: Mapping[str, str],
) -> dict[str, Any]:
    """Merge the dumps, raising SourceError wherever two sources disagree."""

    supported = list(platform["supported"])
    if len(set(supported)) != len(supported):
        raise SourceError("Platform repeats a supported operation")
    pairs = _platform_pairs(platform)
    _check_policy(
        policy,
        {operation_id: pairs[operation_id] for operation_id in supported},
        supported,
        platform["roles"],
    )

    exchange_by_operation: dict[str, dict[str, str]] = {}
    for provider, rows in exchange["toolIds"].items():
        for operation_id, tool_id in rows.items():
            exchange_by_operation.setdefault(operation_id, {})[provider] = tool_id
    binding_rows: dict[str, dict[str, Mapping[str, Any]]] = {}
    for provider, rows in exchange["bindingTools"].items():
        for row in rows:
            binding_rows.setdefault(row["operationId"], {})[provider] = row
            if row["toolId"] != exchange["toolIds"][provider].get(row["operationId"]):
                raise SourceError(
                    f"Exchange's two tables disagree on the tool of {row['operationId']}"
                )
    mutating_set = set(exchange["mutating"])

    if navigator["scopeLocksOutsideGithub"]:
        raise SourceError("Navigator locks an operation outside GitHub")
    unsupported = sorted(
        operation_id
        for rows in navigator["policies"].values()
        for operation_id, row in rows.items()
        if row is not None and operation_id not in supported
    )
    if unsupported:
        raise SourceError(
            f"Navigator lists operations Platform does not support: {unsupported[:5]}"
        )

    used_mutating_exceptions = set()
    used_projection_exceptions = set()
    operations = []
    for operation_id in sorted(supported):
        rows = pairs[operation_id]
        providers = sorted(
            provider for provider, row in rows.items() if row["supported"]
        )
        if not providers:
            raise SourceError(f"Platform supports {operation_id} for no provider")
        tools = exchange_by_operation.get(operation_id, {})
        if not set(tools) <= set(providers):
            raise SourceError(
                f"Exchange maps {operation_id} to a provider Platform does not allow"
            )

        navigator_rows = {
            provider: navigator["policies"][provider].get(operation_id)
            for provider in navigator["policies"]
        }
        known_to_navigator = {
            provider: row for provider, row in navigator_rows.items() if row is not None
        }
        if known_to_navigator and set(known_to_navigator) != set(providers):
            raise SourceError(
                f"Navigator and Platform disagree on the providers of {operation_id}"
            )
        distinct = {
            json.dumps(row, sort_keys=True) for row in known_to_navigator.values()
        }
        if len(distinct) > 1:
            raise SourceError(
                f"Navigator's answer for {operation_id} depends on the provider"
            )
        policy_row = next(iter(known_to_navigator.values()), None)

        if policy_row is not None and any(
            tool != policy_row["toolId"] for tool in tools.values()
        ):
            raise SourceError(
                f"Exchange and Navigator disagree on the tool of {operation_id}"
            )
        mutating = operation_id in mutating_set if tools else None
        if policy_row is not None:
            if mutating is not None and mutating != policy_row["mutating"]:
                raise SourceError(
                    f"Exchange and Navigator disagree on whether {operation_id} mutates"
                )
            mutating = policy_row["mutating"]
        if mutating is None:
            if operation_id not in UNRESOLVED_MUTATING:
                raise SourceError(f"No source states whether {operation_id} mutates")
            used_mutating_exceptions.add(operation_id)

        scope_lock = navigator["scopeLocks"].get(operation_id)
        if scope_lock is not None and mutating is not True:
            raise SourceError(
                f"Navigator locks the scope of {operation_id}, which is not marked mutating"
            )

        exchange_projections = {
            provider: binding_rows[operation_id][provider]["resourceProjection"]
            for provider in sorted(binding_rows.get(operation_id, {}))
        }
        for provider, row in binding_rows.get(operation_id, {}).items():
            if row["mutating"] != mutating:
                raise SourceError(
                    f"Exchange's launcher map disagrees on whether {operation_id} mutates"
                )
            if (
                policy_row is not None
                and row["resourceProjection"] != policy_row["resourceProjection"]
            ):
                if operation_id not in KNOWN_PROJECTION_DISAGREEMENTS:
                    raise SourceError(
                        f"Exchange and Navigator disagree on the projection of {operation_id}"
                    )
                used_projection_exceptions.add(operation_id)

        operations.append(
            {
                "operationId": operation_id,
                "providerIds": providers,
                "roles": _operation_roles(operation_id, rows, providers),
                "automationTransport": any(
                    rows[provider]["transportCatalog"] for provider in providers
                ),
                "mutating": mutating,
                "exchangeToolIds": {
                    provider: tools[provider] for provider in sorted(tools)
                },
                "exchangeResourceProjections": exchange_projections,
                "navigatorToolId": policy_row["toolId"] if policy_row else None,
                "navigatorResourceProjection": policy_row["resourceProjection"]
                if policy_row
                else None,
                "scopeLockBranchField": scope_lock,
            }
        )

    # An exception that no longer applies would quietly excuse a future disagreement.
    if used_mutating_exceptions != UNRESOLVED_MUTATING:
        raise SourceError(
            f"Stale exception: {sorted(UNRESOLVED_MUTATING - used_mutating_exceptions)}"
        )
    if used_projection_exceptions != KNOWN_PROJECTION_DISAGREEMENTS:
        raise SourceError(
            f"Stale exception: {sorted(KNOWN_PROJECTION_DISAGREEMENTS - used_projection_exceptions)}"
        )

    exchange_only = []
    for operation_id, tools in sorted(exchange_by_operation.items()):
        if operation_id in supported:
            continue
        if any(row["supported"] for row in pairs[operation_id].values()):
            raise SourceError(f"Platform supports {operation_id} but does not list it")
        exchange_only.append(
            {
                "operationId": operation_id,
                "mutating": operation_id in mutating_set,
                "exchangeToolIds": {
                    provider: tools[provider] for provider in sorted(tools)
                },
            }
        )

    provider_ids = sorted(
        {provider for row in operations for provider in row["providerIds"]}
    )
    navigator_providers = {
        provider
        for provider, rows in navigator["policies"].items()
        if any(row is not None for row in rows.values())
    }
    if not navigator_providers <= set(provider_ids):
        raise SourceError("Navigator allows a provider Platform does not know")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "sources": dict(sorted(sources.items())),
        "roles": dict(sorted(platform["roles"].items())),
        "providers": [
            {
                "providerId": provider,
                # Not applicable, rather than false, for a provider Navigator refuses to bind.
                "profileIsOuterClaim": navigator["outerClaim"][provider]
                if provider in navigator_providers
                else None,
            }
            for provider in provider_ids
        ],
        "operations": operations,
        "exchangeOnlyOperations": exchange_only,
    }


def render(manifest: Mapping[str, Any]) -> str:
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def _run(
    command: list[str], label: str, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, **kwargs
    )
    if result.returncode:
        raise SourceError(f"{label} failed:\n{(result.stdout + result.stderr)[-3000:]}")
    return result


def _clean_head(root: Path) -> str:
    """Return the checkout's commit, refusing local changes that the pin would not describe."""

    if _run(
        ["git", "-C", str(root), "status", "--porcelain"], "git status"
    ).stdout.strip():
        raise SourceError(f"{root} has local changes")
    return _run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], "git rev-parse"
    ).stdout.strip()


def _probe_platform(
    checkout: Path,
    providers: list[str],
    extra: list[str],
    output: Path,
    target: Path | None,
) -> None:
    """Run the probe as a test inside a throwaway copy of Platform, then check it really ran."""

    with tempfile.TemporaryDirectory(prefix="connector-probe-platform-") as scratch:
        tree = Path(scratch) / "platform"
        _run(
            [
                "git",
                "-C",
                str(checkout),
                "worktree",
                "add",
                "--quiet",
                "--detach",
                str(tree),
                "HEAD",
            ],
            "git worktree add",
        )
        try:
            with (tree / PLATFORM_GRANT).open("a", encoding="utf-8") as source:
                source.write(
                    (TOOL_ROOT / "probe_platform.rs").read_text(encoding="utf-8")
                )
            environment = {
                **os.environ,
                "CARGO_TARGET_DIR": str(target or Path(scratch) / "target"),
                "CONNECTOR_PROBE_OUTPUT": str(output),
                "CONNECTOR_PROBE_PROVIDERS": json.dumps(providers),
                "CONNECTOR_PROBE_EXTRA_OPERATIONS": json.dumps(extra),
            }
            result = _run(
                [
                    "cargo",
                    "test",
                    "--locked",
                    "-p",
                    "cy-workspace-control-plane",
                    "--lib",
                    "connector_operations_probe",
                ],
                "Platform probe",
                cwd=tree,
                env=environment,
            )
            if "test result: ok. 1 passed" not in result.stdout:
                raise SourceError(
                    f"Platform probe did not run:\n{result.stdout[-1000:]}"
                )
        finally:
            _run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "worktree",
                    "remove",
                    "--force",
                    str(tree),
                ],
                "git worktree remove",
            )


def probe(
    platform: Path,
    exchange: Path,
    exchange_python: Path,
    navigator: Path,
    output: Path,
    cargo_target: Path | None,
) -> None:
    """Run all three probes against clean checkouts and write their dumps."""

    output.mkdir(parents=True, exist_ok=True)
    # A dump left by an earlier run must never be mistaken for this run's result.
    for name in DUMP_NAMES:
        (output / f"{name}.json").unlink(missing_ok=True)
    sources = {
        "exchange": _clean_head(exchange),
        "navigator": _clean_head(navigator),
        "platform": _clean_head(platform),
    }
    exchange_dump = output / "exchange.json"
    _run(
        [
            str(exchange_python),
            str(TOOL_ROOT / "probe_exchange.py"),
            str(exchange_dump),
            str(exchange),
        ],
        "Exchange probe",
        env={
            **os.environ,
            "PYTHONPATH": f"{exchange / 'product/src'}{os.pathsep}{exchange / 'src'}",
        },
    )
    tables = json.loads(exchange_dump.read_text(encoding="utf-8"))
    policy = json.loads((platform / PLATFORM_POLICY).read_text(encoding="utf-8"))
    policy_providers = {
        provider for row in policy["operations"] for provider in row["providerIds"]
    }
    providers = sorted(policy_providers | set(tables["toolIds"]) | {PROBE_PROVIDER})
    exchange_operations = sorted(
        {operation for rows in tables["toolIds"].values() for operation in rows}
    )
    policy_operations = sorted({row["operationId"] for row in policy["operations"]})

    _probe_platform(
        platform, providers, exchange_operations, output / "platform.json", cargo_target
    )
    supported = json.loads((output / "platform.json").read_text(encoding="utf-8"))[
        "supported"
    ]
    operations = sorted(
        set(supported) | set(exchange_operations) | set(policy_operations)
    )

    node_input = output / "navigator-input.json"
    node_input.write_text(
        json.dumps({"providers": providers, "operations": operations}), encoding="utf-8"
    )
    _run(
        [
            "node",
            "--experimental-strip-types",
            "--no-warnings",
            str(TOOL_ROOT / "probe_navigator.mjs"),
            str(navigator),
            str(node_input),
            str(output / "navigator.json"),
        ],
        "Navigator probe",
    )
    shutil.copyfile(platform / PLATFORM_POLICY, output / "policy.json")
    (output / "sources.json").write_text(json.dumps(sources), encoding="utf-8")


def _load_dumps(dumps: Path) -> dict[str, Any]:
    def read(name: str) -> Any:
        try:
            return json.loads((dumps / f"{name}.json").read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise SourceError(
                f"{error.filename} is missing; the probe did not finish"
            ) from error

    return build_manifest(
        read("platform"),
        read("policy"),
        read("exchange"),
        read("navigator"),
        read("sources"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "probe", help="run the source repositories' code and write dumps"
    )
    run.add_argument("--platform", type=Path, required=True)
    run.add_argument("--exchange", type=Path, required=True)
    run.add_argument("--exchange-python", type=Path, required=True)
    run.add_argument("--navigator", type=Path, required=True)
    run.add_argument("--dumps", type=Path, required=True)
    run.add_argument("--cargo-target", type=Path)
    for name in ("generate", "check"):
        command = commands.add_parser(name, help=f"{name} the manifest from dumps")
        command.add_argument("--dumps", type=Path, required=True)
        command.add_argument("--root", type=Path, default=Path("."))
    options = parser.parse_args()
    try:
        if options.command == "probe":
            probe(
                options.platform,
                options.exchange,
                options.exchange_python,
                options.navigator,
                options.dumps,
                options.cargo_target,
            )
            print(f"wrote dumps to {options.dumps}")
            return 0
        rendered = render(_load_dumps(options.dumps))
    except SourceError as error:
        print(f"connector operations: {error}", file=sys.stderr)
        return 1
    target = options.root / MANIFEST_PATH
    if options.command == "generate":
        target.write_text(rendered, encoding="utf-8")
        print(f"wrote {target}")
        return 0
    if target.read_text(encoding="utf-8") != rendered:
        print(
            f"connector operations: {target} differs from its sources; regenerate it",
            file=sys.stderr,
        )
        return 1
    print("connector operations match their sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
