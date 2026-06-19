import json
import unittest
from tests.helpers import temp_env


class TestConfig(unittest.TestCase):
    def test_defaults_when_no_file(self):
        with temp_env():
            from ccswitch.config import load_config
            cfg = load_config()
            self.assertEqual(cfg.soft_threshold, 0.90)
            self.assertEqual(cfg.hard_ceiling, 1.00)
            self.assertEqual(cfg.timezone, "America/New_York")

    def test_file_overrides_defaults(self):
        with temp_env():
            from ccswitch import paths
            from ccswitch.config import load_config
            paths.root().mkdir(parents=True, exist_ok=True)
            paths.config_path().write_text(json.dumps({"soft_threshold": 0.75, "claude_bin": "/usr/bin/claude"}))
            cfg = load_config()
            self.assertEqual(cfg.soft_threshold, 0.75)
            self.assertEqual(cfg.claude_bin, "/usr/bin/claude")
            self.assertEqual(cfg.hard_ceiling, 1.00)  # untouched default
