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
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import tomllib
from packaging.requirements import Requirement

from tooling.release import official_plugin_package as release
from tooling.release import select_official_plugin_packages as selection
from tooling.release import training_llama_factory_package as training_release

LOCK_ISOLATED_STARTUP_PACKAGES = frozenset(
    {
        "cyrene.accounts.account-center",
        "cyrene.connectors.teams",
        "cyrene.connectors.wecom",
        "cyrene.providers.cloud-management",
        "cyrene.providers.cloud-resources",
        "cyrene.providers.github-issues",
        "cyrene.providers.huggingface",
        "cyrene.providers.microsoft-graph",
        "cyrene.providers.azure-devops-observation",
    }
)


def _write_preparer_wheel(path: Path) -> Path:
    metadata_root = "cyrene_plugin_runtime-0.2.2.dist-info"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            f"{metadata_root}/METADATA",
            "Metadata-Version: 2.1\nName: cyrene-plugin-runtime\nVersion: 0.2.2\n",
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
        with zipfile.ZipFile(
            wheel_path, "w", compression=zipfile.ZIP_STORED
        ) as archive:
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
                    (repository / source_dependency.license_file_path).read_bytes(),
                )
    return destination


def _read_hashed_runtime_lock(path: Path) -> dict[str, str]:
    """Parse a uv-generated pin/hash lock and reject incomplete requirement blocks."""

    pins: dict[str, str] = {}
    current_name: str | None = None
    current_hashes = 0
    for source_line in path.read_text(encoding="utf-8").splitlines():
        line = source_line.strip().removesuffix("\\").strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash="):
            digest = line.removeprefix("--hash=sha256:")
            if current_name is None or len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise AssertionError(f"invalid or misplaced package hash in {path}: {line}")
            current_hashes += 1
            continue
        if current_name is not None and current_hashes == 0:
            raise AssertionError(f"package pin has no hashes in {path}: {current_name}")
        match = release.LOCK_PIN.fullmatch(line)
        if match is None:
            raise AssertionError(f"package lock line is not exactly pinned in {path}: {line}")
        current_name = release.canonicalize_name(match.group(1))
        if current_name in pins:
            raise AssertionError(f"package lock repeats a distribution in {path}: {current_name}")
        pins[current_name] = match.group(2)
        current_hashes = 0
    if current_name is None or current_hashes == 0:
        raise AssertionError(f"package lock ends without a complete hashed pin: {path}")
    return pins


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

    def test_runtime_version_and_mtls_test_dependency_are_consistent(self) -> None:
        """Keep the tested X.509 helper out of production runtime dependencies."""

        project_path = self.repository / "sdk/python/cyrene_plugin_runtime/pyproject.toml"
        project = tomllib.loads(project_path.read_text(encoding="utf-8"))["project"]

        self.assertEqual(project["version"], release.PREPARER_VERSION)
        self.assertEqual(project["version"], training_release.RUNTIME_VERSION)
        self.assertEqual(release.PREPARER_WHEEL, training_release.RUNTIME_WHEEL_NAME)
        self.assertNotIn(
            "cryptography",
            [Requirement(value).name.lower() for value in project["dependencies"]],
        )
        self.assertEqual(
            project["optional-dependencies"]["test"], ["cryptography>=44,<51"]
        )
        account_center_project = tomllib.loads(
            (
                self.repository
                / "plugins/accounts/account-center/pyproject.toml"
            ).read_text(encoding="utf-8")
        )["project"]
        account_center_cryptography = next(
            Requirement(value)
            for value in account_center_project["dependencies"]
            if Requirement(value).name.lower() == "cryptography"
        )
        teams_cryptography = next(
            line.split("==", 1)[1]
            for line in (
                self.repository / "plugins/connectors/teams/requirements.lock"
            ).read_text(encoding="utf-8").splitlines()
            if line.startswith("cryptography==")
        )
        self.assertTrue(account_center_cryptography.specifier.contains(teams_cryptography))
        self.assertTrue(
            Requirement(project["optional-dependencies"]["test"][0])
            .specifier.contains(teams_cryptography)
        )

    def test_office_provider_packages_have_explicit_sdk_and_license_mappings(self) -> None:
        """Register both Office observation providers with exact source-owned inputs."""

        expected = {
            "cyrene.providers.microsoft-graph": (
                "cyrene-providers-microsoft-graph",
                "plugins/providers/microsoft-graph",
                "tooling/release/requirements/microsoft-graph.in",
                {"grpcio", "protobuf", "rfc8785"},
            ),
            "cyrene.providers.azure-devops-observation": (
                "cyrene-providers-azure-devops-observation",
                "plugins/providers/azure-devops-observation",
                "tooling/release/requirements/azure-devops-observation.in",
                {"grpcio", "httpx", "protobuf", "rfc8785"},
            ),
        }
        for package_id, (component_id, source_dir, requirements_input, external) in expected.items():
            with self.subTest(package_id=package_id):
                spec = release.PACKAGE_SPECS[package_id]
                self.assertEqual(spec.component_id, component_id)
                self.assertEqual(spec.source_dir, source_dir)
                self.assertEqual(spec.requirements_input, requirements_input)
                self.assertEqual(spec.vendored_sdks, (release.RUNTIME_SDK, release.ACCOUNT_CENTER_SDK))
                self.assertTrue(spec.require_spdx_license)

                manifest, _ = release._read_package_spec(self.repository, spec)
                self.assertEqual(manifest["licenseExpression"], "Apache-2.0")
                source_files, _ = release._source_files(self.repository, spec)
                requirements = source_files[spec.requirements_input].decode("utf-8")
                names = {
                    release.canonicalize_name(Requirement(line.strip()).name)
                    for line in requirements.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                }
                self.assertEqual(names, external)
                self.assertIn("cyrene-plugin-runtime==0.2.2", requirements)
                self.assertIn("cyrene-account-center-contracts==0.1.2", requirements)
                release._verify_lock_inputs(spec, source_files)

    def test_office_docker_locks_hash_every_exact_package_lock_pin(self) -> None:
        """Keep the Docker install lock fully hashed and aligned to package releases."""

        for package_id in selection.OFFICE_PILOT_PACKAGE_IDS:
            with self.subTest(package_id=package_id):
                spec = release.PACKAGE_SPECS[package_id]
                package_lock = (self.repository / spec.source_dir / "requirements.lock").read_bytes()
                package_pins = {
                    release.canonicalize_name(match.group(1)): match.group(2)
                    for line in release._lock_lines(package_lock, spec.source_dir)
                    if (match := release.LOCK_PIN.fullmatch(line)) is not None
                }
                docker_lock = self.repository / spec.source_dir / "docker-requirements.lock"
                self.assertEqual(_read_hashed_runtime_lock(docker_lock), package_pins)

    def test_office_provider_packages_build_and_start_from_locked_wheelhouses(self) -> None:
        """Build both source packages and list fixed tools from isolated ZIP runtimes."""

        for index, package_id in enumerate(selection.OFFICE_PILOT_PACKAGE_IDS):
            with self.subTest(package_id=package_id):
                spec = release.PACKAGE_SPECS[package_id]
                output_dir = self.temp_root / f"office-provider-release-{index}"
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

                payload = self.temp_root / f"office-provider-payload-{index}"
                payload.mkdir()
                with zipfile.ZipFile(outputs["package"]) as archive:
                    archive.extractall(payload)
                manifest = json.loads(
                    (self.repository / spec.source_dir / "plugin.manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                python_executable = self._lock_bound_startup_python(
                    package_id=package_id,
                    package_name=package_id,
                    payload=payload,
                )
                self._assert_clean_start(
                    name=package_id,
                    payload=payload,
                    entrypoint=manifest["runtime"]["entrypoint"],
                    capability=manifest["capabilities"][0],
                    environment_overrides=self._package_activation_environment(package_id),
                    python_executable=python_executable,
                    package_id=package_id,
                )

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

    def test_native_target_registry_and_cross_architecture_guard(self) -> None:
        """Map both native Linux targets and reject cross-architecture requests."""

        self.assertEqual(
            release.target_id_for_machine("x86_64"),
            "linux-ubuntu-24.04-x86_64-python-3.12",
        )
        self.assertEqual(
            release.target_id_for_machine("aarch64"),
            "linux-ubuntu-24.04-aarch64-python-3.12",
        )
        self.assertEqual(
            release.target_id_for_machine("arm64"),
            "linux-ubuntu-24.04-aarch64-python-3.12",
        )
        self.assertIsNone(release.target_id_for_machine("riscv64"))
        self.assertEqual(
            release.TARGET["architecture"],
            "aarch64"
            if release.TARGET_ID.endswith("-aarch64-python-3.12")
            else "x86_64",
        )
        with self.assertRaisesRegex(
            release.PackageBuildError, "does not match native builder target"
        ):
            release._require_native_target(
                "linux-ubuntu-24.04-aarch64-python-3.12", machine="x86_64"
            )

    def test_wheel_filename_and_wheel_metadata_architectures_must_match(self) -> None:
        """Reject a wheel whose filename and embedded WHEEL tags disagree."""

        filename = "fixture-1.0-cp312-cp312-manylinux_2_39_x86_64.whl"
        payload = BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(
                "fixture-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: fixture\nVersion: 1.0\n",
            )
            archive.writestr(
                "fixture-1.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nTag: cp312-cp312-manylinux_2_39_aarch64\n",
            )

        with self.assertRaisesRegex(
            release.PackageBuildError, "WHEEL metadata tags do not match"
        ):
            release._wheel_metadata(payload.getvalue(), filename)

    def test_wheelhouse_rejects_a_wheel_for_the_other_architecture(self) -> None:
        """Never accept a foreign-architecture wheel into a native package ZIP."""

        current_architecture = release.TARGET["architecture"]
        other_architecture = "aarch64" if current_architecture == "x86_64" else "x86_64"
        platform_tag = (
            "manylinux_2_39_aarch64"
            if other_architecture == "aarch64"
            else "manylinux_2_39_x86_64"
        )
        filename = f"protobuf-4.25.9-cp312-cp312-{platform_tag}.whl"
        wheel_path = self.temp_root / filename
        with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(
                "protobuf-4.25.9.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: protobuf\nVersion: 4.25.9\n",
            )
            archive.writestr(
                "protobuf-4.25.9.dist-info/WHEEL",
                f"Wheel-Version: 1.0\nTag: cp312-cp312-{platform_tag}\n",
            )

        with self.assertRaisesRegex(
            release.PackageBuildError, "wheel is incompatible with"
        ):
            release._wheelhouse_entries(
                b"grpcio==1.62.3\nprotobuf==4.25.9\n", [wheel_path]
            )

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
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
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

    def test_wecom_vends_integration_sdk_schema_once_from_canonical_source(
        self,
    ) -> None:
        """Bundle the SDK schema once and keep it equal to the public contract.

        中文：WeCom 包内只通过 vendored SDK 携带 schema，且与公开 schema 完全一致。
        """

        spec = release.PACKAGE_SPECS["cyrene.connectors.wecom"]
        sdk = release.INTEGRATION_SUPPORT_SDK
        canonical_schema_path = (
            self.repository / "contracts/json/integration-support/v1/schema.json"
        )
        sdk_schema_path = self.repository / sdk.source_root / "schema.json"
        canonical_schema = canonical_schema_path.read_bytes()

        self.assertIn(sdk, spec.vendored_sdks)
        self.assertEqual(sdk.package_data, ("schema.json",))
        self.assertEqual(sdk_schema_path.read_bytes(), canonical_schema)

        source_files, sdk_files = release._source_files(self.repository, spec)
        source_schema_path = f"{sdk.source_root}/schema.json"
        self.assertEqual(
            sdk_files[sdk.distribution][source_schema_path], canonical_schema
        )
        archive_entries = release._package_entries(source_files, spec, sdk_files)
        archive_schema_path = f"{sdk.archive_root}/schema.json"
        self.assertEqual(archive_entries[archive_schema_path], canonical_schema)
        self.assertEqual(
            [name for name in archive_entries if name.endswith("/schema.json")],
            [archive_schema_path],
        )
        self.assertNotIn(
            "contracts/json/integration-support/v1/schema.json", archive_entries
        )

        requirements_input = source_files[spec.requirements_input].decode("utf-8")
        input_requirements = {
            release.canonicalize_name(Requirement(line.strip()).name)
            for line in requirements_input.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("jsonschema", input_requirements)
        self.assertNotIn("cyrene-integration-contracts", input_requirements)
        self.assertIn("cyrene-integration-contracts==0.1.0", requirements_input)

    def test_email_notification_vends_integration_sdk_schema_once_from_canonical_source(
        self,
    ) -> None:
        """Bundle the shared typed SDK and schema without a second schema copy.

        中文：邮件包复用共享 typed SDK 与 schema，不额外携带第二份 schema。
        """

        spec = release.PACKAGE_SPECS["cyrene.connectors.email-notification"]
        sdk = release.INTEGRATION_SUPPORT_SDK
        canonical_schema_path = (
            self.repository / "contracts/json/integration-support/v1/schema.json"
        )
        sdk_schema_path = self.repository / sdk.source_root / "schema.json"
        canonical_schema = canonical_schema_path.read_bytes()

        self.assertEqual(spec.vendored_sdks, (release.RUNTIME_SDK, sdk))
        self.assertEqual(sdk.package_data, ("schema.json",))
        self.assertEqual(sdk_schema_path.read_bytes(), canonical_schema)

        source_files, sdk_files = release._source_files(self.repository, spec)
        source_schema_path = f"{sdk.source_root}/schema.json"
        self.assertEqual(
            sdk_files[sdk.distribution][source_schema_path], canonical_schema
        )
        archive_entries = release._package_entries(source_files, spec, sdk_files)
        archive_schema_path = f"{sdk.archive_root}/schema.json"
        self.assertEqual(archive_entries[archive_schema_path], canonical_schema)
        self.assertEqual(
            [name for name in archive_entries if name.endswith("/schema.json")],
            [archive_schema_path],
        )
        self.assertNotIn(
            "contracts/json/integration-support/v1/schema.json", archive_entries
        )
        self.assertIn("configuration.schema.json", archive_entries)

        requirements_input = source_files[spec.requirements_input].decode("utf-8")
        input_requirements = {
            release.canonicalize_name(Requirement(line.strip()).name)
            for line in requirements_input.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertEqual(input_requirements, {"grpcio", "jsonschema", "protobuf"})
        self.assertIn("cyrene-plugin-runtime==0.2.2", requirements_input)
        self.assertIn("cyrene-integration-contracts==0.1.0", requirements_input)
        pins = release._verify_lock_inputs(spec, source_files)
        self.assertEqual(
            {release.canonicalize_name(line.split("==", 1)[0]) for line in pins},
            {
                "attrs",
                "grpcio",
                "jsonschema",
                "jsonschema-specifications",
                "protobuf",
                "referencing",
                "rpds-py",
                "typing-extensions",
            },
        )

    def test_cloud_resources_vends_shared_schema_and_locks_external_wheels(
        self,
    ) -> None:
        """Bundle the canonical SDK schema and keep internal SDKs out of the wheel lock.

        中文：Cloud ZIP 携带规范 SDK schema，内部 SDK 不重复作为 registry wheel。
        """

        package_id = "cyrene.providers.cloud-resources"
        spec = release.PACKAGE_SPECS[package_id]
        sdk = release.INTEGRATION_SUPPORT_SDK
        canonical_schema = (
            self.repository / "contracts/json/integration-support/v1/schema.json"
        ).read_bytes()

        self.assertEqual(spec.component_id, "cyrene-providers-cloud-resources")
        self.assertEqual(spec.vendored_sdks, (release.RUNTIME_SDK, sdk))
        self.assertEqual(sdk.package_data, ("schema.json",))
        self.assertEqual(
            (self.repository / sdk.source_root / "schema.json").read_bytes(),
            canonical_schema,
        )

        source_files, sdk_files = release._source_files(self.repository, spec)
        schema_source = f"{sdk.source_root}/schema.json"
        self.assertEqual(sdk_files[sdk.distribution][schema_source], canonical_schema)
        archive_entries = release._package_entries(source_files, spec, sdk_files)
        schema_archive = f"{sdk.archive_root}/schema.json"
        self.assertEqual(archive_entries[schema_archive], canonical_schema)
        self.assertEqual(
            [name for name in archive_entries if name.endswith("/schema.json")],
            [schema_archive],
        )
        self.assertNotIn(
            "contracts/json/integration-support/v1/schema.json", archive_entries
        )
        self.assertIn(f"{spec.source_dir}/configuration.schema.json", source_files)

        requirements_input = source_files[spec.requirements_input].decode("utf-8")
        input_requirements = {
            release.canonicalize_name(Requirement(line.strip()).name)
            for line in requirements_input.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertEqual(
            input_requirements,
            {
                "azure-identity",
                "google-auth",
                "grpcio",
                "httpx",
                "jsonschema",
                "protobuf",
                "requests",
            },
        )
        self.assertIn("cyrene-plugin-runtime==0.2.2", requirements_input)
        self.assertIn("cyrene-integration-contracts==0.1.0", requirements_input)
        pins = release._verify_lock_inputs(spec, source_files)
        pin_versions = {
            release.canonicalize_name(line.split("==", 1)[0]): line.split("==", 1)[1]
            for line in pins
        }
        self.assertEqual(pin_versions["grpcio"], "1.83.1")
        self.assertEqual(pin_versions["protobuf"], "4.25.9")
        self.assertNotIn("cyrene-plugin-runtime", pin_versions)
        self.assertNotIn("cyrene-integration-contracts", pin_versions)

    def test_cloud_resources_package_builds_and_lists_tools_from_exact_wheelhouse(
        self,
    ) -> None:
        """Build a Cloud ZIP and list its tools from its isolated locked runtime.

        中文：从精确锁定 wheelhouse 启动 Cloud ZIP 并列出工具，不访问云端。
        """

        uv = shutil.which("uv")
        self.assertIsNotNone(uv, "the pinned release build environment must provide uv")
        package_id = "cyrene.providers.cloud-resources"
        output_dir = self.temp_root / "cloud-resources-release"
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

        payload = self.temp_root / "cloud-resources-payload"
        payload.mkdir()
        with zipfile.ZipFile(outputs["package"]) as archive:
            archive.extractall(payload)
        runtime = self.temp_root / "cloud-resources-runtime"
        created = subprocess.run(
            [str(uv), "venv", "--python", sys.executable, str(runtime)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        python_executable = runtime / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        installed = subprocess.run(
            [
                str(uv),
                "pip",
                "install",
                "--python",
                str(python_executable),
                "--offline",
                "--no-index",
                "--find-links",
                str(payload / "wheelhouse"),
                "--requirement",
                str(payload / "requirements.lock"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)

        manifest_path = (
            self.repository
            / release.PACKAGE_SPECS[package_id].source_dir
            / "plugin.manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        with zipfile.ZipFile(outputs["package"]) as archive:
            names = archive.namelist()
        self.assertIn("src/cyrene_integration_contracts/schema.json", names)
        self.assertIn("configuration.schema.json", names)
        self._assert_clean_start(
            name="Cloud Resources package with locked wheelhouse",
            payload=payload,
            entrypoint=manifest["runtime"]["entrypoint"],
            capability="tool.provider.v1",
            environment_overrides=self._package_activation_environment(package_id),
            python_executable=python_executable,
            package_id=package_id,
        )

    def test_email_notification_package_builds_and_bootstraps_from_exact_wheelhouse(
        self,
    ) -> None:
        """Build the email ZIP and list tools from its isolated locked runtime.

        中文：从精确 wheelhouse 构建邮件包，并在隔离运行时列出固定工具。
        """

        uv = shutil.which("uv")
        self.assertIsNotNone(uv, "the pinned release build environment must provide uv")
        package_id = "cyrene.connectors.email-notification"
        output_dir = self.temp_root / "email-notification-release"
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

        payload = self.temp_root / "email-notification-payload"
        payload.mkdir()
        with zipfile.ZipFile(outputs["package"]) as archive:
            archive.extractall(payload)
        runtime = self.temp_root / "email-notification-runtime"
        created = subprocess.run(
            [str(uv), "venv", "--python", sys.executable, str(runtime)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        python_executable = runtime / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        installed = subprocess.run(
            [
                str(uv),
                "pip",
                "install",
                "--python",
                str(python_executable),
                "--offline",
                "--no-index",
                "--find-links",
                str(payload / "wheelhouse"),
                "--requirement",
                str(payload / "requirements.lock"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)

        manifest_path = (
            self.repository
            / release.PACKAGE_SPECS[package_id].source_dir
            / "plugin.manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self._assert_clean_start(
            name="email notification package with locked wheelhouse",
            payload=payload,
            entrypoint=manifest["runtime"]["entrypoint"],
            capability="tool.provider.v1",
            environment_overrides=self._package_activation_environment(package_id),
            python_executable=python_executable,
            package_id=package_id,
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

    def test_registered_packages_and_training_template_start_with_pythonpath_unset(
        self,
    ) -> None:
        """Require clean startup from each ZIP's ``src`` tree.

        中文：清除 PYTHONPATH 并切换到无关工作目录，验证所有入口可启动。
        """

        cases: list[tuple[str, Path, str, str, str | None]] = []
        expected_target = release.TARGET
        self.assertEqual(
            expected_target["architecture"],
            "aarch64"
            if release.TARGET_ID.endswith("-aarch64-python-3.12")
            else "x86_64",
        )
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
                if package_id == "cyrene.connectors.email-notification":
                    self.assertIn("configuration.schema.json", archive.namelist())
                    self.assertIn(
                        "src/cyrene_integration_contracts/schema.json",
                        archive.namelist(),
                    )
                    self.assertIn("licenses/plugin/LICENSE", archive.namelist())
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
                    package_id=package_id,
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

    def _package_activation_environment(self, package_id: str | None) -> dict[str, str]:
        """Build synthetic host activation facts for packages that require them."""

        if package_id == "cyrene.providers.cloud-resources":
            configuration = {
                "trusted_callers": [
                    {
                        "caller_workload": "exchange.integrations",
                        "audience": "cloud-resources",
                        "organization_id": "package-fixture-org",
                        "workspace_id": "package-fixture-workspace",
                        "issuer": "https://identity.example.test",
                        "verification_key_ref": "cloud-resources/exchange-verification-key",
                    }
                ],
                "azure_auth": {"mode": "managed_identity"},
                "gcp_auth": {"mode": "metadata"},
                "targets": [
                    {
                        "target_ref": "vm-package-test",
                        "display_name": "Package startup fixture",
                        "provider": "azure",
                        "kind": "azure.vm",
                        "subscription_id": "11111111-2222-3333-4444-555555555555",
                        "resource_group": "package-test",
                        "vm_name": "worker-01",
                    }
                ],
                "hostSecretRefs": {
                    "cloud-resources/exchange-verification-key": (
                        "TEST_PACKAGE_CLOUD_CONTEXT_KEY"
                    )
                },
            }
            return {
                "CYRENE_CAPABILITY_BINDING_ID": "cloud-resources-package-test",
                "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(configuration),
                "TEST_PACKAGE_CLOUD_CONTEXT_KEY": (b"\x55" * 32).hex(),
            }

        if package_id == "cyrene.providers.cloud-management":
            state_directory = self.temp_root / "cloud-management-package-state"
            state_directory.mkdir(mode=0o700)
            os.chmod(state_directory, 0o700)
            configuration = {
                "trusted_callers": [
                    {
                        "caller_workload": "cyrene-exchange",
                        "audience": "cloud-management",
                        "organization_id": "package-fixture-org",
                        "workspace_id": "package-fixture-workspace",
                        "issuer": "cyrene-exchange",
                        "verification_key_ref": "cloud-management/exchange-verification-key",
                    }
                ],
                "profiles": [
                    {
                        "profile_ref": "azure-package-test",
                        "provider": "azure",
                        "identity_ref": "system-assigned",
                        "scope": {
                            "subscription_id": "11111111-2222-3333-4444-555555555555",
                            "resource_groups": ["package-test"],
                        },
                        "auth": {"mode": "managed_identity"},
                    }
                ],
                "hostSecretRefs": {
                    "cloud-management/exchange-verification-key": (
                        "TEST_PACKAGE_CLOUD_MANAGEMENT_EXCHANGE_KEY"
                    ),
                    "cloud-management/state-dir": "TEST_PACKAGE_CLOUD_MANAGEMENT_STATE_DIR",
                },
            }
            return {
                "CYRENE_CAPABILITY_BINDING_ID": "cloud-management-package-test",
                "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(configuration),
                "TEST_PACKAGE_CLOUD_MANAGEMENT_EXCHANGE_KEY": (b"\x56" * 32).hex(),
                "TEST_PACKAGE_CLOUD_MANAGEMENT_STATE_DIR": str(state_directory),
            }

        if package_id == "cyrene.providers.huggingface":
            configuration = {
                "exchange_caller_id": "exchange-service",
                "exchange_verification_key_ref": "huggingface/exchange-verification-key",
                "account_center": {
                    "binding_id": "account-center-package-test",
                    "connection_ref": "grpc://127.0.0.1:1",
                    "transport": "local",
                    "caller_id": "huggingface-provider",
                    "signing_key_ref": "huggingface/account-center-provider-signing-key",
                },
                "repositories": [
                    {
                        "repo_type": "model",
                        "repo_id": "fixture/model",
                        "permissions": ["read"],
                    }
                ],
                "hostSecretRefs": {
                    "huggingface/exchange-verification-key": (
                        "TEST_PACKAGE_HF_EXCHANGE_KEY"
                    ),
                    "huggingface/account-center-provider-signing-key": (
                        "TEST_PACKAGE_HF_ACCOUNT_CENTER_KEY"
                    ),
                },
            }
            return {
                "CYRENE_CAPABILITY_BINDING_ID": "huggingface-package-test",
                "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(configuration),
                "TEST_PACKAGE_HF_EXCHANGE_KEY": (b"\x57" * 32).hex(),
                "TEST_PACKAGE_HF_ACCOUNT_CENTER_KEY": (b"\x58" * 32).hex(),
            }

        if package_id == "cyrene.connectors.email-notification":
            storage = self.temp_root / "email-notification-package-state"
            storage.mkdir(mode=0o700)
            os.chmod(storage, 0o700)
            configuration = {
                "smtp_host": "smtp.example.invalid",
                "smtp_port": 465,
                "tls_mode": "implicit",
                "sender": "notifications@example.invalid",
                "recipients": {"operator": "operator@example.invalid"},
                "state_db_path": str(storage / "submissions.sqlite3"),
                "context_hmac_key_env_ref": "TEST_PACKAGE_EMAIL_CONTEXT_KEY",
                "caller_workload": "cyrene-exchange",
                "audience": "cyrene.connectors.email-notification",
                "organization_id": "example-organization",
                "workspace_id": "example-workspace",
                "expected_issuer": "cyrene-exchange",
                "expected_subject": "notification-worker",
            }
            return {
                "CYRENE_CAPABILITY_BINDING_ID": "email-notification-package-test",
                "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(configuration),
                "TEST_PACKAGE_EMAIL_CONTEXT_KEY": (b"\x44" * 32).hex(),
            }

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

        if package_id == "cyrene.providers.microsoft-graph":
            configuration = {
                "exchange_caller_id": "exchange-service",
                "exchange_verification_key_ref": "microsoft-graph/exchange-verification-key",
                "account_center": {
                    "binding_id": "account-center-package-test",
                    "connection_ref": "grpc://127.0.0.1:1",
                    "transport": "local",
                    "caller_id": "microsoft-graph-provider",
                    "signing_key_ref": "microsoft-graph/account-center-provider-signing-key",
                },
                "hostSecretRefs": {
                    "microsoft-graph/exchange-verification-key": "TEST_PACKAGE_GRAPH_EXCHANGE_KEY",
                    "microsoft-graph/account-center-provider-signing-key": "TEST_PACKAGE_GRAPH_ACCOUNT_CENTER_KEY",
                },
            }
            return {
                "CYRENE_CAPABILITY_BINDING_ID": "microsoft-graph-package-test",
                "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(configuration),
                "TEST_PACKAGE_GRAPH_EXCHANGE_KEY": (b"\x61" * 32).hex(),
                "TEST_PACKAGE_GRAPH_ACCOUNT_CENTER_KEY": (b"\x62" * 32).hex(),
            }

        if package_id == "cyrene.providers.azure-devops-observation":
            configuration = {
                "exchange_caller_id": "exchange-service",
                "exchange_verification_key_ref": "azure-devops-observation/exchange-verification-key",
                "organization": "package-test",
                "tenant_id": "00000000-0000-4000-8000-000000000011",
                "account_center": {
                    "binding_id": "account-center-package-test",
                    "connection_ref": "grpc://127.0.0.1:1",
                    "transport": "local",
                    "caller_id": "azure-devops-observation-provider",
                    "signing_key_ref": "azure-devops-observation/account-center-provider-signing-key",
                },
                "authorized_sources": [
                    {
                        "source_id": "pipeline-source",
                        "kind": "pipeline_runs",
                        "project_id": "00000000-0000-4000-8000-000000000012",
                        "project_name": "Package Test Project",
                        "profile_ref": "00000000-0000-4000-8000-000000000013",
                        "pipeline_id": 7,
                    }
                ],
                "hostSecretRefs": {
                    "azure-devops-observation/exchange-verification-key": "TEST_PACKAGE_ADO_EXCHANGE_KEY",
                    "azure-devops-observation/account-center-provider-signing-key": "TEST_PACKAGE_ADO_ACCOUNT_CENTER_KEY",
                },
            }
            return {
                "CYRENE_CAPABILITY_BINDING_ID": "azure-devops-observation-package-test",
                "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(configuration),
                "TEST_PACKAGE_ADO_EXCHANGE_KEY": (b"\x63" * 32).hex(),
                "TEST_PACKAGE_ADO_ACCOUNT_CENTER_KEY": (b"\x64" * 32).hex(),
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

        Account Center, Graph, Azure DevOps, GitHub, WeCom, Cloud, Teams, and
        Hugging Face have component-specific locks. These checks install each
        ZIP's exact wheelhouse instead of merging provider dependency pins.
        中文：为有独立依赖锁的 package 安装各自 ZIP 中的精确 wheelhouse。
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
        uv = shutil.which("uv")
        venv_command = (
            [uv, "venv", "--seed", "--python", sys.executable, str(virtual_environment)]
            if uv is not None
            else [sys.executable, "-m", "venv", str(virtual_environment)]
        )
        subprocess.run(
            venv_command,
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
        package_id: str | None = None,
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
            "TEST_PACKAGE_CLOUD_MANAGEMENT_EXCHANGE_KEY",
            "TEST_PACKAGE_CLOUD_MANAGEMENT_STATE_DIR",
            "TEST_PACKAGE_HF_EXCHANGE_KEY",
            "TEST_PACKAGE_HF_ACCOUNT_CENTER_KEY",
            "EMAIL_NOTIFICATION_CONTEXT_HMAC_KEY",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
            "TEST_PACKAGE_EMAIL_CONTEXT_KEY",
            "TEST_PACKAGE_CLOUD_CONTEXT_KEY",
            "TEST_PACKAGE_GRAPH_EXCHANGE_KEY",
            "TEST_PACKAGE_GRAPH_ACCOUNT_CENTER_KEY",
            "TEST_PACKAGE_ADO_EXCHANGE_KEY",
            "TEST_PACKAGE_ADO_ACCOUNT_CENTER_KEY",
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
            if package_id == "cyrene.connectors.email-notification":
                self._assert_email_tool_catalog(
                    name=name,
                    payload=payload,
                    connection_ref=ready["connection_ref"],
                    environment=environment,
                    python_executable=python_executable,
                )
            elif package_id == "cyrene.providers.cloud-resources":
                self._assert_cloud_tool_catalog(
                    name=name,
                    payload=payload,
                    connection_ref=ready["connection_ref"],
                    environment=environment,
                    python_executable=python_executable,
                )
            elif package_id in {
                "cyrene.providers.microsoft-graph",
                "cyrene.providers.azure-devops-observation",
            }:
                self._assert_office_provider_tool_catalog(
                    name=name,
                    payload=payload,
                    connection_ref=ready["connection_ref"],
                    python_executable=python_executable,
                    package_id=package_id,
                )
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

    def _assert_email_tool_catalog(
        self,
        *,
        name: str,
        payload: Path,
        connection_ref: str,
        environment: dict[str, str],
        python_executable: Path | None,
    ) -> None:
        """List email tools through the clean-start runtime and bundled SDK schema.

        中文：通过干净启动的运行时确认 ZIP 中携带的 SDK schema 可用于工具目录。
        """

        script = """
import json
import sys

sys.path.insert(0, sys.argv[1])
from cyrene_plugin_runtime.client import DirectPayload, DirectPluginClient
from email_notification_connector._generated import tool_provider_pb2 as wire

request = wire.ListToolsRequest(binding_id="email-notification-package-test")
with DirectPluginClient.for_local_connection_ref(sys.argv[2]) as client:
    response_payload = client.invoke(
        capability="tool.provider.v1",
        interface_version="1",
        method="list_tools",
        request=DirectPayload(
            type_url="type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest",
            value=request.SerializeToString(),
        ),
        deadline_seconds=10,
        request_id="email-package-list-tools",
    )
response = wire.ListToolsResponse.FromString(response_payload.value)
print(json.dumps({
    "type_url": response_payload.type_url,
    "tool_ids": [tool.provider_tool_id for tool in response.catalog.tools],
    "input_schemas": [json.loads(tool.input_schema_json) for tool in response.catalog.tools],
}))
"""
        result = subprocess.run(
            [
                str(python_executable or sys.executable),
                "-I",
                "-c",
                script,
                str(payload / "src"),
                connection_ref,
            ],
            cwd=self.temp_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(
            result.returncode, 0, f"{name} tool catalog failed: {result.stderr}"
        )
        catalog = json.loads(result.stdout)
        self.assertEqual(
            catalog["type_url"],
            "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse",
        )
        self.assertEqual(
            catalog["tool_ids"],
            ["email_notification_send", "email_connection_health"],
        )
        self.assertIn(
            "EmailNotificationSendArguments",
            catalog["input_schemas"][0]["$defs"],
        )

    def _assert_cloud_tool_catalog(
        self,
        *,
        name: str,
        payload: Path,
        connection_ref: str,
        environment: dict[str, str],
        python_executable: Path | None,
    ) -> None:
        """List cloud tools through the ZIP's isolated DirectPluginRuntime.

        中文：通过精确 wheelhouse 中的运行时查询 Cloud ZIP 工具目录。
        """

        script = """
import json
import sys

sys.path.insert(0, sys.argv[1])
from cyrene_plugin_runtime.client import DirectPayload, DirectPluginClient
from cloud_resources_provider._generated import tool_provider_pb2 as wire

request = wire.ListToolsRequest(binding_id="cloud-resources-package-test")
with DirectPluginClient.for_local_connection_ref(sys.argv[2]) as client:
    response_payload = client.invoke(
        capability="tool.provider.v1",
        interface_version="1",
        method="list_tools",
        request=DirectPayload(
            type_url="type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest",
            value=request.SerializeToString(),
        ),
        deadline_seconds=10,
        request_id="cloud-package-list-tools",
    )
response = wire.ListToolsResponse.FromString(response_payload.value)
print(json.dumps({
    "type_url": response_payload.type_url,
    "tool_ids": [tool.provider_tool_id for tool in response.catalog.tools],
}))
"""
        result = subprocess.run(
            [
                str(python_executable or sys.executable),
                "-I",
                "-c",
                script,
                str(payload / "src"),
                connection_ref,
            ],
            cwd=self.temp_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(
            result.returncode, 0, f"{name} tool catalog failed: {result.stderr}"
        )
        catalog = json.loads(result.stdout)
        self.assertEqual(
            catalog["type_url"],
            "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse",
        )
        self.assertEqual(
            catalog["tool_ids"],
            ["cloud_resources_query", "cloud_authorization_health"],
        )

    def _assert_office_provider_tool_catalog(
        self,
        *,
        name: str,
        payload: Path,
        connection_ref: str,
        python_executable: Path | None,
        package_id: str,
    ) -> None:
        """List each Office provider's fixed tools from its packaged runtime."""

        if package_id == "cyrene.providers.microsoft-graph":
            module_name = "microsoft_graph_provider"
            binding_id = "microsoft-graph-package-test"
            expected_tool_ids = [
                "microsoft_graph.planner.plan.get",
                "microsoft_graph.planner.tasks.list",
                "microsoft_graph.planner.task.get",
                "microsoft_graph.planner.task.create",
                "microsoft_graph.planner.task.update",
                "microsoft_graph.sharepoint.site.get",
                "microsoft_graph.sharepoint.list.get",
                "microsoft_graph.sharepoint.list.items.delta",
            ]
        elif package_id == "cyrene.providers.azure-devops-observation":
            module_name = "azure_devops_observation_provider"
            binding_id = "azure-devops-observation-package-test"
            expected_tool_ids = [
                "azure_devops.observations.poll",
                "azure_devops.pipelines.list",
                "azure_devops.pipeline_runs.list",
                "azure_devops.pipeline_run.get",
                "azure_devops.deployments.list",
                "azure_devops.release_environment.get",
            ]
        else:
            self.fail(f"unsupported Office provider package ID: {package_id}")

        script = """
import importlib
import json
import sys

sys.path.insert(0, sys.argv[1])
from cyrene_plugin_runtime.client import DirectPayload, DirectPluginClient

wire = importlib.import_module(sys.argv[3] + "._generated.tool_provider_pb2")
request = wire.ListToolsRequest(binding_id=sys.argv[4])
with DirectPluginClient.for_local_connection_ref(sys.argv[2]) as client:
    response_payload = client.invoke(
        capability="tool.provider.v1",
        interface_version="1",
        method="list_tools",
        request=DirectPayload(
            type_url="type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest",
            value=request.SerializeToString(),
        ),
        deadline_seconds=10,
        request_id="office-package-list-tools",
    )
response = wire.ListToolsResponse.FromString(response_payload.value)
print(json.dumps({
    "type_url": response_payload.type_url,
    "tool_ids": [tool.provider_tool_id for tool in response.catalog.tools],
}))
"""
        result = subprocess.run(
            [
                str(python_executable or sys.executable),
                "-I",
                "-c",
                script,
                str(payload / "src"),
                connection_ref,
                module_name,
                binding_id,
            ],
            cwd=self.temp_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(
            result.returncode, 0, f"{name} tool catalog failed: {result.stderr}"
        )
        catalog = json.loads(result.stdout)
        self.assertEqual(
            catalog["type_url"],
            "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse",
        )
        self.assertEqual(catalog["tool_ids"], expected_tool_ids)


if __name__ == "__main__":
    unittest.main()
