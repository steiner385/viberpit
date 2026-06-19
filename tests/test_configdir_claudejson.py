"""Tests for compose() writing a per-account .claude.json (T008).

Tests:
  1. compose() writes a REAL (non-symlink) .claude.json whose identity comes from the
     per-account store file and whose mcpServers come from the shared overlay.
  2. Migration case: no per-account store file; compose() falls back to global_claude_json()
     and overlays shared keys.
  3. When neither store nor global file exists, compose() produces a valid (empty) .claude.json.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from tests.helpers import temp_env


class TestComposeClaudioJson(unittest.TestCase):
    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _seed_per_account(self, name: str, identity: dict, shared_mcp: dict | None = None) -> None:
        """Write a store/<name>.claude.json and optionally store/claude.shared.json."""
        from ccswitch import paths
        from ccswitch.creds import write_json_secure
        write_json_secure(paths.store_claude_json_path(name), identity)
        if shared_mcp is not None:
            write_json_secure(paths.shared_claude_json_path(), {"mcpServers": shared_mcp})

    def _seed_slot(self, name: str) -> None:
        """Write a minimal .cred.json so compose() can build .credentials.json."""
        from ccswitch import paths
        from ccswitch.creds import write_json_secure
        write_json_secure(paths.slot_path(name), {"accessToken": "A", "expiresAt": 9})

    # ------------------------------------------------------------------
    # T008 case 1 — per-account store file present
    # ------------------------------------------------------------------
    def test_compose_writes_real_claude_json_not_symlink(self):
        """compose() must write .claude.json as a real file, never a symlink."""
        with temp_env():
            from ccswitch import configdir
            self._seed_slot("alpha")
            self._seed_per_account("alpha", {
                "oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-a"},
                "userID": "user-a",
            })
            cfg_dir = configdir.compose("alpha")
            cj = cfg_dir / ".claude.json"
            self.assertTrue(cj.exists(), ".claude.json must exist after compose()")
            self.assertFalse(cj.is_symlink(), ".claude.json must be a real file, not a symlink")

    def test_compose_claude_json_identity_from_per_account_store(self):
        """Identity (oauthAccount/userID) must come from the per-account store file."""
        with temp_env():
            from ccswitch import configdir
            self._seed_slot("alpha")
            self._seed_per_account("alpha", {
                "oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-a"},
                "userID": "user-a",
                "someOtherKey": "preserved",
            })
            cfg_dir = configdir.compose("alpha")
            data = json.loads((cfg_dir / ".claude.json").read_text())
            self.assertEqual(data["oauthAccount"]["emailAddress"], "a@example.com")
            self.assertEqual(data["oauthAccount"]["accountUuid"], "uuid-a")
            self.assertEqual(data["userID"], "user-a")
            self.assertEqual(data["someOtherKey"], "preserved")

    def test_compose_claude_json_overlays_shared_mcp_servers(self):
        """mcpServers from the shared overlay must appear in the composed .claude.json."""
        with temp_env():
            from ccswitch import configdir
            self._seed_slot("alpha")
            self._seed_per_account("alpha", {
                "oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-a"},
                "userID": "user-a",
            }, shared_mcp={"sentry": {"url": "https://sentry.example.com"}})
            cfg_dir = configdir.compose("alpha")
            data = json.loads((cfg_dir / ".claude.json").read_text())
            self.assertIn("mcpServers", data)
            self.assertEqual(data["mcpServers"]["sentry"]["url"], "https://sentry.example.com")
            # identity still correct
            self.assertEqual(data["userID"], "user-a")

    def test_compose_claude_json_shared_overlay_wins_for_mcp(self):
        """Shared mcpServers overlay replaces any mcpServers in the per-account file."""
        with temp_env():
            from ccswitch import configdir
            self._seed_slot("alpha")
            # per-account file has stale mcpServers
            from ccswitch import paths
            from ccswitch.creds import write_json_secure
            write_json_secure(paths.store_claude_json_path("alpha"), {
                "oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-a"},
                "userID": "user-a",
                "mcpServers": {"old_server": {"url": "old"}},
            })
            write_json_secure(paths.shared_claude_json_path(), {
                "mcpServers": {"new_server": {"url": "new"}},
            })
            cfg_dir = configdir.compose("alpha")
            data = json.loads((cfg_dir / ".claude.json").read_text())
            self.assertIn("new_server", data.get("mcpServers", {}))
            self.assertNotIn("old_server", data.get("mcpServers", {}))

    # ------------------------------------------------------------------
    # T008 case 2 — migration: no per-account store file, fall back to global
    # ------------------------------------------------------------------
    def test_compose_falls_back_to_global_claude_json_when_no_store(self):
        """Migration path: if no store/<name>.claude.json, use global HOME/.claude.json."""
        with temp_env() as d:
            from ccswitch import configdir, paths
            from ccswitch.creds import write_json_secure
            self._seed_slot("alpha")
            # Write a fake global .claude.json by monkeypatching global_claude_json()
            global_data = {
                "oauthAccount": {"emailAddress": "global@example.com", "accountUuid": "uuid-g"},
                "userID": "user-global",
            }
            fake_global = d / "fake-home-claude.json"
            fake_global.write_text(json.dumps(global_data))

            import unittest.mock as mock
            with mock.patch("ccswitch.paths.global_claude_json", return_value=fake_global):
                cfg_dir = configdir.compose("alpha")
            data = json.loads((cfg_dir / ".claude.json").read_text())
            self.assertEqual(data["userID"], "user-global")
            self.assertEqual(data["oauthAccount"]["emailAddress"], "global@example.com")

    def test_compose_migration_overlays_shared_onto_global(self):
        """Migration path: shared overlay mcpServers applied even when falling back to global."""
        with temp_env() as d:
            from ccswitch import configdir, paths
            from ccswitch.creds import write_json_secure
            self._seed_slot("alpha")
            global_data = {
                "oauthAccount": {"emailAddress": "global@example.com", "accountUuid": "uuid-g"},
                "userID": "user-global",
            }
            fake_global = d / "fake-home-claude.json"
            fake_global.write_text(json.dumps(global_data))
            write_json_secure(paths.shared_claude_json_path(), {
                "mcpServers": {"shared_srv": {"url": "shared"}},
            })
            import unittest.mock as mock
            with mock.patch("ccswitch.paths.global_claude_json", return_value=fake_global):
                cfg_dir = configdir.compose("alpha")
            data = json.loads((cfg_dir / ".claude.json").read_text())
            self.assertIn("shared_srv", data.get("mcpServers", {}))
            self.assertEqual(data["userID"], "user-global")

    # ------------------------------------------------------------------
    # T008 case 3 — neither store nor global exists → empty but valid dict
    # ------------------------------------------------------------------
    def test_compose_produces_valid_claude_json_when_nothing_exists(self):
        """When no store file and no global file, compose() writes an empty but valid dict."""
        with temp_env() as d:
            from ccswitch import configdir
            import unittest.mock as mock
            self._seed_slot("alpha")
            missing = d / "nonexistent.json"
            with mock.patch("ccswitch.paths.global_claude_json", return_value=missing):
                cfg_dir = configdir.compose("alpha")
            cj = cfg_dir / ".claude.json"
            self.assertTrue(cj.exists())
            self.assertFalse(cj.is_symlink())
            data = json.loads(cj.read_text())
            self.assertIsInstance(data, dict)

    # ------------------------------------------------------------------
    # backups/ cleanup
    # ------------------------------------------------------------------
    def test_compose_removes_backups_directory(self):
        """compose() removes a pre-existing backups/ dir inside the cfg dir."""
        with temp_env():
            from ccswitch import configdir, paths
            from ccswitch.creds import write_json_secure
            self._seed_slot("alpha")
            # pre-create the cfg dir with a backups/ subdirectory
            cfg_dir = paths.account_cfg_dir("alpha")
            cfg_dir.mkdir(parents=True, exist_ok=True)
            backups = cfg_dir / "backups"
            backups.mkdir()
            (backups / "old.json").write_text("{}")

            self._seed_per_account("alpha", {"userID": "u"})
            configdir.compose("alpha")
            self.assertFalse(backups.exists(), "backups/ must be removed by compose()")
