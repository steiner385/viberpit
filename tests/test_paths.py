import os
import unittest
from pathlib import Path
from tests.helpers import temp_env


class TestPaths(unittest.TestCase):
    def test_root_honours_env(self):
        with temp_env():
            from ccswitch import paths
            self.assertEqual(paths.root(), Path(os.environ["CCSWITCH_HOME"]))
            self.assertEqual(paths.slot_path("alpha"), paths.store_dir() / "alpha.cred.json")
            self.assertEqual(paths.account_cfg_dir("alpha"), paths.cfg_root() / "alpha")
            self.assertEqual(paths.shared_base(), Path(os.environ["CLAUDE_CONFIG_DIR"]))
