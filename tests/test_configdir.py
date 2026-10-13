import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import temp_env


class TestConfigDir(unittest.TestCase):
    def _seed_store(self):
        from viberpit import paths
        from viberpit.creds import write_json_secure
        write_json_secure(paths.slot_path("alpha"), {"accessToken": "A", "expiresAt": 9})
        write_json_secure(paths.shared_mcp_path(), {"srv": {"k": 1}})

    def test_compose_creates_symlinks_and_credentials(self):
        with temp_env():
            from viberpit import paths, configdir
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
            from viberpit import configdir
            self._seed_store()
            configdir.compose("alpha")
            configdir.reconcile_links("alpha")  # second run must not raise
            configdir.reconcile_links("alpha")

    def test_shared_base_rejects_account_dir(self):
        """Regression: CLAUDE_CONFIG_DIR inherited from a running session points at
        an account dir. shared_base() must NOT treat an account dir as the mirror
        source (doing so symlinked accounts to themselves -> a plugins symlink loop)."""
        with temp_env():
            from viberpit import paths
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
            from viberpit import paths, configdir
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
            from viberpit import paths, configdir
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


class TestComposeOnEnvelopeRunner(unittest.TestCase):
    """#2629: a runner launched from a broker envelope never composes a claudeAiOauth blob."""

    def test_no_blob_and_sync_back_is_noop(self):
        with temp_env():
            from viberpit import accounts, configdir, credential_envelope as ce, paths
            from viberpit.creds import write_json_secure
            write_json_secure(paths.slot_path("alpha"), {"accessToken": "A", "expiresAt": 9})
            write_json_secure(paths.shared_mcp_path(), {"srv": {"k": 1}})
            ce.reset_runner_store()
            try:
                with mock.patch.dict(os.environ, {"VIBERPIT_DAEMON_ROLE": "worker",
                                                  "VIBERPIT_CLAUDE_CREDS_DIR": str(paths.root() / "ec")}):
                    # Before any envelope arrives the pre-broker path is unchanged.
                    cfg = configdir.compose("alpha")
                    self.assertIn("claudeAiOauth", json.loads((cfg / ".credentials.json").read_text()))
                    ce.runner_store().sync([ce.Envelope("alpha", ce.SETUP_TOKEN, "t", None, 1)], 60)
                    cfg = configdir.compose("alpha")
                    data = json.loads((cfg / ".credentials.json").read_text())
                    self.assertNotIn("claudeAiOauth", data)
                    # A refreshed blob in the cfg dir must not be copied into the slot.
                    write_json_secure(cfg / ".credentials.json",
                                      {"claudeAiOauth": {"accessToken": "ROTATED", "expiresAt": 99}})
                    accounts.sync_back("alpha")
                    self.assertEqual(json.loads(paths.slot_path("alpha").read_text())["accessToken"], "A")
            finally:
                ce.reset_runner_store()


# --- compose publishes the shared MCP store's public view (spec 2026-10-13) ----------------

def _compose_with(dir_mcp, store_mcp):
    from viberpit import configdir, paths
    from viberpit.creds import write_json_secure
    write_json_secure(paths.slot_path("alpha"), {"accessToken": "A", "expiresAt": 9})
    write_json_secure(paths.shared_mcp_path(), store_mcp)
    if dir_mcp is not None:
        write_json_secure(paths.account_cfg_dir("alpha") / ".credentials.json",
                          {"claudeAiOauth": {"accessToken": "A"}, "mcpOAuth": dir_mcp})
    cfg = configdir.compose("alpha")
    return (json.loads((cfg / ".credentials.json").read_text()),
            json.loads(paths.shared_mcp_path().read_text()))


def test_compose_never_writes_a_refresh_token():
    from viberpit.creds import public_view
    bundle = {"serverName": "aws", "accessToken": "a", "refreshToken": "r", "expiresAt": 5}
    with temp_env():
        out, store = _compose_with(None, {"k": bundle})
        assert out["mcpOAuth"] == {"k": public_view(bundle)}
        assert out["claudeAiOauth"]["accessToken"] == "A"
        assert store == {"k": bundle}


def test_compose_captures_a_newer_dir_rotation_instead_of_rolling_it_back():
    from viberpit.creds import public_view
    old = {"serverName": "aws", "accessToken": "a1", "refreshToken": "r1", "expiresAt": 5}
    rotated = {"serverName": "aws", "accessToken": "a2", "refreshToken": "r2", "expiresAt": 9}
    with temp_env():
        out, store = _compose_with({"k": rotated}, {"k": old})
        assert store == {"k": rotated}
        assert out["mcpOAuth"] == {"k": public_view(rotated)}


def test_compose_does_not_resurrect_a_cleared_bundle():
    cleared = {"serverName": "aws", "accessToken": "", "expiresAt": 0, "clientId": "c"}
    with temp_env():
        out, store = _compose_with({"k": cleared}, {"k": cleared})
        assert out["mcpOAuth"] == {"k": cleared}
        assert store == {"k": cleared}


def test_compose_keeps_and_shares_a_dir_only_key_without_refresh_token():
    local = {"serverName": "x", "accessToken": "t", "expiresAt": 3}
    with temp_env():
        out, store = _compose_with({"x": local}, {"srv": {"k": 1}})
        assert out["mcpOAuth"] == {"x": local, "srv": {"k": 1}}
        assert store == {"srv": {"k": 1}, "x": local}


def test_compose_live_store_token_replaces_an_empty_dir_copy():
    from viberpit.creds import public_view
    live = {"serverName": "aws", "accessToken": "a", "refreshToken": "r", "expiresAt": 9}
    with temp_env():
        out, _ = _compose_with({"k": {"serverName": "aws", "accessToken": "", "expiresAt": 0}}, {"k": live})
        assert out["mcpOAuth"] == {"k": public_view(live)}
