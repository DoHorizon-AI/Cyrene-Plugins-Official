"""
┌──────────────────────────────────────────────────────────────────────┐
│  Module: github_issues_provider.vendor_cli                           │
│  Role: Enforce host profile boundaries for the native GitHub CLI.    │
│                                                                      │
│  模块职责：限制 GitHub CLI 的 profile、凭据和本地文件访问边界。         │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import re
from collections.abc import Sequence

GITHUB_CLI_EXECUTABLE = "/usr/local/bin/gh"

_BLOCKED_ROOTS = frozenset(
    {"auth", "config", "extension", "alias", "ssh-key", "gpg-key", "secret", "skills"}
)
_BLOCKED_PAIRS = frozenset(
    {
        ("codespace", "ssh"),
        ("codespace", "code"),
        ("repo", "clone"),
        ("repo", "fork"),
        ("repo", "deploy-key"),
        ("gist", "clone"),
        ("gist", "create"),
        ("release", "download"),
        ("release", "upload"),
        ("run", "download"),
        ("attestation", "download"),
        ("copilot", "suggest"),
        ("copilot", "explain"),
    }
)
_BLOCKED_FLAGS = frozenset(
    {
        "--hostname",
        "--config",
        "--api-protocol",
        "--input",
        "--input-file",
        "--output",
        "--body-file",
        "--notes-file",
        "--title-file",
        "--template-file",
        "--attach",
        "--editor",
        "--web",
        "--browser",
        "--header",
    }
)
_READ_ONLY_COMMANDS = frozenset(
    {
        ("repo", "list"),
        ("repo", "view"),
        ("issue", "list"),
        ("issue", "view"),
        ("issue", "status"),
        ("pr", "list"),
        ("pr", "view"),
        ("pr", "status"),
        ("pr", "checks"),
        ("pr", "diff"),
        ("run", "list"),
        ("run", "view"),
        ("workflow", "list"),
        ("workflow", "view"),
        ("release", "list"),
        ("release", "view"),
        ("label", "list"),
    }
)
_TOKEN_SHAPES = (
    re.compile(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?:^|[\s,])(?:[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API[_-]?KEY|CREDENTIAL|AUTHORIZATION)[A-Z0-9_]*)="
)


def validate_github_cli_argv(argv: Sequence[str], repositories: Sequence[str]) -> None:
    """Reject credential, endpoint, local-file, and out-of-binding arguments."""

    if not argv or not isinstance(argv[0], str) or argv[0].startswith("-"):
        raise ValueError("CLI command is invalid")
    lowered = [item.casefold() for item in argv]
    root = lowered[0]
    if root in _BLOCKED_ROOTS:
        raise ValueError("CLI command is not available")
    if len(lowered) > 1 and (root, lowered[1]) in _BLOCKED_PAIRS:
        raise ValueError("CLI command is not available")
    for index, item in enumerate(argv):
        name, separator, inline_value = item.partition("=")
        normalized = name.casefold()
        if normalized in _BLOCKED_FLAGS or normalized.startswith("--proxy"):
            raise ValueError("CLI option is not available")
        if root == "api" and name == "-H":
            raise ValueError("custom API headers are not available")
        if normalized in {"--token", "--password", "--authorization", "--auth-token"}:
            raise ValueError("credential options are not available")
        if item == "--":
            raise ValueError("option passthrough is not available")
        if item.startswith(("http://", "https://", "//")):
            raise ValueError("arbitrary endpoint arguments are not available")
        if item.startswith(("/", "./", "../", "~/", "@")) or "=@" in item:
            raise ValueError("local file arguments are not available")
        if any(pattern.search(item) for pattern in _TOKEN_SHAPES):
            raise ValueError("credential-shaped CLI arguments are not available")
        if _CREDENTIAL_ASSIGNMENT.search(item):
            raise ValueError("credential-valued CLI arguments are not available")
        if normalized in {"--hostname", "--config", "--api-protocol"} and not separator:
            raise ValueError("host and config overrides are not available")
        if normalized in {
            "--input",
            "--input-file",
            "--output",
            "--body-file",
            "--notes-file",
            "--title-file",
            "--template-file",
            "--attach",
        }:
            raise ValueError("local file arguments are not available")
        if name in {"--repo", "-R"}:
            value = inline_value if separator else _next_value(argv, index)
            if value not in repositories:
                raise ValueError("repository is outside this binding")
        elif item.startswith("-R") and len(item) > 2:
            if item[2:] not in repositories:
                raise ValueError("repository is outside this binding")

    if root == "api":
        endpoint = next(
            (item for item in argv[1:] if not item.startswith("-")),
            "",
        )
        if endpoint.startswith(("http://", "https://", "//")):
            raise ValueError("arbitrary API endpoints are not available")


def github_cli_is_read_only(argv: Sequence[str]) -> bool:
    """Classify only known read verbs as read-only; all other commands are writes."""

    if not argv:
        return False
    if any(
        item.casefold() in {"--async", "--no-wait", "--no-wait=true", "--async=true"}
        for item in argv
    ):
        return False
    if argv[0].casefold() in {"version", "help"}:
        return True
    return len(argv) >= 2 and (argv[0].casefold(), argv[1].casefold()) in _READ_ONLY_COMMANDS


def _next_value(argv: Sequence[str], index: int) -> str:
    """Return one separate flag value or reject an incomplete option."""

    if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
        raise ValueError("CLI option value is missing")
    return argv[index + 1]
