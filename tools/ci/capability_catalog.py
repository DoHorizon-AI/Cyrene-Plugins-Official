#!/usr/bin/env python3
"""Generate and check contracts/capabilities.yaml, the capability index.

The index merges four artifact sources:

- contracts/proto/cyrene/**/*.proto: shared payload contracts. Each contract
  file carries a "Direct plugin invocation identifiers" comment block naming
  the capability ID, interface versions, and methods.
- plugins/**/plugin.manifest.json: published Plugin implementations.
- contracts/runtime-implementations.json: runtime packages that serve
  capabilities without publishing a Plugin manifest package.
- contracts/tck/*: owner-scoped conformance suites, one directory per
  capability (hyphens map to dots, for example message-connector-v1).

Run without arguments to rewrite the index; run with --check to fail when the
committed index drifts from these sources.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# Keep the exported tree free of __pycache__ payloads; the source manifest
# inventory walks the filesystem and would otherwise register bytecode.
sys.dont_write_bytecode = True

import validate_manifests

INDEX_PATH = "contracts/capabilities.yaml"
PROTO_ROOT = "contracts/proto/cyrene"
TRANSPORT_PROTO_PREFIX = "contracts/proto/cyrene/plugin/"
REGISTRY_PATH = "contracts/runtime-implementations.json"
VERIFICATION_PATH = "contracts/capability-verification.json"
MANIFEST_GLOB = "plugins/*/*/plugin.manifest.json"
TCK_GLOB = "contracts/tck/*"
RUST_TCK_GLOB = "contracts/rust/cyrene-plugin-contracts/tests/*_tck.rs"
IDENTIFIER_MARKER = "Direct plugin invocation identifiers"

_COMMENT_LINE = re.compile(r"^//(\s*)(.*)$")
_KEY_VALUE = re.compile(r"^([A-Za-z][A-Za-z ]*?):\s*(.*)$")
_SCALAR_SAFE = re.compile(r"[A-Za-z0-9_./\-]+")
_NUMERIC_LIKE = re.compile(r"\d+(\.\d+)*")
_RESERVED_SCALARS = {"true", "false", "null", "yes", "no", "on", "off", "~"}
VERIFICATION_LEVELS = (
    "DECLARED",
    "CONTRACT_VERIFIED",
    "IMPLEMENTATION_VERIFIED",
    "DISPATCH_VERIFIED",
    "INTEGRATION_VERIFIED",
    "LIVE_VERIFIED",
)
EXECUTION_MODES = {"REAL", "SIMULATED", "MOCK"}
REAL_ONLY_LEVELS = {"INTEGRATION_VERIFIED", "LIVE_VERIFIED"}


class CatalogError(ValueError):
    """Raised when catalog sources cannot be reconciled."""


def _read_json(path: Path) -> object:
    """Read one JSON document with a catalog-scoped error."""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CatalogError(f"Cannot parse {path}: {error}") from error


def _parse_identifiers(text: str, path: str) -> dict:
    """Parse the capability ID, interface versions, and methods of one proto."""

    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if IDENTIFIER_MARKER in line:
            start = index + 1
            break
    if start is None:
        raise CatalogError(f"{path}: missing '{IDENTIFIER_MARKER}' block")

    pairs: list[list[str]] = []
    for line in lines[start:]:
        match = _COMMENT_LINE.match(line)
        if match is None:
            break
        indent, body = match.group(1), match.group(2).strip()
        if not body:
            continue
        if len(indent) < 2:
            break
        key_value = _KEY_VALUE.match(body)
        if key_value is not None:
            pairs.append([key_value.group(1).strip(), key_value.group(2).strip()])
            continue
        if not pairs:
            break
        pairs[-1][1] += " " + body

    capability_id = None
    versions: list[str] = []
    methods: list[str] = []
    for key, value in pairs:
        normalized = key.lower()
        if normalized == "capability id":
            capability_id = value.strip()
        elif "interface version" in normalized:
            versions.extend(re.findall(r"\d+", value))
        elif "method" in normalized or "event type" in normalized:
            for token in value.split("(", 1)[0].split(","):
                token = token.strip()
                if token:
                    methods.append(token)
    if not capability_id:
        raise CatalogError(f"{path}: identifier block has no capability ID")
    if not methods:
        raise CatalogError(f"{path}: identifier block lists no methods")
    return {
        "id": capability_id,
        "interface_versions": sorted(set(versions)),
        "methods": sorted(set(methods)),
    }


def _collect_contracts(root: Path) -> dict[str, dict]:
    """Collect proto-backed capability contracts by capability ID."""

    contracts: dict[str, dict] = {}
    proto_root = root / PROTO_ROOT
    for proto in sorted(proto_root.rglob("*.proto")):
        relative = proto.relative_to(root).as_posix()
        if relative.startswith(TRANSPORT_PROTO_PREFIX):
            continue
        parsed = _parse_identifiers(proto.read_text(encoding="utf-8"), relative)
        if parsed["id"] in contracts:
            raise CatalogError(
                f"Duplicate proto contract for {parsed['id']}: {relative}"
            )
        contracts[parsed["id"]] = {
            "kind": "proto",
            "paths": [relative],
            "interface_versions": parsed["interface_versions"],
            "methods": parsed["methods"],
        }
    return contracts


def _schema_paths(manifest_dir: Path, manifest: dict) -> list[Path]:
    """Return the schema files referenced by one manifest's methods."""

    referenced: list[Path] = []
    for method in manifest.get("methods", []):
        if not isinstance(method, dict):
            raise CatalogError(f"{manifest_dir}: manifest method is not an object")
        for key in ("inputSchema", "outputSchema"):
            reference = method.get(key)
            if not isinstance(reference, str) or not reference:
                continue
            file_part = reference.split("#", 1)[0]
            candidate = (manifest_dir / file_part).resolve()
            if not candidate.is_file():
                raise CatalogError(
                    f"{manifest_dir}: schema reference does not resolve: {reference}"
                )
            referenced.append(candidate)
    return referenced


def _collect_manifests(root: Path) -> tuple[list[dict], dict[str, dict]]:
    """Collect published implementations and their contract hints."""

    try:
        validate_manifests.validate_root(root)
    except validate_manifests.ManifestSchemaError as error:
        raise CatalogError(str(error)) from error

    implementations: list[dict] = []
    contract_hints: dict[str, dict] = {}
    seen_ids: set[str] = set()
    for manifest_path in sorted(root.glob(MANIFEST_GLOB)):
        manifest = _read_json(manifest_path)
        if not isinstance(manifest, dict):
            raise CatalogError(f"{manifest_path}: manifest root is not an object")
        plugin_id = manifest.get("id")
        capabilities = manifest.get("capabilities")
        methods = manifest.get("methods")
        if not isinstance(plugin_id, str) or not plugin_id:
            raise CatalogError(f"{manifest_path}: manifest has no plugin ID")
        if not isinstance(capabilities, list) or not capabilities:
            raise CatalogError(f"{manifest_path}: manifest declares no capabilities")
        if not isinstance(methods, list) or not methods:
            raise CatalogError(f"{manifest_path}: manifest declares no methods")
        if len(capabilities) > 1 and any(
            not isinstance(method, dict) or "capability" not in method
            for method in methods
        ):
            raise CatalogError(
                f"{manifest_path}: multi-capability manifests must scope every method"
            )
        if plugin_id in seen_ids:
            raise CatalogError(f"Duplicate plugin ID: {plugin_id}")
        seen_ids.add(plugin_id)

        relative = manifest_path.relative_to(root).as_posix()
        methods_by_capability = {
            capability: sorted(
                method["name"]
                for method in methods
                if isinstance(method, dict)
                and method.get("capability", capabilities[0]) == capability
            )
            for capability in capabilities
        }
        versions_by_capability = {
            capability: sorted(
                {
                    str(method.get("interfaceVersion", ""))
                    for method in methods
                    if isinstance(method, dict)
                    and method.get("capability", capabilities[0]) == capability
                    and method.get("interfaceVersion")
                }
            )
            for capability in capabilities
        }
        record = {
            "ref": plugin_id,
            "source": relative,
            "kind": manifest.get("kind", ""),
            "language": manifest.get("runtime", {}).get("language", ""),
            "publication": "published",
            "methods_by_capability": methods_by_capability,
            "interface_versions_by_capability": versions_by_capability,
        }
        if manifest.get("maturity"):
            record["maturity"] = manifest["maturity"]
        implementations.append(record)

        owner_paths: list[str] = []
        shared_paths: list[str] = []
        for schema in _schema_paths(manifest_path.parent, manifest):
            relative_schema = schema.relative_to(root).as_posix()
            if relative_schema.startswith("contracts/"):
                shared_paths.append(relative_schema)
            else:
                owner_paths.append(relative_schema)
        for capability in capabilities:
            if not isinstance(capability, str) or not capability:
                raise CatalogError(f"{manifest_path}: capability entry is not a string")
            # Several implementations may share one capability; the catalog
            # records each implementation and merges their contract hints.
            hint = contract_hints.setdefault(
                capability, {"owner_paths": set(), "shared_paths": set()}
            )
            hint["owner_paths"].update(owner_paths)
            hint["shared_paths"].update(shared_paths)
    return implementations, contract_hints


def _collect_registry(
    root: Path,
) -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    """Collect runtime implementations, their capabilities, and unbacked declarations."""

    registry = _read_json(root / REGISTRY_PATH)
    if not isinstance(registry, dict):
        raise CatalogError(f"{REGISTRY_PATH}: registry root is not an object")
    implementations = registry.get("implementations")
    if not isinstance(implementations, list) or not implementations:
        raise CatalogError(f"{REGISTRY_PATH}: no implementations recorded")

    records: list[dict] = []
    per_capability: dict[str, list[dict]] = {}
    seen_ids: set[str] = set()
    for entry in implementations:
        if not isinstance(entry, dict):
            raise CatalogError(
                f"{REGISTRY_PATH}: implementation entry is not an object"
            )
        entry_id = entry.get("id")
        package = entry.get("package")
        capabilities = entry.get("capabilities")
        if not isinstance(entry_id, str) or not entry_id:
            raise CatalogError(f"{REGISTRY_PATH}: implementation has no ID")
        if entry_id in seen_ids:
            raise CatalogError(f"Duplicate runtime implementation ID: {entry_id}")
        seen_ids.add(entry_id)
        if not isinstance(package, str) or not (root / package).is_dir():
            raise CatalogError(
                f"{REGISTRY_PATH}: package directory missing for {entry_id}"
            )
        if not isinstance(capabilities, list) or not capabilities:
            raise CatalogError(f"{REGISTRY_PATH}: {entry_id} declares no capabilities")
        records.append({"id": entry_id, "package": package})
        for capability in capabilities:
            if not isinstance(capability, dict) or not isinstance(
                capability.get("id"), str
            ):
                raise CatalogError(
                    f"{REGISTRY_PATH}: {entry_id} has a malformed capability"
                )
            record = {
                "ref": entry_id,
                "source": REGISTRY_PATH,
                "language": entry.get("language", ""),
                "protocol": entry.get("protocol", ""),
                "publication": entry.get("publication", ""),
                "methods": sorted(set(capability.get("methods", []))),
            }
            per_capability.setdefault(capability["id"], []).append(record)

    declared = registry.get("declaredWithoutContract", [])
    if not isinstance(declared, list):
        raise CatalogError(f"{REGISTRY_PATH}: declaredWithoutContract is not a list")
    unbacked = []
    for entry in declared:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise CatalogError(
                f"{REGISTRY_PATH}: malformed declaredWithoutContract entry"
            )
        unbacked.append(
            {
                "id": entry["id"],
                "declared_by": entry.get("declaredBy", ""),
                "notes": entry.get("notes", ""),
            }
        )
    return records, per_capability, unbacked


def _collect_verification(root: Path) -> dict[tuple[str, str], dict]:
    """Collect authored verification evidence keyed by (capability, implementation)."""

    registry = _read_json(root / VERIFICATION_PATH)
    if not isinstance(registry, dict):
        raise CatalogError(f"{VERIFICATION_PATH}: registry root is not an object")
    records = registry.get("records")
    if not isinstance(records, list) or not records:
        raise CatalogError(f"{VERIFICATION_PATH}: no verification records")
    collected: dict[tuple[str, str], dict] = {}
    for record in records:
        if not isinstance(record, dict):
            raise CatalogError(f"{VERIFICATION_PATH}: record is not an object")
        capability = record.get("capability")
        implementation = record.get("implementation")
        level = record.get("verification_level")
        mode = record.get("execution_mode")
        evidence = record.get("evidence", [])
        if not isinstance(capability, str) or not isinstance(implementation, str):
            raise CatalogError(
                f"{VERIFICATION_PATH}: record needs capability and implementation"
            )
        key = (capability, implementation)
        if key in collected:
            raise CatalogError(
                f"{VERIFICATION_PATH}: duplicate record for {capability} / {implementation}"
            )
        if level not in VERIFICATION_LEVELS:
            raise CatalogError(
                f"{VERIFICATION_PATH}: invalid verification_level for {capability}: {level!r}"
            )
        if not isinstance(evidence, list):
            raise CatalogError(
                f"{VERIFICATION_PATH}: evidence must be a list for {capability}"
            )
        if level == "DECLARED":
            if evidence or mode is not None:
                raise CatalogError(
                    f"{VERIFICATION_PATH}: DECLARED records carry no evidence or mode: {capability}"
                )
        else:
            if mode not in EXECUTION_MODES:
                raise CatalogError(
                    f"{VERIFICATION_PATH}: {capability} / {implementation} needs a valid execution_mode"
                )
            if not evidence:
                raise CatalogError(
                    f"{VERIFICATION_PATH}: {capability} / {implementation} needs evidence above DECLARED"
                )
            if level in REAL_ONLY_LEVELS and mode != "REAL":
                raise CatalogError(
                    f"{VERIFICATION_PATH}: {level} requires execution_mode REAL: "
                    f"{capability} / {implementation}"
                )
            for item in evidence:
                if (
                    not isinstance(item, dict)
                    or not item.get("observed_at")
                    or not item.get("command")
                    or not item.get("result")
                ):
                    raise CatalogError(
                        f"{VERIFICATION_PATH}: evidence needs observed_at, command, and result: {capability}"
                    )
        verified_at = max(
            (item.get("observed_at", "") for item in evidence), default=""
        )
        collected[key] = {
            "verification_level": level,
            "execution_mode": mode,
            "verified_at": verified_at,
        }
    return collected


def _collect_tck(root: Path) -> dict[str, list[str]]:
    """Map TCK suite directories to capability IDs."""

    suites: dict[str, list[str]] = {}
    for suite in sorted(path for path in root.glob(TCK_GLOB) if path.is_dir()):
        capability = suite.name.replace("-", ".")
        suites.setdefault(capability, []).append(suite.relative_to(root).as_posix())
    return suites


def _collect_rust_tck(root: Path, capability_ids: set[str]) -> dict[str, list[str]]:
    """Map Rust contract TCK files to capability IDs by quoted identifiers."""

    suites: dict[str, list[str]] = {}
    for test_file in sorted(root.glob(RUST_TCK_GLOB)):
        text = test_file.read_text(encoding="utf-8")
        relative = test_file.relative_to(root).as_posix()
        for capability_id in sorted(capability_ids):
            if f'"{capability_id}"' in text:
                suites.setdefault(capability_id, []).append(relative)
    return suites


def build_catalog(root: Path) -> dict:
    """Build the capability index document from repository artifacts."""

    contracts = _collect_contracts(root)
    implementations, hints = _collect_manifests(root)
    _, runtime_capabilities, unbacked = _collect_registry(root)
    suites = _collect_tck(root)

    rows: dict[str, dict] = {}
    for capability_id, contract in sorted(contracts.items()):
        rows[capability_id] = {
            "id": capability_id,
            "interface_versions": contract["interface_versions"],
            "contract": {"kind": contract["kind"], "paths": contract["paths"]},
            "contract_methods": contract["methods"],
            "implementations": [],
            "tck": [],
        }

    for record in implementations:
        manifest = _read_json(root / record["source"])
        for capability_id in manifest["capabilities"]:
            row = rows.get(capability_id)
            hint = hints[capability_id]
            if row is None:
                if not hint["owner_paths"]:
                    raise CatalogError(
                        f"{record['source']}: {capability_id} has no contract or owner schema"
                    )
                row = {
                    "id": capability_id,
                    "interface_versions": record["interface_versions_by_capability"][
                        capability_id
                    ],
                    "contract": {
                        "kind": "owner-scoped",
                        "paths": sorted(hint["owner_paths"]),
                    },
                    "contract_methods": record["methods_by_capability"][capability_id],
                    "implementations": [],
                    "tck": [],
                }
                rows[capability_id] = row
            else:
                shared = [
                    path
                    for path in sorted(hint["shared_paths"])
                    if path not in row["contract"]["paths"]
                ]
                row["contract"]["paths"] = sorted(row["contract"]["paths"] + shared)
            entry = {
                "ref": record["ref"],
                "source": record["source"],
                "kind": record["kind"],
                "language": record["language"],
                "publication": record["publication"],
                "methods": record["methods_by_capability"][capability_id],
                "interface_versions": record["interface_versions_by_capability"][
                    capability_id
                ],
            }
            if record.get("maturity"):
                entry["maturity"] = record["maturity"]
            row["implementations"].append(entry)

    for capability_id, records in sorted(runtime_capabilities.items()):
        row = rows.get(capability_id)
        if row is None or row["contract"]["kind"] != "proto":
            raise CatalogError(
                f"{REGISTRY_PATH}: {capability_id} is not backed by a proto contract"
            )
        row["implementations"].extend(records)

    suite_paths = _collect_rust_tck(root, set(rows))
    for capability_id, paths in suite_paths.items():
        suites.setdefault(capability_id, []).extend(paths)

    for capability_id, paths in sorted(suites.items()):
        row = rows.get(capability_id)
        if row is None:
            raise CatalogError(f"TCK suite has no matching capability: {paths[0]}")
        row["tck"] = sorted(paths)

    verification = _collect_verification(root)
    implemented_pairs = {
        (row["id"], implementation["ref"])
        for row in rows.values()
        for implementation in row["implementations"]
    }
    missing = sorted(
        f"{capability} / {implementation}"
        for capability, implementation in implemented_pairs - set(verification)
    )
    if missing:
        raise CatalogError(
            f"{VERIFICATION_PATH}: missing records for: {', '.join(missing)}"
        )
    unknown = sorted(
        f"{capability} / {implementation}"
        for capability, implementation in set(verification) - implemented_pairs
    )
    if unknown:
        raise CatalogError(
            f"{VERIFICATION_PATH}: records reference unknown implementations: {', '.join(unknown)}"
        )
    for row in rows.values():
        for implementation in row["implementations"]:
            record = verification[(row["id"], implementation["ref"])]
            implementation["verification_level"] = record["verification_level"]
            if record["execution_mode"]:
                implementation["execution_mode"] = record["execution_mode"]
            if record["verified_at"]:
                implementation["verified_at"] = record["verified_at"]

    for row in rows.values():
        duplicates = [
            item
            for item in row["implementations"]
            if row["implementations"].count(item) > 1
        ]
        if duplicates:
            raise CatalogError(f"Duplicate implementation record for {row['id']}")
        row["implementations"].sort(key=lambda item: item["ref"])

    document = {
        "schema_version": "cyrene.capabilities.v1",
        "generated_by": "tools/ci/capability_catalog.py",
        "verification_registry": VERIFICATION_PATH,
        "capabilities": [rows[key] for key in sorted(rows)],
    }
    if unbacked:
        document["declared_without_contract"] = sorted(
            unbacked, key=lambda item: item["id"]
        )
    return document


def _yaml_scalar(value: str) -> str:
    """Render one deterministic YAML scalar, quoting when parsing would drift."""

    if (
        _SCALAR_SAFE.fullmatch(value)
        and not _NUMERIC_LIKE.fullmatch(value)
        and value.lower() not in _RESERVED_SCALARS
    ):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_yaml(node: object, indent: int = 0) -> list[str]:
    """Render the catalog document as deterministic block-style YAML."""

    lines: list[str] = []
    pad = "  " * indent
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (list, dict)) and value:
                lines.append(f"{pad}{key}:")
                lines.extend(_render_yaml(value, indent + 1))
            elif isinstance(value, list):
                lines.append(f"{pad}{key}: []")
            elif isinstance(value, dict):
                lines.append(f"{pad}{key}: {{}}")
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
        return lines
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict) and item:
                rendered = _render_yaml(item, indent + 1)
                prefix = "  " * (indent + 1)
                lines.append(f"{pad}- {rendered[0][len(prefix) :]}")
                lines.extend(rendered[1:])
            elif isinstance(item, str):
                lines.append(f"{pad}- {_yaml_scalar(item)}")
            else:
                raise CatalogError(f"Unsupported YAML node: {item!r}")
        return lines
    raise CatalogError(f"Unsupported YAML node: {node!r}")


def render_catalog(document: dict) -> str:
    """Render the catalog document with a trailing newline."""

    return "\n".join(_render_yaml(document)) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Generate the capability index, or check it with --check."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the committed index differs from generated content.",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        document = build_catalog(root)
        rendered = render_catalog(document)
    except CatalogError as error:
        print(f"CAPABILITY_CATALOG: FAIL: {error}", file=sys.stderr)
        return 2

    index_path = root / INDEX_PATH
    capability_count = len(document["capabilities"])
    implementation_count = sum(
        len(row["implementations"]) for row in document["capabilities"]
    )
    if args.check:
        if not index_path.is_file():
            print(f"CAPABILITY_CATALOG: FAIL: missing {INDEX_PATH}", file=sys.stderr)
            return 2
        committed = index_path.read_text(encoding="utf-8")
        if committed != rendered:
            print(
                f"CAPABILITY_CATALOG: FAIL: {INDEX_PATH} is stale; "
                "run python3 tools/ci/capability_catalog.py",
                file=sys.stderr,
            )
            return 2
        print(
            "CAPABILITY_CATALOG: PASS "
            f"capabilities={capability_count} implementations={implementation_count}"
        )
        return 0

    index_path.write_text(rendered, encoding="utf-8")
    print(
        "CAPABILITY_CATALOG: OK "
        f"capabilities={capability_count} implementations={implementation_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
