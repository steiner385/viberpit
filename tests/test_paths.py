import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.helpers import temp_env


class TestPaths(unittest.TestCase):
    def test_root_honours_env(self):
        with temp_env():
            from ccswitch import paths
            self.assertEqual(paths.root(), Path(os.environ["CCSWITCH_HOME"]))
            self.assertEqual(paths.slot_path("alpha"), paths.store_dir() / "alpha.cred.json")
            self.assertEqual(paths.account_cfg_dir("alpha"), paths.cfg_root() / "alpha")
            self.assertEqual(paths.shared_base(), Path(os.environ["CLAUDE_CONFIG_DIR"]))

    def test_archive_dir_default(self):
        """archive_dir() defaults to $CCSWITCH_HOME/archive when CCSWITCH_ARCHIVE_HOME is unset."""
        with temp_env():
            from ccswitch import paths
            self.assertEqual(paths.archive_dir(), paths.root() / "archive")

    def test_archive_dir_honours_archive_home(self):
        """CCSWITCH_ARCHIVE_HOME redirects archive_dir() to a custom path."""
        with temp_env():
            from ccswitch import paths
            with tempfile.TemporaryDirectory() as custom:
                with patch.dict(os.environ, {"CCSWITCH_ARCHIVE_HOME": custom}):
                    self.assertEqual(paths.archive_dir(), Path(custom))
