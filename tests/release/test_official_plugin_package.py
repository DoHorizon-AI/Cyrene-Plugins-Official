"""Contract tests for immutable official Plugin package releases.

中文：验证官方插件包、运行时资产和无 PYTHONPATH 启动布局。
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
from unittest.mock import patch

from tooling.release import official_plugin_package as release
from tooling.release import select_official_plugin_packages as selection
from tooling.release import training_llama_factory_package as training_release

LOCK_ISOLATED_STARTUP_PACKAGES = frozenset(
    {
        "cyrene.accounts.account-center",
        "cyrene.connectors.wecom",
    }
)


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
            source_dependency = next(
                (
                    dependency
                    for dependency in spec.source_dependencies
                    if dependency.distribution == name
                ),
                None,
            )
            if source_dependency is not None:
                archive.writestr(
                    f"{dist_info}/licenses/LICENSE",
                    (
                        repository / source_dependency.license_file_path
                    ).read_bytes(),
                )
    return destination


class OfficialPluginPackageTests(unittest.TestCase):
    """Exercise official packages and the existing training template."""

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

    def test_package_release_selection_preserves_push_scope_and_limits_dispatch(
        self,
    ) -> None:
        """Keep existing push drafts and resolve dispatches from fixed package IDs.

        中文：push 保持旧五包 draft，dispatch 只解析固定 package allowlist。
        """

        push_matrix, push_mode = selection.select_package_release("push", None)
        self.assertEqual(
            [row["package_id"] for row in push_matrix["include"]],
            list(selection.LEGACY_PACKAGE_IDS),
        )
        self.assertEqual(push_mode, "draft")

        default_matrix, default_mode = selection.select_package_release(
            "workflow_dispatch", None
        )
        self.assertEqual(default_matrix, push_matrix)
        self.assertEqual(default_mode, "draft")

        pull_request_matrix, pull_request_mode = selection.select_package_release(
            "pull_request", "integration-v1"
        )
        self.assertEqual(
            [row["package_id"] for row in pull_request_matrix["include"]],
            list(selection.ALL_PACKAGE_IDS),
        )
        self.assertEqual(pull_request_mode, "none")

        integration_matrix, integration_mode = selection.select_package_release(
            "workflow_dispatch", "integration-v1"
        )
        self.assertEqual(
            [row["package_id"] for row in integration_matrix["include"]],
            list(selection.INTEGRATION_V1_PACKAGE_IDS),
        )
        self.assertEqual(integration_mode, "immutable")

        account_matrix, account_mode = selection.select_package_release(
            "workflow_dispatch", "cyrene.accounts.account-center"
        )
        self.assertEqual(
            [row["package_id"] for row in account_matrix["include"]],
            ["cyrene.accounts.account-center"],
        )
        self.assertEqual(account_mode, "immutable")

        legacy_matrix, legacy_mode = selection.select_package_release(
            "workflow_dispatch", "cyrene.tools.dataset-preparation"
        )
        self.assertEqual(
            [row["package_id"] for row in legacy_matrix["include"]],
            ["cyrene.tools.dataset-preparation"],
        )
        self.assertEqual(legacy_mode, "draft")

        with self.assertRaisesRegex(ValueError, "fixed official package allowlist"):
            selection.select_package_release("workflow_dispatch", "arbitrary-package")

    def test_wecom_package_refuses_an_unlicensed_manifest(self) -> None:
        """Do not publish an independent Plugin package without its component grant."""

        spec = release.PACKAGE_SPECS["cyrene.connectors.wecom"]
        manifest = json.loads(
            (self.repository / spec.source_dir / "plugin.manifest.json").read_text(
                encoding="utf-8"
            )
        )
        manifest.pop("licenseExpression")
        with (
            patch.object(release, "_read_json", return_value=manifest),
            self.assertRaisesRegex(
                release.PackageBuildError,
                "must declare an SPDX license before release packaging",
            ),
        ):
            release._read_package_spec(self.repository, spec)

    def test_wecom_sdk_wheel_is_built_from_the_pinned_commit_with_its_mit_license(
        self,
    ) -> None:
        """Build the exact public SDK commit and keep its separate license notice."""

        spec = release.PACKAGE_SPECS["cyrene.connectors.wecom"]
        wheelhouse = self.temp_root / "wecom-source-wheelhouse"
        release._build_source_dependency_wheels(self.repository, spec, wheelhouse)
        wheels = list(wheelhouse.glob("*.whl"))
        self.assertEqual(len(wheels), 1)
        self.assertEqual(
            release._wheel_metadata(wheels[0].read_bytes(), wheels[0].name),
            ("wecom-aibot-python-sdk", "1.0.1"),
        )
        with zipfile.ZipFile(wheels[0]) as archive:
            metadata_path = next(
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            )
            metadata = archive.read(metadata_path).decode("utf-8")
            license_paths = [
                name for name in archive.namelist() if name.endswith("/LICENSE")
            ]
            self.assertIn("License-Expression: MIT", metadata)
            self.assertEqual(len(license_paths), 1)
            self.assertEqual(
                archive.read(license_paths[0]),
                (
                    self.repository / release.WECOM_AIBOT_SDK.license_file_path
                ).read_bytes(),
            )

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

    def test_published_release_readback_requires_immutable_assets_and_source_tag(
        self,
    ) -> None:
        """Verify final GitHub release identity, immutable state, and exact assets.

        中文：核验发布后的不可变状态、资产摘要与精确 source tag。
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
            "id": 101,
            "draft": False,
            "immutable": True,
            "prerelease": True,
            "tag_name": release_tag,
            "target_commitish": source_sha,
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
        tag_ref = {
            "ref": f"refs/tags/{release_tag}",
            "object": {
                "sha": source_sha,
                "type": "commit",
                "url": "https://api.github.com/repos/DoHorizon-AI/Cyrene-Plugins-Official/git/commits/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            },
        }
        release.verify_published_release_readback(
            record_by_id=record,
            record_by_tag=deepcopy(record),
            tag_ref=tag_ref,
            asset_paths=asset_paths,
            release_tag=release_tag,
            package_id=package_id,
            package_version=package_version,
            source_ref=source_ref,
            source_sha=source_sha,
            channel="preview",
        )

        invalid_cases = (
            (
                "draft",
                lambda row, ref: row.update(draft=True),
            ),
            (
                "mutable release",
                lambda row, ref: row.update(immutable=False),
            ),
            (
                "asset digest",
                lambda row, ref: row["assets"][0].update(digest="sha256:" + "0" * 64),
            ),
            (
                "source tag target",
                lambda row, ref: ref["object"].update(sha="b" * 40),
            ),
        )
        for label, mutate in invalid_cases:
            with self.subTest(label=label):
                changed_by_id = deepcopy(record)
                changed_by_tag = deepcopy(record)
                changed_ref = deepcopy(tag_ref)
                mutate(changed_by_id, changed_ref)
                if label != "source tag target":
                    mutate(changed_by_tag, changed_ref)
                with self.assertRaises(release.PackageBuildError):
                    release.verify_published_release_readback(
                        record_by_id=changed_by_id,
                        record_by_tag=changed_by_tag,
                        tag_ref=changed_ref,
                        asset_paths=asset_paths,
                        release_tag=release_tag,
                        package_id=package_id,
                        package_version=package_version,
                        source_ref=source_ref,
                        source_sha=source_sha,
                        channel="preview",
                    )

    def test_eight_packages_and_training_template_start_with_pythonpath_unset(
        self,
    ) -> None:
        """Require clean startup from each ZIP's ``src`` tree.

        中文：清除 PYTHONPATH 并切换到无关工作目录，验证所有入口可启动。
        """

        cases: list[tuple[str, Path, str, str, str | None]] = []
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
            wheelhouse_source = (
                None
                if package_id in LOCK_ISOLATED_STARTUP_PACKAGES
                else _create_wheelhouse_source(
                    self.repository,
                    spec,
                    self.temp_root / f"wheelhouse-source-{index}",
                )
            )
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
                wheelhouse_source=wheelhouse_source,
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
            self.assertEqual(
                package_release["package"]["capabilities"], manifest["capabilities"]
            )
            self.assertEqual(
                package_release["package"]["license_expression"],
                manifest.get("licenseExpression") or manifest.get("license"),
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
                if package_id == "cyrene.connectors.wecom":
                    self.assertIn("configuration.schema.json", archive.namelist())
                    self.assertIn("licenses/plugin/LICENSE", archive.namelist())
                    self.assertIn(
                        "third_party/licenses/wecom-aibot-python-sdk/LICENSE",
                        archive.namelist(),
                    )
                    self.assertEqual(
                        package_release["source_dependencies"][0]["source"]["commit"],
                        release.WECOM_AIBOT_SDK.commit,
                    )
            cases.append(
                (
                    f"package {package_id}",
                    outputs["package"],
                    manifest["runtime"]["entrypoint"],
                    manifest["capabilities"][0],
                    package_id,
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
                None,
            )
        )

        for name, archive_path, entrypoint, capability, package_id in cases:
            with self.subTest(name=name):
                launch_root = (
                    self.temp_root / f"launch-{len(list(self.temp_root.iterdir()))}"
                )
                payload = launch_root / "payload"
                payload.mkdir(parents=True)
                with zipfile.ZipFile(archive_path) as archive:
                    names = archive.namelist()
                    module = entrypoint.split(":", 1)[0].replace(".", "/")
                    self.assertTrue(
                        f"src/{module}.py" in names
                        or f"src/{module}/__init__.py" in names
                    )
                    self.assertNotIn(f"{module}.py", names)
                    archive.extractall(payload)
                python_executable = self._lock_bound_startup_python(
                    package_id=package_id,
                    package_name=name,
                    payload=payload,
                )
                self._assert_clean_start(
                    name=name,
                    payload=payload,
                    entrypoint=entrypoint,
                    capability=capability,
                    environment_overrides=self._package_activation_environment(
                        package_id
                    ),
                    python_executable=python_executable,
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

    def _package_activation_environment(
        self, package_id: str | None
    ) -> dict[str, str]:
        """Build synthetic host activation facts for packages that require them."""

        if package_id == "cyrene.accounts.account-center":
            storage = self.temp_root / "account-center-package-state"
            storage.mkdir(mode=0o700)
            os.chmod(storage, 0o700)
            callers = {
                "exchange-service": "account-center/caller-hmac-keys/exchange",
                "oauth-callback-service": "account-center/caller-hmac-keys/callback",
                "wecom-source": "account-center/caller-hmac-keys/wecom",
                "github-provider": "account-center/caller-hmac-keys/github-provider",
            }
            secret_refs = {
                "account-center/encryption-key": "TEST_PACKAGE_ACCOUNT_ENCRYPTION_KEY",
                "github-app/client-secret": "TEST_PACKAGE_GITHUB_CLIENT_SECRET",
                **{
                    reference: f"TEST_PACKAGE_ACCOUNT_CALLER_{index}"
                    for index, reference in enumerate(callers.values())
                },
            }
            methods = {
                method: ["exchange-service"]
                for method in (
                    "begin_link",
                    "confirm_wecom_link",
                    "list_links",
                    "get_authorization",
                    "unlink",
                    "create_credential_lease",
                    "resolve_linked_sender",
                )
            }
            methods.update(
                {
                    "complete_github_link": ["oauth-callback-service"],
                    "observe_wecom_challenge": ["wecom-source"],
                    "redeem_credential_lease": ["github-provider"],
                }
            )
            configuration = {
                "github_callback_uri": "https://exchange.example.test/accounts/callback",
                "github_client_id": "package-test-client",
                "github_client_secret_ref": "github-app/client-secret",
                "encryption_key_ref": "account-center/encryption-key",
                "caller_key_refs": callers,
                "hostSecretRefs": secret_refs,
                "exchange_caller_id": "exchange-service",
                "github_callback_caller_id": "oauth-callback-service",
                "github_provider_caller_id": "github-provider",
                "method_callers": methods,
                "wecom_sources": {
                    "wecom-source": {
                        "source_authority": "wecom:package-test",
                        "bot_namespaces": ["bot-main"],
                    }
                },
            }
            environment = {
                "CYRENE_PLUGIN_STORAGE_PATH": str(storage),
                "CYRENE_CAPABILITY_BINDING_ID": "account-center-package-test",
                "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(configuration),
                "TEST_PACKAGE_GITHUB_CLIENT_SECRET": "synthetic-package-test-client-secret",
            }
            for index, environment_name in enumerate(secret_refs.values()):
                if environment_name == "TEST_PACKAGE_GITHUB_CLIENT_SECRET":
                    continue
                environment[environment_name] = (bytes([index + 1]) * 32).hex()
            return environment

        if package_id == "cyrene.providers.github-issues":
            references = {
                "github-issues/exchange-verification-key": "TEST_PACKAGE_GH_EXCHANGE_KEY",
                "github-issues/webhook-secret": "TEST_PACKAGE_GH_WEBHOOK_SECRET",
                "github-issues/account-center-provider-signing-key": "TEST_PACKAGE_GH_PROVIDER_KEY",
            }
            configuration = {
                "exchange_caller_id": "exchange-service",
                "exchange_verification_key_ref": "github-issues/exchange-verification-key",
                "github_webhook_secret_ref": "github-issues/webhook-secret",
                "repositories": [
                    {"repository_id": "12345", "owner": "owner", "name": "project"}
                ],
                "allowed_installation_ids": ["99"],
                "allowed_events": ["issues"],
                "allowed_actions": ["opened", "closed", "reopened"],
                "account_center": {
                    "binding_id": "github-provider-package-test",
                    "connection_ref": "grpc://127.0.0.1:1",
                    "transport": "local",
                    "caller_id": "github-provider",
                    "signing_key_ref": (
                        "github-issues/account-center-provider-signing-key"
                    ),
                },
                "hostSecretRefs": references,
            }
            return {
                "CYRENE_CAPABILITY_BINDING_ID": "github-provider-package-test",
                "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(configuration),
                "TEST_PACKAGE_GH_EXCHANGE_KEY": (b"\x22" * 32).hex(),
                "TEST_PACKAGE_GH_WEBHOOK_SECRET": "synthetic-github-webhook-secret-value",
                "TEST_PACKAGE_GH_PROVIDER_KEY": (b"\x33" * 32).hex(),
            }
        return {}

    def _lock_bound_startup_python(
        self,
        *,
        package_id: str | None,
        package_name: str,
        payload: Path,
    ) -> Path | None:
        """Create a runtime from an isolated exact wheelhouse where pins differ.

        Account Center and WeCom intentionally require different cryptography
        versions. Their clean-start checks use the ZIP's real wheelhouse so the
        test validates each lock independently instead of merging incompatible
        pins in the release-test environment.
        中文：为固定密码学版本的 package 独立安装 ZIP 锁定的 wheelhouse。
        """

        if package_id not in LOCK_ISOLATED_STARTUP_PACKAGES:
            return None
        lock_path = payload / "requirements.lock"
        wheelhouse_path = payload / "wheelhouse"
        self.assertTrue(lock_path.is_file(), f"{package_name} package lock is missing")
        self.assertTrue(
            (wheelhouse_path / "manifest.json").is_file(),
            f"{package_name} package wheelhouse is missing",
        )
        virtual_environment = self.temp_root / (
            "clean-start-runtime-" + package_name.replace(" ", "-")
        )
        subprocess.run(
            [sys.executable, "-m", "venv", str(virtual_environment)],
            check=True,
            capture_output=True,
            text=True,
        )
        python_executable = virtual_environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        installation = subprocess.run(
            [
                str(python_executable),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--only-binary=:all:",
                "--find-links",
                str(wheelhouse_path),
                "--requirement",
                str(lock_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            installation.returncode,
            0,
            f"{package_name} exact wheelhouse installation failed:\n{installation.stderr}",
        )
        return python_executable

    def _assert_clean_start(
        self,
        *,
        name: str,
        payload: Path,
        entrypoint: str,
        capability: str,
        environment_overrides: dict[str, str] | None = None,
        python_executable: Path | None = None,
    ) -> None:
        bootstrap = payload / "src/cyrene_plugin_runtime/bootstrap.py"
        environment = os.environ.copy()
        for variable in (
            "PYTHONPATH",
            "CYRENE_CAPABILITY_BINDING_ID",
            "CYRENE_CAPABILITY_CONFIGURATION_JSON",
            "CYRENE_PLUGIN_STORAGE_PATH",
            "WECOM_CORP_SECRET",
            "WECOM_BOT_SECRET",
            "WECOM_NAVIGATOR_BEARER_TOKEN",
            "TEST_PACKAGE_ACCOUNT_ENCRYPTION_KEY",
            "TEST_PACKAGE_GITHUB_CLIENT_SECRET",
            "TEST_PACKAGE_GH_EXCHANGE_KEY",
            "TEST_PACKAGE_GH_WEBHOOK_SECRET",
            "TEST_PACKAGE_GH_PROVIDER_KEY",
        ):
            environment.pop(variable, None)
        environment.update(environment_overrides or {})
        self.assertNotIn("PYTHONPATH", environment)
        process = subprocess.Popen(
            [
                str(python_executable or sys.executable),
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
