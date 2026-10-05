"""Contract tests for native connection component readiness descriptors.

中文：验证连接组件发布清单只签入源码声明的本机 HTTP readiness 探针。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.ci import connection_release as release

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "DoHorizon-AI/Cyrene-Plugins-Official"
SOURCE_COMMIT = "a" * 40
CONTRACT_LOCK = {
    "repository": "DoHorizon-AI/Cyrene-Workspace",
    "commit": "c7dea28958a97ccab3a9cc3199faf0ac5819a2a5",
    "path": "governance/workspace-connection-protocols-v2.lock.json",
    "sha256": "sha256:" + "b" * 64,
}
PRODUCTION_HEALTH_DEFAULTS = {
    "cy-workspace-relay": (
        "runtime/rust/cyrene-workspace-relay/src/main.rs",
        "CYRENE_RELAY_HEALTH_ADDR",
        "127.0.0.1:18080",
    ),
    "cy-workspace-connector": (
        "runtime/rust/cyrene-workspace-connector/src/main.rs",
        "CYRENE_CONNECTOR_HEALTH_ADDR",
        "127.0.0.1:18081",
    ),
    "cy-workspace-frontend-bridge": (
        "runtime/rust/cyrene-workspace-frontend-bridge/src/main.rs",
        "CYRENE_BRIDGE_HEALTH_ADDR",
        "127.0.0.1:18082",
    ),
    "cy-workspace-sidecar": (
        "runtime/rust/cyrene-workspace-sidecar/src/main.rs",
        "CYRENE_SIDECAR_HEALTH_ADDR",
        "127.0.0.1:18083",
    ),
}


def _write_json(path: Path, value: object) -> None:
    """Write deterministic-enough JSON fixture bytes for the publisher input."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _catalog(
    component_id: str, artifact_kind: str, target_id: str
) -> dict[str, object]:
    """Create the smallest C11-shaped catalog accepted by the release builder."""

    unit = component_id.removeprefix("cy-") + ".service"
    component: dict[str, object] = {
        "componentId": component_id,
        "publisher": REPOSITORY,
        "compatibilityGroup": "workspace-product-v2",
        "dependencies": (
            [{"componentId": "cyrene-kernel", "versionRange": ">=0.1.0, <0.2.0"}]
            if component_id == "cy-workspace-sidecar"
            else []
        ),
        "restart": {"group": "single-service", "unit": unit},
        "targets": [
            {
                "targetId": target_id,
                "support": "supported",
                "artifactKind": artifact_kind,
            }
        ],
    }
    if artifact_kind == "oci-image":
        component["ociImageRepository"] = (
            "ghcr.io/dohorizon-ai/cyrene-plugins-official/" + component_id
        )
    return {
        "schemaVersion": 1,
        "publishers": [
            {
                "repository": REPOSITORY,
                "workflow": f"{REPOSITORY}/.github/workflows/component-release.yml",
            }
        ],
        "components": [component],
        "compatibilityGroups": [
            {
                "groupId": "workspace-product-v2",
                "groupVersion": "2",
                "contractApiVersion": "0.1.0",
                "wireApiVersion": "cyrene.workspace.product.v2",
                "contractLock": CONTRACT_LOCK,
                "members": [
                    {
                        "componentId": component_id,
                        "protocolVersion": "cyrene.workspace.product.v2",
                    }
                ],
            }
        ],
    }


class ConnectionReleaseHealthTests(unittest.TestCase):
    """Keep signed health metadata tied to the fixed native runtime defaults."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cyrene-connection-health-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manifest(
        self, component_id: str, *, artifact_kind: str = "native-binary"
    ) -> dict[str, object]:
        """Build one v2 manifest with the production publisher function."""

        if artifact_kind == "native-binary":
            target_id = "linux-ubuntu-22.04-x86_64-systemd"
            target = {
                "os": "linux",
                "osVersion": "22.04",
                "distribution": "ubuntu",
                "distributionVersion": "22.04",
                "architecture": "x86_64",
                "abi": "glibc-2.35",
                "runtime": "systemd",
            }
            artifact: dict[str, object] = {
                "kind": artifact_kind,
                "format": "tar.gz",
                "uri": f"https://github.com/{REPOSITORY}/releases/download/payload.tar.gz",
                "sha256": "sha256:" + "c" * 64,
                "sizeBytes": 1,
                "files": {"bin/component": "sha256:" + "d" * 64},
                "entrypoint": "bin/component",
            }
        else:
            target_id = "windows-10.0-x86_64-docker-linux"
            target = {
                "os": "windows",
                "osVersion": "10.0",
                "architecture": "x86_64",
                "runtime": "docker-desktop:linux",
            }
            artifact = {
                "kind": artifact_kind,
                "repository": (
                    "ghcr.io/dohorizon-ai/cyrene-plugins-official/" + component_id
                ),
                "digest": "sha256:" + "e" * 64,
            }

        catalog_path = self.root / "component-catalog.json"
        target_path = self.root / "target.json"
        metadata_path = self.root / "descriptor.json"
        _write_json(catalog_path, _catalog(component_id, artifact_kind, target_id))
        _write_json(target_path, target)
        _write_json(
            metadata_path,
            {
                "componentId": component_id,
                "contentDigest": artifact.get("sha256", artifact.get("digest")),
                "artifact": artifact,
                "attestationSubject": f"{component_id}.tar.gz",
            },
        )
        args = argparse.Namespace(
            repository=REPOSITORY,
            source_ref="refs/heads/develop",
            source_commit=SOURCE_COMMIT,
            channel="preview",
            release_id=release._expected_release_id(
                "preview", SOURCE_COMMIT, component_id
            ),
            component_release=True,
            component_id=component_id,
            run_id="12345",
            run_attempt=1,
            version="0.1.0",
            target=target_path,
            metadata=metadata_path,
            target_id=target_id,
            catalog=catalog_path,
            catalog_sha256=hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        )
        with patch.object(release, "_verify_protocol_lock", return_value=CONTRACT_LOCK):
            return release.create_manifest(args)

    def test_native_manifests_pin_actual_health_defaults_for_all_components(
        self,
    ) -> None:
        expected = {
            component_id: {
                "kind": "http",
                "port": int(address.rsplit(":", 1)[1]),
                "path": "/readyz",
            }
            for component_id, (_, _, address) in PRODUCTION_HEALTH_DEFAULTS.items()
        }
        self.assertEqual(release.NATIVE_HTTP_READINESS, expected)

        for component_id, (
            source_path,
            env_name,
            address,
        ) in PRODUCTION_HEALTH_DEFAULTS.items():
            with self.subTest(component_id=component_id):
                source = (ROOT / source_path).read_text(encoding="utf-8")
                self.assertRegex(
                    source,
                    re.compile(
                        rf'env\s*=\s*"{re.escape(env_name)}".*?'
                        rf'default_value\s*=\s*"{re.escape(address)}"',
                        re.DOTALL,
                    ),
                )
                self.assertRegex(source, r'(?:Some\("/readyz"\)|GET /readyz)')
                self.assertIn("health_addr.ip().is_loopback()", source)

                manifest = self._manifest(component_id)
                health = manifest["health"]
                self.assertEqual(health, expected[component_id])
                self.assertEqual(set(health), {"kind", "port", "path"})
                self.assertEqual(
                    manifest["manifestDigest"],
                    release._jcs_digest(manifest, "manifestDigest"),
                )

    def test_oci_manifest_does_not_inherit_a_native_loopback_probe(self) -> None:
        manifest = self._manifest("cy-workspace-connector", artifact_kind="oci-image")

        self.assertNotIn("health", manifest)

    def test_native_manifest_fails_closed_without_a_trusted_endpoint(self) -> None:
        with (
            patch.dict(release.NATIVE_HTTP_READINESS, {}, clear=True),
            self.assertRaisesRegex(ValueError, "no trusted HTTP readiness endpoint"),
        ):
            self._manifest("cy-workspace-connector")


if __name__ == "__main__":
    unittest.main()
