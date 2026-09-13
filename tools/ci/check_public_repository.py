#!/usr/bin/env python3
"""Reject tracked state that would make the Plugin repository unsafe to publish."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_ROOT_FILES = (
    "README.md",
    "LICENSE",
    "LICENSING.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "repository-policy.yaml",
)
FORBIDDEN_FILE_PATTERNS = (
    re.compile(r"(^|/)\.env($|\.)"),
    re.compile(r"\.(?:key|pem|p12|pfx|jks|keystore)$", re.IGNORECASE),
    re.compile(r"(^|/)(?:id_rsa|credentials?)(?:\.[^/]*)?$", re.IGNORECASE),
)


def _repository_files() -> list[Path]:
    """Return tracked and pending files so the local gate matches the next commit."""

    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        REPOSITORY_ROOT / item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item
    ]


def _text(path: Path) -> str | None:
    """Read text for hygiene checks while deliberately ignoring binary artifacts."""

    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def main() -> None:
    """Validate public metadata, file names, local paths, and high-confidence secrets."""

    failures: list[str] = []
    for relative_path in REQUIRED_ROOT_FILES:
        if not (REPOSITORY_ROOT / relative_path).is_file():
            failures.append(f"missing public repository file: {relative_path}")

    policy = _text(REPOSITORY_ROOT / "repository-policy.yaml") or ""
    if "lifecycle_class: PUBLIC_COMPONENT_COLLECTION" not in policy:
        failures.append("repository policy is not a public component collection")
    if "ci: github" not in policy:
        failures.append("repository policy does not name GitHub as CI authority")

    workflow = _text(REPOSITORY_ROOT / ".github/workflows/public-ci.yml") or ""
    if not workflow:
        failures.append("missing public GitHub CI workflow")
    if re.search(r"actions/checkout@[^\n]+\n(?:.*\n){0,8}\s+repository:", workflow):
        failures.append("GitHub CI checks out a second repository")

    private_key_marker = "-----BEGIN " + "PRIVATE KEY-----"
    token_patterns = (
        re.compile(r"gh[pousr]_[A-Za-z0-9]{36,255}"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"sk-" + r"[A-Za-z0-9]{24,}"),
    )
    local_path_fragments = (
        "/home/" + "baijin",
        "C:" + "\\Users\\Baiji",
    )

    for path in _repository_files():
        relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
        if any(pattern.search(relative_path) for pattern in FORBIDDEN_FILE_PATTERNS):
            failures.append(f"forbidden credential-like file: {relative_path}")
        content = _text(path)
        if content is None:
            continue
        if private_key_marker in content:
            failures.append(f"private key material in {relative_path}")
        if any(pattern.search(content) for pattern in token_patterns):
            failures.append(f"credential-shaped value in {relative_path}")
        if any(fragment in content for fragment in local_path_fragments):
            failures.append(f"developer-specific absolute path in {relative_path}")

    if failures:
        raise SystemExit("\n".join(f"PUBLIC_HYGIENE: {item}" for item in failures))
    print("PUBLIC_HYGIENE: PASS")


if __name__ == "__main__":
    main()
