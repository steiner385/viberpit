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
