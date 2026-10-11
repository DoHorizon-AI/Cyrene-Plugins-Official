"""Contract tests for immutable data-tools Plugin package releases.

中文：验证五个官方插件包、运行时资产和无 PYTHONPATH 启动布局。
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path

from tooling.release import official_plugin_package as release
from tooling.release import training_llama_factory_package as training_release


def _write_preparer_wheel(path: Path) -> Path:
    metadata_root = "cyrene_plugin_runtime-0.2.1.dist-info"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            f"{metadata_root}/METADATA",
            "Metadata-Version: 2.1\nName: cyrene-plugin-runtime\nVersion: 0.2.1\n",
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


def _create_wheelhouse_source(repository: Path, spec, destination: Path) -> Path:
    """Create tiny identity-valid test wheels for the committed exact lock."""

    destination.mkdir(parents=True, exist_ok=True)
    lock = repository / spec.source_dir / "requirements.lock"
    for line in lock.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = release.LOCK_PIN.fullmatch(line)
        if match is None:
            raise AssertionError(f"test package lock is not exact: {line}")
        name, version = match.groups()
        filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        wheel_path = destination / filename
        dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
        with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(
                f"{dist_info}/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            )
            archive.writestr(
                f"{dist_info}/WHEEL",
                "Wheel-Version: 1.0\nGenerator: test fixture\nTag: py3-none-any\n",
            )
    return destination


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

    def test_staged_release_readback_checks_github_asset_digests_and_sizes(
        self,
    ) -> None:
        """Verify the documented GitHub CLI draft and asset JSON shape.

        中文：按 release view 实际字段核对 draft、tag、target、名称、摘要和大小。
        """

        source_sha = "a" * 40
        source_ref = "refs/heads/develop"
        package_id = "cyrene.tools.dataset-preparation"
        package_version = "0.2.0"
        spec = release.PACKAGE_SPECS[package_id]
        release_tag = f"preview-{spec.component_id}-{package_version}-{source_sha}"
        asset_paths = [
            self.temp_root / "package.zip",
            self.temp_root / "package.zip.attestation.jsonl",
        ]
        for index, path in enumerate(asset_paths):
            path.write_bytes(f"asset-{index}".encode("ascii"))
        record = {
            "isDraft": True,
            "isImmutable": False,
            "isPrerelease": True,
            "tagName": release_tag,
            "targetCommitish": source_sha,
            "assets": [
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "digest": release.training_release.sha256_file(path),
                    "state": "uploaded",
                }
                for path in asset_paths
            ],
        }
        release.verify_staged_release_readback(
            record=record,
            asset_paths=asset_paths,
            release_tag=release_tag,
            package_id=package_id,
            package_version=package_version,
            source_ref=source_ref,
            source_sha=source_sha,
            channel="preview",
        )

        mutations = (
            (
                "asset digest",
                lambda row: row["assets"][0].update(digest="sha256:" + "0" * 64),
            ),
            (
                "asset size",
                lambda row: row["assets"][0].update(size=row["assets"][0]["size"] + 1),
            ),
            (
                "unexpected asset",
                lambda row: row["assets"].append(
                    {**row["assets"][0], "name": "extra.bin"}
                ),
            ),
            ("draft flag", lambda row: row.update(isDraft=False)),
            ("immutable flag", lambda row: row.update(isImmutable=True)),
            ("target commit", lambda row: row.update(targetCommitish="b" * 40)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = deepcopy(record)
                mutate(changed)
                with self.assertRaises(release.PackageBuildError):
                    release.verify_staged_release_readback(
                        record=changed,
                        asset_paths=asset_paths,
                        release_tag=release_tag,
                        package_id=package_id,
                        package_version=package_version,
                        source_ref=source_ref,
                        source_sha=source_sha,
                        channel="preview",
                    )

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
                wheelhouse_source=_create_wheelhouse_source(
                    self.repository,
                    spec,
                    self.temp_root / f"wheelhouse-source-{index}",
                ),
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
            with zipfile.ZipFile(outputs["package"]) as archive:
                wheelhouse_manifest = json.loads(
                    archive.read("wheelhouse/manifest.json")
                )
                lock_bytes = archive.read("requirements.lock")
                self.assertEqual(
                    wheelhouse_manifest["requirementsLockSha256"],
                    training_release.sha256_bytes(lock_bytes),
                )
                self.assertEqual(
                    len(wheelhouse_manifest["wheels"]),
                    len(release._lock_lines(lock_bytes, "requirements.lock")),
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

    def test_dataset_generation_prepares_and_imports_with_uv_offline_and_empty_cache(
        self,
    ) -> None:
        """Exercise the real locked wheelhouse without network or a warm uv cache."""

        uv = shutil.which("uv")
        self.assertIsNotNone(uv, "the pinned release build environment must provide uv")
        package_id = "cyrene.tools.dataset-generation"
        output = self.temp_root / "generation-offline-release"
        artifacts = release.build_release_artifacts(
            root=self.repository,
            output_dir=output,
            package_id=package_id,
            source_sha=self.source_sha,
            source_ref="refs/heads/develop",
            channel="preview",
            run_id="12345",
            run_attempt=1,
        )
        release.verify_release_artifacts(
            root=self.repository,
            output_dir=output,
            package_id=package_id,
            source_sha=self.source_sha,
            source_ref="refs/heads/develop",
            channel="preview",
            run_id="12345",
            run_attempt=1,
        )
        package_root = self.temp_root / "generation-package"
        package_root.mkdir()
        with zipfile.ZipFile(artifacts["package"]) as archive:
            archive.extractall(package_root)

        sdk_target = self.temp_root / "preparer-wheel-install"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--target",
                str(sdk_target),
                str(artifacts["preparer_wheel"]),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        runtime_root = self.temp_root / "prepared-runtime"
        empty_uv_cache = self.temp_root / "empty-uv-cache"
        empty_uv_cache.mkdir()
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(sdk_target),
                "UV_OFFLINE": "1",
                "UV_CACHE_DIR": str(empty_uv_cache),
            }
        )
        lock_digest = training_release.sha256_file(package_root / "requirements.lock")
        prepared = subprocess.run(
            [
                sys.executable,
                "-m",
                "cyrene_plugin_runtime.dependency_preparer",
                "--package-root",
                str(package_root),
                "--runtime-root",
                str(runtime_root),
                "--lock-digest",
                lock_digest,
                "--python",
                sys.executable,
                "--uv",
                str(uv),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertEqual(
            json.loads(prepared.stdout)["protocol"],
            "cyrene.package-dependency-preparer.v1",
        )

        runtime_python = runtime_root / "bin/python"
        load = subprocess.run(
            [
                str(runtime_python),
                "-I",
                "-c",
                (
                    "import sys; "
                    f"sys.path.insert(0, {str(package_root / 'src')!r}); "
                    "import grpc, google.protobuf, dataset_generation; "
                    "print(dataset_generation.DatasetGenerationPlugin.__name__)"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(load.returncode, 0, load.stderr)
        self.assertEqual(load.stdout.strip(), "DatasetGenerationPlugin")

    def test_wheelhouse_rejects_missing_and_tampered_locked_wheels(self) -> None:
        spec = release.PACKAGE_SPECS["cyrene.tools.dataset-generation"]
        source = _create_wheelhouse_source(
            self.repository, spec, self.temp_root / "generation-wheelhouse"
        )
        lock = (self.repository / spec.source_dir / "requirements.lock").read_bytes()
        entries = release._wheelhouse_entries(
            lock, sorted(source.glob("*.whl"), key=lambda item: item.name)
        )
        missing = dict(entries)
        missing.pop(next(name for name in missing if name.endswith(".whl")))
        with self.assertRaisesRegex(release.PackageBuildError, "missing"):
            release._verify_wheelhouse_entries(missing, lock)

        tampered = dict(entries)
        wheel_name = next(name for name in tampered if name.endswith(".whl"))
        tampered[wheel_name] += b"tampered"
        with self.assertRaisesRegex(release.PackageBuildError, "digest mismatch"):
            release._verify_wheelhouse_entries(tampered, lock)

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
