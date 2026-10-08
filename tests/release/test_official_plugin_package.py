"""Contract tests for immutable data-tools Plugin package releases.

中文：验证五个官方插件包、运行时资产和无 PYTHONPATH 启动布局。
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tooling.release import official_plugin_package as release
from tooling.release import training_llama_factory_package as training_release


def _write_preparer_wheel(path: Path) -> Path:
    metadata_root = "cyrene_plugin_runtime-0.2.0.dist-info"
    path.parent.mkdir(parents=True, exist_ok=True)
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


class OfficialPluginPackageTests(unittest.TestCase):
    """Exercise the five official packages and the existing training template."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = Path(__file__).resolve().parents[2]
        cls.source_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cls.repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="cyrene-official-plugin-package-"
        )
        self.temp_root = Path(self.temporary.name)
        self.wheel = _write_preparer_wheel(
            self.temp_root / training_release.RUNTIME_WHEEL_NAME
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_five_packages_and_training_template_start_with_pythonpath_unset(
        self,
    ) -> None:
        """Require clean startup from each ZIP's ``src`` tree.

        中文：清除 PYTHONPATH 并切换到无关工作目录，验证所有入口可启动。
        """

        cases: list[tuple[str, Path, str, str]] = []
        expected_target = {
            "os": "linux",
            "osVersion": "24.04",
            "distribution": "ubuntu",
            "distributionVersion": "24.04",
            "architecture": "x86_64",
            "abi": "glibc-2.39",
            "runtime": "python:3.12",
        }
        self.assertEqual(release.TARGET, expected_target)
        for index, package_id in enumerate(sorted(release.PACKAGE_SPECS)):
            spec = release.PACKAGE_SPECS[package_id]
            output_dir = self.temp_root / f"package-{index}"
            outputs = release.build_release_artifacts(
                root=self.repository,
                output_dir=output_dir,
                package_id=package_id,
                source_sha=self.source_sha,
                source_ref="refs/heads/develop",
                channel="preview",
                run_id="12345",
                run_attempt=1,
                preparer_wheel=self.wheel,
            )
            release.verify_release_artifacts(
                root=self.repository,
                output_dir=output_dir,
                package_id=package_id,
                source_sha=self.source_sha,
                source_ref="refs/heads/develop",
                channel="preview",
                run_id="12345",
                run_attempt=1,
            )
            manifest = json.loads(
                (self.repository / spec.source_dir / "plugin.manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            component_manifest = json.loads(
                outputs["component_manifest"].read_text(encoding="utf-8")
            )
            package_release = json.loads(
                outputs["release_metadata"].read_text(encoding="utf-8")
            )
            self.assertEqual(component_manifest["target"], expected_target)
            self.assertEqual(
                component_manifest["releaseId"],
                f"preview-{spec.component_id}-{manifest['version']}-{self.source_sha}",
            )
            self.assertEqual(
                package_release["attestation_policy"]["provider"], "github-actions"
            )
            cases.append(
                (
                    f"package {package_id}",
                    outputs["package"],
                    manifest["runtime"]["entrypoint"],
                    manifest["capabilities"][0],
                )
            )

        training_output = self.temp_root / "training-template"
        training_outputs = training_release.build_release_artifacts(
            root=self.repository,
            output_dir=training_output,
            source_sha=self.source_sha,
            source_ref="refs/heads/develop",
            channel="preview",
            target=training_release.TARGET_ID,
            preparer_wheel=self.wheel,
        )
        training_manifest = json.loads(
            (
                self.repository
                / training_release.PACKAGE_SOURCE
                / "plugin.manifest.json"
            ).read_text(encoding="utf-8")
        )
        cases.append(
            (
                "existing training template",
                training_outputs["package"],
                training_manifest["runtime"]["entrypoint"],
                training_manifest["capabilities"][0],
            )
        )

        for name, archive_path, entrypoint, capability in cases:
            with self.subTest(name=name):
                launch_root = (
                    self.temp_root / f"launch-{len(list(self.temp_root.iterdir()))}"
                )
                payload = launch_root / "payload"
                payload.mkdir(parents=True)
                with zipfile.ZipFile(archive_path) as archive:
                    names = archive.namelist()
                    module = entrypoint.split(":", 1)[0].replace(".", "/")
                    self.assertIn(f"src/{module}.py", names)
                    self.assertNotIn(f"{module}.py", names)
                    archive.extractall(payload)
                self._assert_clean_start(
                    name=name,
                    payload=payload,
                    entrypoint=entrypoint,
                    capability=capability,
                )

    def _assert_clean_start(
        self, *, name: str, payload: Path, entrypoint: str, capability: str
    ) -> None:
        bootstrap = payload / "src/cyrene_plugin_runtime/bootstrap.py"
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        self.assertNotIn("PYTHONPATH", environment)
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(bootstrap),
                "--entrypoint",
                entrypoint,
                "--capability",
                capability,
                "--interface-version",
                "1",
                "--listen",
                "127.0.0.1:0",
            ],
            cwd=self.temp_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            if not selector.select(timeout=20):
                process.terminate()
                try:
                    _stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _stdout, stderr = process.communicate(timeout=5)
                self.fail(
                    f"{name} did not become ready; exit={process.returncode}, stderr={stderr}"
                )
            ready_line = process.stdout.readline()
            if not ready_line:
                self.fail(
                    f"{name} exited before reporting readiness: {process.stderr.read()}"
                )
            ready = json.loads(ready_line)
            self.assertEqual(ready["event"], "direct_plugin_ready")
            self.assertEqual(ready["capability"], capability)
            self.assertTrue(ready["connection_ref"].startswith("grpc://127.0.0.1:"))
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
