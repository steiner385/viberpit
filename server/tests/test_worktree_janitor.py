"""Unit tests for the orphaned-worktree janitor sweep logic."""
import subprocess
from pathlib import Path
from unittest.mock import patch


from ccswitch.worktrees_janitor import sweep_orphaned_worktrees

# One spelling of the module path, reused: each literal counts against the
# brand ratchet's "python-package" ceiling, which sits at its limit.
_MOD = "ccswitch.worktrees_janitor"
_RUN = f"{_MOD}._run"
_REMOVE = f"{_MOD}.remove_worktree"


def _age(wt: Path, age_s: float, now: float) -> Path:
    """Backdate *wt*'s mtime to ``now - age_s`` (writing into it bumps mtime)."""
    import os
    mtime = now - age_s
    os.utime(wt, (mtime, mtime))
    return wt


def _make_wt(base: Path, name: str, age_s: float, now: float) -> Path:
    """Create a fake worktree dir and set its mtime to ``now - age_s``."""
    wt = base / ".ccswitch" / "worktrees" / "KinDash" / name
    wt.mkdir(parents=True)
    return _age(wt, age_s, now)


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
        with patch(_RUN, side_effect=_fake_run(responses)):
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
        with patch(_RUN, side_effect=_fake_run(responses)):
            counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["kept_unpushed"] == 1

    def test_no_remotes_kept(self, tmp_path):
        wt = _make_wt(tmp_path, "noremote", OLD, NOW)
        responses = {
            "status --porcelain": _cp(0, ""),
            "branch -vv": _cp(0, "* main"),
            "remote": _cp(0, ""),  # no remotes
        }
        with patch(_RUN, side_effect=_fake_run(responses)):
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
        with patch(_RUN, side_effect=_fake_run(responses)):
            with patch(_REMOVE, return_value=True) as mock_rm:
                counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["removed"] == 1
        mock_rm.assert_called_once_with(str(wt.resolve()))

    def test_removes_upstream_gone(self, tmp_path):
        wt = _make_wt(tmp_path, "gone", OLD, NOW)
        responses = {
            "status --porcelain": _cp(0, ""),
            "branch -vv": _cp(0, "* ccswitch/feat-123-abc [origin/ccswitch/feat-123-abc: gone]"),
        }
        with patch(_RUN, side_effect=_fake_run(responses)):
            with patch(_REMOVE, return_value=True):
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
        with patch(_RUN, side_effect=_fake_run(responses)):
            with patch(_REMOVE, return_value=False):
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

        with patch(_RUN, side_effect=exploding_run):
            with patch(_REMOVE, return_value=True):
                counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["scanned"] == 2
        assert counts["kept_error"] == 1


# 8 days — exceeds the default max_dirty_age of 7 days
VERY_OLD = 8 * 86400.0


class TestMaxAgeDirtyEviction:
    """Dirty worktrees past max_dirty_age must be stash-evicted."""

    def test_dirty_within_grace_kept(self, tmp_path):
        """A dirty worktree younger than max_dirty_age stays as kept_dirty."""
        _make_wt(tmp_path, "young-dirty", OLD, NOW)  # 25 h — within 7-day grace
        responses = {"git -C": _cp(0, " M file.py")}
        with patch(_RUN, side_effect=_fake_run(responses)):
            counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["kept_dirty"] == 1
        assert counts["evicted_dirty"] == 0

    def test_dirty_exceeds_grace_stash_succeeds_evicts(self, tmp_path):
        """A dirty worktree past max_dirty_age is stashed and removed."""
        wt = _make_wt(tmp_path, "old-dirty", VERY_OLD, NOW)
        dirty_responses = {
            "status --porcelain": _cp(0, " M file.py"),
            "stash push": _cp(0, "Saved working directory"),
            "rev-parse refs/stash": _cp(0, "abc123def456"),
        }
        with patch(_RUN, side_effect=_fake_run(dirty_responses)):
            with patch(_REMOVE, return_value=True) as mock_rm:
                counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["evicted_dirty"] == 1
        assert counts["removed"] == 1
        assert counts["kept_dirty"] == 0
        # remove_worktree called WITHOUT force (stash cleaned the tree)
        mock_rm.assert_called_once_with(str(wt.resolve()), force=False)

    def test_dirty_exceeds_grace_stash_fails_force_evicts(self, tmp_path):
        """If stash fails the worktree is still force-removed (discard path)."""
        wt = _make_wt(tmp_path, "old-dirty-nostash", VERY_OLD, NOW)
        dirty_responses = {
            "status --porcelain": _cp(0, " M file.py"),
            "stash push": _cp(1, "", "error: stash failed"),
            "rev-parse refs/stash": _cp(1, "", ""),
        }
        with patch(_RUN, side_effect=_fake_run(dirty_responses)):
            with patch(_REMOVE, return_value=True) as mock_rm:
                counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["evicted_dirty"] == 1
        assert counts["removed"] == 1
        assert counts["kept_dirty"] == 0
        # remove_worktree called WITH force=True because stash failed
        mock_rm.assert_called_once_with(str(wt.resolve()), force=True)

    def test_eviction_remove_failure_increments_kept_error(self, tmp_path):
        """If remove_worktree fails after stash, increment kept_error not evicted_dirty."""
        _make_wt(tmp_path, "old-dirty-rmfail", VERY_OLD, NOW)
        dirty_responses = {
            "status --porcelain": _cp(0, " M file.py"),
            "stash push": _cp(0, "Saved working directory"),
            "rev-parse refs/stash": _cp(0, "deadbeef"),
        }
        with patch(_RUN, side_effect=_fake_run(dirty_responses)):
            with patch(_REMOVE, return_value=False):
                counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["evicted_dirty"] == 0
        assert counts["kept_error"] == 1


class TestDirtyAlerts:
    """kept_dirty count triggers WARN / CRITICAL log messages."""

    def test_warn_threshold(self, tmp_path, caplog):
        import logging
        # Create 31 dirty worktrees within grace period → kept_dirty=31 → WARN
        for i in range(31):
            _make_wt(tmp_path, f"d{i}", OLD, NOW)
        dirty = {"git -C": _cp(0, " M f.py")}
        with patch(_RUN, side_effect=_fake_run(dirty)):
            with caplog.at_level(logging.WARNING, logger=_MOD):
                counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["kept_dirty"] == 31
        assert any("kept_dirty" in r.message and "warning threshold" in r.message
                   for r in caplog.records)

    def test_critical_threshold(self, tmp_path, caplog):
        import logging
        # Create 51 dirty worktrees within grace period → kept_dirty=51 → CRITICAL
        for i in range(51):
            _make_wt(tmp_path, f"dc{i}", OLD, NOW)
        dirty = {"git -C": _cp(0, " M f.py")}
        with patch(_RUN, side_effect=_fake_run(dirty)):
            with caplog.at_level(logging.CRITICAL, logger=_MOD):
                counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["kept_dirty"] == 51
        assert any("kept_dirty" in r.message and "critical threshold" in r.message
                   for r in caplog.records)


class TestStrayDirs:
    """A dir git cannot read is a stray, never "uncommitted work" (#1883).

    ``git status`` walks up to the parent dirs and exits 128 for any dir under
    the managed root that is not a linked worktree.  The old boolean
    ``_is_clean()`` read that failure as "has modifications", so every leftover
    dir was filed as ``kept_dirty`` — untouchable *and* uncountable-down — and
    eventually tripped the CRITICAL inode-exhaustion alert on directories
    holding nothing at all.
    """

    # Exactly what real git prints for a dir under the managed root with no
    # .git pointer and no repository above it.
    NOT_A_REPO = {
        "status --porcelain": _cp(
            128, "", "fatal: not a git repository (or any of the parent directories): .git"
        ),
    }

    def _sweep(self, tmp_path, live=frozenset()):
        with patch(_RUN, side_effect=_fake_run(self.NOT_A_REPO)):
            with patch(_REMOVE) as rm:
                counts = sweep_orphaned_worktrees(str(tmp_path), live, now=NOW)
        return counts, rm

    def _stray_with_files(self, tmp_path, name, age_s=OLD):
        wt = _make_wt(tmp_path, name, age_s, NOW)
        (wt / "f.txt").write_text("x")
        return _age(wt, age_s, NOW)  # writing the file bumped the dir mtime

    def test_empty_stray_is_pruned_not_counted_dirty(self, tmp_path):
        wt = _make_wt(tmp_path, "empty-leftover", OLD, NOW)
        counts, rm = self._sweep(tmp_path)
        assert counts["pruned_empty"] == 1
        assert counts["removed"] == 1
        assert counts["kept_dirty"] == 0
        assert counts["kept_error"] == 0
        assert not wt.exists()
        # Never routed through the git-level removal path — there is no worktree
        # record to deregister.
        rm.assert_not_called()

    def test_non_empty_stray_is_kept_and_never_deleted(self, tmp_path):
        wt = _make_wt(tmp_path, "stray-with-files", OLD, NOW)
        (wt / "leftover.py").write_text("x = 1\n")
        _age(wt, OLD, NOW)  # writing the file bumped the dir mtime
        counts, rm = self._sweep(tmp_path)
        assert counts["kept_stray"] == 1
        assert counts["kept_dirty"] == 0
        assert counts["pruned_empty"] == 0
        assert (wt / "leftover.py").read_text() == "x = 1\n"
        rm.assert_not_called()

    def test_stray_past_max_dirty_age_is_never_force_evicted(self, tmp_path):
        """The eviction path stashes and force-removes; on a stray both fail.

        Before the fix an old stray fell through ``_evict_dirty`` into
        ``kept_error`` — equally permanent, just filed under a different name.
        """
        wt = _make_wt(tmp_path, "ancient-stray", VERY_OLD, NOW)
        (wt / "work.txt").write_text("unrecoverable\n")
        _age(wt, VERY_OLD, NOW)
        counts, rm = self._sweep(tmp_path)
        assert counts["kept_stray"] == 1
        assert counts["evicted_dirty"] == 0
        assert counts["kept_error"] == 0
        assert wt.exists()
        rm.assert_not_called()

    def test_live_stray_is_left_alone(self, tmp_path):
        """A worktree mid-creation has no .git pointer yet — never prune it."""
        wt = _make_wt(tmp_path, "being-created", OLD, NOW)
        counts, _ = self._sweep(tmp_path, live=frozenset([str(wt.resolve())]))
        assert counts["kept_live"] == 1
        assert counts["pruned_empty"] == 0
        assert wt.exists()

    def test_young_stray_is_left_alone(self, tmp_path):
        wt = _make_wt(tmp_path, "fresh", 3600.0, NOW)
        counts, _ = self._sweep(tmp_path)
        assert counts["kept_young"] == 1
        assert counts["pruned_empty"] == 0
        assert wt.exists()

    def test_strays_do_not_trip_the_dirty_critical_alert(self, tmp_path, caplog):
        """The #1883 regression: 51 empty leftovers raised a CRITICAL."""
        import logging
        for i in range(51):
            _make_wt(tmp_path, f"leftover{i}", OLD, NOW)
        with caplog.at_level(logging.WARNING, logger=_MOD):
            counts, _ = self._sweep(tmp_path)
        assert counts["kept_dirty"] == 0
        assert counts["pruned_empty"] == 51
        assert not [r for r in caplog.records if r.levelno == logging.CRITICAL]

    def test_stray_alert_is_one_aggregated_warning(self, tmp_path, caplog):
        import logging
        for i in range(3):
            self._stray_with_files(tmp_path, f"stray{i}")
        with caplog.at_level(logging.WARNING, logger=_MOD):
            counts, _ = self._sweep(tmp_path)
        assert counts["kept_stray"] == 3
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "kept_stray=3" in warnings[0].getMessage()

    def test_stray_alert_is_one_triage_fingerprint(self, tmp_path, caplog):
        """Paths stay out of the WARNING text, or triage files one issue per host.

        ``error_signature.normalize`` masks digit-bearing path segments but
        leaves plain ones ("/kindash", "/viberpit") alone, so a message that
        quoted the stray paths would mint a fresh fingerprint for every repo and
        host — the #1848 failure shape, where one loop_lag stall became eight
        auto-error issues.
        """
        import logging
        from server.obs.error_signature import normalize

        def _warning_for(sub, names):
            base = tmp_path / sub
            for n in names:
                wt = base / ".ccswitch" / "worktrees" / sub / n
                wt.mkdir(parents=True)
                (wt / "f.txt").write_text("x")
                _age(wt, OLD, NOW)
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger=_MOD):
                with patch(_RUN,
                           side_effect=_fake_run(self.NOT_A_REPO)):
                    sweep_orphaned_worktrees(str(base), frozenset(), now=NOW)
            return next(r.getMessage() for r in caplog.records
                        if r.levelno == logging.WARNING)

        # Different repos, different hosts' path shapes, different counts.
        a = _warning_for("kindash", ["00737b3e", "1f39f056"])
        b = _warning_for("viberpit", ["b0433855", "c257153a", "25aa9b4f"])
        assert normalize(a) == normalize(b)

    def test_stray_paths_are_journalled_at_info(self, tmp_path, caplog):
        """The paths still reach the journal — just below the triage threshold."""
        import logging
        wt = self._stray_with_files(tmp_path, "findable")
        with caplog.at_level(logging.INFO, logger=_MOD):
            self._sweep(tmp_path)
        info = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any(str(wt.resolve()) in r.getMessage() and "keep_stray" in r.getMessage()
                   for r in info)

    def test_a_real_dirty_worktree_is_still_dirty(self, tmp_path):
        """Guard the guard: rc=0 with output must stay on the dirty path."""
        _make_wt(tmp_path, "genuinely-dirty", OLD, NOW)
        with patch(_RUN,
                   side_effect=_fake_run({"status --porcelain": _cp(0, " M file.py")})):
            counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["kept_dirty"] == 1
        assert counts["kept_stray"] == 0
        assert counts["pruned_empty"] == 0


class TestOrphanedWorktrees:
    """A worktree whose shared clone was deleted (#1003) is named, not reaped."""

    def _orphan(self, tmp_path, name="orphan1", *, live=False):
        wt = _make_wt(tmp_path, name, OLD, NOW)
        clone = tmp_path / ".ccswitch" / "clones" / "cairnea" / "KinDash"
        (wt / ".git").write_text(f"gitdir: {clone}/.git/worktrees/{name}\n")
        return wt

    def test_orphan_is_counted_and_never_removed(self, tmp_path):
        self._orphan(tmp_path)
        with patch(_REMOVE) as rm:
            counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["orphaned"] == 1
        # Not miscounted as dirty — that alert would blame the wrong thing.
        assert counts["kept_dirty"] == 0
        assert counts["removed"] == 0
        rm.assert_not_called()

    def test_orphan_is_reported_even_for_a_live_session(self, tmp_path):
        wt = self._orphan(tmp_path)
        counts = sweep_orphaned_worktrees(
            str(tmp_path), frozenset([str(wt.resolve())]), now=NOW)
        assert counts["orphaned"] == 1

    def test_orphan_alert_names_the_missing_clone_once(self, tmp_path, caplog):
        import logging
        for i in range(3):
            self._orphan(tmp_path, f"orphan{i}")
        with caplog.at_level(logging.CRITICAL, logger=_MOD):
            counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["orphaned"] == 3
        criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert len(criticals) == 1
        assert "clones/cairnea/KinDash" in criticals[0].getMessage()
        assert "ccswitch clones repair" in criticals[0].getMessage()

    def test_healthy_worktree_is_not_an_orphan(self, tmp_path):
        wt = _make_wt(tmp_path, "healthy", OLD, NOW)
        record = tmp_path / ".ccswitch" / "clones" / "o" / "r" / ".git" / "worktrees" / "healthy"
        record.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {record}\n")
        _age(wt, OLD, NOW)  # writing .git bumped the dir mtime
        clean = {
            "status --porcelain": _cp(0, ""),
            "branch -vv": _cp(0, "* main [origin/main]"),
            "remote": _cp(0, "origin"),
            "rev-list": _cp(0, ""),
        }
        with patch(_RUN, side_effect=_fake_run(clean)):
            with patch(_REMOVE, return_value=True):
                counts = sweep_orphaned_worktrees(str(tmp_path), frozenset(), now=NOW)
        assert counts["orphaned"] == 0
        assert counts["removed"] == 1
