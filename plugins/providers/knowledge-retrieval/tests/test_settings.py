"""Activation configuration for signed identities and fixed bundle artifacts.

中文：验证可信调用方、主体映射、主机密钥和固定 ArtifactRef 注册表。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from knowledge_retrieval_provider.settings import (
    CONTEXT_KEY_REF,
    ConfigurationError,
    RetrievalSettings,
)

_KEY = b"\x37" * 32
_ISSUER = "https://identity.example.test"
_SUBJECT = "subject:alice"
_ORGANIZATION = "organization-1"
_WORKSPACE = "workspace-1"


def _configuration(root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    artifact_path = root / "library.zip"
    root.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"fixed artifact bytes")
    digest = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    config = {
        "artifact_root": str(root),
        "trusted_caller": {
            "caller_workload": "navigator.work",
            "audience": "knowledge-retrieval",
            "issuer": _ISSUER,
            "organization_id": _ORGANIZATION,
            "workspace_id": _WORKSPACE,
            "verification_key_ref": CONTEXT_KEY_REF,
        },
        "trusted_principals": [
            {
                "issuer": _ISSUER,
                "subject": _SUBJECT,
                "organization_id": _ORGANIZATION,
                "workspace_id": _WORKSPACE,
                "principal_refs": ["user:alice", "group:research"],
            }
        ],
        "bundle_registry": [
            {
                "library_selector": "research",
                "bundle_locator": "library.zip",
                "artifact_ref": {
                    "uri": f"artifact://sha256/{digest.removeprefix('sha256:')}",
                    "digest": digest,
                    "size_bytes": artifact_path.stat().st_size,
                    "kind": "application/zip",
                },
            }
        ],
        "hostSecretRefs": {CONTEXT_KEY_REF: "KNOWLEDGE_CALLER_CONTEXT_KEY"},
    }
    environment = {"KNOWLEDGE_CALLER_CONTEXT_KEY": _KEY.hex()}
    return config, environment


def test_settings_resolve_trusted_identity_and_fixed_artifact(tmp_path: Path) -> None:
    config, environment = _configuration(tmp_path / "artifacts")
    settings = RetrievalSettings.from_mapping(
        config, binding_id="knowledge-binding", environ=environment
    )
    assert settings.binding_id == "knowledge-binding"
    assert settings.trusted_caller.verification_key == _KEY
    assert settings.principals_for(
        issuer=_ISSUER,
        subject=_SUBJECT,
        organization_id=_ORGANIZATION,
        workspace_id=_WORKSPACE,
    ) == ("user:alice", "group:research")
    expected_digest = config["bundle_registry"][0]["artifact_ref"]["digest"]
    assert settings.bundle("research").digest == expected_digest
    with pytest.raises(ConfigurationError, match="principal mapping"):
        settings.principals_for(
            issuer=_ISSUER,
            subject="subject:unmapped",
            organization_id=_ORGANIZATION,
            workspace_id=_WORKSPACE,
        )


def test_settings_fail_closed_on_scope_escape_and_artifact_changes(tmp_path: Path) -> None:
    config, environment = _configuration(tmp_path / "artifacts")
    escaped_mapping = json.loads(json.dumps(config))
    escaped_mapping["trusted_principals"][0]["workspace_id"] = "workspace-other"
    with pytest.raises(ConfigurationError, match="escapes"):
        RetrievalSettings.from_mapping(
            escaped_mapping, binding_id="knowledge-binding", environ=environment
        )

    escaped_bundle = json.loads(json.dumps(config))
    escaped_bundle["bundle_registry"][0]["bundle_locator"] = "../outside.zip"
    with pytest.raises(ConfigurationError, match="locator"):
        RetrievalSettings.from_mapping(
            escaped_bundle, binding_id="knowledge-binding", environ=environment
        )

    wrong_size = json.loads(json.dumps(config))
    wrong_size["bundle_registry"][0]["artifact_ref"]["size_bytes"] += 1
    with pytest.raises(ConfigurationError, match="size"):
        RetrievalSettings.from_mapping(
            wrong_size, binding_id="knowledge-binding", environ=environment
        )


def test_settings_require_host_injected_key_and_closed_config(tmp_path: Path) -> None:
    config, environment = _configuration(tmp_path / "artifacts")
    with pytest.raises(ConfigurationError, match="key"):
        RetrievalSettings.from_mapping(
            config,
            binding_id="knowledge-binding",
            environ={"KNOWLEDGE_CALLER_CONTEXT_KEY": "short"},
        )

    extra_field = {**config, "arbitrary_path": "/tmp/private.zip"}
    with pytest.raises(ConfigurationError, match="required"):
        RetrievalSettings.from_mapping(
            extra_field, binding_id="knowledge-binding", environ=environment
        )


def test_settings_load_standard_plugin_environment(tmp_path: Path) -> None:
    config, environment = _configuration(tmp_path / "artifacts")
    source = {
        "CYRENE_CAPABILITY_BINDING_ID": "knowledge-binding",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(config),
        **environment,
    }
    settings = RetrievalSettings.from_environment(source)
    assert settings.binding_id == "knowledge-binding"
    assert settings.bundle_registry.keys() == {"research"}

    with pytest.raises(ConfigurationError, match="binding ID"):
        RetrievalSettings.from_environment(
            {"CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(config), **environment}
        )
