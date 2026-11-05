#!/usr/bin/env python3
"""Generate and check the connector operation manifest from its source repositories.

Platform decides which operations can be granted, Exchange maps them to provider
tools, and Navigator holds their resource projections. Nothing reads the manifest
yet: this tool records what those three repositories say at exact commits, and
fails when they disagree instead of choosing a side.

中文：从 Platform、Exchange、Navigator 的固定提交生成并校验操作清单。三处说法不一致时
直接报错，不替它们选择。目前还没有代码读取这份清单。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "cyrene.connector-operations.v1"
MANIFEST_PATH = Path("contracts/connector-operations/v1/connector-operations.json")

PLATFORM_GRANT = (
    "framework/crates/cy-workspace-control-plane/src/authority/connector_grant.rs"
)
PLATFORM_SERVICE = (
    "framework/crates/cy-workspace-control-plane/src/authority/service.rs"
)
PLATFORM_POLICY = "contracts/policies/workspace-connector-operator-policy-v1.json"
EXCHANGE_INTEGRATIONS = "product/src/cyrene_exchange_product/integrations"
EXCHANGE_MODULES = ("microsoft_graph.py", "azure_devops.py", "connector_operations.py")
NAVIGATOR_SERVE = "harness/src/serve.ts"
NAVIGATOR_TOOL_PROVIDERS = "harness/src/tool-providers.ts"

ROLE_CONSTANTS = {
    "operator": "NAVIGATOR_CONNECTOR_OPERATOR_ROLE",
    "automationViewer": "NAVIGATOR_AUTOMATION_VIEWER_ROLE",
    "officeTaskWriter": "NAVIGATOR_OFFICE_TASK_WRITER_ROLE",
}
# The whole authorization rule. If this text changes, the role columns must be rethought.
ROLE_RULE = """
if roles.contains(NAVIGATOR_CONNECTOR_OPERATOR_ROLE)
    || (is_automation_viewer_connector_operation(provider_id, operation_id)
        && roles.contains(NAVIGATOR_AUTOMATION_VIEWER_ROLE))
    || (is_office_task_writer_connector_operation(provider_id, operation_id)
        && roles.contains(NAVIGATOR_OFFICE_TASK_WRITER_ROLE))
    || (is_automation_transport_connector_operation(provider_id, operation_id)
        && (roles.contains(NAVIGATOR_AUTOMATION_VIEWER_ROLE)
            || roles.contains(NAVIGATOR_OFFICE_TASK_WRITER_ROLE)))
"""


class SourceError(ValueError):
    """A source could not be read, or the sources disagree."""


@dataclass(frozen=True)
class SourceTexts:
    """Exact source text of the files the manifest is derived from."""

    platform_grant: str
    platform_service: str
    platform_policy: str
    exchange_modules: tuple[str, ...]
    navigator_serve: str
    navigator_tool_providers: str


def _rust_string_arrays(text: str) -> dict[str, list[str]]:
    arrays = {}
    for match in re.finditer(
        r"pub const ([A-Z_]+): \[&str; (\d+)\] =\s*\[(.*?)\];", text, re.DOTALL
    ):
        items = re.findall(r'"([^"]+)"', match.group(3))
        if len(items) != int(match.group(2)) or len(set(items)) != len(items):
            raise SourceError(
                f"Platform array {match.group(1)} does not match its declared size"
            )
        arrays[match.group(1)] = items
    return arrays


def _rust_string_constant(text: str, name: str) -> str:
    match = re.search(rf'pub const {name}: &str =\s*"([^"]+)";', text)
    if match is None:
        raise SourceError(f"Platform constant {name} is missing")
    return match.group(1)


def _provider_rule(text: str) -> dict[str, frozenset[str]]:
    """Read the match arms of is_supported_connector_operation.

    Returns providers by operation prefix, plus exact operation IDs as their own keys.
    Any arm this reader does not recognize is an error, so a new rule cannot slip past.
    """

    start = text.find("pub fn is_supported_connector_operation(")
    end = text.find(
        "/// Return whether the exact provider/operation pair is in the viewer", start
    )
    if start < 0 or end < 0:
        raise SourceError("Platform provider rule is missing")
    body = text[start:end]
    arms = re.findall(r"^\s*(Some\(.*?|_) => (.*?),$", body, re.MULTILINE)
    if len(arms) != body.count("=>"):
        raise SourceError("Platform provider rule has an arm this reader cannot parse")
    rule: dict[str, frozenset[str]] = {}
    for pattern, result in arms:
        if pattern == "_":
            if result != "false":
                raise SourceError("Platform provider rule has an unexpected default")
            continue
        exact = re.fullmatch(
            r'Some\(\("(\w+)", _\)\) if operation_id == "([^"]+)"', pattern
        )
        prefix = re.fullmatch(r'Some\(\("(\w+)", _\)\)', pattern)
        single = re.fullmatch(r'provider_id == "(\w+)"', result)
        several = re.fullmatch(
            r'matches!\(provider_id, ((?:"\w+"(?: \| )?)+)\)', result
        )
        providers: frozenset[str]
        if single:
            providers = frozenset({single.group(1)})
        elif several:
            providers = frozenset(re.findall(r'"(\w+)"', several.group(1)))
        else:
            raise SourceError(
                f"Platform provider rule has an unrecognized result: {result}"
            )
        if exact:
            rule[exact.group(2)] = providers
        elif prefix:
            rule[prefix.group(1)] = providers
        else:
            raise SourceError(
                f"Platform provider rule has an unrecognized arm: {pattern}"
            )
    if not rule:
        raise SourceError("Platform provider rule has no arms")
    return rule


def _providers_for(
    operation_id: str, rule: Mapping[str, frozenset[str]]
) -> frozenset[str]:
    if operation_id in rule:
        return rule[operation_id]
    prefix = operation_id.split(".", 1)[0]
    if prefix not in rule:
        raise SourceError(f"Platform provider rule does not cover {operation_id}")
    return rule[prefix]


class _Constants:
    """Evaluate the few module-level constants needed, from source, without importing it."""

    def __init__(self, texts: tuple[str, ...]) -> None:
        self._nodes: dict[str, ast.expr] = {}
        for text in texts:
            for statement in ast.parse(text).body:
                if isinstance(statement, ast.Assign) and isinstance(
                    statement.targets[0], ast.Name
                ):
                    self._nodes[statement.targets[0].id] = statement.value
                elif (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    and statement.value is not None
                ):
                    self._nodes[statement.target.id] = statement.value

    def get(self, name: str) -> Any:
        if name not in self._nodes:
            raise SourceError(f"Exchange constant {name} is missing")
        return self._value(self._nodes[name])

    def _value(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Set):
            return {self._value(item) for item in node.elts}
        if isinstance(node, ast.Dict):
            if any(key is None for key in node.keys):
                raise SourceError(
                    "Exchange tool table uses an unsupported dict expansion"
                )
            return {
                self._value(key): self._value(value)
                for key, value in zip(node.keys, node.values, strict=True)
            }
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "frozenset"
        ):
            return set(self._value(node.args[0])) if node.args else set()
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._value(node.left) | self._value(node.right)
        if isinstance(node, ast.Name):
            return self.get(node.id)
        if (
            isinstance(node, ast.DictComp)
            and len(node.generators) == 1
            and isinstance(node.key, ast.Name)
            and isinstance(node.value, ast.Name)
            and node.key.id
            == node.value.id
            == getattr(node.generators[0].target, "id", None)
            and isinstance(node.generators[0].iter, ast.Call)
            and getattr(node.generators[0].iter.func, "id", None) == "sorted"
            and not node.generators[0].ifs
        ):
            return {item: item for item in self._value(node.generators[0].iter.args[0])}
        raise SourceError(
            f"Exchange constant uses an unsupported expression: {ast.dump(node)[:120]}"
        )


def _navigator_policies(text: str) -> dict[str, dict[str, Any]]:
    start = text.find("function connectorOperationPolicy(")
    end = text.find("const policy = policies[operationId];", start)
    if start < 0 or end < 0:
        raise SourceError("Navigator operation table is missing")
    body = text[start:end]
    rows = re.findall(
        r"'([^']+)':\s*\{\s*toolId:\s*'([^']+)',\s*mutating:\s*(true|false),"
        r"\s*resourceProjection:\s*'([^']+)',?\s*\}",
        body,
    )
    if len(rows) != body.count("toolId: '"):
        raise SourceError(
            "Navigator operation table has a row this reader cannot parse"
        )
    table: dict[str, dict[str, Any]] = {}
    for operation_id, tool_id, mutating, projection in rows:
        if operation_id in table:
            raise SourceError(f"Navigator operation table repeats {operation_id}")
        table[operation_id] = {
            "toolId": tool_id,
            "mutating": mutating == "true",
            "resourceProjection": projection,
        }
    return table


def _navigator_outer_claim_providers(text: str) -> frozenset[str]:
    match = re.search(r"const profileIsOuterClaim = (.*?);", text, re.DOTALL)
    if match is None:
        raise SourceError("Navigator profileIsOuterClaim is missing")
    expression = re.sub(r"\s+", " ", match.group(1)).strip()
    terms = expression.split(" || ")
    providers = []
    for term in terms:
        found = re.fullmatch(r"hostBinding\.providerId === '(\w+)'", term)
        if found is None:
            raise SourceError(
                f"Navigator profileIsOuterClaim has an unrecognized term: {term}"
            )
        providers.append(found.group(1))
    return frozenset(providers)


def _navigator_scope_lock_fields(text: str) -> dict[str, str]:
    start = text.find("function connectorScopeLock(")
    if start < 0:
        raise SourceError("Navigator connectorScopeLock is missing")
    match = re.search(r"const branchField = (.*?);", text[start:], re.DOTALL)
    if (
        match is None
        or "if (providerId !== 'github') return undefined;"
        not in text[start : start + match.start()]
    ):
        raise SourceError("Navigator connectorScopeLock has an unrecognized shape")
    expression = re.sub(r"\s+", " ", match.group(1)).strip()
    if not expression.endswith(": undefined"):
        raise SourceError("Navigator connectorScopeLock has an unrecognized default")
    fields: dict[str, str] = {}
    branches = expression.removesuffix(": undefined").strip()
    pieces = list(
        re.finditer(
            r"((?:operationId === '[^']+'(?: \|\| )?)+) \? '(\w+)'(?: : )?", branches
        )
    )
    if not pieces or "".join(piece.group(0) for piece in pieces) != branches:
        raise SourceError(
            "Navigator connectorScopeLock has a branch this reader cannot parse"
        )
    for piece in pieces:
        for operation_id in re.findall(r"operationId === '([^']+)'", piece.group(1)):
            fields[operation_id] = piece.group(2)
    return fields


def _require_role_rule(service_text: str) -> None:
    def normalized(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    if normalized(ROLE_RULE) not in normalized(service_text):
        raise SourceError(
            "Platform role rule changed; the manifest roles must be rethought"
        )


def build_manifest(texts: SourceTexts, sources: Mapping[str, str]) -> dict[str, Any]:
    """Build the manifest, raising SourceError wherever two sources disagree."""

    arrays = _rust_string_arrays(texts.platform_grant)
    supported = arrays["SUPPORTED_CONNECTOR_OPERATION_IDS"]
    viewer = set(arrays["AUTOMATION_VIEWER_CONNECTOR_OPERATION_IDS"])
    writer = set(arrays["OFFICE_TASK_WRITER_CONNECTOR_OPERATION_IDS"])
    transport = set(arrays["AUTOMATION_TRANSPORT_CONNECTOR_OPERATION_IDS"])
    _require_role_rule(texts.platform_service)
    for name, values in (
        ("viewer", viewer),
        ("task writer", writer),
        ("transport", transport),
    ):
        if not values <= set(supported):
            raise SourceError(f"Platform {name} list names an unsupported operation")

    policy = json.loads(texts.platform_policy)
    policy_providers = {
        row["operationId"]: sorted(row["providerIds"]) for row in policy["operations"]
    }
    if len(policy_providers) != len(policy["operations"]):
        raise SourceError("Platform policy repeats an operation")
    if set(policy_providers) != set(supported):
        raise SourceError("Platform policy and code list different operations")
    rule = _provider_rule(texts.platform_grant)

    constants = _Constants(texts.exchange_modules)
    exchange_tools: Mapping[str, Mapping[str, str]] = constants.get(
        "_TOOL_IDS_BY_PROVIDER"
    )
    exchange_mutating: set[str] = constants.get("_MUTATING_OPERATIONS")
    exchange_by_operation: dict[str, dict[str, str]] = {}
    for provider_id, tools in exchange_tools.items():
        for operation_id, tool_id in tools.items():
            exchange_by_operation.setdefault(operation_id, {})[provider_id] = tool_id

    navigator = _navigator_policies(texts.navigator_serve)
    if not set(navigator) <= set(supported):
        raise SourceError("Navigator lists an operation Platform does not support")
    outer_claim = _navigator_outer_claim_providers(texts.navigator_tool_providers)
    scope_lock_fields = _navigator_scope_lock_fields(texts.navigator_tool_providers)

    operations = []
    for operation_id in sorted(supported):
        providers = policy_providers[operation_id]
        if sorted(_providers_for(operation_id, rule)) != providers:
            raise SourceError(
                f"Platform policy and code disagree on the providers of {operation_id}"
            )
        tools = exchange_by_operation.get(operation_id, {})
        if not set(tools) <= set(providers):
            raise SourceError(
                f"Exchange maps {operation_id} to a provider Platform does not allow"
            )
        policy_row = navigator.get(operation_id)
        if policy_row is not None and any(
            tool != policy_row["toolId"] for tool in tools.values()
        ):
            raise SourceError(
                f"Exchange and Navigator disagree on the tool of {operation_id}"
            )
        mutating = None
        if tools:
            mutating = operation_id in exchange_mutating
        if policy_row is not None:
            if mutating is not None and mutating != policy_row["mutating"]:
                raise SourceError(
                    f"Exchange and Navigator disagree on whether {operation_id} mutates"
                )
            mutating = policy_row["mutating"]
        if operation_id in scope_lock_fields and mutating is not True:
            raise SourceError(
                f"Navigator locks the scope of {operation_id}, which is not marked mutating"
            )
        roles = ["operator"]
        if operation_id in viewer or operation_id in transport:
            roles.append("automationViewer")
        if operation_id in writer or operation_id in transport:
            roles.append("officeTaskWriter")
        row: dict[str, Any] = {
            "operationId": operation_id,
            "providerIds": providers,
            "roles": sorted(roles),
            "mutating": mutating,
            "exchangeToolIds": {
                provider: tools[provider] for provider in sorted(tools)
            },
            "navigatorToolId": policy_row["toolId"] if policy_row else None,
            "resourceProjection": policy_row["resourceProjection"]
            if policy_row
            else None,
            "scopeLockBranchField": scope_lock_fields.get(operation_id),
        }
        operations.append(row)

    missing_scope_lock = set(scope_lock_fields) - set(supported)
    if missing_scope_lock:
        raise SourceError(
            f"Navigator locks operations Platform does not support: {sorted(missing_scope_lock)}"
        )

    exchange_only = [
        {
            "operationId": operation_id,
            "mutating": operation_id in exchange_mutating,
            "exchangeToolIds": {
                provider: tools[provider] for provider in sorted(tools)
            },
        }
        for operation_id, tools in sorted(exchange_by_operation.items())
        if operation_id not in set(supported)
    ]
    provider_ids = sorted(
        {provider for providers in policy_providers.values() for provider in providers}
    )
    if not outer_claim <= set(provider_ids):
        raise SourceError(
            "Navigator treats an unknown provider's profile as an outer claim"
        )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "sources": dict(sorted(sources.items())),
        "roles": {
            name: _rust_string_constant(texts.platform_grant, constant)
            for name, constant in sorted(ROLE_CONSTANTS.items())
        },
        "providers": [
            {
                "providerId": provider_id,
                "profileIsOuterClaim": provider_id in outer_claim,
            }
            for provider_id in provider_ids
        ],
        "operations": operations,
        "exchangeOnlyOperations": exchange_only,
    }


def render(manifest: Mapping[str, Any]) -> str:
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def _clean_head(root: Path) -> str:
    """Return the checkout's commit, refusing local changes that the pin would not describe."""

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    if git("status", "--porcelain"):
        raise SourceError(f"{root} has local changes")
    return git("rev-parse", "HEAD")


def _read(root: Path, relative: str) -> str:
    try:
        return (root / relative).read_text(encoding="utf-8")
    except OSError as error:
        raise SourceError(f"{relative} is unavailable in {root}: {error}") from error


def read_checkouts(platform: Path, exchange: Path, navigator: Path) -> SourceTexts:
    return SourceTexts(
        platform_grant=_read(platform, PLATFORM_GRANT),
        platform_service=_read(platform, PLATFORM_SERVICE),
        platform_policy=_read(platform, PLATFORM_POLICY),
        exchange_modules=tuple(
            _read(exchange, f"{EXCHANGE_INTEGRATIONS}/{name}")
            for name in EXCHANGE_MODULES
        ),
        navigator_serve=_read(navigator, NAVIGATOR_SERVE),
        navigator_tool_providers=_read(navigator, NAVIGATOR_TOOL_PROVIDERS),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "check"))
    parser.add_argument("--platform", type=Path, required=True)
    parser.add_argument("--exchange", type=Path, required=True)
    parser.add_argument("--navigator", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    options = parser.parse_args()
    target = options.root / MANIFEST_PATH
    try:
        sources = {
            "platform": _clean_head(options.platform),
            "exchange": _clean_head(options.exchange),
            "navigator": _clean_head(options.navigator),
        }
        rendered = render(
            build_manifest(
                read_checkouts(options.platform, options.exchange, options.navigator),
                sources,
            )
        )
    except SourceError as error:
        print(f"connector operations: {error}", file=sys.stderr)
        return 1
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
