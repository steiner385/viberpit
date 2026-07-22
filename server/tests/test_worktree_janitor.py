"""Unit tests for the orphaned-worktree janitor sweep logic."""
import subprocess
from pathlib import Path
from unittest.mock import patch


from ccswitch.worktrees_janitor import sweep_orphaned_worktrees


def _make_wt(base: Path, name: str, age_s: float, now: float) -> Path:
    """Create a fake worktree dir and set its mtime to ``now - age_s``."""
    wt = base / ".ccswitch" / "worktrees" / "KinDash" / name
    wt.mkdir(parents=True)
    mtime = now - age_s
    import os
    os.utime(wt, (mtime, mtime))
    return wt


def _fake_run(responses: dict):
    """Return a side-effect for subprocess.run; longest-matching key wins."""
    ordered = sorted(responses.items(), key=lambda kv: len(kv[0]), reverse=True)

    def _run(args, **_):
        full = " ".join(args)
        for key, resp in ordered:
            if key in full:
                return resp
        return subprocess.CompletedProcess(args, 0, "", "")
    return _run


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


NOW = 1_700_000_000.0
OLD = 90_000.0   # 25 h — old enough


class TestSweepSkips:
    def test_skips_live_cwd(self, tmp_path):
        wt = _make_wt(tmp_path, "s1", OLD, NOW)
        live = frozenset([str(wt.resolve())])
        counts = sweep_orphaned_worktrees(str(tmp_path), live, now=NOW)
        assert counts["scanned"] == 1
        assert counts["kept_live"] == 1
        assert counts["removed"] == 0

    def test_skips_young_dir(self, tmp_path):
        wt = _make_wt(tmp_path, "s2", 3600.0, NOW)  # only 1 h old
        counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["kept_young"] == 1
        assert counts["removed"] == 0

    def test_no_worktrees_dir(self, tmp_path):
        counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["scanned"] == 0

    def test_empty_worktrees_dir(self, tmp_path):
        (tmp_path / ".ccswitch" / "worktrees").mkdir(parents=True)
        counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["scanned"] == 0


class TestSweepKeepsDirty:
    def test_keeps_dirty(self, tmp_path):
        wt = _make_wt(tmp_path, "dirty", OLD, NOW)
        responses = {"git -C": _cp(0, " M file.py")}
        with patch("ccswitch.worktrees_janitor._run", side_effect=_fake_run(responses)):
            counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["kept_dirty"] == 1
        assert counts["removed"] == 0


class TestSweepKeepsUnpushed:
    def test_keeps_unpushed(self, tmp_path):
        wt = _make_wt(tmp_path, "unpushed", OLD, NOW)
        responses = {
            "status --porcelain": _cp(0, ""),          # clean
            "branch -vv": _cp(0, "* main [origin/main]"),  # upstream present, not gone
            "remote": _cp(0, "origin"),
            "rev-list": _cp(0, "abc123\n"),             # has unpushed commit
        }
        with patch("ccswitch.worktrees_janitor._run", side_effect=_fake_run(responses)):
            counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["kept_unpushed"] == 1

    def test_no_remotes_kept(self, tmp_path):
        wt = _make_wt(tmp_path, "noremote", OLD, NOW)
        responses = {
            "status --porcelain": _cp(0, ""),
            "branch -vv": _cp(0, "* main"),
            "remote": _cp(0, ""),  # no remotes
        }
        with patch("ccswitch.worktrees_janitor._run", side_effect=_fake_run(responses)):
            counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["kept_unpushed"] == 1


class TestSweepRemoves:
    def test_removes_clean_pushed(self, tmp_path):
        wt = _make_wt(tmp_path, "pushed", OLD, NOW)
        responses = {
            "status --porcelain": _cp(0, ""),
            "branch -vv": _cp(0, "* main [origin/main]"),
            "remote": _cp(0, "origin"),
            "rev-list": _cp(0, ""),  # all pushed
        }
        with patch("ccswitch.worktrees_janitor._run", side_effect=_fake_run(responses)):
            with patch("ccswitch.worktrees_janitor.remove_worktree", return_value=True) as mock_rm:
                counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["removed"] == 1
        mock_rm.assert_called_once_with(str(wt.resolve()))

    def test_removes_upstream_gone(self, tmp_path):
        wt = _make_wt(tmp_path, "gone", OLD, NOW)
        responses = {
            "status --porcelain": _cp(0, ""),
            "branch -vv": _cp(0, "* ccswitch/feat-123-abc [origin/ccswitch/feat-123-abc: gone]"),
        }
        with patch("ccswitch.worktrees_janitor._run", side_effect=_fake_run(responses)):
            with patch("ccswitch.worktrees_janitor.remove_worktree", return_value=True):
                counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["removed"] == 1


class TestSweepErrors:
    def test_remove_failure_increments_kept_error(self, tmp_path):
        wt = _make_wt(tmp_path, "rmfail", OLD, NOW)
        responses = {
            "status --porcelain": _cp(0, ""),
            "branch -vv": _cp(0, "* main [origin/main]"),
            "remote": _cp(0, "origin"),
            "rev-list": _cp(0, ""),
        }
        with patch("ccswitch.worktrees_janitor._run", side_effect=_fake_run(responses)):
            with patch("ccswitch.worktrees_janitor.remove_worktree", return_value=False):
                counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["kept_error"] == 1
        assert counts["removed"] == 0

    def test_per_dir_error_continues(self, tmp_path):
        _make_wt(tmp_path, "boom", OLD, NOW)
        _make_wt(tmp_path, "ok", OLD, NOW)

        call_count = [0]

        def exploding_run(args, **_):
            call_count[0] += 1
            if "boom" in " ".join(args):
                raise RuntimeError("simulated error")
            return _cp(0, "")

        with patch("ccswitch.worktrees_janitor._run", side_effect=exploding_run):
            with patch("ccswitch.worktrees_janitor.remove_worktree", return_value=True):
                counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["scanned"] == 2
        assert counts["kept_error"] == 1
