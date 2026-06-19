"""Tests for reconcile_links() share/isolate behavior (T009 / T013).

Verifies:
  - Allowlisted entries from shared_base (plugins, commands, settings.json,
    settings.local.json, projects) are symlinked into the cfg dir.
  - .credentials.json and .claude.json are NEVER symlinked (must be real files).
  - Non-allowlisted entries (e.g. sessions/, agents/, tasks/) are NOT symlinked.
  - Behavior is idempotent (calling twice raises no error, symlinks stable).
"""
import json
import os
import unittest
from pathlib import Path
from tests.helpers import temp_env


class TestReconcileLinks(unittest.TestCase):
    # -------------------------------------------------------------------------
    # helpers
    # -------------------------------------------------------------------------
    def _seed_slot(self, name: str) -> None:
        from ccswitch import paths
        from ccswitch.creds import write_json_secure
        write_json_secure(paths.slot_path(name), {"accessToken": "A", "expiresAt": 9})

    def _seed_shared_base_entries(self, extras: list[str] | None = None) -> None:
        """Create extra entries in shared_base() to test isolation."""
        from ccswitch import paths
        base = paths.shared_base()
        # The helpers.temp_env already creates settings.json and projects/
        # Add any additional extras for this test
        for name in (extras or []):
            entry = base / name
            if name.endswith("/"):
                entry.mkdir(exist_ok=True)
            else:
                entry.write_text("{}")

    # -------------------------------------------------------------------------
    # allowlist entries are symlinked
    # -------------------------------------------------------------------------
    def test_allowlist_entries_are_symlinked(self):
        """settings.json and projects/ (seeded by temp_env) must be symlinked."""
        with temp_env():
            from ccswitch import configdir, paths
            self._seed_slot("alpha")
            cfg_dir = configdir.compose("alpha")
            shared = paths.shared_base()

            self.assertTrue((cfg_dir / "settings.json").is_symlink(),
                            "settings.json must be a symlink")
            self.assertEqual(
                Path(os.readlink(cfg_dir / "settings.json")),
                shared / "settings.json",
            )
            self.assertTrue((cfg_dir / "projects").is_symlink(),
                            "projects/ must be a symlink")
            self.assertEqual(
                Path(os.readlink(cfg_dir / "projects")),
                shared / "projects",
            )

    def test_optional_allowlist_entries_symlinked_when_present(self):
        """plugins/, commands/, and settings.local.json are symlinked when they exist."""
        with temp_env():
            from ccswitch import configdir, paths
            base = paths.shared_base()
            # create optional allowlisted entries
            (base / "plugins").mkdir()
            (base / "commands").mkdir()
            (base / "settings.local.json").write_text("{}")
            self._seed_slot("alpha")
            cfg_dir = configdir.compose("alpha")

            for name in ("plugins", "commands", "settings.local.json"):
                self.assertTrue((cfg_dir / name).is_symlink(),
                                f"{name} must be a symlink")
                self.assertEqual(
                    Path(os.readlink(cfg_dir / name)),
                    base / name,
                )

    # -------------------------------------------------------------------------
    # .credentials.json and .claude.json must be REAL files
    # -------------------------------------------------------------------------
    def test_credentials_json_is_never_a_symlink(self):
        """.credentials.json must be a real file after compose()."""
        with temp_env():
            from ccswitch import configdir
            self._seed_slot("alpha")
            cfg_dir = configdir.compose("alpha")
            cred = cfg_dir / ".credentials.json"
            self.assertTrue(cred.exists(), ".credentials.json must exist")
            self.assertFalse(cred.is_symlink(), ".credentials.json must NOT be a symlink")

    def test_claude_json_is_never_a_symlink(self):
        """.claude.json must be a real file after compose(), never a symlink."""
        with temp_env():
            from ccswitch import configdir
            self._seed_slot("alpha")
            cfg_dir = configdir.compose("alpha")
            cj = cfg_dir / ".claude.json"
            self.assertTrue(cj.exists(), ".claude.json must exist")
            self.assertFalse(cj.is_symlink(), ".claude.json must NOT be a symlink")

    # -------------------------------------------------------------------------
    # non-allowlist entries are NOT symlinked
    # -------------------------------------------------------------------------
    def test_non_allowlist_entries_not_symlinked(self):
        """Dirs like sessions/, agents/, tasks/ must NOT appear as symlinks in the cfg dir."""
        with temp_env():
            from ccswitch import configdir, paths
            base = paths.shared_base()
            # create non-allowlisted entries in shared_base
            (base / "sessions").mkdir()
            (base / "agents").mkdir()
            (base / "tasks").mkdir()
            (base / "some-random-file.txt").write_text("data")
            self._seed_slot("alpha")
            cfg_dir = configdir.compose("alpha")

            for name in ("sessions", "agents", "tasks", "some-random-file.txt"):
                entry = cfg_dir / name
                self.assertFalse(
                    entry.exists() or entry.is_symlink(),
                    f"{name} must NOT be present in cfg dir (not in allowlist)",
                )

    # -------------------------------------------------------------------------
    # idempotency
    # -------------------------------------------------------------------------
    def test_reconcile_links_is_idempotent(self):
        """Calling reconcile_links multiple times must not raise and must leave stable links."""
        with temp_env():
            from ccswitch import configdir, paths
            self._seed_slot("alpha")
            cfg_dir = configdir.compose("alpha")
            # call reconcile again
            configdir.reconcile_links("alpha")
            configdir.reconcile_links("alpha")
            # symlinks still correct
            self.assertTrue((cfg_dir / "settings.json").is_symlink())
            self.assertFalse((cfg_dir / ".credentials.json").is_symlink())
            self.assertFalse((cfg_dir / ".claude.json").is_symlink())

    def test_reconcile_links_replaces_wrong_symlink(self):
        """A symlink pointing to the wrong target is corrected by reconcile_links."""
        with temp_env():
            from ccswitch import configdir, paths
            base = paths.shared_base()
            self._seed_slot("alpha")
            cfg_dir = configdir.compose("alpha")
            # Manually point settings.json symlink to a wrong target
            (cfg_dir / "settings.json").unlink()
            (cfg_dir / "settings.json").symlink_to("/dev/null")
            # reconcile should fix it
            configdir.reconcile_links("alpha")
            self.assertEqual(
                Path(os.readlink(cfg_dir / "settings.json")),
                base / "settings.json",
            )

    # -------------------------------------------------------------------------
    # two accounts: their cfg dirs are independent
    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # stale over-share symlinks are pruned
    # -------------------------------------------------------------------------
    def test_stale_overshare_symlinks_are_pruned(self):
        """reconcile_links() removes stale symlinks into shared_base that are not allowlisted.

        Simulates a cfg dir composed by old code that symlinked sessions/ and agents/
        (which are NOT in SHARE_ALLOWLIST).  After reconcile_links(), those stale
        symlinks must be gone.  Allowlist entries (plugins/, projects) and real files
        (.credentials.json, .claude.json) must be untouched.
        """
        with temp_env():
            from ccswitch import configdir, paths
            from ccswitch.creds import write_json_secure

            base = paths.shared_base()

            # Create non-allowlisted dirs in shared_base (mimicking old over-sharing)
            (base / "sessions").mkdir()
            (base / "agents").mkdir()

            # Ensure an allowlist entry exists in shared_base so we can verify it
            (base / "plugins").mkdir()

            # Seed credentials slot so compose() can write .credentials.json
            write_json_secure(paths.slot_path("alpha"),
                              {"accessToken": "stale-test-A", "expiresAt": 9})

            # Build the cfg dir ourselves (as old code would have done) with stale symlinks
            cfg_dir = paths.account_cfg_dir("alpha")
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "sessions").symlink_to(base / "sessions")
            (cfg_dir / "agents").symlink_to(base / "agents")

            # Also seed real files that must be preserved (.credentials.json / .claude.json)
            (cfg_dir / ".credentials.json").write_text('{"accessToken":"stale-test-A"}')
            (cfg_dir / ".claude.json").write_text('{}')

            # --- call reconcile_links (the function under test) ---
            configdir.reconcile_links("alpha")

            # Stale over-share symlinks must be gone
            self.assertFalse(
                (cfg_dir / "sessions").exists() or (cfg_dir / "sessions").is_symlink(),
                "sessions symlink must be pruned (stale over-share)",
            )
            self.assertFalse(
                (cfg_dir / "agents").exists() or (cfg_dir / "agents").is_symlink(),
                "agents symlink must be pruned (stale over-share)",
            )

            # Allowlist symlinks (plugins, projects, settings.json) must be present
            self.assertTrue((cfg_dir / "plugins").is_symlink(),
                            "plugins must be a symlink (allowlist)")
            self.assertTrue((cfg_dir / "projects").is_symlink(),
                            "projects must be a symlink (allowlist)")
            self.assertTrue((cfg_dir / "settings.json").is_symlink(),
                            "settings.json must be a symlink (allowlist)")

            # Real files must not be touched
            self.assertTrue((cfg_dir / ".credentials.json").exists(),
                            ".credentials.json must still exist")
            self.assertFalse((cfg_dir / ".credentials.json").is_symlink(),
                             ".credentials.json must remain a real file")
            self.assertTrue((cfg_dir / ".claude.json").exists(),
                            ".claude.json must still exist")
            self.assertFalse((cfg_dir / ".claude.json").is_symlink(),
                             ".claude.json must remain a real file")

    def test_two_accounts_have_independent_cfg_dirs(self):
        """Each account's cfg dir is isolated — symlinks point to the same shared base but real
        files (.credentials.json, .claude.json) are separate files."""
        with temp_env():
            from ccswitch import configdir, paths
            from ccswitch.creds import write_json_secure
            for name in ("alpha", "beta"):
                write_json_secure(paths.slot_path(name),
                                  {"accessToken": f"{name}-A", "expiresAt": 9})
            cfg_alpha = configdir.compose("alpha")
            cfg_beta = configdir.compose("beta")

            # credentials are separate real files (different inodes or at least different paths)
            self.assertNotEqual(cfg_alpha / ".credentials.json", cfg_beta / ".credentials.json")
            self.assertFalse((cfg_alpha / ".credentials.json").is_symlink())
            self.assertFalse((cfg_beta / ".credentials.json").is_symlink())

            # settings.json symlinks both point to the same shared source
            shared_settings = paths.shared_base() / "settings.json"
            self.assertEqual(
                Path(os.readlink(cfg_alpha / "settings.json")), shared_settings
            )
            self.assertEqual(
                Path(os.readlink(cfg_beta / "settings.json")), shared_settings
            )
