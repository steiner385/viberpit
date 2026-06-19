import unittest
from unittest import mock
from tests.helpers import temp_env


class TestCli(unittest.TestCase):
    def test_list_runs(self):
        with temp_env():
            from ccswitch import cli
            self.assertEqual(cli.main(["list"]), 0)

    def test_add_invokes_capture(self):
        with temp_env():
            from ccswitch import cli, accounts
            with mock.patch.object(accounts, "add_account") as add:
                self.assertEqual(cli.main(["add", "alpha"]), 0)
                add.assert_called_once_with("alpha")

    def _seed(self, name):
        from ccswitch import paths
        from ccswitch.creds import write_json_secure
        write_json_secure(paths.slot_path(name), {"accessToken": "a", "refreshToken": "r",
                                                  "expiresAt": 9_999_999_999_999, "subscriptionType": "max"})

    def test_list_refreshes_usage_by_default(self):
        with temp_env():
            from ccswitch import cli, accounts
            from tests.helpers import usage
            self._seed("alpha")
            with mock.patch.object(accounts, "usage_for", return_value=usage(session=12.0)) as uf:
                self.assertEqual(cli.main(["list"]), 0)
                uf.assert_called_once_with("alpha")

    def test_list_no_refresh_skips_fetch(self):
        with temp_env():
            from ccswitch import cli, accounts
            self._seed("alpha")
            with mock.patch.object(accounts, "usage_for") as uf:
                self.assertEqual(cli.main(["list", "--no-refresh"]), 0)
                uf.assert_not_called()
