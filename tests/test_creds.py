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

    def test_merge_keep_later_tie_keeps_existing(self):
        from ccswitch.creds import merge_keep_later
        # Equal expiresAt → existing wins (>= keeps existing, monotonic-refresh idempotency)
        existing = {"accessToken": "existing", "expiresAt": 300}
        incoming = {"accessToken": "incoming", "expiresAt": 300}
        result = merge_keep_later(existing, incoming)
        self.assertIs(result, existing)
        self.assertEqual(result["accessToken"], "existing")

    def test_merge_keep_later_empty_dict_is_real_blob(self):
        from ccswitch.creds import merge_keep_later
        incoming = {"accessToken": "fresh", "expiresAt": 100}
        # Empty dict {} is NOT treated as absent — it is a real blob with expiresAt 0
        # incoming has higher expiresAt (100 > 0), so incoming wins
        result = merge_keep_later({}, incoming)
        self.assertIs(result, incoming)
        self.assertEqual(result["accessToken"], "fresh")

    def test_merge_keep_later_empty_dict_higher_expiry_than_incoming(self):
        from ccswitch.creds import merge_keep_later
        # existing={} has expiresAt=0 (missing key defaults to 0)
        # incoming has expiresAt=0 too → existing wins via >= (tie goes to existing)
        result = merge_keep_later({}, {"accessToken": "x", "expiresAt": 0})
        self.assertEqual(result, {})

    def test_merge_keep_later_nonempty_existing_higher_expiry_kept(self):
        from ccswitch.creds import merge_keep_later
        existing = {"accessToken": "keep", "expiresAt": 500}
        incoming = {"accessToken": "discard", "expiresAt": 100}
        result = merge_keep_later(existing, incoming)
        self.assertIs(result, existing)
        self.assertEqual(result["accessToken"], "keep")

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
            parent_mode = stat.S_IMODE(os.stat(p.parent).st_mode)
            self.assertEqual(parent_mode, 0o700)
            # write_json_secure must force the store ROOT to 0700 too (via paths.ensure_root)
            root_mode = stat.S_IMODE(os.stat(paths.root()).st_mode)
            self.assertEqual(root_mode, 0o700)
