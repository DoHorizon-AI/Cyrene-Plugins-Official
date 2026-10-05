"""Contract tests for the vLLM HTTP package release producer.

中文：验证 vLLM HTTP package 发布输入、锁文件、目标和摘要。
"""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tooling.release import vllm_runtime_package as release


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode("utf-8") if isinstance(content, str) else content
    path.write_bytes(payload)


def _manifest() -> dict[str, object]:
    return {
        "id": release.PACKAGE_ID,
        "name": "vLLM Serving Runtime",
        "version": release.PACKAGE_VERSION,
        "capabilities": [release.CAPABILITY_ID],
        "supportedServices": ["Cyrene-Reactor"],
        "methods": [
            {"name": name, "interfaceVersion": "1", "executionMode": "service"}
            for name in ("import_model", "start", "inspect", "stop", "diagnostics")
        ],
        "runtime": {
            "language": "python",
            "entrypoint": release.ENTRYPOINT,
            "protocol": "cyrene.serving.runtime.http.v1",
            "transport": "http",
            "connectionRefScheme": "cyrene-http-v1",
            "launch": {
                "executable": "prepared-runtime",
                "args": ["-B", "package_service_adapter.py"],
            },
        },
    }


def _create_plugins_repository(root: Path) -> None:
    package_root = root / release.PACKAGE_SOURCE
    _write(root / "LICENSE", "Apache License\n")
    for relative in release.EXPECTED_PACKAGE_PATHS:
        content = json.dumps(_manifest()) + "\n" if relative == "plugin.manifest.json" else f"{relative}\n"
        _write(package_root / relative, content)
    _write(
        package_root / "pyproject.toml",
        "[project]\nname = 'fixture'\nversion = '0.1.0'\n"
        f"dependencies = ['{release.ARTIFACT_PROJECT}']\n",
    )
    _write(root / release.LOCK_SOURCE, "# offline runtime lock\ncyrene-artifacts==0.1.0\n")
    _write(root / release.BUILD_LOCK_SOURCE, "\n".join(release.BUILD_LOCK_LINES) + "\n")
    _write(root / release.PRODUCER_PATH, "# producer fixture\n")
    _write(root / release.TEST_SOURCE, "# release test fixture\n")


def _create_platform_source(root: Path) -> None:
    _write(root / "repository-policy.yaml", "artifacts:\n  package_units:\n    - cyrene-artifacts\n")
    _write(root / "tooling/release/build_python_sdks.py", "def build_packages(): pass\n")
    _write(root / "sdk/python/cyrene_artifacts/README.md", "Artifact SDK.\n")
    _write(
        root / "sdk/python/cyrene_artifacts/pyproject.toml",
        "[project]\nname = 'cyrene-artifacts'\nversion = '0.1.0'\n",
    )
    _write(root / "sdk/python/cyrene_artifacts/src/cy_artifacts/__init__.py", "from .local import LocalArtifactProvider\n")
    _write(root / "sdk/python/cyrene_artifacts/src/cy_artifacts/contracts.py", "class ArtifactRef: pass\n")
    _write(root / "sdk/python/cyrene_artifacts/src/cy_artifacts/local.py", "class LocalArtifactProvider: pass\n")


def _create_artifact_wheel(
    path: Path,
    platform_root: Path,
    *,
    name: str = "cyrene-artifacts",
    version: str = "0.1.0",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = "cyrene_artifacts"
    dist_info = f"{normalized}-{version}.dist-info"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
        module_root = platform_root / "sdk/python/cyrene_artifacts/src/cy_artifacts"
        for source in sorted(module_root.rglob("*.py")):
            archive.write(source, source.relative_to(module_root.parent).as_posix())
    return path


class VllmRuntimePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cyrene-vllm-package-test-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "plugins"
        self.platform_root = self.base / "platform"
        self.wheelhouse = self.base / "wheelhouse"
        self.root.mkdir()
        self.platform_root.mkdir()
        _create_plugins_repository(self.root)
        _create_platform_source(self.platform_root)
        self.wheel = _create_artifact_wheel(
            self.wheelhouse / release.ARTIFACT_WHEEL, self.platform_root
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build(self, name: str, source_sha: str = "a" * 40) -> dict[str, Path]:
        return release.build_release_artifacts(
            root=self.root,
            output_dir=self.base / name,
            source_sha=source_sha,
            source_ref="refs/heads/develop",
            channel="preview",
            target=release.TARGET_ID,
            artifact_wheel=self.wheel,
            platform_source_root=self.platform_root,
        )

    def test_deterministic_bundle_matches_package_spec_lock_and_pinned_sdk_source(self) -> None:
        first = self._build("first")
        second = self._build("second")
        self.assertEqual(
            {key: path.read_bytes() for key, path in first.items()},
            {key: path.read_bytes() for key, path in second.items()},
        )
        verified = release.verify_release_artifacts(
            root=self.root,
            output_dir=first["package"].parent,
            source_sha="a" * 40,
            source_ref="refs/heads/develop",
            channel="preview",
            target=release.TARGET_ID,
            platform_source_root=self.platform_root,
        )
        descriptor = json.loads(verified["descriptor"].read_text(encoding="utf-8"))
        metadata = json.loads(verified["release_metadata"].read_text(encoding="utf-8"))
        self.assertEqual(descriptor["publication_status"], "PUBLISHED")
        self.assertEqual(
            descriptor["capability"],
            {"id": release.CAPABILITY_ID, "interface_version": "1"},
        )
        self.assertIsNone(descriptor["integrity"]["signature_ref"])
        self.assertIsNone(descriptor["provenance"]["attestation_ref"])
        self.assertEqual(descriptor["dependencies"]["lock"]["ref"], "requirements.lock")
        self.assertEqual(
            descriptor["dependencies"]["lock"]["digest"],
            release.sha256_file(verified["dependency_lock"]),
        )
        sdk = metadata["external_dependencies"]["artifact_sdk"]
        self.assertEqual(sdk["source_commit"], release.PLATFORM_SOURCE_SHA)
        self.assertEqual(sdk["wheel_sha256"], release.sha256_file(verified["artifact_sdk_wheel"]))
        self.assertEqual(sdk["build_producer"]["path"], "tooling/release/build_python_sdks.py")
        self.assertEqual(
            sdk["builder_support_dependency"]["pin"], "PyYAML==6.0.1"
        )
        self.assertEqual(metadata["package"]["transport"], "http")
        self.assertEqual(metadata["package"]["metadata_endpoint"]["path"], "/metadata")
        self.assertIn("DirectPluginRuntime gRPC is not implemented", metadata["runtime"]["readiness_transport_claim"])
        with zipfile.ZipFile(verified["package"]) as archive:
            self.assertEqual(archive.read("requirements.lock"), verified["dependency_lock"].read_bytes())
            self.assertIn("package_service_adapter.py", archive.namelist())
            self.assertIn("vllm_runtime.py", archive.namelist())
            self.assertNotIn("vllm", archive.read("requirements.lock").decode().lower())
            self.assertNotIn("torch", archive.read("requirements.lock").decode().lower())

    def test_source_commit_changes_release_metadata_without_changing_package_bytes(self) -> None:
        first = self._build("source-a", "a" * 40)
        second = self._build("source-b", "b" * 40)
        self.assertEqual(first["package"].read_bytes(), second["package"].read_bytes())
        self.assertEqual(first["artifact_sdk_wheel"].read_bytes(), second["artifact_sdk_wheel"].read_bytes())
        self.assertNotEqual(first["descriptor"].read_bytes(), second["descriptor"].read_bytes())
        self.assertNotEqual(first["release_metadata"].read_bytes(), second["release_metadata"].read_bytes())
        metadata = json.loads(second["release_metadata"].read_text(encoding="utf-8"))
        self.assertEqual(metadata["source"]["commit"], "b" * 40)
        self.assertEqual(metadata["release_tag"], f"preview-{release.PACKAGE_ID}-0.1.0-{'b' * 40}")

    def test_rejects_wrong_capability_transport_lock_and_target(self) -> None:
        manifest_path = self.root / f"{release.PACKAGE_SOURCE}/plugin.manifest.json"
        manifest = _manifest()
        manifest["capabilities"] = ["training.llama-factory.v1"]
        _write(manifest_path, json.dumps(manifest))
        with self.assertRaisesRegex(release.PackageBuildError, "only execution.engine.v1"):
            self._build("wrong-capability")

        manifest = _manifest()
        manifest["runtime"]["transport"] = "grpc"
        _write(manifest_path, json.dumps(manifest))
        with self.assertRaisesRegex(release.PackageBuildError, "HTTP lifecycle adapter"):
            self._build("wrong-transport")

        _write(manifest_path, json.dumps(_manifest()))
        lock_path = self.root / release.LOCK_SOURCE
        _write(lock_path, "cyrene-artifacts>=0.1.0\n")
        with self.assertRaisesRegex(release.PackageBuildError, "only cyrene-artifacts"):
            self._build("wrong-lock")

        with self.assertRaisesRegex(release.PackageBuildError, "unsupported"):
            release.build_release_artifacts(
                root=self.root,
                output_dir=self.base / "wrong-target",
                source_sha="a" * 40,
                source_ref="refs/heads/develop",
                channel="preview",
                target="linux-aarch64",
                artifact_wheel=self.wheel,
                platform_source_root=self.platform_root,
            )

    def test_verifier_rejects_wheel_or_pinned_platform_source_mismatch(self) -> None:
        artifacts = self._build("tampered")
        source_mismatch_artifacts = self._build("source-mismatch")
        artifacts["artifact_sdk_wheel"].write_bytes(b"tampered")
        with self.assertRaisesRegex(release.PackageBuildError, "Artifact SDK wheel"):
            release.verify_release_artifacts(
                root=self.root,
                output_dir=artifacts["package"].parent,
                source_sha="a" * 40,
                source_ref="refs/heads/develop",
                channel="preview",
                target=release.TARGET_ID,
                platform_source_root=self.platform_root,
            )

        source = self.platform_root / "sdk/python/cyrene_artifacts/src/cy_artifacts/local.py"
        _write(source, "class LocalArtifactProvider: pass  # changed source\n")
        with self.assertRaisesRegex(release.PackageBuildError, "Artifact SDK wheel sources"):
            release.verify_release_artifacts(
                root=self.root,
                output_dir=source_mismatch_artifacts["package"].parent,
                source_sha="a" * 40,
                source_ref="refs/heads/develop",
                channel="preview",
                target=release.TARGET_ID,
                platform_source_root=self.platform_root,
            )


if __name__ == "__main__":
    unittest.main()
