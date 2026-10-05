import os
import stat
import unittest
from tests.helpers import temp_env


class TestCreds(unittest.TestCase):
    def test_split_and_compose_roundtrip(self):
        from viberpit.creds import split_credentials, compose_credentials
        full = {"mcpOAuth": {"srv": {"x": 1}}, "claudeAiOauth": {"accessToken": "a", "expiresAt": 5}}
        claude_oauth, mcp = split_credentials(full)
        self.assertEqual(claude_oauth["accessToken"], "a")
        self.assertEqual(mcp, {"srv": {"x": 1}})
        composed = compose_credentials(claude_oauth, mcp)
        self.assertEqual(composed["claudeAiOauth"]["accessToken"], "a")
        self.assertEqual(composed["mcpOAuth"], {"srv": {"x": 1}})

    def test_split_missing_keys_defaults_empty(self):
        from viberpit.creds import split_credentials
        claude_oauth, mcp = split_credentials({})
        self.assertEqual(claude_oauth, {})
        self.assertEqual(mcp, {})

    def test_merge_keep_later_prefers_later_expiry(self):
        from viberpit.creds import merge_keep_later
        old = {"accessToken": "old", "expiresAt": 100}
        new = {"accessToken": "new", "expiresAt": 200}
        self.assertEqual(merge_keep_later(old, new)["accessToken"], "new")
        self.assertEqual(merge_keep_later(new, old)["accessToken"], "new")
        self.assertEqual(merge_keep_later(None, new)["accessToken"], "new")

    def test_merge_keep_later_tie_keeps_existing(self):
        from viberpit.creds import merge_keep_later
        # Equal expiresAt → existing wins (>= keeps existing, monotonic-refresh idempotency)
        existing = {"accessToken": "existing", "expiresAt": 300}
        incoming = {"accessToken": "incoming", "expiresAt": 300}
        result = merge_keep_later(existing, incoming)
        self.assertIs(result, existing)
        self.assertEqual(result["accessToken"], "existing")

    def test_merge_keep_later_empty_dict_is_real_blob(self):
        from viberpit.creds import merge_keep_later
        incoming = {"accessToken": "fresh", "expiresAt": 100}
        # Empty dict {} is NOT treated as absent — it is a real blob with expiresAt 0
        # incoming has higher expiresAt (100 > 0), so incoming wins
        result = merge_keep_later({}, incoming)
        self.assertIs(result, incoming)
        self.assertEqual(result["accessToken"], "fresh")

    def test_merge_keep_later_empty_dict_higher_expiry_than_incoming(self):
        from viberpit.creds import merge_keep_later
        # existing={} has expiresAt=0 (missing key defaults to 0)
        # incoming has expiresAt=0 too → existing wins via >= (tie goes to existing)
        result = merge_keep_later({}, {"accessToken": "x", "expiresAt": 0})
        self.assertEqual(result, {})

    def test_merge_keep_later_nonempty_existing_higher_expiry_kept(self):
        from viberpit.creds import merge_keep_later
        existing = {"accessToken": "keep", "expiresAt": 500}
        incoming = {"accessToken": "discard", "expiresAt": 100}
        result = merge_keep_later(existing, incoming)
        self.assertIs(result, existing)
        self.assertEqual(result["accessToken"], "keep")

    def test_write_json_secure_is_0600(self):
        with temp_env():
            from viberpit import paths
            from viberpit.creds import write_json_secure, read_json
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

    def test_write_json_secure_concurrent_same_path_no_race(self):
        # Regression: a FIXED temp filename ("<name>.tmp") made concurrent writers
        # to the SAME path collide — one writer's os.replace renamed the temp away
        # before another's os.chmod(tmp) ran, raising FileNotFoundError (seen live
        # as the usage-poller writing the shared mcp.shared.json from every account
        # thread at once). Unique per-write temp names fix it.
        import threading

        with temp_env():
            from viberpit import paths
            from viberpit.creds import write_json_secure, read_json
            paths.store_dir().mkdir(parents=True)
            p = paths.slot_path("shared")
            errors: list[str] = []

            def worker(i: int) -> None:
                try:
                    for j in range(40):
                        write_json_secure(p, {"w": i, "j": j})
                except Exception as e:  # noqa: BLE001 — capture any race failure
                    errors.append(repr(e))

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], f"concurrent writes raced: {errors[:3]}")
            # Final file is always valid JSON (never half-written / corrupted).
            self.assertIn("w", read_json(p))
            # No leftover temp files.
            leftover = [f.name for f in p.parent.iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(leftover, [], f"leftover temp files: {leftover}")


def test_shared_mcp_stale_account_cannot_erase_authenticated_server():
    from viberpit import paths
    from viberpit.creds import write_json_secure, read_json, merge_shared_mcp
    with temp_env():
        fresh = {"serverName": "aws", "accessToken": "fresh", "refreshToken": "refresh", "expiresAt": 200}
        write_json_secure(paths.shared_mcp_path(), {"aws|url": fresh, "other": {"accessToken": "other"}})
        merge_shared_mcp({"aws|url": {"serverName": "aws", "accessToken": "", "clientId": "stale"}})
        assert read_json(paths.shared_mcp_path()) == {"aws|url": fresh, "other": {"accessToken": "other"}}
        merge_shared_mcp({"aws|url": {"accessToken": "old", "expiresAt": 100}})
        assert read_json(paths.shared_mcp_path())["aws|url"] == fresh


def test_shared_mcp_refresh_and_explicit_login_replace_whole_blob():
    from viberpit import paths
    from viberpit.creds import write_json_secure, read_json, merge_shared_mcp
    with temp_env():
        write_json_secure(paths.shared_mcp_path(), {"aws": {"accessToken": "old", "expiresAt": 100, "clientId": "old-client"}})
        fresh = {"serverName": "aws", "accessToken": "new", "expiresAt": 200}
        merge_shared_mcp({"aws": fresh})
        assert read_json(paths.shared_mcp_path())["aws"] == fresh
        explicit = {"serverName": "aws", "accessToken": "reauthorized"}
        merge_shared_mcp({"aws": explicit}, authenticated_keys={"aws"})
        assert read_json(paths.shared_mcp_path())["aws"] == explicit
        merge_shared_mcp({"aws": fresh}, authenticated_keys={"other"})
        # A timed refresh can supersede a token without an expiry.
        assert read_json(paths.shared_mcp_path())["aws"] == fresh


def test_shared_mcp_concurrent_servers_are_not_lost():
    from concurrent.futures import ThreadPoolExecutor
    from viberpit import paths
    from viberpit.creds import read_json, merge_shared_mcp
    with temp_env():
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: merge_shared_mcp({str(i): {"accessToken": str(i)}}), range(30)))
        assert len(read_json(paths.shared_mcp_path())) == 30


def test_shared_mcp_ties_and_missing_expiry_preserve_existing_token():
    from viberpit import paths
    from viberpit.creds import read_json, merge_shared_mcp
    with temp_env():
        fresh = {"serverName": "aws", "accessToken": "fresh", "expiresAt": 200}
        merge_shared_mcp({"aws": fresh})
        for expiry in (200, None, "invalid"):
            merge_shared_mcp({"aws": {"serverName": "aws", "accessToken": "old", "expiresAt": expiry}})
            assert read_json(paths.shared_mcp_path())["aws"] == fresh
        merge_shared_mcp({"aws": {"serverName": "aws", "accessToken": ""}}, authenticated_keys={"aws"})
        assert read_json(paths.shared_mcp_path())["aws"] == fresh


def test_shared_mcp_ignores_malformed_entries_and_keeps_store_private():
    from viberpit import paths
    from viberpit.creds import merge_shared_mcp, read_json
    with temp_env():
        assert merge_shared_mcp({"broken": None}) == 0
        assert not paths.shared_mcp_path().exists()
        assert merge_shared_mcp({"discovery": {"clientId": "client"}}) == 1
        assert read_json(paths.shared_mcp_path()) == {"discovery": {"clientId": "client"}}
        assert stat.S_IMODE(paths.shared_mcp_path().stat().st_mode) == 0o600
        assert stat.S_IMODE(paths.shared_mcp_path().with_name("mcp.shared.json.lock").stat().st_mode) == 0o600


def test_shared_mcp_corrupt_store_fails_without_overwriting():
    import pytest
    from viberpit import paths
    from viberpit.creds import merge_shared_mcp
    with temp_env():
        p = paths.shared_mcp_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json")
        with pytest.raises(ValueError):
            merge_shared_mcp({"aws": {"accessToken": "token"}})
        assert p.read_text() == "not json"


def _merge_mcp_worker(index):
    from viberpit.creds import merge_shared_mcp
    for revision in range(10):
        merge_shared_mcp({str(index): {"accessToken": str(index), "expiresAt": revision + 1}})


def test_shared_mcp_concurrent_processes_preserve_each_server():
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context
    from viberpit import paths
    from viberpit.creds import read_json
    with temp_env():
        with ProcessPoolExecutor(max_workers=4, mp_context=get_context("fork")) as pool:
            list(pool.map(_merge_mcp_worker, range(8)))
        data = read_json(paths.shared_mcp_path())
        assert len(data) == 8
        assert all(value["expiresAt"] == 10 for value in data.values())
