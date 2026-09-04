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


class TestClonesCommands(unittest.TestCase):
    """`ccswitch clones` — the #1003 recovery + guarded-removal surface."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "GitHub"
        self.root.mkdir()
        self.clone = self.root / ".ccswitch" / "clones" / "cairnea" / "KinDash"
        self.wt = self.root / ".ccswitch" / "worktrees" / "KinDash" / "2a158cb3"
        self.wt.mkdir(parents=True)
        (self.wt / ".git").write_text(
            f"gitdir: {self.clone}/.git/worktrees/2a158cb3\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, argv):
        import io
        import os
        from contextlib import redirect_stdout

        from ccswitch import cli

        old = os.environ.get("CCSWITCH_REPO_ROOT")
        os.environ["CCSWITCH_REPO_ROOT"] = str(self.root)
        buf = io.StringIO()
        try:
            with temp_env(), redirect_stdout(buf):
                rc = cli.main(argv)
        finally:
            if old is None:
                os.environ.pop("CCSWITCH_REPO_ROOT", None)
            else:
                os.environ["CCSWITCH_REPO_ROOT"] = old
        return rc, buf.getvalue()

    def test_doctor_reports_missing_clone_and_exits_nonzero(self):
        rc, out = self._run(["clones", "doctor"])
        self.assertEqual(rc, 1)
        self.assertIn("cairnea/KinDash", out)
        self.assertIn("MISSING", out)
        self.assertIn("ccswitch clones repair", out)

    def test_doctor_json_lists_broken_worktrees(self):
        import json

        rc, out = self._run(["clones", "doctor", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload[0]["repo"], "cairnea/KinDash")
        self.assertFalse(payload[0]["cloneExists"])
        self.assertEqual(payload[0]["broken"], [str(self.wt)])

    def test_repair_dry_run_reports_without_cloning(self):
        rc, out = self._run(["clones", "repair", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("would-repair", out)
        self.assertFalse(self.clone.exists())

    def test_remove_refuses_a_clone_worktrees_still_reference(self):
        self.clone.mkdir(parents=True)
        rc, _ = self._run(["clones", "remove", "cairnea/KinDash"])
        self.assertEqual(rc, 2)
        self.assertTrue(self.clone.exists())

    def test_remove_resolves_a_case_variant_repo_name(self):
        # `kindash` must resolve onto the existing `KinDash` clone, so the guard
        # sees its worktrees instead of shrugging at a non-existent path (#1003).
        self.clone.mkdir(parents=True)
        rc, _ = self._run(["clones", "remove", "cairnea/kindash"])
        self.assertEqual(rc, 2)
        self.assertTrue(self.clone.exists())

    def test_remove_deletes_an_unreferenced_clone(self):
        import shutil

        shutil.rmtree(self.wt.parent)
        self.clone.mkdir(parents=True)
        rc, out = self._run(["clones", "remove", "cairnea/KinDash", "--reason", "cleanup"])
        self.assertEqual(rc, 0)
        self.assertIn("removed clone cairnea/KinDash", out)
        self.assertFalse(self.clone.exists())
