"""Unit tests for tools.store.profiles service profile utilities.

中文: tools.store.profiles 服务画像工具单元测试。
"""

import unittest

from tools.store.profiles import (
    JSON_PATH,
    YAML_PATH,
    get_profiles_for_plugin,
    load_profiles,
)


class ProfilesConfigTest(unittest.TestCase):
    """Validate profiles configuration and helper methods."""

    def test_files_exist(self) -> None:
        self.assertTrue(YAML_PATH.is_file(), "profiles.yaml must exist")
        self.assertTrue(JSON_PATH.is_file(), "profiles.json must exist")

    def test_load_profiles_structure(self) -> None:
        profiles = load_profiles()
        expected_keys = {"reactor", "echo", "yield", "catalyst", "core", "all"}
        self.assertTrue(expected_keys.issubset(set(profiles.keys())))

        self.assertEqual(len(profiles["all"]), 14)
        self.assertIn("cyrene.training.llama-factory", profiles["reactor"])
        self.assertIn("cyrene.serving.vllm-runtime", profiles["reactor"])
        self.assertIn("cyrene.tools.dataset-preparation", profiles["reactor"])
        self.assertIn("cyrene.tools.dataset-validator", profiles["reactor"])

        self.assertIn("cyrene.connectors.onebot-v11", profiles["echo"])
        self.assertIn("cyrene.connectors.im", profiles["echo"])
        self.assertIn("cyrene.connectors.wecom", profiles["echo"])

        self.assertIn("cyrene.evaluation.evaluator-pack", profiles["yield"])
        self.assertIn("cyrene.evaluation.exact-match", profiles["yield"])
        self.assertIn("cyrene.evaluation.llm-judge", profiles["yield"])

        self.assertIn("cyrene.providers.model-api-connector", profiles["catalyst"])
        self.assertIn("cyrene.models.hf-analyzer", profiles["catalyst"])
        self.assertIn("cyrene.policy.compat-rules", profiles["catalyst"])

        self.assertIn("cyrene.tools.dataset-validator", profiles["core"])
        self.assertIn("cyrene.policy.compat-rules", profiles["core"])

    def test_get_profiles_for_plugin(self) -> None:
        validator_profiles = get_profiles_for_plugin("cyrene.tools.dataset-validator")
        self.assertIn("reactor", validator_profiles)
        self.assertIn("core", validator_profiles)
        self.assertIn("all", validator_profiles)

        onebot_profiles = get_profiles_for_plugin("cyrene.connectors.onebot-v11")
        self.assertIn("echo", onebot_profiles)
        self.assertIn("all", onebot_profiles)

    def test_json_parity(self) -> None:
        json_data = load_profiles(JSON_PATH)
        yaml_data = load_profiles(YAML_PATH)
        self.assertEqual(json_data, yaml_data)


if __name__ == "__main__":
    unittest.main()
