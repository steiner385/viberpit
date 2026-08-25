import json
import os
import sys
import time
import unittest
from unittest import mock
from tests.helpers import temp_env


def _seed(name):
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {"accessToken": f"{name}A", "refreshToken": f"{name}R",
                                               "expiresAt": int(time.time()*1000)+9_000_000, "subscriptionType": "max"})
    write_json_secure(paths.shared_mcp_path(), {})


class TestRunner(unittest.TestCase):
    def test_runs_on_selected_account(self):
        with temp_env():
            from ccswitch import runner, accounts
            from tests.helpers import usage
            _seed("alpha"); _seed("beta")
            os.environ["CCSWITCH_CLAUDE_BIN"] = f"{sys.executable} {os.path.abspath('tests/fake_claude.py')}"
            with mock.patch.object(accounts, "usage_for", side_effect=lambda n: usage(session=5.0 if n == "beta" else 90.0)):
                rc = runner.main(["--", "-p", "hi"])
            self.assertEqual(rc, 0)

    def test_retries_next_account_on_limit(self):
        with temp_env():
            from ccswitch import runner, accounts
            from tests.helpers import usage
            _seed("alpha"); _seed("beta")
            os.environ["CCSWITCH_CLAUDE_BIN"] = f"{sys.executable} {os.path.abspath('tests/fake_claude.py')}"
            os.environ["CCSWITCH_FAKE_LIMITED"] = "beta"  # beta (lowest util) is limited → retry alpha
            with mock.patch.object(accounts, "usage_for", side_effect=lambda n: usage(session=5.0 if n == "beta" else 50.0)):
                rc = runner.main(["--", "-p", "hi"])
            self.assertEqual(rc, 0)  # succeeded after retrying alpha
            os.environ.pop("CCSWITCH_FAKE_LIMITED", None)

    def test_all_accounts_limited_no_wait_returns_2(self):
        with temp_env():
            from ccswitch import runner, accounts
            from tests.helpers import usage
            _seed("alpha"); _seed("beta")
            os.environ["CCSWITCH_CLAUDE_BIN"] = f"{sys.executable} {os.path.abspath('tests/fake_claude.py')}"
            # Both accounts are limited; all have low utilisation so they're selectable
            os.environ["CCSWITCH_FAKE_LIMITED"] = "alpha,beta"
            with mock.patch.object(accounts, "usage_for", side_effect=lambda n: usage(session=5.0)):
                rc = runner.main(["--", "-p", "hi"])
            self.assertEqual(rc, 2)
            os.environ.pop("CCSWITCH_FAKE_LIMITED", None)

    def test_setup_token_injected_into_env(self):
        """Runner injects CLAUDE_CODE_OAUTH_TOKEN when account has a setup-token."""
        with temp_env():
            from ccswitch import runner, accounts
            from tests.helpers import usage
            _seed("alpha")
            captured_envs: list[dict] = []

            def _fake_run(cmd, env):
                captured_envs.append(dict(env))
                return 0, json.dumps({"type": "result", "is_error": False, "result": "ok"})

            with mock.patch.object(accounts, "usage_for", return_value=usage(session=5.0)), \
                 mock.patch.object(runner, "run_claude", side_effect=_fake_run), \
                 mock.patch.object(accounts, "sync_back"), \
                 mock.patch("ccswitch.runner.read_oauth_token", return_value="sk-ant-oat01-testtoken"):
                rc = runner.main(["--", "-p", "hi"])
            self.assertEqual(rc, 0)
            self.assertEqual(len(captured_envs), 1)
            self.assertEqual(captured_envs[0]["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-testtoken")

    def test_setup_token_absent_when_no_token_stored(self):
        """Runner does not set CLAUDE_CODE_OAUTH_TOKEN when no setup-token is available."""
        with temp_env():
            from ccswitch import runner, accounts
            from tests.helpers import usage
            _seed("alpha")
            captured_envs: list[dict] = []

            def _fake_run(cmd, env):
                captured_envs.append(dict(env))
                return 0, json.dumps({"type": "result", "is_error": False, "result": "ok"})

            with mock.patch.object(accounts, "usage_for", return_value=usage(session=5.0)), \
                 mock.patch.object(runner, "run_claude", side_effect=_fake_run), \
                 mock.patch.object(accounts, "sync_back"), \
                 mock.patch("ccswitch.runner.read_oauth_token", return_value=None):
                rc = runner.main(["--", "-p", "hi"])
            self.assertEqual(rc, 0)
            self.assertEqual(len(captured_envs), 1)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", captured_envs[0])

    def test_last_result_ignores_non_json_and_non_result_lines(self):
        from ccswitch.runner import _last_result
        stdout = "\n".join([
            "not json at all",
            json.dumps({"type": "text", "text": "hello"}),
            json.dumps({"type": "result", "is_error": False, "result": "done"}),
        ])
        obj = _last_result(stdout)
        self.assertIsNotNone(obj)
        self.assertEqual(obj["type"], "result")
        self.assertEqual(obj["result"], "done")
