import json
import unittest
from tests.helpers import temp_env


class TestConfigDir(unittest.TestCase):
    def _seed_store(self):
        from ccswitch import paths
        from ccswitch.creds import write_json_secure
        write_json_secure(paths.slot_path("alpha"), {"accessToken": "A", "expiresAt": 9})
        write_json_secure(paths.shared_mcp_path(), {"srv": {"k": 1}})

    def test_compose_creates_symlinks_and_credentials(self):
        with temp_env():
            from ccswitch import paths, configdir
            self._seed_store()
            cfg_dir = configdir.compose("alpha")
            # settings.json + projects mirrored from shared base as symlinks
            self.assertTrue((cfg_dir / "settings.json").is_symlink())
            self.assertTrue((cfg_dir / "projects").is_symlink())
            self.assertEqual((cfg_dir / "projects").resolve(), (paths.shared_base() / "projects").resolve())
            # .credentials.json is a real composed file
            self.assertFalse((cfg_dir / ".credentials.json").is_symlink())
            data = json.loads((cfg_dir / ".credentials.json").read_text())
            self.assertEqual(data["claudeAiOauth"]["accessToken"], "A")
            self.assertEqual(data["mcpOAuth"], {"srv": {"k": 1}})

    def test_reconcile_is_idempotent(self):
        with temp_env():
            from ccswitch import configdir
            self._seed_store()
            configdir.compose("alpha")
            configdir.reconcile_links("alpha")  # second run must not raise
            configdir.reconcile_links("alpha")
