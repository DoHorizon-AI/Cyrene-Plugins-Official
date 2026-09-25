"""Tests for the mechanism-neutral Plugin activation configuration seam.

中文：与具体机制无关的 Plugin 激活配置接口测试。"""

from __future__ import annotations

import json

import pytest
from cyrene_plugin_runtime.configuration import read_environment_settings


def test_reads_generic_configuration_and_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = json.dumps({"endpoint": "https://example.invalid", "enabled": "true"})
    monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", "binding-main")
    monkeypatch.setenv("CYRENE_CAPABILITY_CONFIGURATION_JSON", encoded)

    assert read_environment_settings() == {
        "binding_id": "binding-main",
        "config": encoded,
    }


def test_rejects_configuration_without_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CYRENE_CAPABILITY_CONFIGURATION_JSON", "{}")

    with pytest.raises(ValueError, match="CYRENE_CAPABILITY_BINDING_ID"):
        read_environment_settings()


def test_rejects_non_object_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", "binding-main")
    monkeypatch.setenv("CYRENE_CAPABILITY_CONFIGURATION_JSON", "[]")

    with pytest.raises(TypeError, match="JSON object"):
        read_environment_settings()
