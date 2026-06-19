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
            from ccswitch import runner, accounts, oauth
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
