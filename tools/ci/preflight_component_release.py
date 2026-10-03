"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 preflight_component_release.py                                       │
│  Module: tools.ci.preflight_component_release                            │
│  Role: Verify immutable release settings and component-scoped tag safety.│
│                                                                          │
│  模块职责：验证不可变 release 设置及组件级 tag 的发布前置条件。           │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

CONNECTION_COMPONENT_IDS = frozenset(
    {
        "cy-workspace-relay",
        "cy-workspace-connector",
        "cy-workspace-sidecar",
        "cy-workspace-frontend-bridge",
    }
)


def _get(url: str, token: str | None) -> tuple[int, dict[str, object] | None]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=20
        ) as response:
            value = json.loads(response.read().decode("utf-8"))
            if not isinstance(value, dict):
                raise TypeError("GitHub API returned a non-object response")
            return response.status, value
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return 404, None
        if error.code in {401, 403} and "immutable-releases" in url:
            raise ValueError(
                "settings credential lacks repository Administration read access"
            ) from error
        raise ValueError(f"GitHub API returned HTTP {error.code}") from error


def preflight(
    repository: str,
    release_id: str,
    channel: str,
    ref: str,
    commit: str,
    root: Path,
    component_id: str | None = None,
) -> None:
    """Fail closed unless the exact immutable release identity is publishable.

    Omitting ``component_id`` preserves the legacy repository-level tag form.
    缺少不可变发布设置、tag 无法证实不存在或 source 身份不符时拒绝发布。
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be owner/name")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("source commit must be a full lowercase Git SHA")
    if component_id is None:
        expected_release_id = f"{channel}-{commit}"
    else:
        if component_id not in CONNECTION_COMPONENT_IDS:
            raise ValueError(f"unknown connection component: {component_id}")
        expected_release_id = f"{channel}-{component_id}-{commit}"
    if release_id != expected_release_id:
        raise ValueError(
            "release ID must bind channel, component scope, and full source commit SHA"
        )
    allowed = {
        "stable": {"refs/heads/main", "refs/heads/release"},
        "preview": {"refs/heads/develop"},
    }
    if channel not in allowed or ref not in allowed[channel]:
        raise ValueError("channel does not match the exact source ref")
    owner, name = repository.split("/", 1)
    api = f"https://api.github.com/repos/{owner}/{name}"
    settings_token = os.environ.get("CYRENE_IMMUTABLE_RELEASES_READ_TOKEN")
    if not settings_token:
        raise ValueError(
            "CYRENE_IMMUTABLE_RELEASES_READ_TOKEN with Administration read is required"
        )
    status, settings = _get(api + "/immutable-releases", settings_token)
    if status != 200 or settings is None or settings.get("enabled") is not True:
        raise ValueError("immutable releases are not enabled or could not be verified")
    status, release = _get(
        api + "/releases/tags/" + quote(release_id, safe="-"),
        os.environ.get("GH_TOKEN"),
    )
    if status != 404 or release is not None:
        raise ValueError("release tag already exists or absence could not be proven")
    if (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        != commit
    ):
        raise ValueError(
            "checked-out source commit differs from the release source SHA"
        )
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if origin not in {
        f"https://github.com/{repository}.git",
        f"https://github.com/{repository}",
    }:
        raise ValueError(
            "source checkout origin does not match the publisher repository"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--channel", choices=("stable", "preview"), required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--component-id")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        preflight(
            args.repository,
            args.release_id,
            args.channel,
            args.ref,
            args.commit,
            args.root.resolve(),
            args.component_id,
        )
    except (
        OSError,
        TypeError,
        ValueError,
        subprocess.CalledProcessError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as error:
        print(f"release preflight failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
