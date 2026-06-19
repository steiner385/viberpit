"""Tests for add_account capturing .claude.json at login time (T007 / T011).

Verifies:
  1. add_account captures <capture_dir>/.claude.json → store/<name>.claude.json (mode 0600).
  2. The shareable keys (mcpServers, projects, allowedTools) from the captured .claude.json are
     merged into store/claude.shared.json (create or update).
  3. If .claude.json is absent from the capture dir (e.g. older claude binary), no error is
     raised and no store file is written for that account.
  4. Merging into shared: existing keys in claude.shared.json are preserved; new account's keys
     are overlaid (account's value wins on conflict for the share-allowlist keys).
"""
import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock
from tests.helpers import temp_env


def _fake_claude_login(capture_dir: str, cred_data: dict, claude_json_data: dict | None) -> None:
    """Simulate what `claude auth login` writes to its CLAUDE_CONFIG_DIR."""
    cred_path = Path(capture_dir) / ".credentials.json"
    cred_path.write_text(json.dumps(cred_data))
    if claude_json_data is not None:
        cj_path = Path(capture_dir) / ".claude.json"
        cj_path.write_text(json.dumps(claude_json_data))


class TestAddAccountCapturesClaudioJson(unittest.TestCase):
    # -------------------------------------------------------------------------
    # helpers
    # -------------------------------------------------------------------------
    def _mock_subprocess_run(self, capture_dir_holder: list, cred_data: dict,
                              claude_json_data: dict | None):
        """Return a mock for subprocess.run that writes fake login artifacts."""
        def _run(cmd, env=None, **kwargs):
            capture = env.get("CLAUDE_CONFIG_DIR", "") if env else ""
            capture_dir_holder.append(capture)
            _fake_claude_login(capture, cred_data, claude_json_data)
        return mock.patch("subprocess.run", side_effect=_run)

    # -------------------------------------------------------------------------
    # T007/T011 — .claude.json captured to store
    # -------------------------------------------------------------------------
    def test_add_account_captures_claude_json_to_store(self):
        """add_account must copy .claude.json from the capture dir to store/<name>.claude.json."""
        with temp_env():
            from ccswitch import accounts, paths
            cred_data = {
                "claudeAiOauth": {
                    "accessToken": "A", "refreshToken": "R",
                    "expiresAt": 9_000_000_000_000, "subscriptionType": "max",
                },
            }
            claude_json_data = {
                "oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-a"},
                "userID": "user-a",
                "mcpServers": {"sentry": {"url": "http://sentry"}},
            }
            capture_holder: list = []
            with self._mock_subprocess_run(capture_holder, cred_data, claude_json_data):
                accounts.add_account("alpha")

            stored = paths.store_claude_json_path("alpha")
            self.assertTrue(stored.exists(),
                            "store/<name>.claude.json must exist after add_account")
            data = json.loads(stored.read_text())
            self.assertEqual(data["userID"], "user-a")
            self.assertEqual(data["oauthAccount"]["emailAddress"], "a@example.com")

    def test_add_account_stored_claude_json_has_mode_0600(self):
        """store/<name>.claude.json must be written with mode 0600."""
        with temp_env():
            from ccswitch import accounts, paths
            cred_data = {
                "claudeAiOauth": {
                    "accessToken": "A", "refreshToken": "R",
                    "expiresAt": 9_000_000_000_000, "subscriptionType": "max",
                },
            }
            claude_json_data = {
                "oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-a"},
                "userID": "user-a",
            }
            capture_holder: list = []
            with self._mock_subprocess_run(capture_holder, cred_data, claude_json_data):
                accounts.add_account("alpha")

            stored = paths.store_claude_json_path("alpha")
            mode = stat.S_IMODE(os.stat(stored).st_mode)
            self.assertEqual(mode, 0o600, "store/<name>.claude.json must have mode 0600")

    def test_add_account_merges_shareable_keys_into_shared_store(self):
        """Shareable keys from the captured .claude.json are merged into store/claude.shared.json."""
        with temp_env():
            from ccswitch import accounts, paths
            cred_data = {
                "claudeAiOauth": {
                    "accessToken": "A", "refreshToken": "R",
                    "expiresAt": 9_000_000_000_000, "subscriptionType": "max",
                },
            }
            claude_json_data = {
                "oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-a"},
                "userID": "user-a",
                "mcpServers": {"sentry": {"url": "http://sentry"}},
                "projects": ["/home/user/myproject"],
                "allowedTools": ["bash"],
                "secretKey": "should-not-be-shared",
            }
            capture_holder: list = []
            with self._mock_subprocess_run(capture_holder, cred_data, claude_json_data):
                accounts.add_account("alpha")

            shared_path = paths.shared_claude_json_path()
            self.assertTrue(shared_path.exists(),
                            "store/claude.shared.json must be created")
            shared = json.loads(shared_path.read_text())
            self.assertIn("mcpServers", shared)
            self.assertEqual(shared["mcpServers"]["sentry"]["url"], "http://sentry")
            self.assertIn("projects", shared)
            self.assertIn("allowedTools", shared)
            # non-shareable keys must NOT be in the shared file
            self.assertNotIn("secretKey", shared)
            self.assertNotIn("oauthAccount", shared)
            self.assertNotIn("userID", shared)

    def test_add_account_merges_shared_preserves_existing_keys(self):
        """Merging into claude.shared.json preserves keys from previously added accounts."""
        with temp_env():
            from ccswitch import accounts, paths
            from ccswitch.creds import write_json_secure

            # pre-seed shared with an existing server from a prior account
            write_json_secure(paths.shared_claude_json_path(), {
                "mcpServers": {"existing": {"url": "http://existing"}},
            })

            cred_data = {
                "claudeAiOauth": {
                    "accessToken": "B", "refreshToken": "R2",
                    "expiresAt": 9_000_000_000_000, "subscriptionType": "max",
                },
            }
            claude_json_data = {
                "oauthAccount": {"emailAddress": "b@example.com", "accountUuid": "uuid-b"},
                "userID": "user-b",
                "mcpServers": {"new_server": {"url": "http://new"}},
            }
            capture_holder: list = []
            with self._mock_subprocess_run(capture_holder, cred_data, claude_json_data):
                accounts.add_account("beta")

            shared = json.loads(paths.shared_claude_json_path().read_text())
            # both servers present
            self.assertIn("existing", shared["mcpServers"])
            self.assertIn("new_server", shared["mcpServers"])

    # -------------------------------------------------------------------------
    # T007/T011 — no .claude.json in capture dir (old binary)
    # -------------------------------------------------------------------------
    def test_add_account_no_claude_json_does_not_raise(self):
        """If the capture dir has no .claude.json (old binary), add_account must not raise."""
        with temp_env():
            from ccswitch import accounts, paths
            cred_data = {
                "claudeAiOauth": {
                    "accessToken": "A", "refreshToken": "R",
                    "expiresAt": 9_000_000_000_000, "subscriptionType": "max",
                },
            }
            capture_holder: list = []
            # claude_json_data=None → no .claude.json written in capture dir
            with self._mock_subprocess_run(capture_holder, cred_data, claude_json_data=None):
                accounts.add_account("alpha")  # must not raise

            # store/<name>.claude.json should NOT exist (nothing to capture)
            stored = paths.store_claude_json_path("alpha")
            self.assertFalse(stored.exists(),
                             "store/<name>.claude.json must not be written when none was captured")
