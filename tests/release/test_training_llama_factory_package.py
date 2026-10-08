"""
Contract tests for the immutable training Plugin release producer.

中文：验证训练 Plugin 不可变发布产物生成器的输入归属、摘要和 target。
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from tooling.release import training_llama_factory_package as release


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode("utf-8") if isinstance(content, str) else content
    path.write_bytes(payload)


def _manifest() -> dict[str, object]:
    return {
        "id": release.PACKAGE_ID,
        "version": "0.1.0",
        "capabilities": [release.CAPABILITY_ID],
        "methods": [
            {"name": "inspect", "interfaceVersion": "1"},
            {"name": "compile", "interfaceVersion": "1"},
            {"name": "parse_event", "interfaceVersion": "1"},
        ],
        "runtime": {
            "language": "python",
            "entrypoint": "llama_factory:LlamaFactoryTrainingPlugin",
            "protocol": "cyrene.plugin.runtime.v1.DirectPluginRuntime",
            "launch": {
                "executable": "prepared-runtime",
                "args": [
                    "-B",
                    "src/cyrene_plugin_runtime/bootstrap.py",
                    "--entrypoint",
                    "llama_factory:LlamaFactoryTrainingPlugin",
                ],
            },
        },
    }


def _create_repository(root: Path) -> None:
    plugin_root = root / release.PACKAGE_SOURCE
    _write(root / "LICENSE", "Apache License\n")
    _write(plugin_root / "README.md", "Training capability.\n")
    _write(plugin_root / "PROVENANCE.md", "Source-owned plugin.\n")
    _write(plugin_root / "plugin.manifest.json", json.dumps(_manifest()))
    _write(plugin_root / "llama_factory.py", "class LlamaFactoryTrainingPlugin: pass\n")
    _write(
        plugin_root / "contracts/v1/schema.json",
        '{"title":"training.llama-factory.v1"}\n',
    )
    _write(
        root / release.LOCK_SOURCE,
        "# Runtime dependencies\ngrpcio==1.62.3\nprotobuf==4.25.9\n",
    )
    _write(
        root / release.BUILD_LOCK_SOURCE,
        "\n".join(release.EXPECTED_BUILD_LOCK_LINES) + "\n",
    )
    _write(root / release.PRODUCER_PATH, "# source-bound producer fixture\n")
    _write(
        root / f"{release.RUNTIME_PROJECT}/pyproject.toml",
        '[project]\nname = "cyrene-plugin-runtime"\nversion = "0.2.0"\n',
    )
    runtime_root = root / release.RUNTIME_SOURCE
    _write(runtime_root / "__init__.py", '"""Runtime package."""\n')
    _write(
        runtime_root / "bootstrap.py", "from cyrene_plugin_runtime.server import main\n"
    )
    _write(runtime_root / "server.py", "def main(): return 0\n")
    _write(runtime_root / "dependency_preparer.py", "def main(): return 0\n")
    _write(runtime_root / "py.typed", b"")


def _create_preparer_wheel(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata_root = "cyrene_plugin_runtime-0.2.0.dist-info"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            f"{metadata_root}/METADATA",
            "Metadata-Version: 2.1\nName: cyrene-plugin-runtime\nVersion: 0.2.0\n",
        )
        archive.writestr(
            f"{metadata_root}/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n"
        )
        archive.writestr(
            f"{metadata_root}/entry_points.txt",
            "[console_scripts]\ncyrene-plugin-python-preparer = "
            "cyrene_plugin_runtime.dependency_preparer:main\n",
        )
        archive.writestr(
            "cyrene_plugin_runtime/dependency_preparer.py", "def main(): return 0\n"
        )
    return path


class TrainingPluginReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cyrene-release-test-")
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        _create_repository(self.root)
        self.wheel = _create_preparer_wheel(
            Path(self.temporary.name) / release.RUNTIME_WHEEL_NAME
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build(self, output_name: str, source_sha: str = "a" * 40) -> dict[str, Path]:
        return release.build_release_artifacts(
            root=self.root,
            output_dir=Path(self.temporary.name) / output_name,
            source_sha=source_sha,
            source_ref="refs/heads/develop",
            channel="preview",
            target=release.TARGET_ID,
            preparer_wheel=self.wheel,
        )

    def test_workflow_assets_match_current_source_owned_package_version(self) -> None:
        """Release the actual built version after a bump. | 发布文件名与源码版本一致。"""

        repository = Path(__file__).resolve().parents[2]
        actual_manifest = json.loads(
            (repository / release.PACKAGE_SOURCE / "plugin.manifest.json").read_text()
        )
        fixture_manifest = _manifest()
        fixture_manifest["version"] = actual_manifest["version"]
        _write(
            self.root / release.PACKAGE_SOURCE / "plugin.manifest.json",
            json.dumps(fixture_manifest),
        )
        outputs = self._build("current-version")
        workflow = (repository / release.WORKFLOW_PATH).read_text()
        published_names = {
            name.replace("${TRAINING_PACKAGE_VERSION}", str(actual_manifest["version"]))
            for name in re.findall(
                r'^\s+"(?:\$PUBLISH_DIR/)?(cyrene[^"\n]+)"$', workflow, re.MULTILINE
            )
        }
        self.assertEqual(published_names, {path.name for path in outputs.values()})

    def test_release_is_deterministic_and_matches_package_runtime_descriptor(
        self,
    ) -> None:
        first = self._build("first")
        second = self._build("second")
        first_bytes = {key: path.read_bytes() for key, path in first.items()}
        second_bytes = {key: path.read_bytes() for key, path in second.items()}
        self.assertEqual(first_bytes, second_bytes)

        verified = release.verify_release_artifacts(
            root=self.root,
            output_dir=first["package"].parent,
            source_sha="a" * 40,
            source_ref="refs/heads/develop",
            channel="preview",
            target=release.TARGET_ID,
        )
        descriptor = json.loads(verified["descriptor"].read_text(encoding="utf-8"))
        metadata = json.loads(verified["release_metadata"].read_text(encoding="utf-8"))
        self.assertEqual(descriptor["publication_status"], "PUBLISHED")
        self.assertEqual(
            descriptor["capability"],
            {"id": release.CAPABILITY_ID, "interface_version": "1"},
        )
        self.assertEqual(descriptor["dependencies"]["lock"]["ref"], "requirements.lock")
        self.assertEqual(
            descriptor["dependencies"]["lock"]["digest"],
            release.sha256_bytes(verified["dependency_lock"].read_bytes()),
        )
        self.assertEqual(metadata["target"]["id"], "linux-x86_64")
        self.assertEqual(metadata["source"]["commit"], "a" * 40)
        self.assertEqual(
            metadata["assets"]["preparer_wheel"]["entrypoint"],
            "cyrene-plugin-python-preparer",
        )
        self.assertEqual(
            metadata["runtime"]["preparer_configuration"]["host_api"],
            "cy-package-runtime::CommandDependencyPreparer",
        )
        self.assertEqual(
            metadata["runtime"]["preparer_wheel_install_command"],
            [
                "<python >=3.11 executable>",
                "-m",
                "pip",
                "install",
                "--no-deps",
                "<verified preparer wheel path>",
            ],
        )
        with zipfile.ZipFile(verified["package"]) as archive:
            self.assertEqual(
                archive.read("requirements.lock"),
                verified["dependency_lock"].read_bytes(),
            )
            self.assertIn("src/cyrene_plugin_runtime/bootstrap.py", archive.namelist())
            self.assertIn("src/llama_factory.py", archive.namelist())
            self.assertNotIn("llama_factory.py", archive.namelist())

    def test_source_sha_binds_metadata_without_changing_identical_package_bytes(
        self,
    ) -> None:
        first = self._build("source-a", "a" * 40)
        second = self._build("source-b", "b" * 40)
        self.assertEqual(first["package"].read_bytes(), second["package"].read_bytes())
        self.assertNotEqual(
            first["descriptor"].read_bytes(), second["descriptor"].read_bytes()
        )
        self.assertNotEqual(
            first["release_metadata"].read_bytes(),
            second["release_metadata"].read_bytes(),
        )
        metadata = json.loads(second["release_metadata"].read_text(encoding="utf-8"))
        self.assertEqual(metadata["source"]["commit"], "b" * 40)
        self.assertEqual(
            metadata["release_tag"], f"preview-{release.PACKAGE_ID}-0.1.0-{'b' * 40}"
        )

    def test_only_the_training_plugin_and_linux_x86_64_target_are_accepted(
        self,
    ) -> None:
        manifest_path = self.root / f"{release.PACKAGE_SOURCE}/plugin.manifest.json"
        manifest = _manifest()
        manifest["capabilities"] = ["execution.engine.v1"]
        _write(manifest_path, json.dumps(manifest))
        with self.assertRaisesRegex(
            release.PackageBuildError, "only training.llama-factory.v1"
        ):
            self._build("wrong-owner")

        _write(manifest_path, json.dumps(_manifest()))
        with self.assertRaisesRegex(
            release.PackageBuildError, "unsupported deployment target"
        ):
            release.build_release_artifacts(
                root=self.root,
                output_dir=Path(self.temporary.name) / "wrong-target",
                source_sha="a" * 40,
                source_ref="refs/heads/develop",
                channel="preview",
                target="linux-aarch64",
                preparer_wheel=self.wheel,
            )

    def test_candidate_package_does_not_claim_a_published_artifact(self) -> None:
        candidate = release.build_release_artifacts(
            root=self.root,
            output_dir=Path(self.temporary.name) / "candidate",
            source_sha="c" * 40,
            source_ref="refs/pull/123/merge",
            channel="candidate",
            target=release.TARGET_ID,
            preparer_wheel=self.wheel,
        )
        descriptor = json.loads(candidate["descriptor"].read_text(encoding="utf-8"))
        metadata = json.loads(candidate["release_metadata"].read_text(encoding="utf-8"))
        self.assertEqual(descriptor["publication_status"], "CANDIDATE")
        self.assertEqual(
            descriptor["implementation"]["artifact"], {"status": "NOT_PUBLISHED"}
        )
        self.assertIsNone(metadata["release_tag"])


if __name__ == "__main__":
    unittest.main()
