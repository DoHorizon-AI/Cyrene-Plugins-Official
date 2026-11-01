"""Media caller, bridge, and state binding configuration tests.

The signed subject is supplied per call and the bridge token has binding scope.
中文：subject由每次签名请求提供，bridge token只标识provider binding。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cyrene_media.settings import ConfigurationError, MediaSettings


def _activation(state_dir: Path) -> dict[str, object]:
    """Return one complete local binding configuration."""

    return {
        "trusted_callers": [
            {
                "caller_workload": "cyrene-navigator",
                "audience": "media-library",
                "organization_id": "org-1",
                "workspace_id": "workspace-1",
                "issuer": "https://identity.local",
                "verification_key_ref": "media-library/caller-verification-key",
            }
        ],
        "artifact_bridge_origin": "http://127.0.0.1:18680",
        "timeout_seconds": 5,
        "hostSecretRefs": {
            "media-library/caller-verification-key": "MEDIA_CALLER_KEY",
            "media-library/state-dir": "MEDIA_STATE_DIR",
            "media-library/artifact-bridge-token": "MEDIA_ARTIFACT_BRIDGE_TOKEN",
        },
    }


def _set_activation(
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
    config: dict[str, object] | None = None,
) -> None:
    """Set standard DirectPluginRuntime configuration and actor secrets."""

    monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", "media-binding-1")
    monkeypatch.setenv(
        "CYRENE_CAPABILITY_CONFIGURATION_JSON",
        json.dumps(config or _activation(state_dir)),
    )
    monkeypatch.setenv("MEDIA_CALLER_KEY", "11" * 32)
    monkeypatch.setenv("MEDIA_STATE_DIR", str(state_dir))
    monkeypatch.setenv("MEDIA_ARTIFACT_BRIDGE_TOKEN", "22" * 32)


def test_binding_pins_workload_workspace_and_bridge_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "media-state"
    _set_activation(monkeypatch, state_dir)

    settings = MediaSettings.from_environment()

    caller = settings.trusted_callers[0]
    assert caller.workspace_id == "workspace-1"
    assert caller.verification_key == bytes.fromhex("11" * 32)
    assert settings.artifact_resolver._bridge_token == "22" * 32
    assert (
        len(
            settings.actor_scope_key(
                issuer=caller.issuer,
                subject="https://identity.local/subjects/actor-1",
                organization_id=caller.organization_id,
                workspace_id=caller.workspace_id,
            )
        )
        == 32
    )
    assert state_dir.exists()


def test_binding_fails_closed_when_bridge_token_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_activation(monkeypatch, tmp_path / "media-state")
    monkeypatch.delenv("MEDIA_ARTIFACT_BRIDGE_TOKEN")

    with pytest.raises(ConfigurationError, match="bridge token"):
        MediaSettings.from_environment()


def test_binding_rejects_unknown_caller_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _activation(tmp_path / "media-state")
    caller = config["trusted_callers"][0]  # type: ignore[index]
    caller["unexpected"] = "value"  # type: ignore[index]
    _set_activation(monkeypatch, tmp_path / "media-state", config)

    with pytest.raises(ConfigurationError, match="trusted caller fields"):
        MediaSettings.from_environment()
