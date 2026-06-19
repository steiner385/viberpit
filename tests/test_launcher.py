import os
import sys
import time
import unittest
from unittest import mock
from tests.helpers import temp_env, usage


def _seed(name):
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {"accessToken": f"{name}A", "refreshToken": f"{name}R",
                                               "expiresAt": int(time.time()*1000)+9_000_000, "subscriptionType": "max"})
    write_json_secure(paths.shared_mcp_path(), {})


class FakeProc:
    """Stand-in for Popen: exits cleanly after `turns` poll cycles."""
    def __init__(self, turns=1):
        self._turns = turns
        self.terminated = False
    def poll(self):
        self._turns -= 1
        return 0 if self._turns < 0 else None
    def wait(self, timeout=None):
        return 0
    def terminate(self):
        self.terminated = True


class TestLauncher(unittest.TestCase):
    def test_switch_when_limit_detected(self):
        with temp_env():
            from ccswitch import launcher, accounts
            _seed("alpha"); _seed("beta")
            # First spawn (alpha) trips the limit on first scan; second spawn (beta) exits clean.
            spawns = [FakeProc(turns=99), FakeProc(turns=0)]
            from ccswitch.detect import ResetInfo
            scan_results = [ResetInfo(resets_at=None, scope=None), None]
            with mock.patch.object(accounts, "usage_for", side_effect=lambda n: usage(session=5.0 if n == "alpha" else 50.0)), \
                 mock.patch.object(launcher, "spawn_claude", side_effect=lambda cmd, env: spawns.pop(0)), \
                 mock.patch.object(launcher, "scan_for_limit", side_effect=lambda d: scan_results.pop(0)), \
                 mock.patch.object(launcher, "current_session_id", return_value="sess-1"), \
                 mock.patch.object(accounts, "sync_back"):
                rc = launcher.main(["--poll-interval", "0"])
            self.assertEqual(rc, 0)
            self.assertTrue(spawns == [])  # both procs consumed → a switch happened
