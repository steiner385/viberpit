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

    def test_setup_token_injected_into_env(self):
        """Launcher injects CLAUDE_CODE_OAUTH_TOKEN when account has a setup-token."""
        with temp_env():
            from ccswitch import launcher, accounts
            _seed("alpha")
            captured_envs: list[dict] = []

            def _capture_spawn(cmd, env):
                captured_envs.append(dict(env))
                return FakeProc(turns=0)

            with mock.patch.object(accounts, "usage_for", return_value=usage(session=5.0)), \
                 mock.patch.object(launcher, "spawn_claude", side_effect=_capture_spawn), \
                 mock.patch.object(launcher, "scan_for_limit", return_value=None), \
                 mock.patch.object(launcher, "current_session_id", return_value="sess-1"), \
                 mock.patch.object(accounts, "sync_back"), \
                 mock.patch("ccswitch.launcher.read_oauth_token", return_value="sk-ant-oat01-testtoken"):
                rc = launcher.main(["--poll-interval", "0"])
            self.assertEqual(rc, 0)
            self.assertEqual(len(captured_envs), 1)
            self.assertEqual(captured_envs[0]["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-testtoken")

    def test_setup_token_absent_when_no_token_stored(self):
        """Launcher does not set CLAUDE_CODE_OAUTH_TOKEN when no setup-token is available."""
        with temp_env():
            from ccswitch import launcher, accounts
            _seed("alpha")
            captured_envs: list[dict] = []

            def _capture_spawn(cmd, env):
                captured_envs.append(dict(env))
                return FakeProc(turns=0)

            with mock.patch.object(accounts, "usage_for", return_value=usage(session=5.0)), \
                 mock.patch.object(launcher, "spawn_claude", side_effect=_capture_spawn), \
                 mock.patch.object(launcher, "scan_for_limit", return_value=None), \
                 mock.patch.object(launcher, "current_session_id", return_value="sess-1"), \
                 mock.patch.object(accounts, "sync_back"), \
                 mock.patch("ccswitch.launcher.read_oauth_token", return_value=None):
                rc = launcher.main(["--poll-interval", "0"])
            self.assertEqual(rc, 0)
            self.assertEqual(len(captured_envs), 1)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", captured_envs[0])

    def test_engine_codex_rejects_when_no_codex_account_configured(self):
        """--engine codex fails closed on the canonical 'no usable accounts' error
        when no Codex-provider account is registered — never falls back to Claude."""
        with temp_env():
            from ccswitch import launcher
            _seed("alpha")  # a Claude-only account; not eligible for engine=codex
            with mock.patch.object(launcher, "spawn_claude") as fake_spawn:
                rc = launcher.main(["--engine", "codex"])
            self.assertEqual(rc, 2)
            fake_spawn.assert_not_called()

    def test_engine_codex_gated_when_account_configured(self):
        """A configured Codex account still gets an explicit 'not supported yet'
        refusal — ccmax has no synchronous Codex launch surface — not a Claude run."""
        with temp_env():
            from ccswitch import launcher, account_registry
            from ccswitch.engines import AgentEngine
            account_registry.register_account(
                "codexy", account_registry.PROVIDER_OPENAI, engine=AgentEngine.CODEX.value)
            with mock.patch.object(launcher, "spawn_claude") as fake_spawn:
                rc = launcher.main(["--engine", "codex"])
            self.assertEqual(rc, 3)
            fake_spawn.assert_not_called()

    def test_engine_rejects_unknown_value(self):
        with temp_env():
            from ccswitch import launcher
            with mock.patch.object(launcher, "spawn_claude") as fake_spawn:
                rc = launcher.main(["--engine", "bogus"])
            self.assertEqual(rc, 64)
            fake_spawn.assert_not_called()

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
