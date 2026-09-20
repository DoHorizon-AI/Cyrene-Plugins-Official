"""Verify the dependency boundary of the direct QQNT runtime.

This is a high-confidence repository gate, not a legal or provenance
clearance.  The direct profile may use the shared Plugin runtime and its
locked protobuf/grpc dependencies, but it must not acquire a NapCat/AstrBot
runtime, a connector-owned network transport, or a copied third-party
runtime through its executable surface.
"""

from __future__ import annotations

import argparse
import ast
import re
import tomllib
from pathlib import Path

CONNECTOR_RELATIVE = Path("plugins/connectors/im")
DIRECT_SOURCE_RELATIVE = CONNECTOR_RELATIVE / "src/qq_connector"
DIRECT_SOURCE_PATTERN = "qqnt_direct*.py"
DEPENDENCY_FILES = (
    CONNECTOR_RELATIVE / "pyproject.toml",
    CONNECTOR_RELATIVE / "requirements.lock",
    CONNECTOR_RELATIVE / "plugin.manifest.json",
    CONNECTOR_RELATIVE / "package-descriptor.json",
)
FORBIDDEN_LINEAGE = re.compile(r"\b(?:napcat|astrbot)\b", re.IGNORECASE)
FORBIDDEN_PATH = re.compile(r"(?:napcat|astrbot)", re.IGNORECASE)
FORBIDDEN_MODULES = {
    "aiohttp",
    "httpx",
    "requests",
    "socket",
    "socketserver",
    "websocket",
    "websockets",
    "urllib3",
}
FORBIDDEN_NETWORK_CALLS = {"accept", "bind", "listen"}
ALLOWED_PROJECT_DEPENDENCIES = {
    "cyrene-plugin-runtime",
    "grpcio",
    "protobuf",
    "setuptools",
}
ALLOWED_LOCK_DEPENDENCIES = {"grpcio", "protobuf"}


def _relative(path: Path, root: Path) -> str:
    """Return a stable repository-relative path for diagnostics."""

    return path.relative_to(root).as_posix()


def _dependency_name(requirement: str) -> str:
    """Extract a normalized package name from a PEP 508 requirement."""

    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return match.group(1).lower().replace("_", "-") if match else ""


def _project_dependencies(path: Path) -> list[str]:
    """Read runtime and build dependencies from the connector project file."""

    with path.open("rb") as stream:
        project = tomllib.load(stream)
    values: list[str] = []
    project_table = project.get("project", {})
    if isinstance(project_table, dict):
        dependencies = project_table.get("dependencies", [])
        if isinstance(dependencies, list):
            values.extend(item for item in dependencies if isinstance(item, str))
    build_table = project.get("build-system", {})
    if isinstance(build_table, dict):
        requires = build_table.get("requires", [])
        if isinstance(requires, list):
            values.extend(item for item in requires if isinstance(item, str))
    return values


def _scan_python(path: Path, root: Path) -> list[str]:
    """Reject forbidden imports and network listener calls in direct sources."""

    relative = _relative(path, root)
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
    except (OSError, UnicodeError, SyntaxError) as error:
        return [f"{relative}: cannot parse direct source: {error}"]

    failures: list[str] = []
    if FORBIDDEN_LINEAGE.search(source):
        failures.append(f"{relative}: forbidden third-party lineage marker")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            modules = []
        for module in modules:
            top_level = module.split(".", 1)[0].lower()
            if top_level in FORBIDDEN_MODULES or module.lower() == "urllib.request":
                failures.append(f"{relative}:{node.lineno}: forbidden import {module}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in FORBIDDEN_NETWORK_CALLS
        ):
            failures.append(
                f"{relative}:{node.lineno}: direct source calls network method "
                f"{node.func.attr}"
            )
    return failures


def scan_root(root: Path) -> list[str]:
    """Return dependency-boundary violations for one repository root."""

    root = root.resolve(strict=True)
    connector_root = root / CONNECTOR_RELATIVE
    direct_source = root / DIRECT_SOURCE_RELATIVE
    failures: list[str] = []
    if not connector_root.is_dir():
        return [f"missing connector root: {_relative(connector_root, root)}"]
    if not direct_source.is_dir():
        failures.append(f"missing direct source root: {_relative(direct_source, root)}")

    for path in connector_root.rglob("*"):
        if path.is_file() and FORBIDDEN_PATH.search(path.name):
            failures.append(f"forbidden third-party file name: {_relative(path, root)}")

    for path in sorted(direct_source.glob(DIRECT_SOURCE_PATTERN)):
        failures.extend(_scan_python(path, root))

    for path in DEPENDENCY_FILES:
        candidate = root / path
        if not candidate.is_file():
            failures.append(
                f"missing dependency metadata: {_relative(candidate, root)}"
            )
            continue
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            failures.append(f"{_relative(candidate, root)}: cannot read: {error}")
            continue
        if FORBIDDEN_LINEAGE.search(content):
            failures.append(
                f"{_relative(candidate, root)}: forbidden third-party dependency marker"
            )

    project_path = root / (CONNECTOR_RELATIVE / "pyproject.toml")
    if project_path.is_file():
        try:
            dependencies = _project_dependencies(project_path)
        except (OSError, tomllib.TOMLDecodeError) as error:
            failures.append(f"{_relative(project_path, root)}: invalid TOML: {error}")
        else:
            for requirement in dependencies:
                name = _dependency_name(requirement)
                if name and name not in ALLOWED_PROJECT_DEPENDENCIES:
                    failures.append(
                        f"{_relative(project_path, root)}: unapproved dependency "
                        f"{requirement}"
                    )

    lock_path = root / (CONNECTOR_RELATIVE / "requirements.lock")
    if lock_path.is_file():
        try:
            lock_lines = lock_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            failures.append(f"{_relative(lock_path, root)}: cannot read: {error}")
        else:
            for line_number, line in enumerate(lock_lines, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                    continue
                name = _dependency_name(stripped)
                if not name or name not in ALLOWED_LOCK_DEPENDENCIES:
                    failures.append(
                        f"{_relative(lock_path, root)}:{line_number}: "
                        f"unapproved locked dependency {stripped}"
                    )

    return failures


def _parse_args() -> argparse.Namespace:
    """Parse the repository root argument."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    """Run the direct dependency-boundary gate."""

    args = _parse_args()
    failures = scan_root(args.root)
    if failures:
        raise SystemExit(
            "\n".join(
                f"QQNT_DIRECT_DEPENDENCY_BOUNDARY: {failure}"
                for failure in failures
            )
        )
    direct_files = sorted(
        (args.root / DIRECT_SOURCE_RELATIVE).glob(DIRECT_SOURCE_PATTERN)
    )
    print(
        "QQNT_DIRECT_DEPENDENCY_BOUNDARY: PASS "
        f"direct_python_files={len(direct_files)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
