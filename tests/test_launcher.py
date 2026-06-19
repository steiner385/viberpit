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
            first_proc = FakeProc(turns=99)
            spawns = [first_proc, FakeProc(turns=0)]
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
            self.assertTrue(first_proc.terminated)  # the switched-away process was terminated

    def test_proactive_no_switch_when_no_less_loaded_account(self):
        with temp_env():
            from ccswitch import launcher, accounts
            _seed("alpha"); _seed("beta")
            # Active (alpha) is 95% — over soft — but the only other account (beta) is 99%,
            # i.e. MORE loaded. _proactive_target returns None → no switch, single proc exits.
            only_proc = FakeProc(turns=1)
            spawns = [only_proc]
            with mock.patch.object(accounts, "usage_for", side_effect=lambda n: usage(session=95.0 if n == "alpha" else 99.0)), \
                 mock.patch.object(launcher, "spawn_claude", side_effect=lambda cmd, env: spawns.pop(0)), \
                 mock.patch.object(launcher, "scan_for_limit", return_value=None), \
                 mock.patch.object(launcher, "current_session_id", return_value="sess-1"), \
                 mock.patch.object(accounts, "sync_back"):
                rc = launcher.main(["--poll-interval", "0"])
            self.assertEqual(rc, 0)
            self.assertEqual(spawns, [])           # the single proc was consumed
            self.assertFalse(only_proc.terminated)  # exited cleanly, never killed/switched

    def test_proactive_switch_when_less_loaded_account_exists(self):
        with temp_env():
            from ccswitch import launcher, accounts
            _seed("alpha"); _seed("beta")
            # Active (alpha) is 95% — over soft — and beta is 10% (strictly less loaded)
            # → _proactive_target picks beta, one switch, then clean exit.
            # pick_account is mocked so the active account starts as alpha (the real selector
            # would otherwise pick the lower-loaded beta first and reserve it).
            first_proc = FakeProc(turns=99)
            spawns = [first_proc, FakeProc(turns=0)]
            picks = ["alpha", "beta"]
            with mock.patch.object(accounts, "usage_for", side_effect=lambda n: usage(session=95.0 if n == "alpha" else 10.0)), \
                 mock.patch.object(accounts, "pick_account", side_effect=lambda cfg, exclude=frozenset(): picks.pop(0)), \
                 mock.patch.object(launcher, "spawn_claude", side_effect=lambda cmd, env: spawns.pop(0)), \
                 mock.patch.object(launcher, "scan_for_limit", return_value=None), \
                 mock.patch.object(launcher, "current_session_id", return_value="sess-1"), \
                 mock.patch.object(accounts, "sync_back"):
                rc = launcher.main(["--poll-interval", "0"])
            self.assertEqual(rc, 0)
            self.assertEqual(spawns, [])         # both procs consumed → exactly one switch
            self.assertTrue(first_proc.terminated)
