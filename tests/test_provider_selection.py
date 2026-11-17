"""Provider selection must keep endpoint, model and credentials together."""
import contextlib
import io
import unittest
from unittest.mock import patch

from experiments.transition_guard_p4.check_output_limit import choose_provider, configured_key, resolve_target


ALIYUN = "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


class ProviderSelectionTests(unittest.TestCase):
    def test_official_choice_does_not_inherit_aliyun_model_or_address(self):
        env = {"OPENAI_BASE_URL": ALIYUN, "MEMFORM_MODEL": "deepseek-v4.1-flash-1017"}
        self.assertEqual(resolve_target("1", None, None, env), ("https://api.deepseek.com", "deepseek-flash"))

    def test_aliyun_choice_does_not_inherit_official_model(self):
        env = {"OPENAI_BASE_URL": "https://api.deepseek.com", "MEMFORM_MODEL": "deepseek-flash", "ALIYUN_BASE_URL": ALIYUN}
        self.assertEqual(resolve_target("2", None, None, env), (ALIYUN, "deepseek-v4.1-flash-1017"))

    def test_existing_aliyun_address_is_reused_without_reading_another_model(self):
        self.assertEqual(resolve_target("aliyun", None, None, {"OPENAI_BASE_URL": ALIYUN}),
                         (ALIYUN, "deepseek-v4.1-flash-1017"))

    def test_explicit_model_override_is_honored(self):
        self.assertEqual(resolve_target("1", None, "deepseek-v4.1-flash", {}),
                         ("https://api.deepseek.com", "deepseek-v4.1-flash"))

    def test_crossed_endpoint_and_missing_workspace_are_rejected_before_request(self):
        with self.assertRaises(ValueError):
            resolve_target("1", ALIYUN, None, {})
        with self.assertRaises(ValueError):
            resolve_target("2", None, None, {"OPENAI_BASE_URL": "https://api.deepseek.com"})

    def test_menu_reprompts_invalid_selection(self):
        with patch("builtins.input", side_effect=["invalid", "1"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(choose_provider(), "deepseek")
        with patch("builtins.input", return_value="2"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(choose_provider(), "aliyun")

    def test_generic_key_is_never_borrowed_from_other_platform_or_workspace(self):
        env = {"OPENAI_BASE_URL": ALIYUN, "OPENAI_API_KEY": "test-placeholder"}
        self.assertIsNone(configured_key("deepseek", "https://api.deepseek.com", env))
        self.assertIsNone(configured_key("aliyun", ALIYUN.replace("ws-example", "ws-other"), env))
        self.assertEqual(configured_key("aliyun", ALIYUN, env), "test-placeholder")

    def test_dedicated_key_and_explicit_environment_mode(self):
        env = {"OPENAI_BASE_URL": ALIYUN, "MEMFORM_MODEL": "custom-model", "DEEPSEEK_API_KEY": "official-placeholder"}
        self.assertEqual(configured_key("deepseek", "https://api.deepseek.com", env), "official-placeholder")
        self.assertEqual(resolve_target("env", None, None, env), (ALIYUN, "custom-model"))


if __name__ == "__main__":
    unittest.main()
