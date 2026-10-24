"""
┌──────────────────────────────────────────────────────────────────────┐
│  Module: huggingface_provider.vendor_cli                             │
│  Role: Enforce host repository and permission limits for the HF CLI. │
│                                                                      │
│  模块职责：按宿主资源范围限制 Hugging Face CLI。                        │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from .settings import RepositoryScope

HUGGINGFACE_CLI_EXECUTABLE = "/usr/local/bin/hf"

_BLOCKED_ROOTS = frozenset(
    {
        "auth",
        "config",
        "token",
        "login",
        "logout",
        "extensions",
        "skills",
        "update",
        "jobs",
        "endpoints",
        "inference",
        "buckets",
        "papers",
        "community",
    }
)
_BLOCKED_COMMANDS = frozenset(
    {"upload", "download", "sync", "cache", "scan-cache", "scan_cache", "lfs-enable-largefiles"}
)
_BLOCKED_FLAGS = frozenset(
    {
        "--token",
        "--api-key",
        "--password",
        "--authorization",
        "--endpoint",
        "--proxy",
        "--proxies",
        "--local-dir",
        "--local-dir-use-symlinks",
        "--cache-dir",
        "--output",
        "--output-dir",
        "--file",
        "--file-name",
        "--script",
        "--env-file",
        "--secret",
        "--secrets",
        "--editor",
        "--browser",
    }
)
_REPO_FLAGS = frozenset({"--repo", "--repo-id", "--repo_id"})
_TYPE_FLAGS = frozenset({"--type", "--repo-type", "--repo_type", "-t"})
_VALUE_FLAGS = frozenset(
    {
        *_REPO_FLAGS,
        *_TYPE_FLAGS,
        "--revision",
        "--expand",
        "--format",
        "--commit-message",
        "--commit-description",
        "--gated",
        "--sdk",
        "--template",
        "--search",
        "--author",
        "--filter",
        "--limit",
        "--flavor",
        "--region",
        "--storage",
        "--sleep-time",
        "--resource-group-id",
        "--volume",
        "--env",
        "-e",
        "--message",
    }
)
_TOKEN_SHAPES = (
    re.compile(r"\bhf_[A-Za-z0-9]{16,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?:^|[\s,])(?:[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API[_-]?KEY|CREDENTIAL|AUTHORIZATION)[A-Z0-9_]*)="
)
_REPO_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_REVISION = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
_TYPES = frozenset({"model", "dataset", "space"})


@dataclass(frozen=True, slots=True)
class _CommandPolicy:
    """One command family whose first data argument is a repository target."""

    prefix_length: int
    fixed_repo_type: str | None
    permission: str


def validate_huggingface_cli_argv(
    argv: Sequence[str], repositories: Sequence[RepositoryScope]
) -> bool:
    """Validate argv against one explicit host profile scope and return read-only status.

    Args:
        argv: Exact CLI arguments supplied by the signed tool request.
        repositories: Host-configured repository and permission allowlist.
    Returns:
        True only for a read-only command; writes require downstream reconciliation.
    """

    if not argv or not all(isinstance(item, str) and "\x00" not in item for item in argv):
        raise ValueError("CLI argv is invalid")
    if len(argv) > 128 or sum(len(item.encode("utf-8")) for item in argv) > 16_384:
        raise ValueError("CLI argv exceeds its limit")
    if _is_scope_agnostic_metadata(argv):
        _validate_common_arguments(argv, target_indices=frozenset())
        return True

    policy = _command_policy(argv)
    target, target_indices, selected_type = _repository_target(argv, policy)
    _validate_common_arguments(argv, target_indices=target_indices)
    repo_type = selected_type or policy.fixed_repo_type
    if policy.fixed_repo_type is not None and selected_type not in {
        None,
        policy.fixed_repo_type,
    }:
        raise ValueError("repository type does not match the command")
    if repo_type is None:
        matching_types = {
            item.repo_type for item in repositories if item.repo_id == target
        }
        if len(matching_types) != 1:
            raise ValueError("repository type is missing or ambiguous")
        repo_type = next(iter(matching_types))
    required = policy.permission
    if not any(
        item.repo_type == repo_type
        and item.repo_id == target
        and required in item.permissions
        for item in repositories
    ):
        raise ValueError("repository is outside the host profile scope")
    return required == "read"


def _command_policy(argv: Sequence[str]) -> _CommandPolicy:
    """Return a narrow repository-targeted command policy or reject the command."""

    lowered = [item.casefold() for item in argv]
    root = lowered[0]
    if root in _BLOCKED_ROOTS or root in _BLOCKED_COMMANDS:
        raise ValueError("CLI command is not available")
    if len(lowered) < 2:
        raise ValueError("CLI command must identify a scoped resource")
    action = lowered[1]

    if root == "models" and action in {"info", "card", "list", "ls"}:
        return _CommandPolicy(2, "model", "read")
    if root == "datasets" and action in {"info", "card", "list", "ls", "parquet"}:
        return _CommandPolicy(2, "dataset", "read")
    if root == "spaces" and action in {"info", "card"}:
        return _CommandPolicy(2, "space", "read")
    if root == "spaces" and action == "dev-mode":
        return _CommandPolicy(2, "space", "write")
    if root == "repos" and action in {"delete", "delete-files", "settings"}:
        return _CommandPolicy(2, None, "write")
    if root == "repos" and action == "branch" and len(lowered) > 2:
        if lowered[2] in {"create", "delete"}:
            return _CommandPolicy(3, None, "write")
        if lowered[2] == "list":
            return _CommandPolicy(3, None, "read")
    if root == "repos" and action == "tag" and len(lowered) > 2:
        if lowered[2] in {"create", "delete"}:
            return _CommandPolicy(3, None, "write")
        if lowered[2] == "list":
            return _CommandPolicy(3, None, "read")
    if root == "repo-files" and action == "delete":
        return _CommandPolicy(2, None, "write")
    raise ValueError("CLI command has no supported profile-scoped resource mapping")


def _repository_target(
    argv: Sequence[str], policy: _CommandPolicy
) -> tuple[str, frozenset[int], str | None]:
    """Resolve one repository from command position or an explicit repo flag."""

    positional: tuple[str, int, str | None] | None = None
    explicit_repositories: list[tuple[str | None, str, int]] = []
    explicit_types: set[str] = set()
    value_flags = _VALUE_FLAGS | (
        {"--gated"}
        if len(argv) > 1 and argv[0].casefold() == "repos" and argv[1].casefold() == "settings"
        else set()
    )
    index = policy.prefix_length
    while index < len(argv):
        value = argv[index]
        name, separator, inline = value.partition("=")
        lowered_name = name.casefold()
        if value.startswith("-"):
            if lowered_name in _REPO_FLAGS | _TYPE_FLAGS:
                flag_value = inline if separator else _following_value(argv, index)
                flag_index = index if separator else index + 1
                if lowered_name in _REPO_FLAGS:
                    parsed = _parse_repo_locator(flag_value)
                    if parsed is None:
                        raise ValueError("repository flag is invalid")
                    explicit_repositories.append((parsed[0], parsed[1], flag_index))
                else:
                    repo_type = _repository_type(flag_value)
                    explicit_types.add(repo_type)
                index += 1 if separator else 2
                continue
            if separator or lowered_name not in value_flags:
                index += 1
                continue
            _following_value(argv, index)
            index += 2
            continue
        parsed = _parse_repo_locator(value)
        if positional is None:
            if parsed is None:
                raise ValueError("command requires an explicit repository identifier")
            positional = (parsed[0], parsed[1], index)
        index += 1

    if len(explicit_types) > 1:
        raise ValueError("repository type is ambiguous")
    selected_type = next(iter(explicit_types), None)
    candidates = list(explicit_repositories)
    if positional is not None:
        candidates.append(positional)
    if not candidates:
        raise ValueError("command requires one explicit repository identifier")
    normalized = {(candidate_type, repo_id) for candidate_type, repo_id, _ in candidates}
    if len(normalized) != 1:
        raise ValueError("command contains ambiguous repository identifiers")
    candidate_type, repo_id = next(iter(normalized))
    if candidate_type is not None:
        if selected_type is not None and selected_type != candidate_type:
            raise ValueError("repository URL type conflicts with the type flag")
        selected_type = candidate_type
    indices = frozenset(item[2] for item in candidates)
    return repo_id, indices, selected_type


def _parse_repo_locator(value: str) -> tuple[str | None, str] | None:
    """Parse a repo ID or an official Hub URL without accepting host ambiguity."""

    if value.startswith("hf://"):
        raw = value.removeprefix("hf://")
        if "?" in raw or "#" in raw:
            raise ValueError("Hub URI query and fragment are not supported")
        parts = raw.split("/")
        repo_type = _pop_repo_type(parts)
        if len(parts) < 2:
            raise ValueError("Hub URI must identify one repository")
        repo_owner, repo_name = parts[:2]
        extras = parts[2:]
        revision = None
        if "@" in repo_name:
            repo_name, revision = repo_name.split("@", 1)
        elif extras and extras[0].startswith("@"):
            revision = extras.pop(0)[1:]
        if revision is not None:
            revision = unquote(revision)
            if not revision or _REVISION.fullmatch(revision) is None:
                raise ValueError("Hub URI revision is invalid")
        _validate_path_segments(extras)
        repo_id = f"{repo_owner}/{repo_name}"
        return _validated_repo_id(repo_type, repo_id)

    if value.startswith(("https://", "http://", "//")):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"huggingface.co", "www.huggingface.co", "hf.co"}
            or parsed.username
            or parsed.password
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Hub URL host or query is not allowed")
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if parts and parts[0].casefold() in {"api", "api-inference", "inference-api"}:
            raise ValueError("Hub API URLs are not repository identifiers")
        repo_type = _pop_repo_type(parts)
        if len(parts) < 2:
            raise ValueError("Hub URL must identify one repository")
        repo_id = "/".join(parts[:2])
        _validate_hub_path(parts[2:])
        return _validated_repo_id(repo_type, repo_id)

    if _REPO_ID.fullmatch(value):
        return None, value
    if "://" in value or value.startswith("//"):
        raise ValueError("external endpoints are not repository identifiers")
    return None


def _pop_repo_type(parts: list[str]) -> str | None:
    """Remove an optional type prefix from a URL or Hub URI path."""

    if not parts:
        return None
    aliases = {
        "models": "model",
        "model": "model",
        "datasets": "dataset",
        "dataset": "dataset",
        "spaces": "space",
        "space": "space",
    }
    repo_type = aliases.get(parts[0].casefold())
    if repo_type is not None:
        parts.pop(0)
    return repo_type


def _validate_hub_path(parts: Sequence[str]) -> None:
    """Accept only standard repository tree/blob URL suffixes."""

    if not parts:
        return
    if len(parts) < 2 or parts[0].casefold() not in {"tree", "blob", "resolve"}:
        raise ValueError("Hub URL path is not a repository path")
    if any(not part or part in {".", ".."} or "\\" in part for part in parts[1:]):
        raise ValueError("Hub URL path is invalid")


def _validate_path_segments(parts: Sequence[str]) -> None:
    """Reject encoded traversal while keeping an HF URI within its repository."""

    decoded = [unquote(part) for part in parts]
    if any(not part or part in {".", ".."} or "\\" in part for part in decoded):
        raise ValueError("Hub URI path is invalid")


def _validated_repo_id(repo_type: str | None, repo_id: str) -> tuple[str | None, str]:
    """Validate a path-derived namespace/repository pair."""

    if _REPO_ID.fullmatch(repo_id) is None:
        raise ValueError("Hub repository identifier is invalid")
    if repo_type is not None and repo_type not in _TYPES:
        raise ValueError("Hub repository type is invalid")
    return repo_type, repo_id


def _repository_type(value: str) -> str:
    """Normalize one explicit model, dataset, or Space type selector."""

    normalized = value.casefold()
    aliases = {"models": "model", "datasets": "dataset", "spaces": "space"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in _TYPES:
        raise ValueError("repository type is not supported")
    return normalized


def _following_value(argv: Sequence[str], index: int) -> str:
    """Return one explicit flag value or reject an incomplete option."""

    if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
        raise ValueError("CLI option value is missing")
    return argv[index + 1]


def _validate_common_arguments(argv: Sequence[str], *, target_indices: frozenset[int]) -> None:
    """Reject credential, file, proxy, and external-host arguments."""

    lowered = [item.casefold() for item in argv]
    for index, item in enumerate(argv):
        name = item.partition("=")[0].casefold()
        if name in _BLOCKED_FLAGS or name.startswith("--proxy"):
            raise ValueError("CLI option is not available")
        if name.startswith("--config"):
            raise ValueError("CLI configuration overrides are not available")
        if item == "--":
            raise ValueError("option passthrough is not available")
        if any(pattern.search(item) for pattern in _TOKEN_SHAPES):
            raise ValueError("credential-shaped CLI arguments are not available")
        if _CREDENTIAL_ASSIGNMENT.search(item):
            raise ValueError("credential-valued CLI arguments are not available")
        if index in target_indices:
            continue
        if item.startswith(("http://", "https://", "//", "hf://")) or "://" in item:
            raise ValueError("arbitrary endpoints are not available")
        if item.startswith(("/", "./", "../", "~/", "@")) or "=@" in item:
            raise ValueError("local file arguments are not available")
    if "--async" in lowered or "--no-wait" in lowered:
        raise ValueError("asynchronous CLI operations are not available")


def _is_scope_agnostic_metadata(argv: Sequence[str]) -> bool:
    """Allow local help/version/environment metadata without an upstream target."""

    lowered = [item.casefold() for item in argv]
    return (
        tuple(lowered) in (("--help",), ("-h",), ("--version",))
        or lowered[0] in {"version", "env"}
        or "--help" in lowered
        or "-h" in lowered
    )
