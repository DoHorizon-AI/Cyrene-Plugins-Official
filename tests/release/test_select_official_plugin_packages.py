"""Tests for the fixed official Python package selection boundary.

中文：验证官方 Python package 发布选择器的固定集合与发布模式。
"""

from __future__ import annotations

import unittest

from tooling.release import select_official_plugin_packages as selection


class OfficialPackageSelectionTests(unittest.TestCase):
    """Keep event behavior and package matrices bound to fixed identities."""

    def test_push_keeps_the_legacy_five_package_draft_set(self) -> None:
        """Automatic pushes retain the pre-existing draft package selection."""

        matrix, publish_mode = selection.select_package_release("push", None)

        self.assertEqual(
            [row["package_id"] for row in matrix["include"]],
            list(selection.LEGACY_PACKAGE_IDS),
        )
        self.assertEqual(len(selection.LEGACY_PACKAGE_IDS), 5)
        self.assertEqual(publish_mode, "draft")

    def test_pull_request_validates_all_ten_packages_without_publishing(self) -> None:
        """Pull requests cover all packages while keeping publication disabled."""

        matrix, publish_mode = selection.select_package_release(
            "pull_request", "authorization-cloud-v1"
        )
        package_ids = [row["package_id"] for row in matrix["include"]]

        self.assertEqual(package_ids, list(selection.ALL_PACKAGE_IDS))
        self.assertEqual(len(package_ids), 10)
        self.assertEqual(len(set(package_ids)), 10)
        self.assertEqual(publish_mode, "none")

    def test_authorization_cloud_named_set_is_immutable_and_exact(self) -> None:
        """Only cloud resources, email notification, and WeCom join this set."""

        matrix, publish_mode = selection.select_package_release(
            "workflow_dispatch", "authorization-cloud-v1"
        )

        self.assertEqual(
            [row["package_id"] for row in matrix["include"]],
            list(selection.AUTHORIZATION_CLOUD_V1_PACKAGE_IDS),
        )
        self.assertEqual(
            selection.AUTHORIZATION_CLOUD_V1_PACKAGE_IDS,
            (
                "cyrene.providers.cloud-resources",
                "cyrene.connectors.email-notification",
                "cyrene.connectors.wecom",
            ),
        )
        self.assertEqual(publish_mode, "immutable")

    def test_integration_v1_remains_immutable_and_unchanged(self) -> None:
        """The existing Integration v1 package set remains intact."""

        matrix, publish_mode = selection.select_package_release(
            "workflow_dispatch", "integration-v1"
        )

        self.assertEqual(
            [row["package_id"] for row in matrix["include"]],
            [
                "cyrene.accounts.account-center",
                "cyrene.providers.github-issues",
                "cyrene.connectors.wecom",
            ],
        )
        self.assertEqual(publish_mode, "immutable")

    def test_new_single_package_dispatches_are_allowlisted_immutable_releases(
        self,
    ) -> None:
        """Manual cloud and email package selections are fixed immutable builds."""

        for package_id, expected_name in (
            ("cyrene.providers.cloud-resources", "cloud-resources"),
            ("cyrene.connectors.email-notification", "email-notification"),
        ):
            with self.subTest(package_id=package_id):
                matrix, publish_mode = selection.select_package_release(
                    "workflow_dispatch", package_id
                )
                self.assertEqual(
                    matrix["include"],
                    [{"name": expected_name, "package_id": package_id}],
                )
                self.assertEqual(publish_mode, "immutable")

    def test_unknown_dispatch_values_fail_closed(self) -> None:
        """Arbitrary package selectors do not affect the release matrix."""

        with self.assertRaisesRegex(ValueError, "fixed official package allowlist"):
            selection.select_package_release("workflow_dispatch", "arbitrary-package")


if __name__ == "__main__":
    unittest.main()
