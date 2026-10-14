"""Packaging, manifest, and activation-schema checks for the new component.

中文:验证独立包清单、许可证和激活配置模式。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

PACKAGE_ROOT = Path(__file__).parents[1]


def all_provider_configuration() -> dict[str, Any]:
    """Return a no-secret activation profile for all executable provider kinds."""

    return {
        "authorization_generation": 0,
        "trusted_callers": [
            {
                "caller_workload": "exchange.integrations",
                "audience": "cloud-resources",
                "organization_id": "org-fixture",
                "workspace_id": "workspace-fixture",
                "issuer": "https://identity.example.test",
                "verification_key_ref": "cloud-resources/exchange-verification-key",
            }
        ],
        "azure_auth": {"mode": "managed_identity"},
        "gcp_auth": {"mode": "metadata"},
        "hf_token_ref": "cloud-resources/hf-read-token",
        "tailscale_auth": {
            "mode": "oauth_client",
            "client_id": "fixture-client",
            "client_secret_ref": "cloud-resources/tailscale-client-secret",
            "tailnet": "example.com",
        },
        "runpod_auth": {
            "mode": "read_only_api_key",
            "api_key_ref": "cloud-resources/runpod-api-key",
        },
        "targets": [
            {
                "target_ref": "vm-prod",
                "display_name": "Production VM",
                "provider": "azure",
                "kind": "azure.vm",
                "subscription_id": "11111111-2222-3333-4444-555555555555",
                "resource_group": "compute-prod",
                "vm_name": "worker-01",
            },
            {
                "target_ref": "gce-prod",
                "display_name": "GCE worker",
                "provider": "gcp",
                "kind": "gcp.instance",
                "project_id": "project-fixture",
                "zone": "us-central1-a",
                "instance_name": "worker-1",
            },
            {
                "target_ref": "hf-model",
                "display_name": "Hub model metadata",
                "provider": "hf",
                "kind": "hf.model",
                "repo_id": "org-fixture/model-fixture",
            },
            {
                "target_ref": "hf-dataset",
                "display_name": "Hub dataset metadata",
                "provider": "hf",
                "kind": "hf.dataset",
                "repo_id": "org-fixture/dataset-fixture",
            },
            {
                "target_ref": "hf-space",
                "display_name": "Hub Space metadata",
                "provider": "hf",
                "kind": "hf.space",
                "repo_id": "org-fixture/space-fixture",
            },
            {
                "target_ref": "device-prod",
                "display_name": "Build runner",
                "provider": "tailscale",
                "kind": "tailscale.device",
                "tailnet": "example.com",
                "device_id": "device-42",
            },
            {
                "target_ref": "pod-prod",
                "display_name": "Training pod",
                "provider": "runpod",
                "kind": "runpod.pod",
                "pod_id": "pod-42_fixture",
            },
        ],
        "hostSecretRefs": {
            "cloud-resources/exchange-verification-key": "TEST_EXCHANGE_KEY",
            "cloud-resources/hf-read-token": "TEST_HF_READ_TOKEN",
            "cloud-resources/tailscale-client-secret": "TEST_TAILSCALE_SECRET",
            "cloud-resources/runpod-api-key": "TEST_RUNPOD_READ_ONLY_KEY",
        },
    }


def test_manifest_and_activation_schema_are_package_ready() -> None:
    """The package manifest and closed schema declare all implemented provider kinds."""

    manifest = json.loads((PACKAGE_ROOT / "plugin.manifest.json").read_text())
    schema = json.loads((PACKAGE_ROOT / "configuration.schema.json").read_text())
    Draft202012Validator.check_schema(schema)

    assert manifest["id"] == "cyrene.providers.cloud-resources"
    assert manifest["licenseExpression"] == "Apache-2.0"
    assert manifest["runtime"]["entrypoint"] == (
        "cloud_resources_provider.plugin:CloudResourcesProvider"
    )
    assert {method["name"] for method in manifest["methods"]} == {"list_tools", "call_tool"}
    Draft202012Validator(schema).validate(all_provider_configuration())

    requirements = (PACKAGE_ROOT / "requirements.lock").read_text().splitlines()
    assert requirements
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+", line) for line in requirements)
    names = [line.split("==", maxsplit=1)[0].lower().replace("_", "-") for line in requirements]
    assert len(names) == len(set(names))
    assert {"grpcio", "protobuf"}.issubset(names)


def test_direct_runtime_pins_match_requirements_lock() -> None:
    """Every registry runtime dependency has the same exact source and wheelhouse pin."""

    project = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text())
    locked_versions = {
        line.split("==", maxsplit=1)[0].lower().replace("_", "-"): line.split("==", maxsplit=1)[1]
        for line in (PACKAGE_ROOT / "requirements.lock").read_text().splitlines()
    }
    vendored_distributions = {
        "cyrene-integration-contracts",
        "cyrene-plugin-runtime",
    }
    exact_dependency = re.compile(
        r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9_.+!-]+)$"
    )

    for dependency in project["project"]["dependencies"]:
        match = exact_dependency.fullmatch(dependency)
        assert match is not None, f"runtime dependency must be exactly pinned: {dependency}"
        name, version = match.groups()
        canonical_name = name.lower().replace("_", "-")
        if canonical_name in vendored_distributions:
            continue
        assert locked_versions[canonical_name] == version


def test_activation_schema_rejects_dynamic_urls_and_unimplemented_kinds() -> None:
    """Callers cannot smuggle endpoints or claim unsupported resource adapters."""

    schema = json.loads((PACKAGE_ROOT / "configuration.schema.json").read_text())
    validator = Draft202012Validator(schema)
    configuration = all_provider_configuration()
    configuration["targets"][0]["url"] = "https://attacker.example/"
    assert list(validator.iter_errors(configuration))

    configuration = all_provider_configuration()
    configuration["targets"].append(
        {
            "target_ref": "unsupported",
            "display_name": "Unsupported",
            "provider": "unlisted",
            "kind": "unlisted.resource",
        }
    )
    assert list(validator.iter_errors(configuration))
