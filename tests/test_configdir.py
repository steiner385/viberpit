import json
import os
import unittest
from pathlib import Path
from unittest import mock

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

    def test_shared_base_rejects_account_dir(self):
        """Regression: CLAUDE_CONFIG_DIR inherited from a running session points at
        an account dir. shared_base() must NOT treat an account dir as the mirror
        source (doing so symlinked accounts to themselves -> a plugins symlink loop)."""
        with temp_env():
            from ccswitch import paths
            acct = paths.account_cfg_dir("alpha")
            acct.mkdir(parents=True, exist_ok=True)
            os.environ["CLAUDE_CONFIG_DIR"] = str(acct)
            self.assertNotEqual(paths.shared_base().resolve(), acct.resolve())
            self.assertEqual(paths.shared_base(), Path.home() / ".claude")

    def test_compose_never_creates_plugins_loop(self):
        """End-to-end: composing an account while CLAUDE_CONFIG_DIR points at an
        account dir must mirror plugins from the canonical ~/.claude, never produce
        a self-referential or into-the-store symlink."""
        with temp_env() as d:
            from ccswitch import paths, configdir
            self._seed_store()
            real_home = d / "real-home"
            (real_home / ".claude" / "plugins").mkdir(parents=True)
            paths.account_cfg_dir("alpha").mkdir(parents=True, exist_ok=True)
            # Simulate the inherited per-account env that triggered the bug.
            os.environ["CLAUDE_CONFIG_DIR"] = str(paths.account_cfg_dir("alpha"))
            with mock.patch.object(Path, "home", staticmethod(lambda: real_home)):
                configdir.compose("alpha")
                link = paths.account_cfg_dir("alpha") / "plugins"
                self.assertTrue(link.is_symlink())
                target = Path(os.readlink(link))
                # not a self-loop (compare target to the link's OWN path, not the
                # resolved path — resolving follows the link to its target), and not
                # pointing into the account store
                link_self = link.parent.resolve() / link.name
                self.assertNotEqual(target.resolve(), link_self)
                with self.assertRaises(ValueError):
                    target.resolve().relative_to(paths.cfg_root().resolve())
                self.assertEqual(target.resolve(), (real_home / ".claude" / "plugins").resolve())

    def test_compose_heals_existing_loop(self):
        """A cfg dir left with a self-referential plugins link by old buggy code is
        repaired on the next compose() — the stale link no longer matches the
        corrected source, so it is unlinked and recreated against ~/.claude."""
        with temp_env() as d:
            from ccswitch import paths, configdir
            self._seed_store()
            real_home = d / "real-home"
            (real_home / ".claude" / "plugins").mkdir(parents=True)
            acct = paths.account_cfg_dir("alpha")
            acct.mkdir(parents=True, exist_ok=True)
            # Plant the broken self-loop the old code produced.
            loop = acct / "plugins"
            loop.symlink_to(loop)
            self.assertTrue(loop.is_symlink())
            # The production trigger: an inherited per-account CLAUDE_CONFIG_DIR,
            # which makes shared_base() fall back to the canonical ~/.claude.
            os.environ["CLAUDE_CONFIG_DIR"] = str(acct)
            with mock.patch.object(Path, "home", staticmethod(lambda: real_home)):
                configdir.compose("alpha")
                target = Path(os.readlink(acct / "plugins"))
                self.assertEqual(target.resolve(), (real_home / ".claude" / "plugins").resolve())
