import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.helpers import temp_env


class TestPaths(unittest.TestCase):
    def test_root_honours_env(self):
        with temp_env():
            from viberpit import paths
            self.assertEqual(paths.root(), Path(os.environ["VIBERPIT_HOME"]))
            self.assertEqual(paths.slot_path("alpha"), paths.store_dir() / "alpha.cred.json")
            self.assertEqual(paths.account_cfg_dir("alpha"), paths.cfg_root() / "alpha")
            self.assertEqual(paths.shared_base(), Path(os.environ["CLAUDE_CONFIG_DIR"]))

    def test_archive_dir_default(self):
        """archive_dir() defaults to $VIBERPIT_HOME/archive when VIBERPIT_ARCHIVE_HOME is unset."""
        with temp_env():
            from viberpit import paths
            self.assertEqual(paths.archive_dir(), paths.root() / "archive")

    def test_archive_dir_honours_archive_home(self):
        """VIBERPIT_ARCHIVE_HOME redirects archive_dir() to a custom path."""
        with temp_env():
            from viberpit import paths
            with tempfile.TemporaryDirectory() as custom:
                with patch.dict(os.environ, {"VIBERPIT_ARCHIVE_HOME": custom}):
                    self.assertEqual(paths.archive_dir(), Path(custom))


class TestEnsureRootTransient(unittest.TestCase):
    """#3057: an sshfs blip on stat surfaced as FileExistsError from mkdir."""

    def _patched(self, stat_effects):
        import errno as _e
        real_stat = os.stat
        calls = {"n": 0}

        def fake_stat(p, *a, **k):
            if str(p) == str(self.root):
                i = calls["n"]
                calls["n"] += 1
                if i < len(stat_effects) and stat_effects[i]:
                    raise OSError(_e.EIO, "blip")
            return real_stat(p, *a, **k)
        return fake_stat

    def test_retries_through_transient_stat_error(self):
        import errno as _e
        with temp_env():
            from viberpit import paths
            self.root = paths.root()
            self.root.mkdir(parents=True, exist_ok=True)
            with patch.object(Path, "mkdir", side_effect=FileExistsError(_e.EEXIST, "x")), \
                 patch("os.stat", self._patched([True])), \
                 patch("time.sleep"):
                self.assertEqual(paths.ensure_root(), self.root)

    def test_non_directory_still_raises(self):
        with temp_env():
            from viberpit import paths
            self.root = paths.root()
            self.root.parent.mkdir(parents=True, exist_ok=True)
            self.root.write_text("not a dir")
            with self.assertRaises(FileExistsError):
                paths.ensure_root()
