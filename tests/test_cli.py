import unittest
from unittest import mock
from tests.helpers import temp_env


class TestFmtReset(unittest.TestCase):
    """`_fmt_reset` renders a usage window's reset time in local tz, and a clear
    'fresh' state when there is no active window (resets_at is None/empty)."""

    def test_none_renders_fresh(self):
        from ccswitch.cli import _fmt_reset
        self.assertEqual(_fmt_reset(None, "America/New_York"), "fresh")

    def test_empty_string_renders_fresh(self):
        from ccswitch.cli import _fmt_reset
        self.assertEqual(_fmt_reset("", "America/New_York"), "fresh")

    def test_iso_rendered_in_local_tz(self):
        from ccswitch.cli import _fmt_reset
        # 18:30 UTC -> 13:30 (1:30 PM) in America/New_York (EST, UTC-5).
        out = _fmt_reset("2026-01-15T18:30:00+00:00", "America/New_York")
        self.assertEqual(out, "Thu 1:30 PM EST")


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
                add.assert_called_once_with("alpha", email=None)

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
                uf.assert_called_once_with("alpha", path="cli")

    def test_list_no_refresh_skips_fetch(self):
        with temp_env():
            from ccswitch import cli, accounts
            self._seed("alpha")
            with mock.patch.object(accounts, "usage_for") as uf:
                self.assertEqual(cli.main(["list", "--no-refresh"]), 0)
                uf.assert_not_called()


class TestCliUse(unittest.TestCase):
    def _seed(self, name):
        from ccswitch import paths
        from ccswitch.creds import write_json_secure
        write_json_secure(paths.slot_path(name), {"accessToken": "a", "refreshToken": "r",
                                                  "expiresAt": 9_999_999_999_999, "subscriptionType": "max"})
        write_json_secure(paths.shared_mcp_path(), {})

    def test_use_named_prints_export(self):
        import contextlib, io
        with temp_env():
            from ccswitch import cli, paths
            self._seed("secondary")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["use", "secondary"])
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("export CLAUDE_CONFIG_DIR=", out)
            self.assertIn(str(paths.account_cfg_dir("secondary")), out)
            self.assertIn("ccswitch: secondary", out)

    def test_use_dir_prints_only_path(self):
        import contextlib, io
        with temp_env():
            from ccswitch import cli, paths
            self._seed("secondary")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["use", "--dir", "secondary"])
            out = buf.getvalue().strip()
            self.assertEqual(rc, 0)
            self.assertNotIn("export", out)
            self.assertEqual(out, str(paths.account_cfg_dir("secondary")))

    def test_use_unknown_account_errors(self):
        with temp_env():
            from ccswitch import cli
            self._seed("secondary")
            self.assertEqual(cli.main(["use", "nope"]), 1)

    def test_use_default_picks_best(self):
        import contextlib, io
        with temp_env():
            from ccswitch import cli, accounts
            self._seed("alpha"); self._seed("beta")
            from tests.helpers import usage
            with mock.patch.object(accounts, "usage_for",
                                   side_effect=lambda n: usage(session=5.0 if n == "beta" else 80.0)):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.main(["use"])
            self.assertEqual(rc, 0)
            self.assertIn("ccswitch: beta", buf.getvalue())
