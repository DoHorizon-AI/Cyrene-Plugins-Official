"""Select a fixed official Plugin package release set for one workflow event.

中文：按工作流事件从固定 package allowlist 中选择官方插件发布集合。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

LEGACY_PACKAGE_IDS = (
    "cyrene.tools.dataset-preparation",
    "cyrene.tools.document-parsing",
    "cyrene.tools.dataset-generation",
    "cyrene.tools.knowledge-preparation",
    "cyrene.evaluation.exact-match",
)
INTEGRATION_V1_PACKAGE_IDS = (
    "cyrene.accounts.account-center",
    "cyrene.providers.github-issues",
    "cyrene.connectors.wecom",
)
AUTHORIZATION_CLOUD_V1_PACKAGE_IDS = (
    "cyrene.providers.cloud-resources",
    "cyrene.connectors.email-notification",
    "cyrene.connectors.wecom",
)
TEAMS_CLOUD_V1_PACKAGE_IDS = (
    "cyrene.connectors.teams",
    "cyrene.providers.cloud-management",
    "cyrene.providers.huggingface",
    "cyrene.accounts.account-center",
    "cyrene.providers.github-issues",
)
OFFICE_PILOT_PACKAGE_IDS = (
    "cyrene.providers.microsoft-graph",
    "cyrene.providers.azure-devops-observation",
)
CONVERSATION_ASSISTANT_PACKAGE_IDS = (
    "cyrene.providers.model-api-connector",
    "cyrene.providers.memory-sqlite",
    "cyrene.providers.memory-pgvector",
    "cyrene.providers.knowledge-graph",
    "cyrene.providers.knowledge-retrieval",
    "cyrene.providers.speech-sensevoice",
    "cyrene.tools.media",
)
ALL_PACKAGE_IDS = LEGACY_PACKAGE_IDS + tuple(
    dict.fromkeys(
        INTEGRATION_V1_PACKAGE_IDS
        + AUTHORIZATION_CLOUD_V1_PACKAGE_IDS
        + TEAMS_CLOUD_V1_PACKAGE_IDS
        + OFFICE_PILOT_PACKAGE_IDS
        + CONVERSATION_ASSISTANT_PACKAGE_IDS
    )
)
PACKAGE_NAMES = {
    "cyrene.tools.dataset-preparation": "dataset-preparation",
    "cyrene.tools.document-parsing": "document-parsing",
    "cyrene.tools.dataset-generation": "dataset-generation",
    "cyrene.tools.knowledge-preparation": "knowledge-preparation",
    "cyrene.evaluation.exact-match": "exact-match",
    "cyrene.accounts.account-center": "account-center",
    "cyrene.providers.github-issues": "github-issues",
    "cyrene.connectors.wecom": "wecom-connector",
    "cyrene.providers.cloud-resources": "cloud-resources",
    "cyrene.connectors.teams": "teams-connector",
    "cyrene.providers.cloud-management": "cloud-management",
    "cyrene.providers.huggingface": "huggingface-provider",
    "cyrene.connectors.email-notification": "email-notification",
    "cyrene.providers.microsoft-graph": "microsoft-graph-provider",
    "cyrene.providers.azure-devops-observation": "azure-devops-observation",
    "cyrene.providers.model-api-connector": "model-api-connector",
    "cyrene.providers.memory-sqlite": "memory-sqlite",
    "cyrene.providers.memory-pgvector": "memory-pgvector",
    "cyrene.providers.knowledge-graph": "knowledge-graph",
    "cyrene.providers.knowledge-retrieval": "knowledge-retrieval",
    "cyrene.providers.speech-sensevoice": "speech-sensevoice",
    "cyrene.tools.media": "media",
}
PACKAGE_SET_CHOICES = (
    "legacy5",
    "integration-v1",
    "authorization-cloud-v1",
    "teams-cloud-v1",
    "office-pilot",
    "conversation-assistant",
    *ALL_PACKAGE_IDS,
)


def select_package_release(
    event_name: str,
    package_set: str | None,
) -> tuple[dict[str, list[dict[str, str]]], str]:
    """Return a static package matrix and publication mode for one event.

    Pushes retain the pre-existing five-package draft publication. Pull
    requests validate all registered package producers without publishing.
    Manual dispatch accepts only named sets or package IDs in the fixed
    allowlist; approved named sets publish immutable releases.
    中文：push 保持旧五包 draft 行为，PR 验证全部包而不发布；手动 dispatch
    只接受固定集合或 allowlist 中的单个 package ID。
    """

    if event_name == "push":
        selected = LEGACY_PACKAGE_IDS
        publish_mode = "draft"
    elif event_name == "pull_request":
        selected = ALL_PACKAGE_IDS
        publish_mode = "none"
    elif event_name == "workflow_dispatch":
        selection = package_set or "legacy5"
        if selection == "legacy5":
            selected = LEGACY_PACKAGE_IDS
            publish_mode = "draft"
        elif selection == "integration-v1":
            selected = INTEGRATION_V1_PACKAGE_IDS
            publish_mode = "immutable"
        elif selection == "authorization-cloud-v1":
            selected = AUTHORIZATION_CLOUD_V1_PACKAGE_IDS
            publish_mode = "immutable"
        elif selection == "teams-cloud-v1":
            selected = TEAMS_CLOUD_V1_PACKAGE_IDS
            publish_mode = "immutable"
        elif selection == "office-pilot":
            selected = OFFICE_PILOT_PACKAGE_IDS
            publish_mode = "immutable"
        elif selection == "conversation-assistant":
            selected = CONVERSATION_ASSISTANT_PACKAGE_IDS
            publish_mode = "immutable"
        elif selection in PACKAGE_NAMES:
            selected = (selection,)
            publish_mode = (
                "immutable"
                if selection
                in INTEGRATION_V1_PACKAGE_IDS
                + AUTHORIZATION_CLOUD_V1_PACKAGE_IDS
                + TEAMS_CLOUD_V1_PACKAGE_IDS
                + OFFICE_PILOT_PACKAGE_IDS
                + CONVERSATION_ASSISTANT_PACKAGE_IDS
                else "draft"
            )
        else:
            raise ValueError(
                "package_set is not in the fixed official package allowlist"
            )
    else:
        raise ValueError("unsupported workflow event for package release selection")

    return {
        "include": [
            {"name": PACKAGE_NAMES[package_id], "package_id": package_id}
            for package_id in selected
        ]
    }, publish_mode


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument(
        "--package-set",
        choices=PACKAGE_SET_CHOICES,
        default=os.environ.get("PACKAGE_SET") or "legacy5",
    )
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        matrix, publish_mode = select_package_release(args.event_name, args.package_set)
    except ValueError as error:
        print(f"PACKAGE_RELEASE_SELECTION: FAIL: {error}", file=sys.stderr)
        return 2

    outputs: dict[str, Any] = {
        "package_matrix": json.dumps(matrix, separators=(",", ":")),
        "publish_mode": publish_mode,
    }
    lines = "".join(f"{name}={value}\n" for name, value in outputs.items())
    if args.github_output is None:
        sys.stdout.write(lines)
    else:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
