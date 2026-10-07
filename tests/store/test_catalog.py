"""Unit tests for tools.store.catalog plugin catalog engine.

中文: tools.store.catalog 插件 Catalog 生成与索引引擎单元测试。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import ClassVar

# Ensure REPO_ROOT is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.store.catalog import (
    CANONICAL_SERVICES,
    SCHEMA_VERSION,
    build_catalog,
    discover_plugin_manifests,
    get_plugin,
    get_profile_plugins,
    get_service_plugins,
    list_plugins,
    load_catalog,
    parse_plugin_manifest,
)


class PluginCatalogTest(unittest.TestCase):
    """Test suite covering plugin discovery, metadata extraction, profile/language filtering,

    and catalog export format and field integrity.
    """

    EXPECTED_PLUGIN_COUNT = 17
    EXPECTED_DATA_TOOLS_METADATA: ClassVar[dict[str, tuple[str, str]]] = {
        "cyrene.tools.document-parsing": ("Document Parsing", "document.parsing.v1"),
        "cyrene.tools.knowledge-preparation": (
            "Knowledge Preparation",
            "dataset.knowledge.v1",
        ),
        "cyrene.tools.dataset-generation": (
            "Dataset Generation",
            "dataset.generation.v1",
        ),
    }
    REQUIRED_METADATA_FIELDS: ClassVar[set[str]] = {
        "id",
        "name",
        "version",
        "description",
        "language",
        "kind",
        "capabilities",
        "supportedServices",
        "profiles",
        "runtime",
        "contributions",
        "artifacts",
    }

    def setUp(self) -> None:
        self.catalog = load_catalog(root_dir=REPO_ROOT)

    def test_discover_all_17_plugins_and_metadata_extraction(self) -> None:
        """1. Test discovery of all 17 official plugins and verify metadata extraction."""
        manifests = discover_plugin_manifests(REPO_ROOT)
        self.assertEqual(
            len(manifests),
            self.EXPECTED_PLUGIN_COUNT,
            f"Expected {self.EXPECTED_PLUGIN_COUNT} plugin manifests, found {len(manifests)}",
        )

        plugin_ids: set[str] = set()
        data_tools_ids: set[str] = set()
        for manifest_path in manifests:
            meta = parse_plugin_manifest(manifest_path, root=REPO_ROOT)

            # Check that all 11 required fields are present
            missing_fields = self.REQUIRED_METADATA_FIELDS - set(meta.keys())
            self.assertFalse(
                missing_fields,
                f"Plugin {manifest_path} is missing required metadata fields: {missing_fields}",
            )

            # Check field types and validity
            self.assertTrue(isinstance(meta["id"], str) and len(meta["id"]) > 0)
            self.assertTrue(isinstance(meta["name"], str) and len(meta["name"]) > 0)
            self.assertTrue(isinstance(meta["version"], str) and len(meta["version"]) > 0)
            self.assertTrue(isinstance(meta["description"], str))
            self.assertIn(meta["language"], ("python", "rust", "csharp"))
            self.assertTrue(isinstance(meta["kind"], str) and len(meta["kind"]) > 0)
            self.assertTrue(isinstance(meta["capabilities"], list) and len(meta["capabilities"]) > 0)
            self.assertTrue(isinstance(meta["profiles"], list))
            self.assertTrue(isinstance(meta["runtime"], dict))
            self.assertTrue(isinstance(meta["contributions"], dict))
            self.assertTrue(isinstance(meta["artifacts"], list))

            # Helper path fields
            self.assertTrue(isinstance(meta.get("path"), str))
            self.assertTrue(isinstance(meta.get("manifest_path"), str))

            plugin_ids.add(meta["id"])
            expected_data_tools = self.EXPECTED_DATA_TOOLS_METADATA.get(meta["id"])
            if expected_data_tools is not None:
                expected_name, expected_capability = expected_data_tools
                self.assertEqual(meta["name"], expected_name)
                self.assertEqual(meta["language"], "python")
                self.assertEqual(meta["kind"], "capability-plugin")
                self.assertEqual(meta["capabilities"], [expected_capability])
                self.assertEqual(meta["supportedServices"], ["Cyrene-Catalyst"])
                data_tools_ids.add(meta["id"])

        self.assertEqual(
            len(plugin_ids),
            self.EXPECTED_PLUGIN_COUNT,
            "All 17 discovered plugins must have unique IDs",
        )
        self.assertEqual(data_tools_ids, set(self.EXPECTED_DATA_TOOLS_METADATA))

    def test_profile_filtering(self) -> None:
        """2. Test profile filtering (reactor filters out 4 plugins, echo filters out 3 plugins)."""
        # Reactor profile: exactly 4 plugins
        reactor_plugins = list_plugins(profile="reactor", catalog=self.catalog)
        self.assertEqual(
            len(reactor_plugins),
            4,
            f"Reactor profile should filter out 4 plugins, got {len(reactor_plugins)}",
        )
        reactor_ids = {p["id"] for p in reactor_plugins}
        expected_reactor_ids = {
            "cyrene.training.llama-factory",
            "cyrene.serving.vllm-runtime",
            "cyrene.tools.dataset-preparation",
            "cyrene.tools.dataset-validator",
        }
        self.assertEqual(reactor_ids, expected_reactor_ids)

        # get_profile_plugins API parity
        profile_api_reactor = get_profile_plugins("reactor", catalog=self.catalog)
        self.assertEqual({p["id"] for p in profile_api_reactor}, expected_reactor_ids)

        # Echo profile: exactly 3 plugins
        echo_plugins = list_plugins(profile="echo", catalog=self.catalog)
        self.assertEqual(
            len(echo_plugins),
            3,
            f"Echo profile should filter out 3 plugins, got {len(echo_plugins)}",
        )
        echo_ids = {p["id"] for p in echo_plugins}
        expected_echo_ids = {
            "cyrene.connectors.onebot-v11",
            "cyrene.connectors.im",
            "cyrene.connectors.wecom",
        }
        self.assertEqual(echo_ids, expected_echo_ids)

        profile_api_echo = get_profile_plugins("echo", catalog=self.catalog)
        self.assertEqual({p["id"] for p in profile_api_echo}, expected_echo_ids)

        # Case-insensitive profile test
        case_insensitive_reactor = list_plugins(profile="REACTOR", catalog=self.catalog)
        self.assertEqual(len(case_insensitive_reactor), 4)

        # Non-existent profile test
        empty_plugins = list_plugins(profile="non_existent_profile_xyz", catalog=self.catalog)
        self.assertEqual(len(empty_plugins), 0)

    def test_language_filtering(self) -> None:
        """3. Test language filtering across python (14), rust (1), and csharp (2)."""
        # Python: 14 plugins
        python_plugins = list_plugins(language="python", catalog=self.catalog)
        self.assertEqual(
            len(python_plugins),
            14,
            f"Expected 14 Python plugins, got {len(python_plugins)}",
        )
        for p in python_plugins:
            self.assertEqual(p["language"], "python")

        # Rust: 1 plugin
        rust_plugins = list_plugins(language="rust", catalog=self.catalog)
        self.assertEqual(
            len(rust_plugins),
            1,
            f"Expected 1 Rust plugin, got {len(rust_plugins)}",
        )
        self.assertEqual(rust_plugins[0]["id"], "cyrene.tools.computer-runtime")
        self.assertEqual(rust_plugins[0]["language"], "rust")

        # CSharp: 2 plugins
        csharp_plugins = list_plugins(language="csharp", catalog=self.catalog)
        self.assertEqual(
            len(csharp_plugins),
            2,
            f"Expected 2 CSharp plugins, got {len(csharp_plugins)}",
        )
        csharp_ids = {p["id"] for p in csharp_plugins}
        self.assertEqual(csharp_ids, {"cyrene.connectors.im", "cyrene.connectors.onebot-v11"})
        for p in csharp_plugins:
            self.assertEqual(p["language"], "csharp")

        # Sum of languages must equal total 17 plugins
        self.assertEqual(
            len(python_plugins) + len(rust_plugins) + len(csharp_plugins),
            self.EXPECTED_PLUGIN_COUNT,
        )

        # Case-insensitivity and aliases
        self.assertEqual(len(list_plugins(language="CSharp", catalog=self.catalog)), 2)
        self.assertEqual(len(list_plugins(language="c#", catalog=self.catalog)), 2)
        self.assertEqual(len(list_plugins(language="dotnet", catalog=self.catalog)), 2)
        self.assertEqual(len(list_plugins(language="RUST", catalog=self.catalog)), 1)
        self.assertEqual(len(list_plugins(language="Python", catalog=self.catalog)), 14)

    def test_export_format_and_field_integrity(self) -> None:
        """4. Test plugins-catalog.json export format, schemaVersion, and field integrity."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "dist" / "plugins" / "plugins-catalog.json"
            build_catalog(output_path=out_file, root_dir=REPO_ROOT)

            # Check that file was created
            self.assertTrue(out_file.is_file(), f"Export file {out_file} must exist")

            # Validate loaded content from disk
            loaded = load_catalog(out_file)

            # Top-level schema properties
            self.assertEqual(loaded["schemaVersion"], SCHEMA_VERSION)
            self.assertEqual(loaded["schemaVersion"], "cyrene.plugins.catalog.v1")
            self.assertIn("generatedAt", loaded)
            self.assertEqual(loaded["totalPlugins"], self.EXPECTED_PLUGIN_COUNT)
            self.assertEqual(len(loaded["plugins"]), self.EXPECTED_PLUGIN_COUNT)
            self.assertTrue(isinstance(loaded["profiles"], dict))
            self.assertIn("reactor", loaded["profiles"])
            self.assertIn("echo", loaded["profiles"])

            # Field integrity for every single plugin in exported catalog
            for plugin in loaded["plugins"]:
                for field in self.REQUIRED_METADATA_FIELDS:
                    self.assertIn(
                        field,
                        plugin,
                        f"Plugin {plugin.get('id')} in exported catalog is missing field '{field}'",
                    )
                    self.assertIsNotNone(
                        plugin[field],
                        f"Plugin {plugin.get('id')} field '{field}' must not be None",
                    )

                # Type checks
                self.assertIsInstance(plugin["id"], str)
                self.assertIsInstance(plugin["name"], str)
                self.assertIsInstance(plugin["version"], str)
                self.assertIsInstance(plugin["description"], str)
                self.assertIsInstance(plugin["language"], str)
                self.assertIsInstance(plugin["kind"], str)
                self.assertIsInstance(plugin["capabilities"], list)
                self.assertIsInstance(plugin["profiles"], list)
                self.assertIsInstance(plugin["runtime"], dict)
                self.assertIsInstance(plugin["contributions"], dict)
                self.assertIsInstance(plugin["artifacts"], list)

    def test_get_plugin_and_keyword_search(self) -> None:
        """5. Test get_plugin lookup and keyword search capabilities."""
        vllm = get_plugin("cyrene.serving.vllm-runtime", catalog=self.catalog)
        self.assertIsNotNone(vllm)
        self.assertEqual(vllm["id"], "cyrene.serving.vllm-runtime")
        self.assertIn("execution.engine.v1", vllm["capabilities"])

        unknown = get_plugin("non.existent.plugin", catalog=self.catalog)
        self.assertIsNone(unknown)

        # Keyword search
        vllm_search = list_plugins(keyword="vllm", catalog=self.catalog)
        self.assertTrue(any(p["id"] == "cyrene.serving.vllm-runtime" for p in vllm_search))

        onebot_search = list_plugins(keyword="onebot", catalog=self.catalog)
        self.assertTrue(any(p["id"] == "cyrene.connectors.onebot-v11" for p in onebot_search))

        # Combined filter: profile + language
        reactor_python = list_plugins(profile="reactor", language="python", catalog=self.catalog)
        self.assertEqual(len(reactor_python), 4)
        for p in reactor_python:
            self.assertEqual(p["language"], "python")
            self.assertIn("reactor", p["profiles"])

    def test_supported_services_indexing_and_filtering(self) -> None:
        """6. Test supportedServices metadata extraction, by_service index, and service query filtering."""
        # 1. Verify by_service index in catalog
        self.assertIn("indexes", self.catalog)
        self.assertIn("by_service", self.catalog["indexes"])
        by_service = self.catalog["indexes"]["by_service"]

        for s in CANONICAL_SERVICES:
            self.assertIn(s, by_service)

        # 2. Check canonical counts per service
        self.assertEqual(len(by_service["Cyrene-Navigator"]), 5)
        self.assertEqual(len(by_service["Cyrene-Echo"]), 3)
        self.assertEqual(len(by_service["Cyrene-Reactor"]), 3)
        self.assertEqual(len(by_service["Cyrene-Yield"]), 3)
        self.assertEqual(len(by_service["Cyrene-Exchange"]), 4)
        self.assertEqual(len(by_service["Cyrene-Catalyst"]), 5)
        self.assertEqual(
            set(by_service["Cyrene-Catalyst"]),
            {
                "cyrene.tools.dataset-generation",
                "cyrene.tools.dataset-preparation",
                "cyrene.tools.dataset-validator",
                "cyrene.tools.document-parsing",
                "cyrene.tools.knowledge-preparation",
            },
        )
        self.assertEqual(len(by_service["Cyrene-Platform"]), 0)

        # 3. Check specific plugins under Cyrene-Navigator
        navigator_plugins = list_plugins(service="Cyrene-Navigator", catalog=self.catalog)
        self.assertEqual(len(navigator_plugins), 5)
        navigator_ids = {p["id"] for p in navigator_plugins}
        expected_navigator_ids = {
            "cyrene.connectors.im",
            "cyrene.connectors.onebot-v11",
            "cyrene.connectors.wecom",
            "cyrene.providers.model-api-connector",
            "cyrene.tools.computer-runtime",
        }
        self.assertEqual(navigator_ids, expected_navigator_ids)

        # Parity with get_service_plugins
        api_navigator = get_service_plugins("Cyrene-Navigator", catalog=self.catalog)
        self.assertEqual({p["id"] for p in api_navigator}, expected_navigator_ids)

        # 4. Check case-insensitivity and prefix stripping
        self.assertEqual(len(list_plugins(service="cyrene-navigator", catalog=self.catalog)), 5)
        self.assertEqual(len(list_plugins(service="navigator", catalog=self.catalog)), 5)
        self.assertEqual(len(list_plugins(service="echo", catalog=self.catalog)), 3)
        self.assertEqual(len(list_plugins(service="CYRENE-ECHO", catalog=self.catalog)), 3)

        # 5. Non-existent service returns empty list
        self.assertEqual(len(list_plugins(service="unknown-service", catalog=self.catalog)), 0)


if __name__ == "__main__":
    unittest.main()
