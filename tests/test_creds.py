import json
import os
import stat
import unittest
from tests.helpers import temp_env


class TestCreds(unittest.TestCase):
    def test_split_and_compose_roundtrip(self):
        from ccswitch.creds import split_credentials, compose_credentials
        full = {"mcpOAuth": {"srv": {"x": 1}}, "claudeAiOauth": {"accessToken": "a", "expiresAt": 5}}
        claude_oauth, mcp = split_credentials(full)
        self.assertEqual(claude_oauth["accessToken"], "a")
        self.assertEqual(mcp, {"srv": {"x": 1}})
        composed = compose_credentials(claude_oauth, mcp)
        self.assertEqual(composed["claudeAiOauth"]["accessToken"], "a")
        self.assertEqual(composed["mcpOAuth"], {"srv": {"x": 1}})

    def test_split_missing_keys_defaults_empty(self):
        from ccswitch.creds import split_credentials
        claude_oauth, mcp = split_credentials({})
        self.assertEqual(claude_oauth, {})
        self.assertEqual(mcp, {})

    def test_merge_keep_later_prefers_later_expiry(self):
        from ccswitch.creds import merge_keep_later
        old = {"accessToken": "old", "expiresAt": 100}
        new = {"accessToken": "new", "expiresAt": 200}
        self.assertEqual(merge_keep_later(old, new)["accessToken"], "new")
        self.assertEqual(merge_keep_later(new, old)["accessToken"], "new")
        self.assertEqual(merge_keep_later(None, new)["accessToken"], "new")

    def test_write_json_secure_is_0600(self):
        with temp_env():
            from ccswitch import paths
            from ccswitch.creds import write_json_secure, read_json
            paths.store_dir().mkdir(parents=True)
            p = paths.slot_path("alpha")
            write_json_secure(p, {"k": "v"})
            mode = stat.S_IMODE(os.stat(p).st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(read_json(p), {"k": "v"})
